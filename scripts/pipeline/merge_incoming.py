#!/usr/bin/env python3
"""merge_incoming.py

Folds the daily headline increments produced by the GitHub Action into the
local headlines master, then archives the files it consumed.

Why increments exist: the master CSV is ~65 MB and gitignored, so a CI job has
nothing to dedup against and can't commit the master back. Instead the Action
writes one small dated file per run (data/incoming/YYYY-MM-DD.csv, typically
tens of KB) and commits that. This script is the local side of that handshake.

Dedup is done on BOTH keys, because sources disagree about URLs:
  - normalized URL — catches the same article from Media Cloud vs GDELT
  - (normalized title, date, outlet) — catches Google News rows, whose links
    are news.google.com redirects and therefore never URL-match anything else

Consumed files move to data/incoming/archive/ rather than being deleted, so a
bad merge can always be reconstructed.

Usage:
  python3 merge_incoming.py \\
      --incoming-dir data/incoming \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv

  # inspect without changing anything
  python3 merge_incoming.py --incoming-dir ... --master-csv ... --dry-run

  # leave the increment files in place after merging
  python3 merge_incoming.py --incoming-dir ... --master-csv ... --no-archive
"""

import argparse
import glob
import os
import re
import shutil
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
    p = argparse.ArgumentParser(description="Merge daily headline increments into master")
    p.add_argument("--incoming-dir", required=True,
                   help="Directory holding YYYY-MM-DD.csv increments")
    p.add_argument("--master-csv",   required=True,
                   help="Headlines master CSV to merge into")
    p.add_argument("--dry-run",      action="store_true",
                   help="Report what would be merged, change nothing")
    p.add_argument("--no-archive",   action="store_true",
                   help="Leave increment files in place after merging")
    args = p.parse_args()

    files = sorted(glob.glob(os.path.join(args.incoming_dir, "*.csv")))
    if not files:
        print(f"No increment files in {args.incoming_dir} — nothing to merge.")
        return

    print(f"Found {len(files)} increment file(s) in {args.incoming_dir}")

    # ---- Load master and build both dedup indexes ----------------------
    if os.path.exists(args.master_csv):
        master = pd.read_csv(args.master_csv, dtype=str, low_memory=False)
        print(f"Master: {len(master):,} rows")
    else:
        master = pd.DataFrame(columns=MASTER_COLUMNS)
        print("Master does not exist yet — it will be created.")

    seen_urls = set()
    seen_keys = set()
    if "url" in master.columns:
        seen_urls = {normalize_url(u) for u in master["url"].dropna()}
        seen_urls.discard("")
    if {"title", "date", "outlet"}.issubset(master.columns):
        seen_keys = {
            (normalize_title(t), str(d)[:10], str(o))
            for t, d, o in zip(master["title"], master["date"], master["outlet"])
        }
    print(f"Dedup index: {len(seen_urls):,} URLs, {len(seen_keys):,} title keys\n")

    # ---- Walk the increments -------------------------------------------
    all_new = []
    per_file = []
    for f in files:
        try:
            inc = pd.read_csv(f, dtype=str, low_memory=False)
        except Exception as e:
            print(f"  {os.path.basename(f)}: SKIPPED (unreadable: {e})")
            per_file.append((f, 0, 0, False))
            continue

        if inc.empty:
            print(f"  {os.path.basename(f)}: empty")
            per_file.append((f, 0, 0, True))
            continue

        # Tolerate increments missing the classification column
        for col in MASTER_COLUMNS:
            if col not in inc.columns:
                inc[col] = ""
        inc = inc[MASTER_COLUMNS]

        kept = []
        for row in inc.to_dict("records"):
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

        print(f"  {os.path.basename(f)}: {len(inc):,} rows → {len(kept):,} new")
        per_file.append((f, len(inc), len(kept), True))
        all_new.extend(kept)

    if not all_new:
        print("\nNothing new across all increments.")
    else:
        print(f"\nTotal new rows to add: {len(all_new):,}")
        by_outlet = pd.DataFrame(all_new)["outlet"].value_counts()
        print("\nNew rows by outlet:")
        for outlet, n in by_outlet.items():
            print(f"  {outlet:<22} {n:>6,}")

    if args.dry_run:
        print("\n[dry-run] No files were written or moved.")
        return

    # ---- Write master ---------------------------------------------------
    if all_new:
        merged = pd.concat([master, pd.DataFrame(all_new)], ignore_index=True)
        merged["__k"] = pd.to_datetime(merged["date"], errors="coerce")
        merged = merged.sort_values(["__k", "outlet"]).drop(columns="__k")
        merged.to_csv(args.master_csv, index=False)
        print(f"\n✓ Master updated: {len(master):,} → {len(merged):,} rows")
    else:
        print("\nMaster unchanged.")

    # ---- Archive consumed increments ------------------------------------
    if args.no_archive:
        print("Increment files left in place (--no-archive).")
        return

    archive_dir = os.path.join(args.incoming_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)
    moved = 0
    for f, _, _, readable in per_file:
        if not readable:
            continue
        try:
            shutil.move(f, os.path.join(archive_dir, os.path.basename(f)))
            moved += 1
        except Exception as e:
            print(f"  [could not archive {os.path.basename(f)}: {e}]",
                  file=sys.stderr)
    print(f"✓ Archived {moved} consumed increment file(s) → {archive_dir}")


if __name__ == "__main__":
    main()
