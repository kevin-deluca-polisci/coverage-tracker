#!/usr/bin/env python3
"""
Combines all per-network per-month CSV files into a single master CSV.
Skips any identifiers already present in the existing master CSV.

Usage:
    python3 combine_networks.py --dataset-dir /path/to/all_networks_dataset --output /path/to/master.csv
"""

import os
import argparse
import pandas as pd
import glob
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

NETWORK_FILES = {
    "CBS":       "all_cbs_shows_dataset.csv",
    "CNN":       "all_cnn_shows_dataset.csv",
    "Fox":       "all_fox_shows_dataset.csv",
    "ABC":       "all_abc_shows_dataset.csv",
    "MSNBC/NBC": "all_msnbc_shows_dataset.csv",
}

def combine(dataset_dir: str, output_path: str):
    # Load existing master to get already-seen identifiers
    already_seen = set()
    if os.path.exists(output_path):
        try:
            existing = pd.read_csv(output_path, usecols=["identifier"], dtype=str)
            already_seen = set(existing["identifier"].dropna().tolist())
            logger.info(f"Existing master has {len(already_seen):,} identifiers — will skip these")
        except Exception as e:
            logger.warning(f"Could not read existing master: {e}")

    # Find all CSVs in dataset_dir (any subdirectory)
    all_csvs = glob.glob(os.path.join(dataset_dir, "**", "*.csv"), recursive=True)
    # Exclude master, checkpoint, summary, and analysis files
    skip_keywords = ["master", "checkpoint", "summary", "trump_performance", "progress"]
    data_csvs = [f for f in all_csvs
                 if not any(kw in os.path.basename(f).lower() for kw in skip_keywords)]

    logger.info(f"Found {len(data_csvs)} network CSV files to process")

    frames = []
    for csv_path in sorted(data_csvs):
        try:
            df = pd.read_csv(csv_path, dtype=str)
            if "identifier" not in df.columns:
                continue
            # Add network column if missing
            if "network" not in df.columns:
                # Infer from filename
                fname = os.path.basename(csv_path).lower()
                if "cbs" in fname:    df["network"] = "CBS"
                elif "cnn" in fname:  df["network"] = "CNN"
                elif "fox" in fname:  df["network"] = "Fox"
                elif "abc" in fname:  df["network"] = "ABC"
                elif "msnbc" in fname: df["network"] = "MSNBC/NBC"
                else:                  df["network"] = "Unknown"

            # Only keep new identifiers
            new_rows = df[~df["identifier"].isin(already_seen)]
            if len(new_rows) > 0:
                frames.append(new_rows)
                already_seen.update(new_rows["identifier"].tolist())
                logger.info(f"  {os.path.basename(csv_path)}: {len(new_rows):,} new rows")
            else:
                logger.info(f"  {os.path.basename(csv_path)}: all already in master, skipping")
        except Exception as e:
            logger.warning(f"Could not read {csv_path}: {e}")

    if not frames:
        logger.info("No new rows to add to master.")
        return

    new_data = pd.concat(frames, ignore_index=True)
    new_data.drop_duplicates(subset=["identifier"], keep="last", inplace=True)

    # Append to existing master or create new
    if os.path.exists(output_path):
        existing_master = pd.read_csv(output_path, dtype=str)
        combined = pd.concat([existing_master, new_data], ignore_index=True)
        combined.drop_duplicates(subset=["identifier"], keep="last", inplace=True)
    else:
        combined = new_data

    combined.to_csv(output_path, index=False, encoding="utf-8")
    logger.info(f"\n✓ Master CSV updated: {os.path.abspath(output_path)}")
    logger.info(f"  Total rows: {len(combined):,}")
    logger.info(f"  New rows added: {len(new_data):,}")
    if "network" in combined.columns:
        logger.info(f"  Breakdown by network:")
        for nw, cnt in combined["network"].value_counts().items():
            logger.info(f"    {nw}: {cnt:,}")


def main():
    parser = argparse.ArgumentParser(description="Combine all network CSVs into master dataset")
    parser.add_argument("--dataset-dir", required=True, help="Root directory containing all network folders")
    parser.add_argument("--output", required=True, help="Path for the master CSV output")
    args = parser.parse_args()
    combine(args.dataset_dir, args.output)


if __name__ == "__main__":
    main()
