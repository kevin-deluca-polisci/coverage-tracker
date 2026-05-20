#!/bin/bash
#SBATCH --job-name=upd_cbs
#SBATCH --output=/nfs/roberts/scratch/pi_kd769/zsk9/logs/update_cbs_%j.log
#SBATCH --error=/nfs/roberts/scratch/pi_kd769/zsk9/logs/update_cbs_%j.err
#SBATCH --time=12:00:00
#SBATCH --mem=8G
#SBATCH --cpus-per-task=1
#SBATCH --partition=day

set -euo pipefail

START_DATE="${1:?Usage: sbatch $0 START_DATE END_DATE}"
END_DATE="${2:?Usage: sbatch $0 START_DATE END_DATE}"

module load Python/3.12.3-GCCcore-13.3.0

BASE=/nfs/roberts/scratch/pi_kd769/zsk9

echo "Updating cbs — ${START_DATE} to ${END_DATE}"
echo "Start: $(date)"

python3 $BASE/scrape_all_cbs_shows.py \
  --start-date "$START_DATE" \
  --end-date   "$END_DATE" \
  --output-dir $BASE/all_networks_dataset/cbs_latest \
  --delay 2.0 \
  --auto

echo "End: $(date)"
