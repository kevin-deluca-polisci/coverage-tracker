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
def fetch_chunk(domain, start_dt, end_dt, session, retries=1):
    """One GDELT API call for a single domain + date range.

    Returns (articles, ok) where `ok` distinguishes "GDELT answered and there
    genuinely were no articles" from "we never got a usable answer". Without
    that distinction a throttled request looks identical to a quiet news week,
    which is how whole weeks of Fox News and Bloomberg silently went missing.

    Backoff is deliberately short. GDELT under load does not recover within a
    single request's lifetime, so long waits just burn wall-clock — a previous
    run spent ~34 minutes waiting to collect 131 rows. Better to give up
    quickly and let the next scheduled run retry."""
    # NOTE: GDELT 2.0 uses `domain:` for URL host filtering, NOT `source:`.
    # Per-outlet override lives in OUTLET_OPERATOR for outlets that need
    # the stricter `domainis:` exact-match.
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
                            headers={"User-Agent": USER_AGENT}, timeout=45)
            if r.status_code == 200:
                # GDELT sometimes returns HTML on errors; check
                try:
                    data = r.json()
                except ValueError:
                    if attempt < retries:
                        time.sleep(5)
                        continue
                    return [], False
                return data.get("articles", []) or [], True
            elif r.status_code == 429:
                wait = 15 * (attempt + 1)      # 15s, then 30s — then give up
                print(f"    [rate-limited, waiting {wait}s]",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
            else:
                print(f"    [HTTP {r.status_code} for {domain} "
                      f"{start_dt.date()} → {end_dt.date()}]",
                      file=sys.stderr, flush=True)
                if attempt < retries:
                    time.sleep(5)
        except requests.RequestException as e:
            print(f"    [network error: {type(e).__name__}]",
                  file=sys.stderr, flush=True)
            if attempt < retries:
                time.sleep(5)
    return [], False


def fetch_outlet(domain, outlet_name, start_date, end_date, delay, session,
                 deadline=None, max_consecutive_failures=2):
    """Fetch all articles for one outlet across the full date range.

    Chunks weekly; subdivides to daily if a weekly chunk hits the 250 cap.

    Two guards keep a throttled GDELT from eating the whole run:

      * circuit breaker — after `max_consecutive_failures` chunks that never
        returned a usable answer, stop trying this outlet. When GDELT is
        refusing one domain it will keep refusing it, so continuing just
        multiplies the wait.

      * deadline — a wall-clock cutoff shared across all outlets. Once passed
        we stop entirely and report how much of the window we covered.

    Returns (articles, stats) where stats records what actually happened, so
    the caller can tell "no articles exist" from "we never got an answer"."""
    all_articles = []
    stats = {"chunks": 0, "ok": 0, "failed": 0,
             "aborted": False, "reason": None}
    consecutive_failures = 0
    cur = start_date

    while cur <= end_date:
        if deadline is not None and time.time() > deadline:
            stats["aborted"] = True
            stats["reason"] = "time budget exhausted"
            print(f"    [stopping {outlet_name}: global time budget exhausted]",
                  flush=True)
            break

        chunk_end = min(cur + timedelta(days=6), end_date)
        articles, ok = fetch_chunk(domain, cur, chunk_end, session)
        stats["chunks"] += 1

        if not ok:
            stats["failed"] += 1
            consecutive_failures += 1
            print(f"    {cur.date()} → {chunk_end.date()}: NO ANSWER "
                  f"(failure {consecutive_failures}/{max_consecutive_failures})",
                  flush=True)
            if consecutive_failures >= max_consecutive_failures:
                stats["aborted"] = True
                stats["reason"] = "repeated failures"
                print(f"    [skipping rest of {outlet_name}: "
                      f"{consecutive_failures} consecutive failures]", flush=True)
                break
            time.sleep(delay)
            cur = chunk_end + timedelta(days=1)
            continue

        consecutive_failures = 0
        stats["ok"] += 1

        if len(articles) >= MAX_RECORDS:
            # Hit the cap — subdivide to daily for full coverage
            print(f"    {cur.date()}: {len(articles)} (capped) — subdividing to daily",
                  flush=True)
            articles = []
            sub = cur
            while sub <= chunk_end:
                if deadline is not None and time.time() > deadline:
                    stats["aborted"] = True
                    stats["reason"] = "time budget exhausted mid-subdivision"
                    break
                day_articles, day_ok = fetch_chunk(domain, sub, sub, session)
                if day_ok:
                    articles.extend(day_articles)
                time.sleep(delay)
                sub += timedelta(days=1)

        all_articles.extend(articles)
        print(f"    {cur.date()} → {chunk_end.date()}: {len(articles)} articles",
              flush=True)
        time.sleep(delay)
        cur = chunk_end + timedelta(days=1)

    return all_articles, stats


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
                   help="Specific domain(s) only. Defaults to all configured.")
    p.add_argument("--budget",     type=float, default=12.0,
                   help="Wall-clock budget in minutes for the whole GDELT pass "
                        "(default: 12; 0 disables). GDELT is a supplementary "
                        "source — Media Cloud and Google News cover the same "
                        "outlets — so it is not worth stalling a run for.")
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
    run_start = time.time()
    deadline = run_start + args.budget * 60 if args.budget > 0 else None
    if deadline:
        print(f"Time budget: {args.budget:.0f} min "
              f"(GDELT under load can otherwise stall a run for an hour)\n",
              flush=True)

    report = []   # (outlet, fetched, kept, stats)
    for domain, outlet_name in outlets.items():
        if deadline is not None and time.time() > deadline:
            print(f"=== {outlet_name} ({domain}) — SKIPPED, time budget exhausted ===\n",
                  flush=True)
            report.append((outlet_name, 0, 0,
                           {"aborted": True, "reason": "budget exhausted before start",
                            "chunks": 0, "ok": 0, "failed": 0}))
            continue

        print(f"=== {outlet_name} ({domain}) ===", flush=True)
        articles, stats = fetch_outlet(domain, outlet_name, start_date, end_date,
                                       args.delay, session, deadline=deadline)
        new_rows = to_master_rows(articles, outlet_name, domain)
        before = len(new_rows)
        new_rows = [r for r in new_rows
                    if normalize_url(r["url"]) not in existing_urls]
        for r in new_rows:
            existing_urls.add(normalize_url(r["url"]))
        print(f"  {outlet_name}: {len(articles)} fetched, "
              f"{before} after headline filter, {len(new_rows)} new after dedup",
              flush=True)
        report.append((outlet_name, len(articles), len(new_rows), stats))

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
            print(f"  ✓ Saved (master now {len(existing_df):,} rows)\n", flush=True)
            total_new += len(new_rows)
        else:
            print("  (nothing new to save)\n", flush=True)

    # ---- Honest summary -------------------------------------------------
    # The point of this block is that a silent zero used to be indistinguishable
    # from a genuine zero. Now incomplete coverage is stated explicitly, so a
    # throttled GDELT looks like a problem rather than like quiet news.
    elapsed = (time.time() - run_start) / 60
    print("=" * 62)
    print(f"GDELT summary — {elapsed:.1f} min elapsed")
    print(f"{'outlet':<22} {'fetched':>8} {'new':>6}  coverage")
    print("-" * 62)
    degraded = []
    for name, fetched, kept, st in report:
        chunks, ok, failed = st.get("chunks", 0), st.get("ok", 0), st.get("failed", 0)
        if st.get("aborted"):
            cov = f"PARTIAL — {st.get('reason')}"
            degraded.append(name)
        elif failed:
            cov = f"{ok}/{chunks} chunks ({failed} no-answer)"
            degraded.append(name)
        else:
            cov = f"{ok}/{chunks} chunks"
        print(f"{name:<22} {fetched:>8} {kept:>6}  {cov}")
    print("-" * 62)
    print(f"{'TOTAL':<22} {'':>8} {total_new:>6}")
    if degraded:
        print(f"\n⚠  Incomplete coverage for: {', '.join(degraded)}")
        print("   These windows were not fully retrieved. Media Cloud and Google")
        print("   News cover the same outlets, so this is a gap in supplementary")
        print("   data rather than a hole in the dataset. The next run retries.")
    if existing_df is not None:
        print(f"\nMaster CSV now has {len(existing_df):,} total rows.")


if __name__ == "__main__":
    main()
