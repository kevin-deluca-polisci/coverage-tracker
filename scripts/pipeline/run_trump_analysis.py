#!/usr/bin/env python3
"""
Trump Debate Performance Analyzer
Follows exact same logic as CNN_ABC_FOX_DEBATE_Trump_script notebook.
- Chunks transcripts into 3-sentence windows
- Filters for Trump mentions
- Classifies each chunk: +1 (well), -1 (poorly), 0 (neutral)
- Saves each chunk to disk as it goes (crash-safe)
- Skips articles already in existing chunks CSV

Usage:
    python3 run_trump_analysis.py \
        --input /path/to/all_networks_master.csv \
        --output-dir /path/to/output \
        --chunks-csv /path/to/trump_performance_chunks.csv
"""

import os
import re
import sys
import argparse
import logging
import pandas as pd
import torch
from transformers import pipeline
from tqdm.auto import tqdm

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Settings (same as your notebook) ─────────────────────────────────────────
CHUNK_SIZE  = 500
BATCH_SIZE  = 64
MAX_LENGTH  = 256

# ── Figure definitions ────────────────────────────────────────────────────────
figures_full = {
    "Donald Trump": ["Trump"]
}
last_name_to_full = {alt.lower(): full for full, alts in figures_full.items() for alt in alts}
figure_pattern    = r'\b(?:' + '|'.join(re.escape(name) for name in last_name_to_full.keys()) + r')\b'
key_figures       = ["Donald Trump"]

# ── Performance templates (same as your notebook) ─────────────────────────────
performance_templates = [
    "The author of this text believes {} is performing/performed/will perform well",
    "The author of this text believes {} is performing/performed/will perform poorly"
]


