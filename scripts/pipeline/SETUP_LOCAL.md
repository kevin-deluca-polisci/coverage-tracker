# Local pipeline setup (one-time)

This guide gets the coverage-tracker pipeline running on a Mac, so you can refresh data without cluster access.

## What you're setting up

End-to-end on your laptop:

1. Scrape new TV transcripts from the Internet Archive
2. Classify them with the DEBATE NLI model (on Apple Silicon MPS, ~10–20 min per refresh)
3. Scrape new headlines from Media Cloud
4. Classify headlines
5. Build weekly aggregates and topic counts (R)
6. Commit and push to GitHub

A single command — `./scripts/update_local.sh` — runs all of this.

## Requirements

- macOS 12.3+ (for MPS support on Apple Silicon)
- Python 3.11 or 3.12
- R 4.2+ with `dplyr`, `lubridate`, `stringr`, `yaml` installed
- ~10 GB free disk (for the DEBATE model + raw scraped data over a few months)
- A Media Cloud API key (sign in at <https://search.mediacloud.org> to get one)
- A working `git` and an SSH key authorized to push to the `coverage-tracker` repo

## One-time setup

### 1. Clone the repo

```bash
cd ~/Library/CloudStorage/Dropbox/Claude/website
# If you already cloned it via GitHub Desktop you can skip this
git clone git@github.com:kevin-deluca-polisci/coverage-tracker.git
cd coverage-tracker
```

### 2. Create a Python virtual environment and install deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r scripts/pipeline/requirements.txt
```

This installs PyTorch (with MPS support on Apple Silicon by default), HuggingFace `transformers`, the Media Cloud client, pandas, etc. About 2–3 GB on disk including PyTorch.

The first time the pipeline runs, `transformers` will download the DEBATE model (`zkava01/DEBATE_Performance_Jan21`) to `~/.cache/huggingface/`. About 1 GB. Subsequent runs use the cached copy.

### 3. Install the R packages (only if missing)

```bash
Rscript -e 'pkgs <- c("dplyr","lubridate","stringr","yaml"); install.packages(pkgs[!pkgs %in% rownames(installed.packages())], repos="https://cloud.r-project.org")'
```

### 4. Set your API keys

The headlines pipeline pulls from three sources. You'll want at least Media Cloud and NYT keys (GDELT requires no key).

```bash
# Media Cloud (https://search.mediacloud.org → sign in → API key)
echo 'export MEDIACLOUD_API_KEY="paste-your-key-here"' >> ~/.zshrc
# NYT (https://developer.nytimes.com → My Apps → enable Article Search API)
echo 'export NYT_API_KEY="paste-your-nyt-key-here"' >> ~/.zshrc
source ~/.zshrc
```

Verify with `echo $MEDIACLOUD_API_KEY` and `echo $NYT_API_KEY`. **Do not commit either key to the repo.**

If you only set one, the pipeline will skip the source with the missing key and continue with whatever's available. GDELT runs regardless.

### 5. Verify the setup runs

Do a small dry test that scrapes nothing but loads PyTorch and confirms the model loads on MPS:

```bash
source .venv/bin/activate
python3 -c "import torch; print('CUDA:', torch.cuda.is_available()); print('MPS:', torch.backends.mps.is_available())"
```

You should see `MPS: True` on Apple Silicon.

### 6. (Recommended) Seed `data/raw/` from the cluster

If you've been running on the YCRC cluster and already have months of classified data there, copy it down before doing your first local refresh. Otherwise the first `--update` run will treat everything as new and try to scrape and classify from the default start date (2025-01-01), which can take many hours.

The canonical layout the pipeline expects:

```
data/raw/
├── all_networks_dataset/
│   ├── trump_performance_chunks.csv     # classified TV chunks (the big one)
│   ├── all_<network>_shows_dataset.csv  # per-network show CSVs (optional but help dedup)
│   └── scraper_state.json               # per-network last-run dates
├── mediacloud_data/
│   └── trump_headlines_master.csv       # raw scraped headlines (master)
├── trump_headlines_analyzed.csv         # classified headlines
└── all_networks_master.csv              # combined master (optional, large)
```

**Important: `trump_performance_chunks.csv` goes inside `all_networks_dataset/`, NOT directly in `data/raw/`.** The scraper reads + appends to it in place; putting it at the wrong path causes the pipeline to start from scratch and overwrite your historical data with a tiny new file.

To pull from the cluster:

```bash
CLUSTER=YOUR_USER@mccleary.ycrc.yale.edu
mkdir -p data/raw/all_networks_dataset data/raw/mediacloud_data
scp $CLUSTER:/nfs/roberts/scratch/pi_kd769/zsk9/trump_performance_chunks.csv data/raw/all_networks_dataset/
scp $CLUSTER:/nfs/roberts/scratch/pi_kd769/zsk9/trump_headlines_analyzed.csv data/raw/
scp -r $CLUSTER:/nfs/roberts/scratch/pi_kd769/zsk9/mediacloud_data data/raw/
# Optional but recommended for proper --update behavior:
scp -r $CLUSTER:/nfs/roberts/scratch/pi_kd769/zsk9/all_networks_dataset data/raw/
```

If you don't have a usable `scraper_state.json` from the cluster, hand-write one — the pipeline reads `data/raw/all_networks_dataset/scraper_state.json` with the shape:

```json
{
  "cbs":   { "last_run": "YYYY-MM-DD", "last_start": "YYYY-MM-DD", "last_end": "YYYY-MM-DD" },
  "cnn":   { "last_run": "YYYY-MM-DD", ... },
  "fox":   { ... },
  "abc":   { ... },
  "msnbc": { ... }
}
```

Set `last_run` to whatever date your cluster pipeline last covered. `--update` mode will then start from `last_run − 8 days` (the default lookback window) instead of re-scraping everything from 2025.

## Day-to-day usage

Once a refresh window is decided, from the `coverage-tracker` directory:

```bash
# Incremental update (lookback ~8 days, the recommended default)
./scripts/update_local.sh

# Or pin to an exact date window
./scripts/update_local.sh 2026-05-15 2026-05-29

# Skip the push at the end (you'll commit manually later)
./scripts/update_local.sh --no-push

# TV-only or news-only refresh
./scripts/update_local.sh --no-news
./scripts/update_local.sh --no-tv
```

The script prints what it's doing at each of the five steps, then stages updated `data/weekly_*.csv` + `data/topics_weekly.csv`, commits, and pushes. GitHub Pages picks up the change in ~1 minute.

Raw scraped data (the big chunks and headlines files) lives in `data/raw/` on your Mac and is gitignored — it stays local.

### How updates handle existing data

Every scrape/classify step in the pipeline reads the existing canonical file, classifies only new rows, then merges + dedupes + writes the combined result back. Specifically:

- TV chunks: dedupe by `(article_id, chunk_id)`
- Headlines: dedupe by `(title, date)`
- Per-network show CSVs: dedupe by `identifier`
- Headlines master: dedupe by URL

So as long as the canonical files are at the right paths, **the pipeline always appends, never overwrites historical data.** `update_local.sh` includes a safety check that refuses to rebuild aggregates if the chunks or analyzed-headlines file is missing or empty (so you don't get a "dashboard lost all its history" surprise from an upstream failure).

### Catch-up classification (built in)

Step 1 of `update_local.sh` runs `scrape_all_networks.py --analysis-only` *before* the regular `--update` scrape. This catches and classifies any shows that previously got scraped but never made it into the chunks file (which can happen if a run crashed mid-classification or the chunks file got restored from backup). The catch-up step is fast when there's nothing stranded — it just reads per-network CSVs, compares identifiers against the chunks file, and exits — but it rescues your data when there is. You don't need to run anything manually; it's part of every TV refresh.

### Headlines: three sources, one master CSV

Step 2 of `update_local.sh` collects digital headlines from three independent sources, each writing into `data/raw/mediacloud_data/trump_headlines_master.csv` with URL-based dedup:

1. **Media Cloud** (`scrape_mediacloud_news.py`) — still works well for most outlets; primary historical source.
2. **GDELT 2.0** (`scrape_gdelt.py`) — covers all 10 non-NYT outlets; no API key needed; immune to outlet-by-outlet blocks. Used to backfill and supplement Media Cloud where it has gaps (NYT mid-2025, ABC and Bloomberg early 2026).
3. **NYT Article Search API** (`scrape_nyt_api.py`) — for NYT only; cleaner and more complete than the other two for that outlet. Requires `NYT_API_KEY`.

All three run in sequence every refresh. If any one fails (network error, API outage), the others still complete and the pipeline continues — there's a per-source warning but no hard fail.

### One-time backfill for the Media Cloud gap

After the new sources are wired up, run these once to fill in historical coverage that Media Cloud missed:

```bash
# GDELT — refetch all 10 outlets for the historical window
python3 scripts/pipeline/scrape_gdelt.py \
    --start-date 2025-01-01 --end-date "$(date +%Y-%m-%d)" \
    --master-csv data/raw/mediacloud_data/trump_headlines_master.csv \
    --delay 2.0

# NYT — refetch NYT for the Media Cloud gap
python3 scripts/pipeline/scrape_nyt_api.py \
    --start-date 2025-08-01 --end-date "$(date +%Y-%m-%d)" \
    --master-csv data/raw/mediacloud_data/trump_headlines_master.csv \
    --delay 0.5

# Then classify everything new and rebuild aggregates
./scripts/update_local.sh --no-tv
```

Expected backfill runtime: ~30–60 minutes of API calls + ~30–60 minutes of classifier time depending on how many new headlines come through. After this, ongoing collection happens automatically as part of every `./scripts/update_local.sh` run.

## How long does it actually take?

Rough numbers on an M2/M3 Mac with MPS for an "every 3 days" refresh window:

| Step | Time |
|------|------|
| TV scrape (5 networks, 3 days)          | 5–10 min |
| TV classify (~5–10K new chunks)         | 10–20 min |
| Headline scrape (3 days)                | 1–2 min |
| Headline classify (~10K headlines)      | 3–5 min |
| Aggregates + topics                     | <30 sec |
| **Total**                                | **20–40 min** |

CPU-only (Intel Mac) is roughly 3–5x slower. Most of the time is the TV classifier — you can leave it running in the background.

## Scheduling (optional, "every few days")

The simplest reliable option on a Mac is `launchd`. A starter `plist` is at `scripts/pipeline/com.kevinmdeluca.coverage-tracker.plist`. To install:

```bash
cp scripts/pipeline/com.kevinmdeluca.coverage-tracker.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.kevinmdeluca.coverage-tracker.plist
```

By default it runs every 3 days at 18:00 local time **if your Mac is awake** at that moment (launchd will run it as soon as the Mac wakes up if it missed the slot). Adjust the `StartCalendarInterval` block to taste.

Logs go to `~/Library/Logs/coverage-tracker.log`.

To unschedule:

```bash
launchctl unload ~/Library/LaunchAgents/com.kevinmdeluca.coverage-tracker.plist
```

## Troubleshooting

- **`MPS: False`**: you're on Intel Mac or torch < 2.1. Pipeline still works, just slower on CPU.
- **Model download fails**: check network. Once cached, future runs work offline as long as the cache is intact.
- **Out of memory during classification**: edit the script to drop `--batch-size 64` to e.g. 16 or 8.
- **`MEDIACLOUD_API_KEY isn't set`**: open a fresh terminal after editing `~/.zshrc` so the export is sourced, or `source ~/.zshrc` in the current shell.
- **R packages missing**: install with the command in step 3 above.
- **"WARNING: a stale chunks file exists at data/raw/trump_performance_chunks.csv"**: legacy location. Move the file to `data/raw/all_networks_dataset/trump_performance_chunks.csv` (or delete it if you already have the canonical copy there).
- **"ERROR: data/raw/all_networks_dataset/trump_performance_chunks.csv is missing or empty"**: the safety check tripped. Either the scrape step failed, or the file is in the wrong place. Confirm `ls -lh data/raw/all_networks_dataset/trump_performance_chunks.csv` shows a reasonably-sized file before re-running.
- **"data/weekly_tv.csv only shows recent weeks"**: the chunks file got truncated somewhere upstream. Restore from a backup (Dropbox file history, Time Machine, cluster) — do not re-run aggregates until the canonical chunks file has the full history again.
