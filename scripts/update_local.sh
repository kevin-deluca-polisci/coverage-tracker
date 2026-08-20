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

# ---- Status tracking + final status line ------------------------------
# Track which stage we're currently in so an interrupted/failed run leaves a
# clear marker in the log instead of just trailing off into silence. Last
# audit found that several recent runs simply stopped mid-stage with no way
# to tell what happened.
CURRENT_STAGE="starting"
PIPELINE_START_TS=$(date +%s)

_print_status() {
  local exit_code=$?
  local elapsed=$(( $(date +%s) - PIPELINE_START_TS ))
  local mins=$(( elapsed / 60 ))
  local secs=$(( elapsed % 60 ))
  echo ""
  echo "============================================================"
  if [[ "$exit_code" -eq 0 ]]; then
    echo "✓ PIPELINE COMPLETE — finished cleanly in ${mins}m ${secs}s"
  else
    echo "✗ PIPELINE FAILED — exit ${exit_code} at stage: ${CURRENT_STAGE}"
    echo "                    after ${mins}m ${secs}s"
  fi
  echo "============================================================"
}
trap _print_status EXIT

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
  CURRENT_STAGE="1/5 TV catch-up + scrape + classify"
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
  CURRENT_STAGE="2/5 Headlines scrape (MC + GDELT + RSS + GNews + NYT)"
  echo "[2/5] Headlines: scrape (Media Cloud + GDELT + RSS + Google News + NYT)..."
  # If no explicit dates, default to last 14 days
  if [[ -z "$START" || -z "$END" ]]; then
    END="$(date +%Y-%m-%d)"
    START="$(date -v-14d +%Y-%m-%d 2>/dev/null || date -d "14 days ago" +%Y-%m-%d)"
  fi

  # 2·0 — Merge any daily increments collected by the GitHub Action.
  # The Action runs the headline scrapers daily and commits small dated CSVs
  # to data/incoming/. This matters most for the RSS-backed outlets, whose
  # feeds only expose ~25 recent items — anything published between local
  # runs is otherwise lost for good (ABC News and Washington Post both show
  # a literal zero for July 2026 because of exactly this).
  # Pull first so we pick up whatever CI has committed since the last run.
  INCOMING_DIR="$DATA_RAW/incoming"
  if [[ -d "$REPO_ROOT/data/incoming" ]]; then
    echo "  → Merging daily increments from CI..."
    git -C "$REPO_ROOT" pull --rebase --autostash 2>&1 | sed 's/^/      /' || \
      echo "      [warning: git pull failed, merging whatever is already local]"
    "$PY" "$PIPE/merge_incoming.py" \
      --incoming-dir "$REPO_ROOT/data/incoming" \
      --master-csv "$HEADLINES_MASTER" 2>&1 | sed 's/^/      /' || \
      echo "      [warning: merge_incoming failed, continuing with live scrape]"
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

  # 2c — RSS feeds for ABC News, Bloomberg, Politico, Washington Post
  # These outlets are under-covered or missing from MC/GDELT:
  #   - ABC News: MC dropped them in March 2026, GDELT doesn't index them
  #   - Bloomberg: MC failing since April 2026
  #   - Politico: MC only captured 2 articles total over the dashboard's life
  #   - Washington Post: never made it to the dashboard despite being in MC config
  # RSS gets the recent items per feed; for high-volume outlets some articles
  # may roll off RSS between runs. Documented in the methodology.
  RSS_CONFIG="$PIPE/rss_feeds.yaml"
  if [[ -f "$RSS_CONFIG" ]]; then
    echo "  → RSS (ABC News, Bloomberg, Politico, Washington Post)..."
    "$PY" "$PIPE/scrape_rss.py" \
      --config "$RSS_CONFIG" \
      --master-csv "$HEADLINES_MASTER" \
      --delay 2.0 || \
      echo "    [warning: RSS scrape failed, continuing]"
  fi

  # 2d — Google News RSS for outlets Media Cloud has dropped
  # Media Cloud's per-outlet indexing keeps decaying without warning: NYT
  # (Aug 2025), ABC News (Mar 2026), Bloomberg (Apr 2026), and Reuters
  # (Jun 2026 — our largest outlet). Google News search feeds give a uniform
  # fallback that doesn't depend on one aggregator staying healthy.
  GNEWS_CONFIG="$PIPE/gnews_feeds.yaml"
  if [[ -f "$GNEWS_CONFIG" ]]; then
    echo "  → Google News RSS (Reuters, ABC, WaPo, Politico, Bloomberg)..."
    "$PY" "$PIPE/scrape_gnews.py" \
      --config "$GNEWS_CONFIG" \
      --master-csv "$HEADLINES_MASTER" \
      --delay 3.0 || \
      echo "    [warning: Google News scrape failed, continuing]"
  fi

  # 2e — NYT.
  # NYT is collected in TWO passes, because the Archive API can only serve
  # COMPLETED months. Its per-month JSON files live in Google Cloud Storage
  # and aren't generated until a month ends — asking for the in-progress
  # month returns 403 AccessDenied ("object ... may not exist"). So:
  #   - Archive API    → previous month. One call, high recall (~700/mo).
  #   - Article Search → current month-to-date. Paginated, closes the gap.
  # Together these give continuous NYT coverage with no monthly blind spot.
  if [[ -n "${NYT_API_KEY:-}" ]]; then
    PREV_MONTH="$(date -v-1m +%Y-%m 2>/dev/null || date -d '1 month ago' +%Y-%m)"
    MONTH_START="$(date +%Y-%m-01)"
    TODAY_STR="$(date +%Y-%m-%d)"

    echo "  → NYT Archive API (${PREV_MONTH} — completed month)..."
    "$PY" "$PIPE/scrape_nyt_archive.py" \
      --start-month "$PREV_MONTH" --end-month "$PREV_MONTH" \
      --master-csv "$HEADLINES_MASTER" \
      --delay 1.0 || \
      echo "    [warning: NYT Archive scrape failed, continuing]"

    echo "  → NYT Article Search (${MONTH_START} → ${TODAY_STR} — current month)..."
    "$PY" "$PIPE/scrape_nyt_api.py" \
      --start-date "$MONTH_START" --end-date "$TODAY_STR" \
      --master-csv "$HEADLINES_MASTER" \
      --delay 2.0 || \
      echo "    [warning: NYT Article Search scrape failed, continuing]"
  else
    echo "  → NYT: SKIPPED (NYT_API_KEY env var not set)"
  fi

  # ---- 3) Headlines: classify -----------------------------------------
  echo ""
  CURRENT_STAGE="3/5 Headlines classify"
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
CURRENT_STAGE="4/5 Building weekly aggregates"
echo "[4/5] Building weekly aggregates..."
"$RS" "$REPO_ROOT/scripts/build_aggregates.R" \
  --chunks    "$CHUNKS_FILE" \
  --headlines "$HEADLINES_FILE" \
  --out-dir   "$DATA_OUT"

# ---- 5) Build topic counts --------------------------------------------
echo ""
CURRENT_STAGE="5/5 Building topics"
echo "[5/5] Building topic counts..."
"$RS" "$REPO_ROOT/scripts/build_topics.R" \
  --chunks    "$CHUNKS_FILE" \
  --headlines "$HEADLINES_FILE" \
  --topics    "$REPO_ROOT/scripts/topics.yaml" \
  --out-dir   "$DATA_OUT"

# ---- Commit + push ----------------------------------------------------
CURRENT_STAGE="commit + push"
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
