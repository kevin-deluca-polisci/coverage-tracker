#!/usr/bin/env python3
"""
Headline Trump Performance Analysis
=====================================
Runs the DEBATE model on newspaper headlines to classify
whether they frame Trump as performing well or poorly.

Usage:
    python3 run_headline_analysis.py \
        --input /nfs/roberts/scratch/pi_kd769/zsk9/mediacloud_data/trump_headlines_master.csv \
        --output /nfs/roberts/scratch/pi_kd769/zsk9/mediacloud_data/trump_headlines_analyzed.csv
"""

import argparse
import os
import pandas as pd
import torch
from transformers import pipeline

MODEL_NAME  = "zkava01/DEBATE_Performance_Jan21"
BATCH_SIZE  = 128  # headlines are short so we can use larger batches
MAX_LENGTH  = 128  # headlines are short

HYP_POS = "The author of this text believes that Trump is performing/performed/will perform well"
HYP_NEG = "The author of this text believes that Trump is performing/performed/will perform poorly"


def classify_headlines(texts, classifier):
    """Classify a batch of headlines."""
    results_pos = classifier(
        texts,
        candidate_labels=[HYP_POS],
        hypothesis_template="{}",
        multi_label=True,
        truncation=True,
        max_length=MAX_LENGTH
    )
    results_neg = classifier(
        texts,
        candidate_labels=[HYP_NEG],
        hypothesis_template="{}",
        multi_label=True,
        truncation=True,
        max_length=MAX_LENGTH
    )

    scores = []
    for pos, neg in zip(results_pos, results_neg):
        score_pos = pos['scores'][0]
        score_neg = neg['scores'][0]
        if score_pos > 0.5:
            scores.append(1)
        elif score_neg > 0.5:
            scores.append(-1)
        else:
            scores.append(0)
    return scores


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    print(f"Loading headlines from {args.input}")
    df = pd.read_csv(args.input, stringsAsFactors=False) if False else pd.read_csv(args.input)
    print(f"Total headlines: {len(df):,}")

    # Skip already analyzed if output exists
    if os.path.exists(args.output):
        existing = pd.read_csv(args.output)
        already_done = set(zip(existing['title'], existing['date']))
        mask = ~df.apply(lambda r: (r['title'], str(r['date'])) in already_done, axis=1)
        df_new = df[mask].copy()
        print(f"Already analyzed: {len(existing):,} — skipping to {len(df_new):,} new headlines")
    else:
        df_new = df.copy()
        existing = None

    if len(df_new) == 0:
        print("Nothing new to analyze.")
        return

    # Load model
    print(f"Loading model: {MODEL_NAME}")
    if torch.cuda.is_available():
        device, device_label = 0, "CUDA GPU"
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        device, device_label = "mps", "Apple Silicon MPS"
    else:
        device, device_label = -1, "CPU"
    print(f"Using device: {device_label}")

    classifier = pipeline(
        "zero-shot-classification",
        model=MODEL_NAME,
        device=device
    )

    # Run analysis in batches
    titles = df_new['title'].fillna('').tolist()
    all_scores = []
    total_batches = (len(titles) + BATCH_SIZE - 1) // BATCH_SIZE

    print(f"Classifying {len(titles):,} headlines in {total_batches} batches...")
    for i in range(0, len(titles), BATCH_SIZE):
        batch = titles[i:i+BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        if batch_num % 10 == 0 or batch_num == 1:
            print(f"  Batch {batch_num}/{total_batches} ({i:,}/{len(titles):,} headlines)")
        scores = classify_headlines(batch, classifier)
        all_scores.extend(scores)

    df_new['debate_performance'] = all_scores

    # Combine with existing
    if existing is not None:
        combined = pd.concat([existing, df_new], ignore_index=True)
    else:
        combined = df_new

    combined = combined.sort_values(['date', 'outlet'])
    combined.to_csv(args.output, index=False)
    print(f"\nDone! Saved {len(combined):,} analyzed headlines to {args.output}")

    # Summary
    print("\nOverall breakdown:")
    print(f"  Positive: {(combined['debate_performance']==1).sum():,} ({(combined['debate_performance']==1).mean()*100:.1f}%)")
    print(f"  Negative: {(combined['debate_performance']==-1).sum():,} ({(combined['debate_performance']==-1).mean()*100:.1f}%)")
    print(f"  Neutral:  {(combined['debate_performance']==0).sum():,} ({(combined['debate_performance']==0).mean()*100:.1f}%)")

    print("\nBreakdown by outlet:")
    for outlet, grp in combined.groupby('outlet'):
        pos = (grp['debate_performance']==1).mean()*100
        neg = (grp['debate_performance']==-1).mean()*100
        net = pos - neg
        print(f"  {outlet:<25} net={net:+.1f}%  pos={pos:.1f}%  neg={neg:.1f}%  n={len(grp):,}")


if __name__ == "__main__":
    main()