def classify_performance(classifier, transcripts, figure):
    perf_hyps = [t.format(figure) for t in performance_templates]
    perf_out  = classifier(
        transcripts,
        candidate_labels=perf_hyps,
        multi_label=True,
        batch_size=BATCH_SIZE,
        truncation=True,
        max_length=MAX_LENGTH
    )
    if isinstance(perf_out, dict):
        perf_out = [perf_out]
    n = len(transcripts)
    if len(perf_out) != n:
        perf_out = (perf_out + [None] * n)[:n]
    perf_vals = []
    for o in perf_out:
        if not isinstance(o, dict):
            perf_vals.append(0)
            continue
        scores = dict(zip(o["labels"], o["scores"]))
        if scores.get(perf_hyps[0], 0) > 0.5:
            perf_vals.append(1)
        elif scores.get(perf_hyps[1], 0) > 0.5:
            perf_vals.append(-1)
        else:
            perf_vals.append(0)
    return perf_vals


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",      required=True, help="Master CSV with all transcripts")
    parser.add_argument("--output-dir", required=True, help="Directory to save chunk files and final output")
    parser.add_argument("--chunks-csv", required=True, help="Cumulative chunks output CSV (appended to each run)")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    chunk_dir = os.path.join(args.output_dir, "analysis_chunks")
    os.makedirs(chunk_dir, exist_ok=True)

    # ── Load master CSV ───────────────────────────────────────────────────────
    logger.info(f"Loading master CSV: {args.input}")
    df = pd.read_csv(args.input, low_memory=False)
    logger.info(f"Loaded {len(df):,} rows")
    logger.info(f"Networks: {df['network'].value_counts().to_dict()}")

    if 'date' in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    # ── Skip already-analyzed articles ────────────────────────────────────────
    already_analyzed = set()
    if os.path.exists(args.chunks_csv):
        try:
            existing = pd.read_csv(args.chunks_csv, usecols=["article_id"], dtype=str)
            already_analyzed = set(existing["article_id"].dropna().tolist())
            logger.info(f"{len(already_analyzed):,} articles already analyzed — skipping")
        except Exception as e:
            logger.warning(f"Could not read existing chunks CSV: {e}")

    df = df[~df["identifier"].astype(str).isin(already_analyzed)]
    logger.info(f"{len(df):,} new articles to analyze")

    if df.empty:
        logger.info("Nothing new to analyze.")
        return

    # ── Chunk transcripts (every 3 sentences) ────────────────────────────────
    logger.info("Chunking transcripts...")
    all_chunks = []
    for idx, row in tqdm(df.iterrows(), total=len(df), desc="Chunking transcripts"):
        article_id   = str(row['identifier'])
        article_text = str(row.get('transcript', '') or '')
        if not article_text.strip() or article_text == 'nan':
            continue
        sentences = re.split(r'(?<=[.!?])\s+', article_text)
        sentences = [s.strip() for s in sentences if s.strip()]
        for j in range(0, len(sentences), 3):
            chunk_text = ' '.join(sentences[j:j+3])
            all_chunks.append({
                'article_id': article_id,
                'chunk_id':   j // 3 + 1,
                'transcript': chunk_text,
                'show_name':  row.get('show_name', ''),
                'network':    row.get('network', ''),
                'station':    row.get('station', ''),
                'date':       row.get('date', ''),
                'subject':    row.get('subject', ''),
                'url':        row.get('url', ''),
            })

    chunks_df = pd.DataFrame(all_chunks)
    logger.info(f"Created {len(chunks_df):,} chunks from {df['identifier'].nunique():,} articles")
    logger.info(f"Breakdown by network:\n{chunks_df['network'].value_counts()}")

    # ── Filter for Trump mentions ─────────────────────────────────────────────
    logger.info("Filtering for Trump mentions...")
    records = []
    for _, row in tqdm(chunks_df.iterrows(), total=len(chunks_df), desc="Filtering for Trump"):
        found = re.findall(figure_pattern, str(row["transcript"]), flags=re.IGNORECASE)
        if found:
            for match in set(found):
                full_name = last_name_to_full.get(match.lower())
                if full_name in key_figures:
                    rec = row.copy()
                    rec["figure"] = full_name
                    records.append(rec)
                    break

    df_filtered = pd.DataFrame(records)
    logger.info(f"Filtered to {len(df_filtered):,} chunks mentioning Trump")
    if 'network' in df_filtered.columns:
        logger.info(f"Breakdown:\n{df_filtered['network'].value_counts()}")

    if df_filtered.empty:
        logger.info("No Trump mentions found.")
        return

    # ── Load classifier ───────────────────────────────────────────────────────
    if torch.cuda.is_available():
        logger.info(f"GPU detected (CUDA): {torch.cuda.get_device_name(0)}")
        device = 0
        dtype  = torch.float16
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        logger.info("GPU detected (Apple Silicon MPS)")
        device = "mps"
        dtype  = None  # fp16 on MPS is unreliable across torch versions
    else:
        logger.info("No GPU — using CPU")
        device = -1
        dtype  = None

    performance_classifier = pipeline(
        "zero-shot-classification",
        model="zkava01/DEBATE_Performance_Jan21",
        tokenizer="zkava01/DEBATE_Performance_Jan21",
        device=device,
        torch_dtype=dtype
    )

    # ── Classify in chunks of CHUNK_SIZE, saving each to disk ────────────────
    n          = len(df_filtered)
    num_chunks = (n + CHUNK_SIZE - 1) // CHUNK_SIZE
    logger.info(f"Running performance classification in {num_chunks} chunks...")

    chunk_paths = []
    for i in range(num_chunks):
        start = i * CHUNK_SIZE
        end   = min((i + 1) * CHUNK_SIZE, n)

        chunk = df_filtered.iloc[start:end].copy().reset_index(drop=True)
        chunk["debate_performance"] = pd.NA

        transcript_ok = (chunk["transcript"].notna() &
                         chunk["transcript"].astype("string").str.strip().ne(""))
        mask = chunk["figure"].isin(key_figures) & transcript_ok
        sub  = chunk.loc[mask, ["transcript", "figure"]].copy()

        for fig, g in tqdm(sub.groupby("figure"), desc=f"Chunk {i+1}/{num_chunks}"):
            pos       = g.index.to_list()
            texts     = g["transcript"].astype("string").str.strip().to_list()
            perf_vals = classify_performance(performance_classifier, texts, fig)
            chunk.iloc[pos, chunk.columns.get_loc("debate_performance")] = perf_vals

        chunk_path = os.path.join(chunk_dir, f"trump_perf_chunk{i+1}.csv")
        chunk.to_csv(chunk_path, index=False)
        chunk_paths.append(chunk_path)
        logger.info(f"Saved chunk {i+1}/{num_chunks}")

    # ── Merge all chunks ──────────────────────────────────────────────────────
    logger.info("Merging all chunks...")
    new_results = pd.concat((pd.read_csv(p) for p in chunk_paths), ignore_index=True)

    # Append to cumulative chunks CSV
    if os.path.exists(args.chunks_csv):
        old      = pd.read_csv(args.chunks_csv, dtype=str)
        combined = pd.concat([old, new_results], ignore_index=True)
        combined.drop_duplicates(subset=["article_id", "chunk_id"], keep="last", inplace=True)
    else:
        combined = new_results

    combined.to_csv(args.chunks_csv, index=False, encoding="utf-8")
    logger.info(f"Saved final combined file: {args.chunks_csv}")
    logger.info(f"Total chunks with Trump mentions and performance scores: {len(combined):,}")

    logger.info("Final breakdown by network:")
    if 'network' in combined.columns:
        try:
            grp = combined.groupby('network')['debate_performance'].apply(
                lambda x: pd.to_numeric(x, errors='coerce')
            ).groupby(level=0)
            for nw, g in combined.groupby('network'):
                d = pd.to_numeric(g['debate_performance'], errors='coerce')
                logger.info(f"  {nw}: +1={int((d==1).sum())}  0={int((d==0).sum())}  -1={int((d==-1).sum())}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
