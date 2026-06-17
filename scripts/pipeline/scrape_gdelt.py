#!/usr/bin/env python3
"""scrape_gdelt.py

Fetches Trump-mentioning headlines from the GDELT 2.0 Doc API for the 10
non-NYT outlets tracked by the dashboard. (NYT has its own dedicated API and
is handled by scrape_nyt_api.py.)

Why GDELT: it's a stable, free, no-key academic project (Georgetown / NSF)
that indexes news from thousands of sources globally. It's much less prone to
outlet-by-outlet blocking than Media Cloud, and it has a deep historical
archive going back to 2017 — making it viable for both ongoing collection and
backfill.

How it works:
  - For each outlet, query GDELT for "trump source:<domain>"
  - Chunk by week to stay under GDELT's 250-records-per-query cap
  - If a weekly chunk hits the cap, subdivide to daily
  - Filter results to ensure "trump" appears in the headline (GDELT's full-text
    search would otherwise return body-only matches)
  - Append to the master CSV with URL-based dedup against existing rows

The output schema matches trump_headlines_master.csv:
  title, outlet, media_url, media_name, publish_date, url, language, date,
  debate_performance (left blank — filled later by run_headline_analysis.py)

Usage:
  # ongoing (last 14 days, default)
  python3 scrape_gdelt.py \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv

  # backfill (explicit window)
  python3 scrape_gdelt.py \\
      --start-date 2025-01-01 --end-date 2026-05-21 \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv \\
      --delay 2.0

  # single outlet (e.g., refresh just NYT-replacement coverage)
  python3 scrape_gdelt.py \\
      --outlets abcnews.go.com bloomberg.com \\
      --start-date 2026-03-01 --end-date 2026-05-21 \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pandas as pd
import requests

# --------------------------------------------------------------------------
# Domain → outlet name mapping
# --------------------------------------------------------------------------
# Excludes nytimes.com — NYT is handled by scrape_nyt_api.py because their
# Article Search API gives cleaner, more complete results than GDELT for NYT
# specifically. If you add NYT here, you'll get redundant rows that dedup
# will handle but it's wasted work.
OUTLETS = {
    # Outlets that GDELT actually indexes well.
    "foxnews.com":      "Fox News",
    "cbsnews.com":      "CBS News",
    "bloomberg.com":    "Bloomberg",
    "cnn.com":          "CNN",
    "nbcnews.com":      "NBC News",
    "latimes.com":      "Los Angeles Times",
    "npr.org":          "NPR",

    # NOTE: Reuters, ABC News, and USA Today have been REMOVED — empirical
    # testing showed GDELT does not index these outlets at all. A `domainis:`
    # query with no keyword filter returns an empty result, confirming the
    # absence isn't query-syntax-related. Media Cloud covers all three with
    # large historical archives (Reuters 64K+ headlines, ABC 15K+, USAT 15K+),
    # so we rely on MC alone for these outlets. If GDELT adds coverage in the
    # future, just add them back here.
}

# Per-outlet GDELT operator override. `domain:` does substring matching and
# works for all currently-tracked outlets. Kept as a hook for future outlets
# that need stricter `domainis:` exact-match (e.g. domains shared with other
# sites like "un.org").
OUTLET_OPERATOR = {}

GDELT_URL    = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS  = 250    # GDELT hard cap per query
USER_AGENT   = "coverage-tracker (research; kevin-deluca-polisci/coverage-tracker)"


# --------------------------------------------------------------------------
# URL normalization (for cross-source dedup)
# --------------------------------------------------------------------------
def normalize_url(url):
    """Strip query params, fragment, trailing slash, www, scheme casing.
    Two URLs to the same article from different sources should normalize
    to the same string."""
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
# GDELT API call
# --------------------------------------------------------------------------
def fetch_chunk(domain, start_dt, end_dt, session, retries=2):
    """One GDELT API call for a single domain + date range."""
    # NOTE: GDELT 2.0 uses `domain:` for URL host filtering, NOT `source:`.
    # Per-outlet override lives in OUTLET_OPERATOR for outlets that need
    # the stricter `domainis:` exact-match (Reuters, ABC, USA Today).
    operator = OUTLET_OPERATOR.get(domain, "domain")
    query = f"trump {operator}:{domain}"
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "startdatetime": start_dt.strftime("%Y%m%d000000"),
        "enddatetime":   end_dt.strftime("%Y%m%d235959"),
        "maxrecords":    MAX_RECORDS,
        "sort":          "DateAsc",
    }
    for attempt in range(retries + 1):
        try:
            r = session.get(GDELT_URL, params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=120)
            if r.status_code == 200:
                # GDELT sometimes returns HTML on errors; check
                try:
                    data = r.json()
                except ValueError:
                    if attempt < retries:
                        time.sleep(5 * (attempt + 1))
                        continue
                    return []
                return data.get("articles", [])
            elif r.status_code == 429:
                # rate limited
                wait = 30 * (attempt + 1)
                print(f"    [rate-limited, waiting {wait}s]", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"    [HTTP {r.status_code} for {domain} "
                      f"{start_dt.date()} → {end_dt.date()}]", file=sys.stderr)
                if attempt < retries:
                    time.sleep(5 * (attempt + 1))
        except requests.RequestException as e:
            print(f"    [network error: {e}; retrying]", file=sys.stderr)
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    return []


def fetch_outlet(domain, outlet_name, start_date, end_date, delay, session):
    """Fetch all articles for one outlet across the full date range.
    Chunks weekly; subdivides to daily if a weekly chunk hits the 250 cap."""
    all_articles = []
    cur = start_date
    while cur <= end_date:
        chunk_end = min(cur + timedelta(days=6), end_date)
        articles = fetch_chunk(domain, cur, chunk_end, session)
        if len(articles) >= MAX_RECORDS:
            # Hit the cap — subdivide to daily
            print(f"    {cur.date()}: {len(articles)} (capped) — subdividing to daily")
            articles = []
            sub = cur
            while sub <= chunk_end:
                day_articles = fetch_chunk(domain, sub, sub, session)
                articles.extend(day_articles)
                time.sleep(delay)
                sub += timedelta(days=1)
        all_articles.extend(articles)
        print(f"    {cur.date()} → {chunk_end.date()}: {len(articles)} articles")
        time.sleep(delay)
        cur = chunk_end + timedelta(days=1)
    return all_articles


# --------------------------------------------------------------------------
# Convert GDELT articles → master CSV rows
# --------------------------------------------------------------------------
def to_master_rows(articles, outlet_name, domain):
    rows = []
    for a in articles:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        # GDELT's q=trump matches body; require "trump" in the headline too
        if "trump" not in title.lower():
            continue
        # English only
        lang = (a.get("language") or "").lower()
        if lang and lang != "english":
            continue
        # Parse seendate: format is YYYYMMDDTHHMMSSZ
        seendate = a.get("seendate") or ""
        if len(seendate) < 8:
            continue
        date_str = f"{seendate[0:4]}-{seendate[4:6]}-{seendate[6:8]}"
        url = (a.get("url") or "").strip()
        rows.append({
            "title":              title,
            "outlet":             outlet_name,
            "media_url":          domain,
            "media_name":         domain,
            "publish_date":       date_str,
            "url":                url,
            "language":           "en",
            "date":               date_str,
            "debate_performance": "",
        })
    return rows


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="GDELT 2.0 scraper for Trump headlines")
    p.add_argument("--start-date", default=None,
                   help="YYYY-MM-DD. Defaults to 14 days ago.")
    p.add_argument("--end-date",   default=None,
                   help="YYYY-MM-DD. Defaults to today.")
    p.add_argument("--master-csv", required=True,
                   help="Path to trump_headlines_master.csv")
    p.add_argument("--delay",      type=float, default=8.0,
                   help="Seconds between API calls (default: 8.0). GDELT's "
                        "documented floor is 5s, but under load 6s still triggers "
                        "intermittent 429s and connection resets. 8s is reliable.")
    p.add_argument("--outlets",    nargs="+", default=None,
                   help="Specific domain(s) only. Defaults to all 10.")
    args = p.parse_args()

    # Default date window: last 14 days
    today = datetime.utcnow().date()
    end_date = (datetime.strptime(args.end_date, "%Y-%m-%d").date()
                if args.end_date else today)
    start_date = (datetime.strptime(args.start_date, "%Y-%m-%d").date()
                  if args.start_date else (today - timedelta(days=14)))
    start_date = datetime.combine(start_date, datetime.min.time())
    end_date   = datetime.combine(end_date,   datetime.min.time())

    if start_date > end_date:
        print("ERROR: start-date is after end-date", file=sys.stderr)
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

    # Resolve outlet list
    if args.outlets:
        outlets = {d: OUTLETS[d] for d in args.outlets if d in OUTLETS}
        unknown = [d for d in args.outlets if d not in OUTLETS]
        if unknown:
            print(f"WARNING: unknown domains skipped: {unknown}", file=sys.stderr)
    else:
        outlets = OUTLETS

    print(f"\nFetching GDELT articles {start_date.date()} → {end_date.date()} "
          f"for {len(outlets)} outlets, delay={args.delay}s\n")

    session = requests.Session()
    total_new = 0
    for domain, outlet_name in outlets.items():
        print(f"=== {outlet_name} ({domain}) ===")
        articles = fetch_outlet(domain, outlet_name, start_date, end_date,
                                args.delay, session)
        new_rows = to_master_rows(articles, outlet_name, domain)
        before = len(new_rows)
        new_rows = [r for r in new_rows
                    if normalize_url(r["url"]) not in existing_urls]
        for r in new_rows:
            existing_urls.add(normalize_url(r["url"]))
        print(f"  {outlet_name}: {len(articles)} fetched, "
              f"{before} after headline filter, {len(new_rows)} new after dedup")

        # ---- Save after EACH outlet so an interrupt doesn't lose progress ----
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
            print(f"  ✓ Saved (master now {len(existing_df):,} rows)\n")
            total_new += len(new_rows)
        else:
            print("  (nothing new to save)\n")

    if total_new == 0:
        print("No new articles added across any outlet.")
    else:
        print(f"\nDone. Added {total_new:,} new GDELT rows total.")
        print(f"Master CSV now has {len(existing_df):,} total rows.")


if __name__ == "__main__":
    main()
