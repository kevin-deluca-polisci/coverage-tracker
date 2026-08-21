#!/usr/bin/env python3
"""dedupe_master.py

One-shot cleanup for duplicate rows in a headlines CSV.

The live scrapers and merge_incoming.py all dedup on write, so the master stays
clean going forward — a scan on 2026-08-20 found only 333 duplicates in 286,715
rows (0.1%), all predating the current logic. This exists to clear that residue
and to have a tool on hand if a future source introduces a new duplicate shape.

Duplicates are identified on the same two keys used everywhere else:
  - normalized URL
  - (normalized title, date, outlet)

Which copy survives matters: a row that already carries a `debate_performance`
label is kept over an unlabeled one, so cleanup never discards classification
work and never forces a re-run of the model.

A timestamped backup is written before anything changes.

Usage:
  # look, don't touch
  python3 dedupe_master.py --csv data/raw/mediacloud_data/trump_headlines_master.csv --dry-run

  # clean it
  python3 dedupe_master.py --csv data/raw/mediacloud_data/trump_headlines_master.csv
"""

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd


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
    p = argparse.ArgumentParser(description="Remove duplicate rows from a headlines CSV")
    p.add_argument("--csv",     required=True, help="CSV to clean")
    p.add_argument("--dry-run", action="store_true", help="Report only, change nothing")
    p.add_argument("--no-backup", action="store_true",
                   help="Skip the timestamped backup (not recommended)")
    args = p.parse_args()

    if not os.path.exists(args.csv):
        print(f"ERROR: {args.csv} not found", file=sys.stderr)
        return 1

    df = pd.read_csv(args.csv, dtype=str, low_memory=False)
    before = len(df)
    print(f"Rows: {before:,}")

    # Prefer keeping classified rows. Sorting so labeled rows come first means
    # drop_duplicates(keep='first') retains the classification.
    if "debate_performance" in df.columns:
        df["__labeled"] = df["debate_performance"].notna() & \
                          (df["debate_performance"].astype(str).str.strip() != "")
        n_labeled = int(df["__labeled"].sum())
        print(f"Classified rows: {n_labeled:,} "
              f"({n_labeled / before * 100:.1f}%) — these win any tie")
    else:
        df["__labeled"] = False

    df["__u"] = df["url"].map(normalize_url) if "url" in df.columns else ""
    df["__k"] = [
        (normalize_title(t), str(d)[:10], str(o))
        for t, d, o in zip(df.get("title", ""), df.get("date", ""), df.get("outlet", ""))
    ]

    df = df.sort_values("__labeled", ascending=False, kind="stable")

    # URL dupes first, but only among rows that actually have a URL — every
    # row with an empty URL would otherwise collapse into one.
    has_url = df["__u"] != ""
    with_url = df[has_url].drop_duplicates(subset=["__u"], keep="first")
    without_url = df[~has_url]
    df2 = pd.concat([with_url, without_url], ignore_index=True)
    removed_url = len(df) - len(df2)

    df3 = df2.drop_duplicates(subset=["__k"], keep="first")
    removed_key = len(df2) - len(df3)

    total_removed = before - len(df3)
    print(f"\nRemoved by URL             : {removed_url:,}")
    print(f"Removed by (title,date,outlet): {removed_key:,}")
    print(f"Total removed              : {total_removed:,} "
          f"({total_removed / before * 100:.2f}%)")
    print(f"Rows after                 : {len(df3):,}")

    if total_removed:
        dropped_ids = set(df.index) - set(df3.index) if df.index.is_unique else None
        by_outlet = (df[~df.index.isin(df3.index)]["outlet"].value_counts()
                     if dropped_ids is not None and "outlet" in df.columns else None)
        if by_outlet is not None and len(by_outlet):
            print("\nRemoved by outlet:")
            for outlet, n in by_outlet.head(12).items():
                print(f"  {outlet:<22} {n:>5}")

    df3 = df3.drop(columns=["__u", "__k", "__labeled"])

    if args.dry_run:
        print("\n[dry-run] Nothing written.")
        return 0

    if total_removed == 0:
        print("\nNo duplicates — file left untouched.")
        return 0

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{args.csv}.bak-{stamp}"
        shutil.copy2(args.csv, backup)
        print(f"\nBackup: {backup}")

    df3["__k"] = pd.to_datetime(df3["date"], errors="coerce")
    df3 = df3.sort_values(["__k", "outlet"]).drop(columns="__k")
    df3.to_csv(args.csv, index=False)
    print(f"✓ Cleaned {args.csv}: {before:,} → {len(df3):,} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
