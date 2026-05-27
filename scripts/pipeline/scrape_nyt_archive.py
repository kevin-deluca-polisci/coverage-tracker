#!/usr/bin/env python3
"""scrape_nyt_archive.py

Fetches NYT articles mentioning Trump via the NYT *Archive* API. One API
call per month returns the entire month's published articles; we filter
client-side for headlines mentioning Trump.

This is the recommended tool for **historical backfill** because:
  - 1 API call per month (vs ~30-100 paginated calls for Article Search)
  - Much lower rate-limit pressure
  - Simpler to reason about — no pagination, no in-month chunking

For ongoing collection (last 14 days every 3 days), either this script or
scrape_nyt_api.py works fine; this one is more efficient if the window
spans multiple months, the other is more efficient if you only need a
narrow window inside a single month.

API: https://developer.nytimes.com/docs/archive-product/1/overview
Reads NYT_API_KEY from environment (or --api-key flag).

Schema match: writes to trump_headlines_master.csv with the same columns
as scrape_nyt_api.py and scrape_gdelt.py.

Usage:
  # backfill the Media Cloud gap (Aug 2025 → now)
  python3 scrape_nyt_archive.py \\
      --start-month 2025-08 --end-month 2026-05 \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv

  # full history from 2025-01
  python3 scrape_nyt_archive.py \\
      --start-month 2025-01 --end-month 2026-05 \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv
"""

import argparse
import calendar
import os
import sys
import time
from datetime import date, datetime
from urllib.parse import urlparse

import pandas as pd
import requests

NYT_ARCHIVE_URL = "https://api.nytimes.com/svc/archive/v1/{year}/{month}.json"
USER_AGENT      = "coverage-tracker (research; kevin-deluca-polisci/coverage-tracker)"


# --------------------------------------------------------------------------
# URL normalization (matches scrape_gdelt.py and scrape_nyt_api.py)
# --------------------------------------------------------------------------
def normalize_url(url):
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        p = urlparse(url.strip())
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path.rstrip("/")
        scheme = (p.scheme or "https").lower()
        return f"{scheme}://{netloc}{path}"
    except Exception:
        return url


# --------------------------------------------------------------------------
# Archive API call
# --------------------------------------------------------------------------
def fetch_month(year, month, api_key, session, retries=3):
    """Fetch one month's archive. Returns a list of doc dicts (or [] on failure)."""
    url = NYT_ARCHIVE_URL.format(year=year, month=month)
    params = {"api-key": api_key}
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=120)
            if r.status_code == 200:
                return r.json().get("response", {}).get("docs", []) or []
            elif r.status_code == 429:
                wait = 60 * (attempt + 1)
                print(f"    [rate-limited, waiting {wait}s]", file=sys.stderr)
                time.sleep(wait)
            elif r.status_code == 401:
                print("    [HTTP 401 — bad API key? Check $NYT_API_KEY]",
                      file=sys.stderr)
                return []
            else:
                print(f"    [HTTP {r.status_code} for {year}-{month:02d}]",
                      file=sys.stderr)
                if attempt < retries:
                    time.sleep(10 * (attempt + 1))
        except requests.RequestException as e:
            print(f"    [network error: {e}; retrying]", file=sys.stderr)
            if attempt < retries:
                time.sleep(10 * (attempt + 1))
    return []


# --------------------------------------------------------------------------
# Convert NYT docs → master CSV rows
# --------------------------------------------------------------------------
def to_master_rows(docs):
    rows = []
    for d in docs:
        hl = d.get("headline") or {}
        title = ""
        if isinstance(hl, dict):
            title = (hl.get("main") or "").strip()
        elif isinstance(hl, str):
            title = hl.strip()
        if not title:
            continue
        # Filter to headlines actually mentioning Trump (case-insensitive)
        if "trump" not in title.lower():
            continue
        pub_date = (d.get("pub_date") or "")[:10]  # YYYY-MM-DD
        if not pub_date:
            continue
        url = (d.get("web_url") or "").strip()
        rows.append({
            "title":              title,
            "outlet":             "New York Times",
            "media_url":          "nytimes.com",
            "media_name":         "nytimes.com",
            "publish_date":       pub_date,
            "url":                url,
            "language":           "en",
            "date":               pub_date,
            "debate_performance": "",
        })
    return rows


