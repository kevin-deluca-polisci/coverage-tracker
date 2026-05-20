#!/bin/bash
#SBATCH --job-name=combine
#SBATCH --output=/nfs/roberts/scratch/pi_kd769/zsk9/logs/combine_%j.log
#SBATCH --error=/nfs/roberts/scratch/pi_kd769/zsk9/logs/combine_%j.err
#SBATCH --time=2:00:00
#SBATCH --mem=64G
#SBATCH --cpus-per-task=2
#SBATCH --partition=day

set -euo pipefail

module load Python/3.12.3-GCCcore-13.3.0

BASE=/nfs/roberts/scratch/pi_kd769/zsk9

echo "Combining all network CSVs..."
echo "Start: $(date)"

python3 $BASE/combine_networks.py \
  --dataset-dir $BASE/all_networks_dataset \
  --output      $BASE/all_networks_dataset/all_networks_master.csv

echo "End: $(date)"
