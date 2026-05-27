#!/usr/bin/env bash
# update_local.sh
#
# Runs the entire coverage-tracker pipeline locally on a Mac. End-to-end:
#
#   1. Scrape new TV transcripts from Internet Archive + classify them
#      (one Python script does both via --update mode)
#   2. Scrape new headlines from Media Cloud
#   3. Classify headlines
#   4. Build weekly aggregates (R)
#   5. Build topic counts (R)
#   6. Stage updated CSVs in data/ and offer to commit + push
#
# Designed for an Apple Silicon Mac with MPS-capable PyTorch. Falls back to
# CPU on Intel — slower but still works for small date windows.
#
# Setup (one-time): see scripts/pipeline/SETUP_LOCAL.md
#
# Usage:
#   ./update_local.sh                     # incremental update via --update mode
#   ./update_local.sh 2026-05-01 2026-05-15   # fixed date window
#   ./update_local.sh --no-push           # skip commit/push at the end
#   ./update_local.sh --no-tv             # skip TV pipeline (digital only)
#   ./update_local.sh --no-news           # skip digital pipeline (TV only)

set -euo pipefail

# ---- Resolve paths ----------------------------------------------------
REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$REPO_ROOT"

DATA_RAW="$REPO_ROOT/data/raw"          # gitignored — large local-only files
DATA_OUT="$REPO_ROOT/data"              # committed — small aggregate CSVs
PIPE="$REPO_ROOT/scripts/pipeline"
TV_OUT_DIR="$DATA_RAW/all_networks_dataset"
MC_OUT_DIR="$DATA_RAW/mediacloud_data"
# Canonical raw-data paths. These are the SINGLE locations the pipeline reads
# from / writes to — never copy these around to other paths (that's how we lost
# the chunks file before). The scraper appends, the R scripts read in place.
CHUNKS_FILE="$TV_OUT_DIR/trump_performance_chunks.csv"
HEADLINES_FILE="$DATA_RAW/trump_headlines_analyzed.csv"
HEADLINES_MASTER="$MC_OUT_DIR/trump_headlines_master.csv"
mkdir -p "$DATA_RAW" "$TV_OUT_DIR" "$MC_OUT_DIR"

# ---- Parse args -------------------------------------------------------
NO_PUSH=0
DO_TV=1
DO_NEWS=1
START=""
END=""
for arg in "$@"; do
  case "$arg" in
    --no-push) NO_PUSH=1 ;;
    --no-tv)   DO_TV=0 ;;
    --no-news) DO_NEWS=0 ;;
    --help|-h)
      sed -n '1,28p' "$0"; exit 0 ;;
    2[0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9])
      if [[ -z "$START" ]]; then START="$arg"; else END="$arg"; fi ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

# ---- Sanity: venv + env -----------------------------------------------
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  if [[ -d "$REPO_ROOT/.venv" ]]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.venv/bin/activate"
    echo "Activated venv: $VIRTUAL_ENV"
  else
    echo "WARNING: no .venv at $REPO_ROOT/.venv and no VIRTUAL_ENV active."
    echo "If Python deps aren't installed globally, this will fail."
  fi
fi

if [[ -z "${MEDIACLOUD_API_KEY:-}" ]]; then
  echo "ERROR: \$MEDIACLOUD_API_KEY isn't set in this shell."
  echo "Add 'export MEDIACLOUD_API_KEY=...' to ~/.zshrc (or ~/.bashrc) and re-source."
  exit 1
fi

PY="python3"
RS="Rscript"
command -v "$PY" >/dev/null || { echo "python3 not found"; exit 1; }
command -v "$RS" >/dev/null || { echo "Rscript not found"; exit 1; }

# ---- 1) TV: scrape + classify -----------------------------------------
# Sanity check: if a stale chunks file exists at data/raw/ (legacy location),
# warn loudly. The pipeline now reads/writes only at $CHUNKS_FILE.
LEGACY_CHUNKS="$DATA_RAW/trump_performance_chunks.csv"
if [[ -f "$LEGACY_CHUNKS" ]]; then
  echo "WARNING: a stale chunks file exists at $LEGACY_CHUNKS"
  echo "         The pipeline no longer reads from there. Move it to:"
  echo "         $CHUNKS_FILE (if it's the canonical copy) or delete it."
fi

if [[ "$DO_TV" -eq 1 ]]; then
  echo ""
  echo "[1/5] TV: catch-up + scrape + classify..."

  # ---- Catch-up analysis pass --------------------------------------
  # scrape_all_networks.py's --update mode only classifies shows it scraped
  # THIS run. That means shows scraped in a previous run but never classified
  # (e.g. interrupted, crashed, or chunks file was restored from backup) get
  # stranded in per-network CSVs forever. --analysis-only ignores `new_identifiers`
  # and classifies every show in per-network CSVs whose article_id isn't already
  # in the chunks file. Cheap when nothing's stranded; rescues the data when
  # something is.
  echo "  → catch-up analysis (any unanalyzed shows from prior scrapes)..."
  "$PY" "$PIPE/scrape_all_networks.py" \
    --output-dir "$TV_OUT_DIR" \
    --analysis-only --auto

  # ---- Fresh scrape + classify -------------------------------------
  echo "  → fresh scrape + classify..."
  if [[ -n "$START" && -n "$END" ]]; then
    "$PY" "$PIPE/scrape_all_networks.py" \
      --start-date "$START" --end-date "$END" \
      --output-dir "$TV_OUT_DIR" --auto
  else
    "$PY" "$PIPE/scrape_all_networks.py" \
      --update --output-dir "$TV_OUT_DIR" --auto
  fi
  # NOTE: $CHUNKS_FILE is the canonical chunks path. The scraper's internal
  # logic appends + dedupes by (article_id, chunk_id) on every run. We do
  # NOT copy this file anywhere else — the R scripts below read it in place.
