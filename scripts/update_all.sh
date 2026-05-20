#!/usr/bin/env bash
# update_all.sh
#
# Submit the full pipeline for a date window with proper SLURM dependencies:
#
#   1. Scrape each network in parallel (job_update_<network>.sh)
#   2. Combine all networks' CSVs (job_combine.sh) — waits for step 1
#   3. Classify + smooth + aggregate + topics (job_analysis.sh) — waits for step 2
#
# After step 3 finishes, the small CSVs are staged in
#   /nfs/roberts/scratch/pi_kd769/zsk9/coverage-tracker-data/
# From your Mac, run ./update.sh in the coverage-tracker repo to pull them
# down, commit, and push.
#
# Usage (on the cluster, from this scripts/ directory):
#   ./update_all.sh 2026-05-15 2026-05-29
#
# Requires $MEDIACLOUD_API_KEY to be set (the analysis job will refuse
# to run otherwise).

set -euo pipefail

START="${1:-}"
END="${2:-}"
if [[ -z "$START" || -z "$END" ]]; then
  echo "Usage: $0 START_DATE END_DATE   (e.g. $0 2026-05-15 2026-05-29)"
  exit 1
fi

# Basic date sanity check
date -d "$START" >/dev/null 2>&1 || { echo "Bad start date: $START"; exit 1; }
date -d "$END"   >/dev/null 2>&1 || { echo "Bad end date: $END"; exit 1; }

if [[ -z "${MEDIACLOUD_API_KEY:-}" ]]; then
  echo "WARNING: \$MEDIACLOUD_API_KEY isn't set in this shell."
  echo "The analysis job will fail when it reaches the Media Cloud scrape step."
  echo "Add 'export MEDIACLOUD_API_KEY=...' to ~/.bashrc and re-source it first."
  read -r -p "Continue anyway? [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] || exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

echo "Submitting pipeline for ${START} to ${END}"
echo ""

# Step 1 — per-network scrapes, in parallel
ABC=$(sbatch   --parsable job_update_abc.sh   "$START" "$END")
CBS=$(sbatch   --parsable job_update_cbs.sh   "$START" "$END")
CNN=$(sbatch   --parsable job_update_cnn.sh   "$START" "$END")
FOX=$(sbatch   --parsable job_update_fox.sh   "$START" "$END")
MSNBC=$(sbatch --parsable job_update_msnbc.sh "$START" "$END")
echo "[1/3] Scrape jobs queued:"
printf "      ABC=%s  CBS=%s  CNN=%s  FOX=%s  MSNBC=%s\n" \
       "$ABC" "$CBS" "$CNN" "$FOX" "$MSNBC"

# Step 2 — combine, waits for all 5
COMBINE=$(sbatch --parsable \
  --dependency=afterok:${ABC}:${CBS}:${CNN}:${FOX}:${MSNBC} \
  job_combine.sh)
echo "[2/3] Combine job queued: ${COMBINE} (waits for all scrapes)"

# Step 3 — analysis + aggregates + topics
ANALYSIS=$(sbatch --parsable \
  --dependency=afterok:${COMBINE} \
  job_analysis.sh "$START" "$END")
echo "[3/3] Analysis job queued: ${ANALYSIS} (waits for combine)"

echo ""
echo "All jobs submitted. Useful commands:"
echo "  squeue -u \$USER                     # see queue state"
echo "  squeue -u \$USER --start             # see estimated start times"
echo "  scancel ${ABC} ${CBS} ${CNN} ${FOX} ${MSNBC} ${COMBINE} ${ANALYSIS}   # cancel everything"
echo ""
echo "When job ${ANALYSIS} finishes, on your Mac run:"
echo "  cd ~/Library/CloudStorage/Dropbox/Claude/website/coverage-tracker"
echo "  ./update.sh"