# --------------------------------------------------------------------------
# Month iteration helper
# --------------------------------------------------------------------------
def months_between(start_ym, end_ym):
    """Yield (year, month) tuples from start_ym to end_ym inclusive."""
    y, m = start_ym
    while (y, m) <= end_ym:
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def parse_year_month(s, label):
    try:
        y, m = s.split("-")
        return (int(y), int(m))
    except Exception:
        print(f"ERROR: --{label} must be YYYY-MM (got {s!r})", file=sys.stderr)
        sys.exit(1)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="NYT Archive API scraper")
    p.add_argument("--start-month", required=True,
                   help="First month to fetch (YYYY-MM, e.g. 2025-08)")
    p.add_argument("--end-month",   required=True,
                   help="Last month to fetch (YYYY-MM, e.g. 2026-05)")
    p.add_argument("--master-csv",  required=True,
                   help="Path to trump_headlines_master.csv")
    p.add_argument("--delay",       type=float, default=1.0,
                   help="Seconds between month calls (default: 1.0)")
    p.add_argument("--api-key",     default=None,
                   help="NYT API key (or set NYT_API_KEY env var)")
    args = p.parse_args()

    api_key = args.api_key or os.environ.get("NYT_API_KEY", "").strip()
    if not api_key:
        print("ERROR: NYT_API_KEY not set. Either pass --api-key or add\n"
              "  export NYT_API_KEY=\"...\"  to your ~/.zshrc and re-source.",
              file=sys.stderr)
        sys.exit(1)

    start_ym = parse_year_month(args.start_month, "start-month")
    end_ym   = parse_year_month(args.end_month,   "end-month")
    if start_ym > end_ym:
        print("ERROR: start-month is after end-month", file=sys.stderr)
        sys.exit(1)

    # Load existing master for dedup
    existing_urls = set()
    existing_df = None
    if os.path.exists(args.master_csv):
        existing_df = pd.read_csv(args.master_csv, dtype=str, low_memory=False)
        if "url" in existing_df.columns:
            existing_urls = {normalize_url(u) for u in existing_df["url"].dropna()}
            existing_urls.discard("")
        print(f"Loaded {len(existing_df):,} existing master rows "
              f"({len(existing_urls):,} unique URLs)")

    print(f"\nFetching NYT archive {args.start_month} → {args.end_month}, "
          f"delay={args.delay}s\n")

    session = requests.Session()
    total_new = 0

    for (year, month) in months_between(start_ym, end_ym):
        print(f"  {year}-{month:02d}...", end=" ", flush=True)
        docs = fetch_month(year, month, api_key, session)
        if not docs:
            print("(0 docs returned)")
            time.sleep(args.delay)
            continue

        new_rows = to_master_rows(docs)
        before = len(new_rows)
        new_rows = [r for r in new_rows
                    if normalize_url(r["url"]) not in existing_urls]
        for r in new_rows:
            existing_urls.add(normalize_url(r["url"]))
        print(f"{len(docs):,} total docs → {before:,} Trump headlines → "
              f"{len(new_rows):,} new after dedup")

        # Save incrementally after each month
        if new_rows:
            new_df = pd.DataFrame(new_rows)
            if existing_df is not None:
                existing_df = pd.concat([existing_df, new_df], ignore_index=True)
            else:
                existing_df = new_df
            existing_df["__sortkey"] = pd.to_datetime(existing_df["date"], errors="coerce")
            existing_df = (existing_df
                           .sort_values(["__sortkey", "outlet"])
                           .drop(columns="__sortkey"))
            existing_df.to_csv(args.master_csv, index=False)
            total_new += len(new_rows)
            print(f"    ✓ Saved (master now {len(existing_df):,} rows)")

        time.sleep(args.delay)

    if total_new == 0:
        print("\nNo new NYT articles added.")
    else:
        print(f"\nDone. Added {total_new:,} new NYT rows total.")
        print(f"Master CSV now has {len(existing_df):,} total rows.")


if __name__ == "__main__":
    main()