else
  echo "[1/5] TV: skipped (--no-tv)"
fi

# ---- 2) Headlines: scrape (Media Cloud + GDELT + NYT API) -------------
# Multi-source for resilience:
#   - Media Cloud: still works fine for ~8 outlets; keep as a source
#   - GDELT 2.0:   covers all 10 non-NYT outlets, replaces MC where MC has
#                  outages (NYT, ABC, Bloomberg). No API key needed.
#   - NYT API:     for NYT only — cleaner + more complete than MC or GDELT
#                  for that outlet. Requires NYT_API_KEY env var.
# All three scripts write to $HEADLINES_MASTER with URL-based dedup, so
# duplicate articles across sources are collapsed automatically.

if [[ "$DO_NEWS" -eq 1 ]]; then
  echo ""
  echo "[2/5] Headlines: scrape (Media Cloud + GDELT + NYT API)..."
  # If no explicit dates, default to last 14 days
  if [[ -z "$START" || -z "$END" ]]; then
    END="$(date +%Y-%m-%d)"
    START="$(date -v-14d +%Y-%m-%d 2>/dev/null || date -d "14 days ago" +%Y-%m-%d)"
  fi

  # 2a — Media Cloud (still useful for outlets where it works)
  echo "  → Media Cloud..."
  "$PY" "$PIPE/scrape_mediacloud_news.py" \
    --start-date "$START" --end-date "$END" \
    --output-dir "$MC_OUT_DIR" \
    --master-csv "$HEADLINES_MASTER" || \
    echo "    [warning: Media Cloud scrape failed, continuing with other sources]"

  # 2b — GDELT for the 10 non-NYT outlets
  echo "  → GDELT (10 outlets)..."
  "$PY" "$PIPE/scrape_gdelt.py" \
    --start-date "$START" --end-date "$END" \
    --master-csv "$HEADLINES_MASTER" \
    --delay 8.0 || \
    echo "    [warning: GDELT scrape failed, continuing]"

  # 2c — NYT Article Search API
  if [[ -n "${NYT_API_KEY:-}" ]]; then
    echo "  → NYT Article Search API..."
    "$PY" "$PIPE/scrape_nyt_api.py" \
      --start-date "$START" --end-date "$END" \
      --master-csv "$HEADLINES_MASTER" \
      --delay 0.5 || \
      echo "    [warning: NYT API scrape failed, continuing]"
  else
    echo "  → NYT API: SKIPPED (NYT_API_KEY env var not set)"
  fi

  # ---- 3) Headlines: classify -----------------------------------------
  echo ""
  echo "[3/5] Headlines: classify..."
  # run_headline_analysis.py reads its own --output file to dedupe by
  # (title, date) and appends only new rows. Safe to point at canonical path.
  "$PY" "$PIPE/run_headline_analysis.py" \
    --input  "$HEADLINES_MASTER" \
    --output "$HEADLINES_FILE"
else
  echo "[2/5] Headlines: skipped (--no-news)"
  echo "[3/5] Headlines: skipped (--no-news)"
fi

# ---- Safety check: refuse to proceed if canonical files are missing ---
if [[ ! -s "$CHUNKS_FILE" ]]; then
  echo "ERROR: $CHUNKS_FILE is missing or empty."
  echo "       Aborting before aggregates would overwrite live data with nothing."
  exit 1
fi
if [[ ! -s "$HEADLINES_FILE" ]]; then
  echo "ERROR: $HEADLINES_FILE is missing or empty."
  echo "       Aborting before aggregates would overwrite live data with nothing."
  exit 1
fi

# ---- 4) Build weekly aggregates ---------------------------------------
echo ""
echo "[4/5] Building weekly aggregates..."
"$RS" "$REPO_ROOT/scripts/build_aggregates.R" \
  --chunks    "$CHUNKS_FILE" \
  --headlines "$HEADLINES_FILE" \
  --out-dir   "$DATA_OUT"

# ---- 5) Build topic counts --------------------------------------------
echo ""
echo "[5/5] Building topic counts..."
"$RS" "$REPO_ROOT/scripts/build_topics.R" \
  --chunks    "$CHUNKS_FILE" \
  --headlines "$HEADLINES_FILE" \
  --topics    "$REPO_ROOT/scripts/topics.yaml" \
  --out-dir   "$DATA_OUT"

# ---- Commit + push ----------------------------------------------------
echo ""
echo "Changes in data/:"
git -C "$REPO_ROOT" status --short data/ || true

if [[ "$NO_PUSH" -eq 1 ]]; then
  echo "Done. Skipping commit/push (--no-push)."
  exit 0
fi

if git -C "$REPO_ROOT" diff --quiet data/ && git -C "$REPO_ROOT" diff --cached --quiet data/; then
  echo "No data changes. Nothing to commit."
  exit 0
fi

TODAY="$(date +%Y-%m-%d)"
git -C "$REPO_ROOT" add data/
git -C "$REPO_ROOT" commit -m "data: local refresh ${TODAY}"
git -C "$REPO_ROOT" push
echo ""
echo "Pushed. GitHub Pages will redeploy in ~1 minute."
