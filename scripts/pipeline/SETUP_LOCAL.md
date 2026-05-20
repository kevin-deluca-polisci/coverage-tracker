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

### 4. Set your Media Cloud API key

Add to `~/.zshrc` (default shell on modern macOS) — or `~/.bashrc` if you use bash:

```bash
echo 'export MEDIACLOUD_API_KEY="paste-your-key-here"' >> ~/.zshrc
source ~/.zshrc
```

Verify with `echo $MEDIACLOUD_API_KEY`. **Do not commit the key to the repo.**

### 5. Verify the setup runs

Do a small dry test that scrapes nothing but loads PyTorch and confirms the model loads on MPS:

```bash
source .venv/bin/activate
python3 -c "import torch; print('CUDA:', torch.cuda.is_available()); print('MPS:', torch.backends.mps.is_available())"
```

You should see `MPS: True` on Apple Silicon.

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

By default it runs every 3 days at 09:00 local time **if your Mac is awake** at that moment (launchd will run it as soon as the Mac wakes up if it missed the slot). Adjust the `StartCalendarInterval` block to taste.

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
