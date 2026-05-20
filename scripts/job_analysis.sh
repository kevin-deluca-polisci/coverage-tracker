#!/bin/bash
#SBATCH --job-name=trump_analysis
#SBATCH --output=/nfs/roberts/scratch/pi_kd769/zsk9/logs/analysis_%j.log
#SBATCH --error=/nfs/roberts/scratch/pi_kd769/zsk9/logs/analysis_%j.err
#SBATCH --time=24:00:00
#SBATCH --mem=64G
#SBATCH --cpus-per-task=4
#SBATCH --gres=gpu:1
#SBATCH --partition=gpu_rtx6000
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=zsk9@yale.edu

# -------------------------------------------------------------------
# Trump coverage analysis pipeline.
#
# Requires $MEDIACLOUD_API_KEY to be set in the calling shell (e.g.
# export it from ~/.bashrc on the cluster). The script will refuse to
# run if it's missing.
#
# Pass --start-date and --end-date as the two args (YYYY-MM-DD).
# -------------------------------------------------------------------

set -euo pipefail

START_DATE="${1:-}"
END_DATE="${2:-}"

if [[ -z "$START_DATE" || -z "$END_DATE" ]]; then
  echo "Usage: sbatch $0 <start-date YYYY-MM-DD> <end-date YYYY-MM-DD>"
  exit 1
fi

if [[ -z "${MEDIACLOUD_API_KEY:-}" ]]; then
  echo "ERROR: \$MEDIACLOUD_API_KEY is not set. Add 'export MEDIACLOUD_API_KEY=...' to ~/.bashrc."
  exit 1
fi

BASE=/nfs/roberts/scratch/pi_kd769/zsk9
PY=$BASE/ycrc_conda/scraper/bin/python3.11

echo "Running Trump analysis for ${START_DATE} to ${END_DATE}"
echo "Start: $(date)"

# 1) Classify TV transcript chunks
$PY $BASE/run_trump_analysis-2.py \
  --input       $BASE/all_networks_dataset/all_networks_master.csv \
  --output-dir  $BASE/all_networks_dataset \
  --chunks-csv  $BASE/all_networks_dataset/trump_performance_chunks.csv

echo "TV analysis done: $(date)"

# 2) Scrape headlines
echo "Scraping Media Cloud headlines..."
$PY $BASE/scrape_mediacloud_news.py \
  --start-date  "$START_DATE" \
  --end-date    "$END_DATE" \
  --output-dir  $BASE/mediacloud_data \
  --master-csv  $BASE/mediacloud_data/trump_headlines_master.csv

# 3) Classify headlines
echo "Running headline analysis..."
$PY $BASE/run_headline_analysis.py \
  --input  $BASE/mediacloud_data/trump_headlines_master.csv \
  --output $BASE/mediacloud_data/trump_headlines_analyzed.csv

# 4) GP smoothing for the line charts (writes gp_smooth_*.csv)
echo "Running GP smoothing..."
module load R
cd $BASE && Rscript $BASE/build_loess_smooths.R

# 5) Stage canonical copies in $BASE for downstream steps
cp $BASE/mediacloud_data/trump_headlines_analyzed.csv $BASE/trump_headlines_analyzed.csv

# 6) Normalize chunk file (filter to known shows, keep show_name)
Rscript - << 'REOF'
df <- read.csv("/nfs/roberts/scratch/pi_kd769/zsk9/all_networks_dataset/trump_performance_chunks.csv",
               stringsAsFactors = FALSE)
df$show_name <- trimws(df$show_name)
write.csv(df, "/nfs/roberts/scratch/pi_kd769/zsk9/trump_performance_chunks.csv", row.names = FALSE)
cat("Wrote staged chunk file with", nrow(df), "rows\n")
REOF

# 7) Build small weekly aggregates for the static site
echo "Building weekly aggregates..."
mkdir -p $BASE/coverage-tracker-data
Rscript $BASE/coverage-tracker/scripts/build_aggregates.R \
  --chunks    $BASE/trump_performance_chunks.csv \
  --headlines $BASE/trump_headlines_analyzed.csv \
  --out-dir   $BASE/coverage-tracker-data

# 8) Also copy the smoothed CSVs to the same staging dir
cp $BASE/gp_smooth_tv.csv      $BASE/coverage-tracker-data/
cp $BASE/gp_smooth_tv_agg.csv  $BASE/coverage-tracker-data/
cp $BASE/gp_smooth_news.csv    $BASE/coverage-tracker-data/
cp $BASE/gp_smooth_news_agg.csv $BASE/coverage-tracker-data/

echo "=============================================="
echo "  Pipeline complete: $(date)"
echo "  Aggregates staged in $BASE/coverage-tracker-data/"
echo "  Next step: from your Mac, run ./update.sh in"
echo "  the coverage-tracker repo to pull and push."
echo "=============================================="
