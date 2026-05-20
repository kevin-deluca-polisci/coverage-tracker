# Deploy workflow (internal)

This is the operator's guide for keeping the live tracker fresh. Public-facing description lives in `README.md`.

## One-time setup

### On the cluster (`/nfs/roberts/scratch/pi_kd769/zsk9/`)

1. Add the Media Cloud key to your shell rc:

   ```bash
   echo 'export MEDIACLOUD_API_KEY="..."' >> ~/.bashrc
   ```

   (Rotate the key on Media Cloud first — the previous one was committed to a script and should be treated as exposed.)

2. Clone this repo on the cluster so `build_aggregates.R` is available to the analysis job:

   ```bash
   cd /nfs/roberts/scratch/pi_kd769/zsk9
   git clone git@github.com:kevinmdeluca/coverage-tracker.git
   ```

3. Make the analysis job executable:

   ```bash
   chmod +x coverage-tracker/scripts/job_analysis.sh
   ```

### On your Mac

1. Clone the repo (if you haven't already):

   ```bash
   cd ~/Library/CloudStorage/Dropbox/Claude/website
   git clone git@github.com:kevinmdeluca/coverage-tracker.git
   ```

2. Confirm `update.sh` can reach the cluster. The defaults assume `mccleary.ycrc.yale.edu`; override with environment variables if your cluster login differs:

   ```bash
   export CLUSTER_HOST=transfer.ycrc.yale.edu
   export CLUSTER_USER=kd769
   ```

3. Make sure SSH key login to the cluster is set up (no password prompts).

### On GitHub

1. Create the `coverage-tracker` repo under your account.
2. Settings → Pages → Source: **Deploy from a branch**, branch: `main`, folder: `/ (root)`. The site will publish at `https://kevinmdeluca.github.io/coverage-tracker/`.
3. (Optional) Add a custom domain via CNAME if you want it on `kevinmdeluca.com/tracker/` or similar.

## Regular update cycle (every few days)

```bash
# 1) On the cluster: submit the analysis job for a new date window
ssh mccleary.ycrc.yale.edu
cd /nfs/roberts/scratch/pi_kd769/zsk9
sbatch coverage-tracker/scripts/job_analysis.sh 2026-05-01 2026-05-15

# 2) Wait for the job to finish (check `squeue -u $USER`). When done, the
#    aggregates are staged in /nfs/roberts/scratch/pi_kd769/zsk9/coverage-tracker-data/

# 3) Back on your Mac: pull, commit, push
cd ~/Library/CloudStorage/Dropbox/Claude/website/coverage-tracker
./update.sh
```

That's it. GitHub Pages picks up the change in ~1 minute and the embedded view on kevinmdeluca.com updates automatically.

## What gets committed vs. what stays local

**Committed to the repo (in `data/`):**
- `gp_smooth_tv.csv`, `gp_smooth_tv_agg.csv`
- `gp_smooth_news.csv`, `gp_smooth_news_agg.csv`
- `weekly_tv.csv`, `weekly_news.csv`

All small (well under 100 KB total). Safe to track in git.

**Never committed** (excluded by `.gitignore`):
- `trump_performance_chunks.csv` (~459 MB)
- `trump_headlines_analyzed.csv` (~58 MB)
- `trump_headlines_master.csv`, `all_networks_master.csv`
- `.env` files, SSH keys, logs

Raw files live on the cluster (and as needed on your Mac); the public repo only has the derived aggregates.

## Troubleshooting

- **`./update.sh` fails with "permission denied"**: make it executable: `chmod +x update.sh`.
- **scp asks for a password**: SSH key login isn't set up. Add your public key to `~/.ssh/authorized_keys` on the cluster.
- **GitHub Pages shows a 404 after push**: check the Actions tab on github.com — the Pages build might still be running, or check that Pages is enabled in repo settings.
- **The page loads but charts are blank**: open browser devtools → console. Most likely a CSV column name changed; check `build_aggregates.R` output against what `index.html` expects.
