#!/usr/bin/env python3
"""scrape_gnews.py

Collects Trump headlines via Google News RSS search, scoped per-publisher with
the `site:` operator. This exists because Media Cloud's per-outlet indexing has
repeatedly decayed without warning — NYT (Aug 2025), ABC News (Mar 2026),
Bloomberg (Apr 2026), and most recently Reuters (Jun 2026, our largest outlet
at ~66K headlines). Google News gives a uniform fallback that doesn't depend
on any one aggregator staying healthy.

Endpoint (no API key required):
  https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en

Two quirks of Google News RSS that this script handles:

  1. Titles arrive with the publisher appended, e.g.
     "Trump signs order on tariffs - Reuters". The suffix is stripped so the
     headline text matches what the other scrapers produce (important: the
     DEBATE classifier sees this text, and a trailing " - Reuters" would be
     noise in the NLI input).

  2. Links are news.google.com redirect URLs, not canonical article URLs. That
     means URL-based dedup can't catch an article we already have from Media
     Cloud or GDELT. So this script ALSO dedups on (normalized title, date,
     outlet), which is the same key run_headline_analysis.py uses downstream.

Limits: roughly 100 items per query. Fine for ongoing daily collection, not
usable for historical backfill.

Usage:
  # ongoing (uses window from config, default 7d)
  python3 scrape_gnews.py \\
      --config scripts/pipeline/gnews_feeds.yaml \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv

  # tighter window for daily automated runs
  python3 scrape_gnews.py --config ... --master-csv ... --window 2d

  # one outlet only
  python3 scrape_gnews.py --config ... --master-csv ... --outlets Reuters
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, quote_plus
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET

import pandas as pd

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run:\n  pip install pyyaml",
          file=sys.stderr)
    sys.exit(1)

GNEWS_URL = ("https://news.google.com/rss/search"
             "?q={query}&hl=en-US&gl=US&ceid=US:en")
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36")


# --------------------------------------------------------------------------
# Normalization helpers (kept consistent with the other scrapers)
# --------------------------------------------------------------------------
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
    """Lowercase, collapse whitespace, drop punctuation — for fuzzy-ish dedup
    against titles that arrived from a different source with slightly
    different typography (curly vs straight quotes, en-dash vs hyphen, etc.)."""
    if not isinstance(t, str):
        return ""
    t = t.lower()
    t = re.sub(r"[‘’“”]", "'", t)   # smart quotes
    t = re.sub(r"[‐-―]", "-", t)              # dash variants
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def strip_publisher_suffix(title, publisher):
    """Google appends ' - Publisher' to every headline. Remove it.

    Uses the publisher string Google supplies in <source> when available,
    and otherwise falls back to trimming any short trailing ' - X' segment.
    Careful not to mangle legitimate headlines that contain a hyphen."""
    if not title:
        return title
    t = title.strip()
    if publisher:
        suffix = f" - {publisher.strip()}"
        if t.endswith(suffix):
            return t[: -len(suffix)].strip()
    # Fallback: strip a trailing " - Something" when "Something" looks like a
    # publisher name (short, no sentence punctuation).
    m = re.search(r"\s+-\s+([^-]{2,40})$", t)
    if m and not re.search(r"[.!?]$", m.group(1)):
        return t[: m.start()].strip()
    return t


def parse_date(raw):
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    try:
        cleaned = re.sub(r"[+-]\d{2}:?\d{2}$|Z$", "", raw.strip())
        return datetime.fromisoformat(cleaned[:19]).strftime("%Y-%m-%d")
    except Exception:
        pass
    m = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})", raw)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# --------------------------------------------------------------------------
# Fetch + parse
# --------------------------------------------------------------------------
def build_query(terms, domain, window, query_override=None):
    """Compose the Google News search string.

    `query_override` lets a single outlet in the config supply its own query
    when the default `site:<domain>` form doesn't work. Needed for publishers
    living on a subdomain of an unrelated registered domain — ABC News at
    abcnews.go.com being the known case, where `site:abcnews.go.com` returns
    nothing. The override may contain a literal {when} placeholder."""
    if query_override:
        return query_override.replace("{when}", f"when:{window}" if window else "")
    q = f"{terms} site:{domain}"
    if window:
        q += f" when:{window}"
    return q


def fetch_gnews(terms, domain, window, timeout=30, retries=2, query_override=None):
    """One Google News RSS search for a single publisher."""
    q = build_query(terms, domain, window, query_override)
    url = GNEWS_URL.format(query=quote_plus(q))

    for attempt in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            break
        except (URLError, HTTPError, TimeoutError) as e:
            if attempt < retries:
                wait = 5 * (attempt + 1)
                print(f"    [fetch error: {e}; retrying in {wait}s]",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
            else:
                print(f"    [fetch failed after {retries + 1} attempts: {e}]",
                      file=sys.stderr, flush=True)
                return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"    [parse error: {e}]", file=sys.stderr, flush=True)
        return []

    entries = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        # <source url="https://www.reuters.com">Reuters</source>
        src_el = item.find("source")
        publisher = (src_el.text or "").strip() if src_el is not None else ""
        if title and link:
            entries.append({"title": title, "link": link,
                            "published": pub, "publisher": publisher})
    return entries


def to_master_rows(entries, outlet_name, domain, publisher_match=None):
    """Convert Google News entries to master-CSV rows.

    `publisher_match` guards outlets whose query can't use `site:`. ABC News
    lives at abcnews.go.com — a subdomain of an unrelated registered domain —
    and Google's site: operator returns nothing for it, so its query has to be
    a plain keyword search ("trump abcnews"). That would happily match a CNN
    article that mentions ABC News in passing, which would then be filed under
    the wrong outlet. So when publisher_match is set we keep only entries whose
    <source> element confirms the publisher.

    Matching is a case-insensitive substring test because Google's source text
    is often a full site tagline, e.g.
    "ABC News - Breaking News, Latest News and Videos"."""
    rows = []
    needle = publisher_match.lower() if publisher_match else None
    for e in entries:
        if needle:
            pub = (e.get("publisher") or "").lower()
            if needle not in pub:
                continue
        title = strip_publisher_suffix(e["title"], e.get("publisher", ""))
        if not title or "trump" not in title.lower():
            continue
        date_str = parse_date(e["published"]) or datetime.utcnow().strftime("%Y-%m-%d")
        rows.append({
            "title":              title,
            "outlet":             outlet_name,
            "media_url":          domain,
            "media_name":         domain,
            "publish_date":       date_str,
            "url":                e["link"],
            "language":           "en",
            "date":               date_str,
            "debate_performance": "",
        })
    return rows


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Google News RSS scraper")
    p.add_argument("--config",     required=True, help="Path to gnews_feeds.yaml")
    p.add_argument("--master-csv", required=True, help="Headlines master CSV")
    p.add_argument("--outlets",    nargs="+", default=None,
                   help="Only these outlet names (default: all in config)")
    p.add_argument("--window",     default=None,
                   help="Override the when: window (e.g. 1d, 2d, 7d)")
    p.add_argument("--terms",      default=None,
                   help="Override search terms (default from config)")
    p.add_argument("--delay",      type=float, default=3.0,
                   help="Seconds between queries (default: 3.0)")
    args = p.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)
    defaults = cfg.get("defaults", {}) or {}
    terms = args.terms or defaults.get("terms", "trump")
    window = args.window or defaults.get("window", "7d")

    outlets = cfg.get("outlets", []) or []
    if args.outlets:
        outlets = [o for o in outlets if o["name"] in args.outlets]
        if not outlets:
            print(f"No matching outlets for {args.outlets}", file=sys.stderr)
            sys.exit(1)

    # Load master for dedup — by URL and by (title, date, outlet)
    existing_urls, existing_keys = set(), set()
    existing_df = None
    if os.path.exists(args.master_csv):
        existing_df = pd.read_csv(args.master_csv, dtype=str, low_memory=False)
        if "url" in existing_df.columns:
            existing_urls = {normalize_url(u) for u in existing_df["url"].dropna()}
            existing_urls.discard("")
        if {"title", "date", "outlet"}.issubset(existing_df.columns):
            existing_keys = {
                (normalize_title(t), str(d)[:10], str(o))
                for t, d, o in zip(existing_df["title"],
                                   existing_df["date"],
                                   existing_df["outlet"])
            }
        print(f"Loaded {len(existing_df):,} existing master rows "
              f"({len(existing_urls):,} URLs, {len(existing_keys):,} title keys)",
              flush=True)

    print(f"\nGoogle News RSS — terms={terms!r}, window={window!r}, "
          f"{len(outlets)} outlet(s), delay={args.delay}s\n", flush=True)

    total_new = 0
    for outlet in outlets:
        name, domain = outlet["name"], outlet["domain"]
        o_terms = outlet.get("terms", terms)
        o_window = outlet.get("window", window)
        o_query = outlet.get("query")
        shown_q = build_query(o_terms, domain, o_window, o_query)
        print(f"=== {name} [{shown_q}] ===", flush=True)

        entries = fetch_gnews(o_terms, domain, o_window, query_override=o_query)
        rows = to_master_rows(entries, name, domain,
                              publisher_match=outlet.get("publisher_match"))
        after_filter = len(rows)

        kept = []
        for r in rows:
            u = normalize_url(r["url"])
            k = (normalize_title(r["title"]), r["date"], r["outlet"])
            if u and u in existing_urls:
                continue
            if k in existing_keys:
                continue
            existing_urls.add(u)
            existing_keys.add(k)
            kept.append(r)

        print(f"  {name}: {len(entries)} fetched, {after_filter} after Trump "
              f"filter, {len(kept)} new after dedup", flush=True)

        if kept:
            new_df = pd.DataFrame(kept)
            existing_df = (pd.concat([existing_df, new_df], ignore_index=True)
                           if existing_df is not None else new_df)
            existing_df["__k"] = pd.to_datetime(existing_df["date"], errors="coerce")
            existing_df = (existing_df.sort_values(["__k", "outlet"])
                                      .drop(columns="__k"))
            existing_df.to_csv(args.master_csv, index=False)
            total_new += len(kept)
            print(f"  ✓ Saved (master now {len(existing_df):,} rows)\n", flush=True)
        else:
            print("  (nothing new to save)\n", flush=True)

        time.sleep(args.delay)

    if total_new == 0:
        print("No new articles added.", flush=True)
    else:
        print(f"\nDone. Added {total_new:,} new Google News rows.", flush=True)
        print(f"Master CSV now has {len(existing_df):,} total rows.", flush=True)


if __name__ == "__main__":
    main()
