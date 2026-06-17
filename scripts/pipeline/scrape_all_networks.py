#!/usr/bin/env python3
"""
Unified Multi-Network TV Transcript Scraper + Trump Debate Performance Analyzer
================================================================================
Scrapes CBS, CNN, Fox, ABC, and MSNBC/NBC/MSNOW from the Internet Archive,
then automatically runs the Trump debate-performance hypothesis on every new
transcript, adding a `debate_performance` column (+1 / 0 / -1) per chunk.

PIPELINE
────────
  Step 1 – Scrape: fetch new shows from archive.org, skip already-scraped items
  Step 2 – Analyze: chunk transcripts → filter Trump mentions → classify performance

USAGE
─────
  # Initial full scrape + analysis (all networks):
  python scrape_all_networks.py --start-date 2025-01-01 --end-date 2026-03-25 --auto

  # Scrape specific networks only:
  python scrape_all_networks.py --networks cbs cnn --start-date 2025-01-01 --end-date 2026-03-25 --auto

  # Weekly update — only fetches new content, then re-runs analysis on new items:
  python scrape_all_networks.py --update

  # Update with custom lookback window (default 8 days):
  python scrape_all_networks.py --update --lookback-days 14

  # Skip analysis (scrape only):
  python scrape_all_networks.py --update --no-analysis

  # Analysis only (no new scraping):
  python scrape_all_networks.py --analysis-only

GPU / CPU
─────────
  The classifier auto-detects a CUDA GPU (fp16) and falls back to CPU (fp32).
  On CPU this is slow — reduce --batch-size if you run out of memory.

OUTPUT FILES (all inside --output-dir, default: all_networks_dataset/)
───────────
  all_{network}_shows_dataset.csv          per-network full dataset (with transcripts)
  all_{network}_shows_summary.csv          same without transcript column
  all_{network}_shows_dataset.xlsx         Excel version
  trump_performance_chunks.csv             ALL chunk-level results across networks
  trump_performance_chunks_new.csv         only the newly processed chunks this run
  all_networks_master.csv                  combined summary across all networks
  scraper_state.json                       last-run state for --update mode
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import sys
import argparse
from typing import List, Dict, Any, Optional, Set
import time
import logging
import re
import pandas as pd
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# Network definitions
# ══════════════════════════════════════════════════════════════════════════════

NETWORKS: Dict[str, Dict[str, str]] = {
    "cbs":   {"label": "CBS",       "search_terms": "CBS",                     "dataset_name": "cbs"},
    "cnn":   {"label": "CNN",       "search_terms": "CNN",                     "dataset_name": "cnn"},
    "fox":   {"label": "Fox",       "search_terms": "Fox",                     "dataset_name": "fox"},
    "abc":   {"label": "ABC",       "search_terms": "ABC",                     "dataset_name": "abc"},
    "msnbc": {"label": "MSNBC/NBC", "search_terms": "(MSNBC OR MSNOW OR NBC)", "dataset_name": "msnbc_nbc"},
}

STATE_FILE = "scraper_state.json"
MASTER_CSV = "all_networks_master.csv"
CHUNKS_CSV = "trump_performance_chunks.csv"

# ══════════════════════════════════════════════════════════════════════════════
# State helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_state(output_dir: str) -> Dict[str, Any]:
    path = os.path.join(output_dir, STATE_FILE)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(output_dir: str, state: Dict[str, Any]):
    path = os.path.join(output_dir, STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    logger.info(f"State saved → {path}")


# ══════════════════════════════════════════════════════════════════════════════
# Scraper
# ══════════════════════════════════════════════════════════════════════════════

class NetworkTranscriptScraper:
    BASE_URL   = "https://archive.org"
    SEARCH_API = f"{BASE_URL}/advancedsearch.php"

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(os.path.join(output_dir, "transcripts"), exist_ok=True)
        os.makedirs(os.path.join(output_dir, "metadata"),    exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; research-scraper/1.0)"})

    def get_scraped_identifiers(self, network_key: str) -> Set[str]:
        net  = NETWORKS[network_key]
        csv  = os.path.join(self.output_dir, f"all_{net['dataset_name']}_shows_dataset.csv")
        seen: Set[str] = set()
        if os.path.exists(csv):
            try:
                df = pd.read_csv(csv, usecols=["identifier"], dtype=str)
                seen = set(df["identifier"].dropna().tolist())
                logger.info(f"[{net['label']}] {len(seen):,} identifiers already in CSV")
            except Exception as e:
                logger.warning(f"Could not read existing CSV ({e}); scanning transcripts folder")
        transcript_dir = os.path.join(self.output_dir, "transcripts")
        if os.path.isdir(transcript_dir):
            for fname in os.listdir(transcript_dir):
                if fname.endswith(".txt"):
                    seen.add(fname[:-4])
        return seen

    def fetch_show_list(self, network_key: str, start_date: str, end_date: str,
                        already_scraped: Set[str]) -> List[Dict[str, Any]]:
        net          = NETWORKS[network_key]
        search_query = (f'collection:tvarchive AND {net["search_terms"]} '
                        f'AND date:[{start_date} TO {end_date}]')
        try:
            r     = self.session.get(self.SEARCH_API,
                                     params={"q": search_query, "fl[]": ["identifier"],
                                             "rows": 1, "output": "json"}, timeout=30)
            r.raise_for_status()
            total = r.json().get("response", {}).get("numFound", 0)
        except Exception as e:
            logger.error(f"[{net['label']}] Count query failed: {e}")
            return []

        print(f"  ✓ Found {total:,} total {net['label']} items in date range")

        all_docs: List[Dict] = []
        batch_size = 1000
        for page in range(1, (total // batch_size) + 2):
            params = {"q": search_query,
                      "fl[]": ["identifier", "title", "date", "publicdate",
                                "subject", "description", "creator"],
                      "rows": batch_size, "page": page,
                      "output": "json", "sort[]": "date desc"}
            try:
                r    = self.session.get(self.SEARCH_API, params=params, timeout=30)
                r.raise_for_status()
                docs = r.json().get("response", {}).get("docs", [])
            except Exception as e:
                logger.error(f"[{net['label']}] Page {page} fetch failed: {e}")
                break
            if not docs:
                break
            all_docs.extend(docs)
            if len(docs) < batch_size:
                break

        new_docs = [d for d in all_docs
                    if d.get("identifier") not in already_scraped
                    and d.get("identifier", "").replace("/", "_") not in already_scraped]
        print(f"  → Skipping {len(all_docs)-len(new_docs):,} already-scraped; "
              f"{len(new_docs):,} new to fetch")
        return new_docs

    def get_transcript(self, identifier: str) -> Optional[str]:
        try:
            r = self.session.get(f"{self.BASE_URL}/details/{identifier}", timeout=30)
            r.raise_for_status()
            soup  = BeautifulSoup(r.text, "html.parser")
            parts = [s.get_text(strip=True)
                     for s in soup.find_all("div", class_="snippet")
                     if s.get_text(strip=True)]
            return "\n\n".join(parts) if parts else None
        except Exception as e:
            logger.error(f"Transcript fetch failed for {identifier}: {e}")
            return None

    def save_files(self, identifier: str, transcript: str, metadata: Dict[str, Any]):
        safe_id = identifier.replace("/", "_")
        with open(os.path.join(self.output_dir, "transcripts", f"{safe_id}.txt"),
                  "w", encoding="utf-8") as f:
            f.write(transcript)
        with open(os.path.join(self.output_dir, "metadata", f"{safe_id}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

    def scrape_network(self, network_key: str, start_date: str, end_date: str,
                       delay: float = 2.0) -> pd.DataFrame:
        net = NETWORKS[network_key]
        print(f"\n{'='*60}")
        print(f"  Network : {net['label']}")
        print(f"  Range   : {start_date} → {end_date}")
        print(f"{'='*60}")

        already_scraped = self.get_scraped_identifiers(network_key)
        new_shows       = self.fetch_show_list(network_key, start_date, end_date, already_scraped)

        if not new_shows:
            print(f"  Nothing new to scrape for {net['label']}.")
            return pd.DataFrame()

        results: List[Dict] = []
        for i, show in enumerate(new_shows, 1):
            identifier = show.get("identifier", "")
            title      = show.get("title", "Unknown")
            date       = show.get("date", "")

            if i % 10 == 0 or i == 1:
                print(f"  Progress: {i}/{len(new_shows)} ({i/len(new_shows)*100:.1f}%)")
            if i % 100 == 0:
                elapsed   = i * delay / 60
                remaining = (len(new_shows) - i) * delay / 60
                print(f"  ⏱  Elapsed: {elapsed:.1f} min | Remaining ≈ {remaining:.1f} min")

            try:
                transcript  = self.get_transcript(identifier)
                show_parts  = title.split(" : ")
                subject_raw = show.get("subject", "")
                subject_str = (", ".join(subject_raw) if isinstance(subject_raw, list)
                               else (subject_raw or ""))
                record = {
                    "network":           net["label"],
                    "identifier":        identifier,
                    "show_name":         show_parts[0] if show_parts else title,
                    "station":           show_parts[1] if len(show_parts) > 1 else "",
                    "title":             title,
                    "date":              date,
                    "publicdate":        show.get("publicdate", ""),
                    "creator":           show.get("creator", ""),
                    "subject":           subject_str,
                    "description":       show.get("description", ""),
                    "transcript":        transcript or "",
                    "transcript_length": len(transcript) if transcript else 0,
                    "has_transcript":    bool(transcript and len(transcript) > 100),
                    "url":               f"https://archive.org/details/{identifier}",
                    "scraped_at":        datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                results.append(record)
                if transcript:
                    self.save_files(identifier, transcript, show)
            except Exception as e:
                logger.error(f"Error processing {identifier}: {e}")
                show_parts = title.split(" : ")
                results.append({
                    "network": net["label"], "identifier": identifier,
                    "show_name": show_parts[0] if show_parts else title,
                    "station": show_parts[1] if len(show_parts) > 1 else "",
                    "title": title, "date": date,
                    "publicdate": show.get("publicdate", ""),
                    "creator": show.get("creator", ""),
                    "subject": "", "description": show.get("description", ""),
                    "transcript": "", "transcript_length": 0, "has_transcript": False,
                    "url": f"https://archive.org/details/{identifier}",
                    "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                })

            if i % 100 == 0:
                pd.DataFrame(results).to_csv(
                    os.path.join(self.output_dir, f"checkpoint_{net['dataset_name']}_{i}.csv"),
                    index=False, encoding="utf-8")

            time.sleep(delay)

        new_df = pd.DataFrame(results)
        self._merge_and_save(network_key, new_df)
        return new_df

    def _merge_and_save(self, network_key: str, new_df: pd.DataFrame):
        net     = NETWORKS[network_key]
        csv     = os.path.join(self.output_dir, f"all_{net['dataset_name']}_shows_dataset.csv")
        summary = os.path.join(self.output_dir, f"all_{net['dataset_name']}_shows_summary.csv")

        if os.path.exists(csv):
            existing = pd.read_csv(csv, dtype=str)
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined.drop_duplicates(subset=["identifier"], keep="last", inplace=True)
        else:
            combined = new_df

        combined.to_csv(csv, index=False, encoding="utf-8")
        # XLSX writing was removed — Excel's 32,767-char cell limit caused
        # ~1,200 UserWarnings per run (transcripts are routinely >50K chars)
        # and the .xlsx files were never read by anything downstream. The
        # CSV above is the canonical per-network record.
        combined.drop(columns=["transcript"], errors="ignore").to_csv(summary, index=False)

        with_t = combined["has_transcript"].astype(str).str.lower().eq("true").sum()
        print(f"\n  [{net['label']}] Dataset updated — "
              f"{len(combined):,} total rows, {with_t:,} with transcript")


# ══════════════════════════════════════════════════════════════════════════════
# Trump Debate Performance Analyzer
# ══════════════════════════════════════════════════════════════════════════════

PERFORMANCE_TEMPLATES = [
    "The author of this text believes {} is performing/performed/will perform well",
    "The author of this text believes {} is performing/performed/will perform poorly",
]

# Add more figures here if you expand the analysis later
FIGURE_PATTERNS: Dict[str, List[str]] = {
    "Donald Trump": ["Trump"],
}


def load_classifier(device_id: int = 0, use_fp16: bool = True):
    """Load the DEBATE_Performance_Jan21 zero-shot classifier. Auto-detects GPU/CPU."""
    try:
        import torch
        from transformers import pipeline as hf_pipeline

        if torch.cuda.is_available():
            logger.info("GPU detected (CUDA) — using fp16" if use_fp16 else "GPU detected (CUDA) — using fp32")
            dtype  = torch.float16 if use_fp16 else None
            device = device_id
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            logger.info("GPU detected (Apple Silicon MPS) — using fp32 (MPS fp16 is unreliable)")
            dtype  = None
            device = "mps"
        else:
            logger.info("No GPU found — falling back to CPU (slower, fp32)")
            dtype  = None
            device = -1

        return hf_pipeline(
            "zero-shot-classification",
            model="zkava01/DEBATE_Performance_Jan21",
            tokenizer="zkava01/DEBATE_Performance_Jan21",
            device=device,
            torch_dtype=dtype,
        )
    except ImportError:
        logger.error(
            "transformers / torch not installed.\n"
            "Run:  pip install transformers torch\n"
            "Analysis step will be skipped."
        )
        return None


def _build_pattern(figures: Dict[str, List[str]]):
    last_name_to_full = {alt.lower(): full for full, alts in figures.items() for alt in alts}
    pattern = r'\b(?:' + '|'.join(re.escape(k) for k in last_name_to_full) + r')\b'
    return pattern, last_name_to_full


def classify_performance(clf, transcripts: List[str], figure: str,
                          batch_size: int = 64, max_length: int = 256) -> List[int]:
    """Returns list of ints: +1 (performing well), -1 (poorly), 0 (neutral)."""
    hyps = [t.format(figure) for t in PERFORMANCE_TEMPLATES]
    out  = clf(transcripts, candidate_labels=hyps, multi_label=True,
               batch_size=batch_size, truncation=True, max_length=max_length)

    if isinstance(out, dict):
        out = [out]
    n = len(transcripts)
    if len(out) != n:
        out = (out + [None] * n)[:n]

    results = []
    for o in out:
        if not isinstance(o, dict):
            results.append(0)
            continue
        scores = dict(zip(o["labels"], o["scores"]))
        if scores.get(hyps[0], 0) > 0.5:
            results.append(1)
        elif scores.get(hyps[1], 0) > 0.5:
            results.append(-1)
        else:
            results.append(0)
    return results


def run_analysis(output_dir: str, new_identifiers: Optional[Set[str]] = None,
                 batch_size: int = 64, chunk_group_size: int = 500) -> Optional[pd.DataFrame]:
    """
    Step 2: chunk transcripts → filter Trump mentions → classify performance.

    `new_identifiers`: if provided, only those shows are analyzed (targeted update mode).
    Otherwise, all shows not yet in trump_performance_chunks.csv are processed.

    Returns DataFrame of newly classified chunks, or None on classifier failure.
    """
    print(f"\n{'='*60}")
    print("  STEP 2 — Trump Debate Performance Analysis")
    print(f"  Model: zkava01/DEBATE_Performance_Jan21")
    print(f"{'='*60}")

    clf = load_classifier()
    if clf is None:
        print("  ✗ Classifier not available. Skipping analysis.")
        return None

    # -- Which articles have already been analyzed?
    existing_chunks_csv = os.path.join(output_dir, CHUNKS_CSV)
    already_analyzed: Set[str] = set()
    if os.path.exists(existing_chunks_csv):
        try:
            existing_chunks  = pd.read_csv(existing_chunks_csv, usecols=["article_id"], dtype=str)
            already_analyzed = set(existing_chunks["article_id"].dropna().tolist())
            print(f"  ✓ {len(already_analyzed):,} articles already analyzed — skipping")
        except Exception as e:
            logger.warning(f"Could not read existing chunks CSV: {e}")

    # -- Collect rows to process from per-network CSVs
    frames: List[pd.DataFrame] = []
    for nk, net in NETWORKS.items():
        csv = os.path.join(output_dir, f"all_{net['dataset_name']}_shows_dataset.csv")
        if not os.path.exists(csv):
            continue
        df = pd.read_csv(csv, dtype=str)
        df["network"] = net["label"]
        if new_identifiers is not None:
            df = df[df["identifier"].isin(new_identifiers)]
        df = df[~df["identifier"].isin(already_analyzed)]
        frames.append(df)

    if not frames:
        print("  No new transcripts to analyze.")
        return pd.DataFrame()

    all_shows = pd.concat(frames, ignore_index=True)
    print(f"  Shows to analyze: {len(all_shows):,}")

    # -- Chunk into 3-sentence windows
    print("  Chunking transcripts (3-sentence windows)...")
    pattern_str, last_name_to_full = _build_pattern(FIGURE_PATTERNS)
    key_figures = list(FIGURE_PATTERNS.keys())

    all_chunks: List[Dict] = []
    for _, row in all_shows.iterrows():
        text = str(row.get("transcript", "") or "")
        if not text.strip() or text == "nan":
            continue
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
        for j in range(0, len(sentences), 3):
            all_chunks.append({
                "article_id": row.get("identifier", ""),
                "chunk_id":   j // 3 + 1,
                "transcript": " ".join(sentences[j:j+3]),
                "show_name":  row.get("show_name", ""),
                "network":    row.get("network", ""),
                "station":    row.get("station", ""),
                "date":       row.get("date", ""),
                "subject":    row.get("subject", ""),
                "url":        row.get("url", ""),
            })

    chunks_df = pd.DataFrame(all_chunks)
    print(f"  Created {len(chunks_df):,} chunks from "
          f"{all_shows['identifier'].nunique():,} articles")

    # -- Filter for Trump mentions
    print("  Filtering for Trump mentions...")
    records: List[Dict] = []
    for _, row in chunks_df.iterrows():
        found = re.findall(pattern_str, str(row["transcript"]), flags=re.IGNORECASE)
        if found:
            for match in set(found):
                full_name = last_name_to_full.get(match.lower())
                if full_name in key_figures:
                    rec = row.to_dict()
                    rec["figure"] = full_name
                    records.append(rec)
                    break

    df_filtered = pd.DataFrame(records)
    if df_filtered.empty:
        print("  No Trump mentions found in new transcripts.")
        return df_filtered

    print(f"  Trump-mention chunks: {len(df_filtered):,}")
    if "network" in df_filtered.columns:
        for nw, cnt in df_filtered["network"].value_counts().items():
            print(f"    {nw}: {cnt:,} chunks")

    # -- Classify in groups of chunk_group_size
    df_filtered["debate_performance"] = pd.NA
    n          = len(df_filtered)
    num_groups = (n + chunk_group_size - 1) // chunk_group_size
    chunk_dir  = os.path.join(output_dir, "analysis_chunks")
    os.makedirs(chunk_dir, exist_ok=True)
    chunk_paths: List[str] = []

    # IMPORTANT: tqdm progress bars use \r and get swallowed by `tee`. Use
    # explicit print() with flush=True so progress survives shell piping and
    # the user can SEE that classification is making progress (otherwise it
    # looks like the script has hung for hours of silent MPS work).
    import time as _time
    t_classify_start = _time.time()
    print(f"\n  Running classifier in {num_groups} group(s) "
          f"({n:,} chunks, batch_size={batch_size})...", flush=True)
    for i in range(num_groups):
        t_group = _time.time()
        start = i * chunk_group_size
        end   = min((i + 1) * chunk_group_size, n)
        chunk = df_filtered.iloc[start:end].copy().reset_index(drop=True)
        chunk["debate_performance"] = pd.NA

        transcript_ok = (chunk["transcript"].notna() &
                         chunk["transcript"].astype("string").str.strip().ne(""))
        mask = chunk["figure"].isin(key_figures) & transcript_ok
        sub  = chunk.loc[mask, ["transcript", "figure"]].copy()

        for fig, g in sub.groupby("figure"):
            pos       = g.index.tolist()
            texts     = g["transcript"].astype("string").str.strip().tolist()
            perf_vals = classify_performance(clf, texts, fig, batch_size=batch_size)
            for p, v in zip(pos, perf_vals):
                chunk.at[p, "debate_performance"] = v

        fpath = os.path.join(chunk_dir, f"analysis_chunk_{i+1}.csv")
        chunk.to_csv(fpath, index=False)
        chunk_paths.append(fpath)

        # Per-group line with timing + ETA. Flushed so it appears immediately.
        elapsed = _time.time() - t_group
        total_elapsed = _time.time() - t_classify_start
        avg_per_group = total_elapsed / (i + 1)
        eta_min = avg_per_group * (num_groups - i - 1) / 60
        print(f"  Group {i+1}/{num_groups} done in {elapsed:.1f}s "
              f"(total {total_elapsed/60:.1f} min, ETA ≈ {eta_min:.1f} min)",
              flush=True)

    print(f"  Classification complete: {n:,} chunks in {(_time.time()-t_classify_start)/60:.1f} min",
          flush=True)

    # -- Merge and append to cumulative chunks CSV
    new_results = pd.concat((pd.read_csv(p) for p in chunk_paths), ignore_index=True)

    new_csv = os.path.join(output_dir, "trump_performance_chunks_new.csv")
    new_results.to_csv(new_csv, index=False, encoding="utf-8")

    if os.path.exists(existing_chunks_csv):
        old      = pd.read_csv(existing_chunks_csv, dtype=str)
        combined = pd.concat([old, new_results], ignore_index=True)
        combined.drop_duplicates(subset=["article_id", "chunk_id"], keep="last", inplace=True)
    else:
        combined = new_results

    combined.to_csv(existing_chunks_csv, index=False, encoding="utf-8")

    # -- Summary
    print(f"\n  ── Analysis complete ──")
    print(f"  New chunks classified : {len(new_results):,}")
    print(f"  Cumulative total      : {len(combined):,}")
    print(f"\n  debate_performance breakdown (new results):  +1=well  0=neutral  -1=poorly")
    if "network" in new_results.columns:
        try:
            for nw, g in new_results.groupby("network"):
                d = g["debate_performance"].astype(float)
                print(f"    {nw:12s}  +1={int((d==1).sum())}  "
                      f"0={int((d==0).sum())}  -1={int((d==-1).sum())}")
        except Exception:
            pass
    print(f"\n  → Cumulative chunks CSV : {os.path.abspath(existing_chunks_csv)}")
    print(f"  → This-run chunks CSV   : {os.path.abspath(new_csv)}")

    return new_results


# ══════════════════════════════════════════════════════════════════════════════
# Master CSV
# ══════════════════════════════════════════════════════════════════════════════

def rebuild_master_csv(output_dir: str):
    frames = []
    for nk, net in NETWORKS.items():
        csv = os.path.join(output_dir, f"all_{net['dataset_name']}_shows_dataset.csv")
        if os.path.exists(csv):
            df = pd.read_csv(csv, dtype=str)
            df["network"] = net["label"]
            frames.append(df)
    if not frames:
        return
    master = pd.concat(frames, ignore_index=True)
    master.drop_duplicates(subset=["identifier"], keep="last", inplace=True)
    path = os.path.join(output_dir, MASTER_CSV)
    master.drop(columns=["transcript"], errors="ignore").to_csv(path, index=False, encoding="utf-8")
    print(f"\n  📊 Master CSV updated → {os.path.abspath(path)}  ({len(master):,} total rows)")


# ══════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ══════════════════════════════════════════════════════════════════════════════

def run(network_keys, start_date, end_date, output_dir, delay,
        update_mode, lookback_days, run_analysis_flag, analysis_only, batch_size):
    os.makedirs(output_dir, exist_ok=True)
    state   = load_state(output_dir)
    scraper = NetworkTranscriptScraper(output_dir=output_dir)
    today   = datetime.utcnow().strftime("%Y-%m-%d")

    new_identifiers: Set[str] = set()

    # ── Step 1: Scrape ────────────────────────────────────────────────────────
    if not analysis_only:
        print(f"\n{'='*60}")
        print("  STEP 1 — Scraping")
        print(f"{'='*60}")
        for nk in network_keys:
            net = NETWORKS[nk]
            if update_mode:
                last_run_str = state.get(nk, {}).get("last_run")
                if last_run_str:
                    eff_start = (datetime.strptime(last_run_str, "%Y-%m-%d")
                                 - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
                    print(f"\n[--update] {net['label']}: {eff_start} → {today} "
                          f"(last run: {last_run_str})")
                else:
                    eff_start = start_date
                    print(f"\n[--update] {net['label']}: no prior run; using {eff_start} → {today}")
                eff_end = today
            else:
                eff_start = start_date
                eff_end   = end_date

            new_df = scraper.scrape_network(nk, eff_start, eff_end, delay)
            if not new_df.empty and "identifier" in new_df.columns:
                new_identifiers.update(new_df["identifier"].dropna().tolist())

            state.setdefault(nk, {})["last_run"]   = today
            state[nk]["last_start"] = eff_start
            state[nk]["last_end"]   = eff_end
            save_state(output_dir, state)

        rebuild_master_csv(output_dir)

    # ── Step 2: Analysis ──────────────────────────────────────────────────────
    if run_analysis_flag:
        # In update/full mode, only analyze newly scraped items for speed.
        # In analysis-only mode, process everything not yet analyzed.
        targeted = new_identifiers if new_identifiers else None
        run_analysis(output_dir=output_dir, new_identifiers=targeted, batch_size=batch_size)

    print("\n✅ Pipeline complete!")


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Unified TV transcript scraper + Trump debate performance analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
────────
  python scrape_all_networks.py --start-date 2025-01-01 --end-date 2026-03-25 --auto
  python scrape_all_networks.py --networks cbs cnn --start-date 2025-01-01 --auto
  python scrape_all_networks.py --update
  python scrape_all_networks.py --update --networks fox --lookback-days 14
  python scrape_all_networks.py --update --no-analysis
  python scrape_all_networks.py --analysis-only

Network keys:  cbs  cnn  fox  abc  msnbc
        """
    )
    parser.add_argument("--networks", nargs="+", choices=list(NETWORKS.keys()),
                        default=list(NETWORKS.keys()), metavar="NETWORK",
                        help="Networks to scrape (default: all)")
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default=datetime.utcnow().strftime("%Y-%m-%d"),
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--output-dir", default="all_networks_dataset")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between requests (default: 2.0)")
    parser.add_argument("--auto", action="store_true", help="Skip confirmation prompts")
    parser.add_argument("--update", action="store_true",
                        help="Fetch only content newer than last run, then analyze")
    parser.add_argument("--lookback-days", type=int, default=8,
                        help="Days of overlap before last_run in --update mode (default: 8)")
    parser.add_argument("--no-analysis", action="store_true",
                        help="Scrape only — skip the performance analysis step")
    parser.add_argument("--analysis-only", action="store_true",
                        help="Run analysis on existing data only, no new scraping")
    parser.add_argument("--batch-size", type=int, default=64,
                        help="Classifier inference batch size (reduce if OOM, default: 64)")

    args = parser.parse_args()

    for d in [args.start_date, args.end_date]:
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            print(f"Error: '{d}' is not a valid YYYY-MM-DD date.")
            sys.exit(1)

    run_analysis_flag = not args.no_analysis
    mode = ("ANALYSIS ONLY" if args.analysis_only
            else ("UPDATE" if args.update else "FULL SCRAPE"))

    print("=" * 60)
    print(f"  TV Transcript Scraper + Performance Analyzer  [{mode}]")
    print("=" * 60)
    print(f"  Networks      : {', '.join(args.networks)}")
    if not args.update and not args.analysis_only:
        print(f"  Date range    : {args.start_date} → {args.end_date}")
    elif args.update:
        print(f"  Lookback      : {args.lookback_days} days before last run")
    print(f"  Output dir    : {args.output_dir}")
    print(f"  Delay         : {args.delay}s")
    print(f"  Analysis      : {'YES — zkava01/DEBATE_Performance_Jan21' if run_analysis_flag else 'SKIPPED (--no-analysis)'}")
    print("=" * 60)

    if not args.auto and not args.update and not args.analysis_only:
        if input("\nProceed? (yes/no): ").strip().lower() != "yes":
            print("Cancelled.")
            sys.exit(0)

    try:
        run(network_keys=args.networks, start_date=args.start_date, end_date=args.end_date,
            output_dir=args.output_dir, delay=args.delay, update_mode=args.update,
            lookback_days=args.lookback_days, run_analysis_flag=run_analysis_flag,
            analysis_only=args.analysis_only, batch_size=args.batch_size)
        sys.exit(0)
    except KeyboardInterrupt:
        print("\n✗ Interrupted.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
