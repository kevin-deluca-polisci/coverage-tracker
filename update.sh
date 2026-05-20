#!/usr/bin/env bash
# update.sh
#
# Local helper that pulls the latest small aggregate CSVs from the cluster,
# commits them, and pushes. Run from the root of the coverage-tracker repo
# on your Mac after the cluster's analysis job has finished.
#
# Usage:
#   ./update.sh                 # uses defaults below
#   ./update.sh --no-push       # pull + commit only, skip push
#   ./update.sh --dry-run       # show what would change, no commit
#
# Configure once: edit CLUSTER_HOST and CLUSTER_DIR if they differ.

set -euo pipefail

# ---- Config (edit these once) -----------------------------------------
CLUSTER_HOST="${CLUSTER_HOST:-mccleary.ycrc.yale.edu}"
CLUSTER_USER="${CLUSTER_USER:-$USER}"
CLUSTER_DIR="${CLUSTER_DIR:-/nfs/roberts/scratch/pi_kd769/zsk9/coverage-tracker-data}"
# -----------------------------------------------------------------------

NO_PUSH=0
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --no-push) NO_PUSH=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '1,18p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $arg"; exit 1 ;;
  esac
done

REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$REPO_ROOT"

if [[ ! -d .git ]]; then
  echo "ERROR: $REPO_ROOT is not a git repo yet. Initialize it first:"
  echo "  git init && git remote add origin git@github.com:kevinmdeluca/coverage-tracker.git"
  exit 1
fi

echo "Pulling aggregates from ${CLUSTER_USER}@${CLUSTER_HOST}:${CLUSTER_DIR}/"
SCP_OPTS=""
[[ $DRY_RUN -eq 1 ]] && SCP_OPTS="-n"  # not a real scp flag; we just skip below

if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run] would scp -p ${CLUSTER_USER}@${CLUSTER_HOST}:${CLUSTER_DIR}/*.csv data/"
else
  scp -p "${CLUSTER_USER}@${CLUSTER_HOST}:${CLUSTER_DIR}/*.csv" data/
fi

echo ""
echo "Changes in data/:"
git status --short data/

if [[ $DRY_RUN -eq 1 ]]; then
  echo "[dry-run] no commit/push performed."
  exit 0
fi

if git diff --quiet data/ && git diff --cached --quiet data/; then
  echo "No data changes. Nothing to commit."
  exit 0
fi

TODAY="$(date +%Y-%m-%d)"
git add data/
git commit -m "data: refresh aggregates ${TODAY}"

if [[ $NO_PUSH -eq 0 ]]; then
  git push
  echo "Pushed. GitHub Pages will redeploy in ~1 minute."
else
  echo "Committed locally. Skipped push (--no-push)."
fi
