#!/usr/bin/env python3
"""scrape_rss.py

Polls RSS feeds for outlets where Media Cloud's coverage is broken or absent:
ABC News (gap from Mar 2026 onward), Bloomberg (gap from Apr 2026 onward),
Politico (only 2 articles ever captured by MC), and Washington Post (added
because it's a major outlet we'd been missing).

Uses Python's built-in xml/urllib only — no extra dependencies. Filters
headlines for 'trump' (case-insensitive) and writes to the same master CSV
schema as the other scrapers (Media Cloud, GDELT, NYT API/Archive). URL-based
dedup against the existing master ensures no doubles when an article appears
in both this and another source.

Caveat: RSS only exposes the most recent items (typically last 20-100 per
feed). For high-volume outlets, articles roll off RSS within hours. Running
this every 3 days as part of update_local.sh captures most articles but
some may be missed during high-volume news cycles. This is documented in
the methodology section of the dashboard.

Usage:
  # ongoing (poll all configured feeds, save new matches)
  python3 scrape_rss.py \\
      --config scripts/pipeline/rss_feeds.yaml \\
      --master-csv data/raw/mediacloud_data/trump_headlines_master.csv

  # specific outlets only
  python3 scrape_rss.py \\
      --config scripts/pipeline/rss_feeds.yaml \\
      --master-csv ... \\
      --outlets "ABC News" "Politico"
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET

import pandas as pd

# Check yaml is available with friendly error
if True:
    try:
        import yaml
    except ImportError:
        print("ERROR: pyyaml not installed. Run:\n  pip install pyyaml",
              file=sys.stderr)
        sys.exit(1)

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36 "
              "coverage-tracker/1.0 (research; kevin-deluca-polisci/coverage-tracker)")


# --------------------------------------------------------------------------
# URL normalization (matches other scrapers' behavior)
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
# RSS feed parsing
# --------------------------------------------------------------------------
def fetch_feed(url, timeout=30):
    """Fetch one RSS/Atom feed. Returns list of {title, link, published} dicts."""
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (URLError, HTTPError, TimeoutError) as e:
        print(f"    [fetch error: {e}]", file=sys.stderr, flush=True)
        return []

    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"    [parse error: {e}]", file=sys.stderr, flush=True)
        return []

    # Strip XML namespaces for easier traversal — both RSS 2.0 and Atom
    # use various namespaces but their core elements have consistent names.
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    entries = []
    # RSS 2.0: channel/item with title, link, pubDate
    # Atom:    feed/entry with title, link/@href, published or updated
    for item in root.iter():
        if item.tag not in ("item", "entry"):
            continue
        title = ""
        link = ""
        pub = ""
        for child in item:
            if child.tag == "title" and child.text:
                title = child.text.strip()
            elif child.tag == "link":
                # Atom: <link href="..." />, RSS: <link>...</link>
                href = child.attrib.get("href", "")
                if href:
                    link = href.strip()
                elif child.text:
                    link = child.text.strip()
            elif child.tag in ("pubDate", "published", "updated", "date"):
                if child.text:
                    pub = child.text.strip()
        if title and link:
            entries.append({"title": title, "link": link, "published": pub})
    return entries


def parse_date(raw):
    """Best-effort date parser. RSS uses RFC822, Atom uses ISO 8601."""
    if not raw:
        return None
    # Try RFC822 first
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    # Try ISO 8601
    try:
        # Truncate timezone if present
        cleaned = re.sub(r"[+-]\d{2}:?\d{2}$|Z$", "", raw.strip())
        dt = datetime.fromisoformat(cleaned[:19])
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    # Try just YYYY-MM-DD prefix
    m = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


# --------------------------------------------------------------------------
# Filter + format to master CSV rows
# --------------------------------------------------------------------------
def to_master_rows(entries, outlet_name, domain):
    rows = []
    for e in entries:
        title = e["title"].strip()
        if not title:
            continue
        if "trump" not in title.lower():
            continue
        date_str = parse_date(e["published"])
        if not date_str:
            # If feed doesn't supply a date, fall back to today — better than
            # dropping the row entirely. Many RSS feeds are reliable enough
            # that this fallback rarely fires.
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
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
    p = argparse.ArgumentParser(description="RSS scraper for outlets MC/GDELT miss")
    p.add_argument("--config",     required=True,
                   help="Path to rss_feeds.yaml")
    p.add_argument("--master-csv", required=True,
                   help="Path to trump_headlines_master.csv")
    p.add_argument("--outlets",    nargs="+", default=None,
                   help="Only poll specific outlet names (default: all in config)")
    p.add_argument("--delay",      type=float, default=2.0,
                   help="Seconds between feed fetches (default: 2.0)")
    args = p.parse_args()

    with open(args.config, "r") as f:
        cfg = yaml.safe_load(f)
    outlets = cfg.get("outlets", [])
    if args.outlets:
        outlets = [o for o in outlets if o["name"] in args.outlets]
        if not outlets:
            print(f"No matching outlets in config for {args.outlets}", file=sys.stderr)
            sys.exit(1)

    # Load existing master for URL dedup
    existing_urls = set()
    existing_df = None
    if os.path.exists(args.master_csv):
        existing_df = pd.read_csv(args.master_csv, dtype=str, low_memory=False)
        if "url" in existing_df.columns:
            existing_urls = {normalize_url(u) for u in existing_df["url"].dropna()}
            existing_urls.discard("")
        print(f"Loaded {len(existing_df):,} existing master rows "
              f"({len(existing_urls):,} unique URLs)", flush=True)

    print(f"\nPolling {len(outlets)} outlet(s) via RSS, delay={args.delay}s\n",
          flush=True)

    total_new = 0
    for outlet in outlets:
        name = outlet["name"]
        domain = outlet["domain"]
        feeds = outlet.get("feeds", [])
        print(f"=== {name} ({domain}) — {len(feeds)} feed(s) ===", flush=True)
        all_entries = []
        for feed_url in feeds:
            entries = fetch_feed(feed_url)
            print(f"  {feed_url}: {len(entries)} entries", flush=True)
            all_entries.extend(entries)
            time.sleep(args.delay)
        if not all_entries:
            print(f"  (no entries fetched for {name})\n", flush=True)
            continue
        rows = to_master_rows(all_entries, name, domain)
        before = len(rows)
        rows = [r for r in rows if normalize_url(r["url"]) not in existing_urls]
        # Track URLs to avoid intra-run duplicates
        for r in rows:
            existing_urls.add(normalize_url(r["url"]))
        print(f"  {name}: {len(all_entries)} fetched, {before} after Trump filter, "
              f"{len(rows)} new after dedup", flush=True)

        # Save after each outlet so an interrupted run preserves progress
        if rows:
            new_df = pd.DataFrame(rows)
            if existing_df is not None:
                existing_df = pd.concat([existing_df, new_df], ignore_index=True)
            else:
                existing_df = new_df
            existing_df["__sortkey"] = pd.to_datetime(existing_df["date"], errors="coerce")
            existing_df = (existing_df
                           .sort_values(["__sortkey", "outlet"])
                           .drop(columns="__sortkey"))
            existing_df.to_csv(args.master_csv, index=False)
            total_new += len(rows)
            print(f"  ✓ Saved (master now {len(existing_df):,} rows)\n", flush=True)
        else:
            print(f"  (nothing new to save)\n", flush=True)

    if total_new == 0:
        print("No new articles added across any outlet.", flush=True)
    else:
        print(f"\nDone. Added {total_new:,} new RSS rows total.", flush=True)
        print(f"Master CSV now has {len(existing_df):,} total rows.", flush=True)


if __name__ == "__main__":
    main()
