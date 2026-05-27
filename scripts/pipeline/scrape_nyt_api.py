#!/usr/bin/env python3
"""scrape_nyt_api.py

Fetches NYT articles mentioning Trump via the NYT Article Search API.
Used for both ongoing collection and historical backfill (Aug 2025 onward,
when Media Cloud's NYT coverage degraded).

API: https://developer.nytimes.com/docs/articlesearch-product/1/overview
Reads NYT_API_KEY from environment (or --api-key flag).

Mechanics:
  - The API returns 10 docs per page, up to 100 pages (= 1000 results) per
    query. For queries with more hits, we chunk by month — Trump-headline
    counts at NYT typically run ~30/day (~900/month, comfortably under cap).
  - For dense political news months, we automatically subdivide to weekly if
    a monthly chunk hits 1000.
  - Rate limit: 10 req/sec (we default to 0.5s delay = 2 req/sec for safety).

Output schema matches trump_headlines_master.csv. Dedup by normalized URL
against the existing master.

Usage:
  # ongoing (last 14 days, default)
  python3 scrape_nyt_api.py \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv

  # backfill the Media Cloud gap
  python3 scrape_nyt_api.py \\
      --start-date 2025-08-01 --end-date 2026-05-21 \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv \\
      --delay 0.5
"""

import argparse
import calendar
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pandas as pd
import requests

NYT_URL        = "https://api.nytimes.com/svc/search/v2/articlesearch.json"
MAX_PAGE       = 100   # 0–99 pages = 1000 results per query
HITS_PER_PAGE  = 10
USER_AGENT     = "coverage-tracker (research; kevin-deluca-polisci/coverage-tracker)"


# --------------------------------------------------------------------------
# URL normalization (matches scrape_gdelt.py)
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
# NYT API call
# --------------------------------------------------------------------------
def fetch_page(start_date, end_date, page, api_key, session, retries=3):
    """Fetch one page of NYT search results."""
    params = {
        "q":          "Trump",
        "begin_date": start_date.strftime("%Y%m%d"),
        "end_date":   end_date.strftime("%Y%m%d"),
        "sort":       "oldest",
        "fl":         "headline,pub_date,web_url",
        "page":       page,
        "api-key":    api_key,
    }
    for attempt in range(retries + 1):
        try:
            r = session.get(NYT_URL, params=params,
                            headers={"User-Agent": USER_AGENT}, timeout=60)
            if r.status_code == 200:
                return r.json().get("response", {}).get("docs", []) or []
            elif r.status_code == 429:
                wait = 60
                print(f"    [rate-limited, waiting {wait}s]", file=sys.stderr)
                time.sleep(wait)
            elif r.status_code == 401:
                print("    [HTTP 401 — bad API key? Check $NYT_API_KEY]",
                      file=sys.stderr)
                return []
            else:
                print(f"    [HTTP {r.status_code} on page {page}]", file=sys.stderr)
                if attempt < retries:
                    time.sleep(5 * (attempt + 1))
        except requests.RequestException as e:
            print(f"    [network error: {e}; retrying]", file=sys.stderr)
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    return []


def fetch_chunk(start_date, end_date, api_key, delay, session):
    """Fetch all pages for one date range chunk."""
    all_docs = []
    for page in range(MAX_PAGE):
        docs = fetch_page(start_date, end_date, page, api_key, session)
        if not docs:
            break
        all_docs.extend(docs)
        time.sleep(delay)
        # Last page may have fewer than 10 results — that's the end
        if len(docs) < HITS_PER_PAGE:
            break
    return all_docs


