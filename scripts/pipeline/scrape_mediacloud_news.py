#!/usr/bin/env python3
"""
Media Cloud News Scraper
========================
Scrapes headlines mentioning Trump from major US newspapers
using the Media Cloud API v4.

Usage:
    python3 scrape_mediacloud_news.py \
        --start-date 2025-01-01 \
        --end-date 2025-01-14 \
        --output-dir /nfs/roberts/scratch/pi_kd769/zsk9/mediacloud_data \
        --api-key YOUR_KEY  # or set MEDIACLOUD_API_KEY env var

Outlets covered:
    New York Times, Washington Post, Wall Street Journal,
    Los Angeles Times, USA Today, AP, Reuters, Politico,
    The Hill, NPR, NBC News, ABC News, CBS News, CNN, Fox News
"""

import argparse
import datetime
import json
import os
import time
import pandas as pd
import mediacloud.api

# ── Target outlets (Media Cloud source IDs) ───────────────────────────────────
# These are the media_id values for major national outlets in Media Cloud
# We filter by these after fetching from the US national collection
TARGET_OUTLETS = {
    "nytimes.com":          "New York Times",
    "washingtonpost.com":   "Washington Post",
    "wsj.com":              "Wall Street Journal",
    "latimes.com":          "Los Angeles Times",
    "usatoday.com":         "USA Today",
    "apnews.com":           "Associated Press",
    "reuters.com":          "Reuters",
    "politico.com":         "Politico",
    "thehill.com":          "The Hill",
    "npr.org":              "NPR",
    "nbcnews.com":          "NBC News",
    "abcnews.go.com":       "ABC News",
    "cbsnews.com":          "CBS News",
    "cnn.com":              "CNN",
    "foxnews.com":          "Fox News",
    "axios.com":            "Axios",
    "bloomberg.com":        "Bloomberg",
}

US_NATIONAL_COLLECTION = 34412234


def get_outlet_name(media_url):
    """Match a story's media URL to one of our target outlets."""
    if not media_url:
        return None
    for domain, name in TARGET_OUTLETS.items():
        if domain in media_url:
            return name
    return None


def scrape_trump_headlines(start_date, end_date, api_key, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    mc_search = mediacloud.api.SearchApi(api_key)

    print(f"Searching for Trump headlines: {start_date} to {end_date}")
    print(f"Target outlets: {len(TARGET_OUTLETS)}")

    all_stories = []
    pagination_token = None
    page_num = 0
    total_fetched = 0

    while True:
        page_num += 1
        print(f"  Fetching page {page_num}...", end=" ", flush=True)

        # Retry up to 5 times with increasing delay
        retries = 0
        page = None
        while retries < 8:
            try:
                page, pagination_token = mc_search.story_list(
                    'Trump',
                    start_date=start_date,
                    end_date=end_date,
                    collection_ids=[US_NATIONAL_COLLECTION],
                    pagination_token=pagination_token
                )
                break
            except Exception as e:
                retries += 1
                wait = retries * 15
                print(f"\nAPI error (attempt {retries}/5): {e}. Waiting {wait}s...")
                time.sleep(wait)
        if page is None:
            print(f"\nFailed after 5 retries on page {page_num}, stopping.")
            break

        if not page:
            print("empty page, done.")
            break

        # Filter to target outlets only
        matched = []
        for story in page:
            outlet_name = get_outlet_name(story.get('media_url', ''))
            if outlet_name:
                matched.append({
                    'title':        story.get('title', ''),
                    'outlet':       outlet_name,
                    'media_url':    story.get('media_url', ''),
                    'media_name':   story.get('media_name', ''),
                    'publish_date': story.get('publish_date', ''),
                    'url':          story.get('url', ''),
                    'language':     story.get('language', ''),
                })

        all_stories.extend(matched)
        total_fetched += len(page)
        print(f"got {len(page)} stories, {len(matched)} matched target outlets (total matched: {len(all_stories)})")

        if not pagination_token:
            print("  No more pages.")
            break

        # Be polite to the API
        time.sleep(3)

    print(f"\nTotal fetched: {total_fetched} stories")
    print(f"Total matched to target outlets: {len(all_stories)}")

    if not all_stories:
        print("No stories found for target outlets in this date range.")
        return None

    df = pd.DataFrame(all_stories)

    # Clean up date
    df['publish_date'] = pd.to_datetime(df['publish_date'], errors='coerce')
    df['date'] = df['publish_date'].dt.date
    df = df.dropna(subset=['title', 'date'])
    df = df[df['title'].str.strip() != '']

    # Remove duplicates
    df = df.drop_duplicates(subset=['title', 'outlet', 'date'])

    # Save
    stamp = f"{start_date}_{end_date}"
    out_path = os.path.join(output_dir, f"trump_headlines_{stamp}.csv")
    df.to_csv(out_path, index=False)
    print(f"Saved {len(df)} headlines to {out_path}")

    # Print breakdown by outlet
    print("\nBreakdown by outlet:")
    print(df.groupby('outlet').size().sort_values(ascending=False).to_string())

    return out_path


def combine_headline_csvs(data_dir, output_path):
    """Combine all headline CSVs into one master file, skipping duplicates."""
    import glob
    files = glob.glob(os.path.join(data_dir, "trump_headlines_*.csv"))
    if not files:
        print("No headline CSVs found.")
        return

    dfs = []
    for f in sorted(files):
        try:
            dfs.append(pd.read_csv(f))
        except Exception as e:
            print(f"Error reading {f}: {e}")

    if not dfs:
        return

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset=['title', 'outlet', 'date'])
    combined = combined.sort_values(['date', 'outlet'])

    # If master already exists, merge and deduplicate
    if os.path.exists(output_path):
        existing = pd.read_csv(output_path)
        combined = pd.concat([existing, combined], ignore_index=True)
        combined = combined.drop_duplicates(subset=['title', 'outlet', 'date'])
        combined = combined.sort_values(['date', 'outlet'])

    combined.to_csv(output_path, index=False)
    print(f"Master file: {len(combined)} headlines saved to {output_path}")
    print("\nBreakdown by outlet:")
    print(combined.groupby('outlet').size().sort_values(ascending=False).to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape Trump headlines from Media Cloud")
    parser.add_argument("--start-date",  required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end-date",    required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--output-dir",  default="/nfs/roberts/scratch/pi_kd769/zsk9/mediacloud_data")
    parser.add_argument("--master-csv",  default="/nfs/roberts/scratch/pi_kd769/zsk9/mediacloud_data/trump_headlines_master.csv")
    parser.add_argument("--api-key",     default=None, help="Media Cloud API key (or set MEDIACLOUD_API_KEY env var)")
    parser.add_argument("--combine-only", action="store_true", help="Just combine existing CSVs, don't scrape")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("MEDIACLOUD_API_KEY")
    if not api_key and not args.combine_only:
        raise ValueError("No API key provided. Use --api-key or set MEDIACLOUD_API_KEY env var.")

    if not args.combine_only:
        start = datetime.date.fromisoformat(args.start_date)
        end   = datetime.date.fromisoformat(args.end_date)
        scrape_trump_headlines(start, end, api_key, args.output_dir)

    combine_headline_csvs(args.output_dir, args.master_csv)
