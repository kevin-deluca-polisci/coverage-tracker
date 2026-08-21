#!/usr/bin/env python3
"""filter_increment.py

Trims a freshly-collected batch down to only the rows that aren't already in
previously-committed increments, then writes today's increment file.

Why this is needed: CI has no access to the headlines master (it's ~65 MB and
gitignored), so each daily run collects a rolling window with no memory of what
it collected yesterday. With a 2-3 day window that means roughly half to
two-thirds of every committed file is data we already have. Left alone that's
~114 MB of git history a year to carry maybe 38 MB of actual information.

The increments already sitting in data/incoming/ ARE available to CI, though —
they're committed files. So we dedup against those, including the ones a local
merge has moved into data/incoming/archive/. Scanning the archive matters: it
holds every increment that has already been consumed, which on the day after a
local refresh is all of them. Skipping it turns this script into a no-op
exactly when it is most needed.

Dedup here is an efficiency measure, not a correctness one — merge_incoming.py
dedups against the master on the same two keys, so a row can never reach the
master twice regardless of what lands in an increment.

Dedup uses the same two keys as merge_incoming.py:
  - normalized URL
  - (normalized title, date, outlet)  ← catches Google News redirect URLs

Usage:
  python3 filter_increment.py \\
      --collected /tmp/collected.csv \\
      --incoming-dir data/incoming \\
      --out data/incoming/2026-08-21.csv
"""

import argparse
import datetime
import glob
import os
import re
import sys
from urllib.parse import urlparse

import pandas as pd

MASTER_COLUMNS = ["title", "outlet", "media_url", "media_name",
                  "publish_date", "url", "language", "date",
                  "debate_performance"]


def normalize_url(url):
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        p = urlparse(url.strip())
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return f"{(p.scheme or 'https').lower()}://{netloc}{p.path.rstrip('/')}"
    except Exception:
        return url


def normalize_title(t):
    if not isinstance(t, str):
        return ""
    t = t.lower()
    t = re.sub(r"[‘’“”]", "'", t)
    t = re.sub(r"[‐-―]", "-", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def main():
    p = argparse.ArgumentParser(description="Filter a collected batch against prior increments")
    p.add_argument("--collected",    required=True,
                   help="Freshly collected CSV (all sources appended)")
    p.add_argument("--incoming-dir", required=True,
                   help="Directory of previously-committed increments")
    p.add_argument("--out",          required=True,
                   help="Where to write today's filtered increment")
    p.add_argument("--dedup-days",   type=int, default=14,
                   help="Only dedup against increments this recent (default 14). "
                        "Must comfortably exceed the collection lookback window.")
    args = p.parse_args()

    if not os.path.exists(args.collected):
        print(f"No collected file at {args.collected} — every source failed "
              f"or found nothing. Writing nothing.")
        return 0

    collected = pd.read_csv(args.collected, dtype=str, low_memory=False)
    if collected.empty:
        print("Collected file is empty — writing nothing.")
        return 0

    for col in MASTER_COLUMNS:
        if col not in collected.columns:
            collected[col] = ""
    collected = collected[MASTER_COLUMNS]
    print(f"Collected this run: {len(collected):,} rows")

    # ---- Build the "already committed" index from prior increments ------
    #
    # Includes archive/ as well as the top level. A local merge moves consumed
    # increments into data/incoming/archive/, so on any day following a local
    # refresh the top-level directory is empty and there is nothing left to
    # dedup against — the filter silently becomes a no-op and re-commits the
    # whole lookback window. That is how it behaved on 2026-08-21: 77% of the
    # committed increment was already sitting in the archive.
    #
    # Bounded by recency rather than reading the whole archive: an increment
    # older than the collection window cannot overlap with today's batch, so
    # there is no reason to keep parsing it as the archive grows.
    out_abs = os.path.abspath(args.out)
    candidates = (glob.glob(os.path.join(args.incoming_dir, "*.csv")) +
                  glob.glob(os.path.join(args.incoming_dir, "archive", "*.csv")))

    cutoff = datetime.date.today() - datetime.timedelta(days=args.dedup_days)
    prior_files = []
    for f in sorted(candidates):
        if os.path.abspath(f) == out_abs:
            continue
        stem = os.path.splitext(os.path.basename(f))[0]
        try:
            stamp = datetime.date.fromisoformat(stem)
        except ValueError:
            prior_files.append(f)   # unparseable name — keep it, be safe
            continue
        if stamp >= cutoff:
            prior_files.append(f)

    seen_urls, seen_keys = set(), set()
    for f in prior_files:
        try:
            prior = pd.read_csv(f, dtype=str, low_memory=False)
        except Exception as e:
            print(f"  [skipping unreadable {os.path.basename(f)}: {e}]",
                  file=sys.stderr)
            continue
        if "url" in prior.columns:
            seen_urls |= {normalize_url(u) for u in prior["url"].dropna()}
        if {"title", "date", "outlet"}.issubset(prior.columns):
            seen_keys |= {
                (normalize_title(t), str(d)[:10], str(o))
                for t, d, o in zip(prior["title"], prior["date"], prior["outlet"])
            }
    seen_urls.discard("")
    print(f"Prior increments: {len(prior_files)} file(s), "
          f"{len(seen_urls):,} URLs / {len(seen_keys):,} title keys")

    # This failed silently once — the archive wasn't being scanned, so the
    # dedup index was empty and the whole lookback window got re-committed
    # every day while the logs looked entirely normal. An empty index is
    # legitimate only on the very first run, so say so loudly otherwise.
    if not prior_files:
        print("  WARNING: no prior increments found. Expected only on the "
              "first ever run — otherwise the increment will re-commit the "
              "entire lookback window. Check data/incoming/ and its archive/.",
              file=sys.stderr)

    # ---- Filter ---------------------------------------------------------
    kept = []
    for row in collected.to_dict("records"):
        u = normalize_url(row.get("url", ""))
        k = (normalize_title(row.get("title", "")),
             str(row.get("date", ""))[:10],
             str(row.get("outlet", "")))
        if u and u in seen_urls:
            continue
        if k in seen_keys:
            continue
        if u:
            seen_urls.add(u)
        seen_keys.add(k)
        kept.append(row)

    dropped = len(collected) - len(kept)
    pct = (dropped / len(collected) * 100) if len(collected) else 0
    print(f"Dropped as already-collected: {dropped:,} ({pct:.0f}%)")
    print(f"New rows for today's increment: {len(kept):,}")

    if not kept:
        print("Nothing new — no increment written.")
        return 0

    out_df = pd.DataFrame(kept)
    out_df["__k"] = pd.to_datetime(out_df["date"], errors="coerce")
    out_df = out_df.sort_values(["__k", "outlet"]).drop(columns="__k")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    out_df.to_csv(args.out, index=False)

    print(f"\nWrote {args.out}")
    print("\nBy outlet:")
    for outlet, n in out_df["outlet"].value_counts().items():
        print(f"  {outlet:<22} {n:>5}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