def fetch_nyt(start_date, end_date, api_key, delay, session,
              on_chunk_done=None):
    """Walk the date range in monthly chunks, subdividing to weekly if needed.
    Calls `on_chunk_done(docs_so_far)` after each monthly chunk so the caller
    can persist partial progress (so an interrupt doesn't lose everything)."""
    all_docs = []
    cur = start_date
    while cur <= end_date:
        # Month end
        _, last_day = calendar.monthrange(cur.year, cur.month)
        chunk_end = datetime(cur.year, cur.month, last_day)
        chunk_end = min(chunk_end, end_date)
        print(f"  {cur.date()} → {chunk_end.date()}...", end=" ", flush=True)
        docs = fetch_chunk(cur, chunk_end, api_key, delay, session)
        # If we hit the 1000 cap, subdivide to weekly
        if len(docs) >= MAX_PAGE * HITS_PER_PAGE:
            print(f"({len(docs)}, hit cap — subdividing weekly)")
            docs = []
            sub = cur
            while sub <= chunk_end:
                sub_end = min(sub + timedelta(days=6), chunk_end)
                week_docs = fetch_chunk(sub, sub_end, api_key, delay, session)
                docs.extend(week_docs)
                print(f"    {sub.date()} → {sub_end.date()}: {len(week_docs)}")
                sub = sub_end + timedelta(days=1)
        else:
            print(f"{len(docs)} articles")
        all_docs.extend(docs)
        if on_chunk_done is not None:
            on_chunk_done(docs)
        cur = chunk_end + timedelta(days=1)
    return all_docs


# --------------------------------------------------------------------------
# Convert NYT docs → master CSV rows
# --------------------------------------------------------------------------
def to_master_rows(docs):
    rows = []
    for d in docs:
        # headline is a dict with a "main" key in the modern API
        hl = d.get("headline") or {}
        title = ""
        if isinstance(hl, dict):
            title = (hl.get("main") or "").strip()
        elif isinstance(hl, str):
            title = hl.strip()
        if not title:
            continue
        # Filter to headlines actually mentioning Trump
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
# Main
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="NYT Article Search API scraper")
    p.add_argument("--start-date", default=None,
                   help="YYYY-MM-DD. Defaults to 14 days ago.")
    p.add_argument("--end-date",   default=None,
                   help="YYYY-MM-DD. Defaults to today.")
    p.add_argument("--master-csv", required=True,
                   help="Path to trump_headlines_master.csv")
    p.add_argument("--delay",      type=float, default=0.5,
                   help="Seconds between page calls (default: 0.5 = 2 req/s)")
    p.add_argument("--api-key",    default=None,
                   help="NYT API key (or set NYT_API_KEY env var)")
    args = p.parse_args()

    api_key = args.api_key or os.environ.get("NYT_API_KEY", "").strip()
    if not api_key:
        print("ERROR: NYT_API_KEY not set. Either pass --api-key or add\n"
              "  export NYT_API_KEY=\"...\"  to your ~/.zshrc and re-source.",
              file=sys.stderr)
        sys.exit(1)

    today = datetime.utcnow().date()
    end_date = (datetime.strptime(args.end_date, "%Y-%m-%d")
                if args.end_date
                else datetime.combine(today, datetime.min.time()))
    start_date = (datetime.strptime(args.start_date, "%Y-%m-%d")
                  if args.start_date
                  else datetime.combine(today - timedelta(days=14), datetime.min.time()))

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

    print(f"\nFetching NYT articles {start_date.date()} → {end_date.date()}, "
          f"delay={args.delay}s\n")

    session = requests.Session()

    # Persist progress after every monthly chunk so an interrupt is safe.
    total_new = 0
    state = {"df": existing_df, "urls": existing_urls}

    def save_chunk(month_docs):
        nonlocal total_new
        new_rows = to_master_rows(month_docs)
        new_rows = [r for r in new_rows
                    if normalize_url(r["url"]) not in state["urls"]]
        if not new_rows:
            print("    (no new rows after dedup)")
            return
        for r in new_rows:
            state["urls"].add(normalize_url(r["url"]))
        new_df = pd.DataFrame(new_rows)
        if state["df"] is not None:
            state["df"] = pd.concat([state["df"], new_df], ignore_index=True)
        else:
            state["df"] = new_df
        state["df"]["__sortkey"] = pd.to_datetime(state["df"]["date"], errors="coerce")
        state["df"] = (state["df"]
                       .sort_values(["__sortkey", "outlet"])
                       .drop(columns="__sortkey"))
        state["df"].to_csv(args.master_csv, index=False)
        total_new += len(new_rows)
        print(f"    ✓ Saved {len(new_rows)} new rows "
              f"(master now {len(state['df']):,})")

    fetch_nyt(start_date, end_date, api_key, args.delay, session,
              on_chunk_done=save_chunk)

    if total_new == 0:
        print("\nNo new NYT articles added.")
    else:
        print(f"\nDone. Added {total_new:,} new NYT rows total.")
        print(f"Master CSV now has {len(state['df']):,} total rows.")


if __name__ == "__main__":
    main()
