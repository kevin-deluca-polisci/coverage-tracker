# Presidential Coverage Tracker

A research project from Yale political scientists measuring the tone of presidential coverage across major US broadcast networks and digital news outlets, 2025–present.

**Live site:** https://kevin-deluca-polisci.github.io/coverage-tracker/ *(once deployed)*

## What this is

We collect closed-caption transcripts from the [Internet Archive's TV News Archive](https://archive.org/details/tv) and news headlines from [Media Cloud](https://search.mediacloud.org), then classify each transcript segment and headline that mentions the president using a fine-tuned natural language inference (NLI) model. The result is a weekly net coverage score (% positive minus % negative) for each network and outlet.

Unlike dictionary-based sentiment or document-level transformer classifiers, our approach assigns tone to a specific candidate rather than to the document as a whole. A headline like "Biden struggles to contain inflation" carries a different signal than "Inflation eases under Biden," and our method distinguishes them.

## What's in this repo

```
coverage-tracker/
├── index.html              # The static interactive page (Plotly.js)
├── data/                   # Small aggregated CSVs the page reads
│   ├── gp_smooth_tv.csv          # GP-smoothed net + % negative by network/week
│   ├── gp_smooth_tv_agg.csv      # GP-smoothed aggregate across all networks
│   ├── gp_smooth_news.csv        # Same, by outlet
│   ├── gp_smooth_news_agg.csv    # Aggregate across all outlets
│   ├── weekly_tv.csv             # Per-week, per-network counts and shares
│   └── weekly_news.csv           # Per-week, per-outlet counts and shares
├── scripts/                # Pipeline that produces the data
│   ├── build_aggregates.R        # Builds the weekly_* CSVs from raw data
│   ├── job_analysis.sh           # SLURM wrapper for the analysis pipeline
│   ├── job_combine.sh            # SLURM wrapper for combining network CSVs
│   └── job_update_<network>.sh   # Per-network update jobs
├── update.sh               # Local helper: pull aggregates from cluster, commit, push
├── DEPLOY.md               # Internal workflow doc
└── README.md
```

Raw transcript chunks and analyzed headline CSVs are **not** tracked in this repo (they're large and live on the compute cluster). Only the small aggregated CSVs the public-facing page needs are committed.

## Networks and outlets covered

**Broadcast / cable:** CBS, CNN, Fox News, ABC, NBC, MSNBC/MSNow. National programs only — no local affiliates.

**Digital news:** Reuters, Fox News, CBS News, Bloomberg, CNN, ABC News, USA Today, New York Times, NBC News, Los Angeles Times, NPR.

## Methodology summary

Each broadcast transcript is split into 3-sentence windows. Windows mentioning the president are classified using the [Political DEBATE model](https://huggingface.co/mlburnham/Political_DEBATE_large_v1.0) further trained on a positive/negative/neutral performance-cue task. The same approach applies to digital headlines at the headline level.

A segment is positive if the model agrees with "the author of this text believes that [president] is performing/performed/will perform well," and negative if it agrees with the parallel "poorly" hypothesis. The weekly net score is `% positive − % negative`, averaged equally across outlets within a network.

Detailed methodology and reliability/validity assessment are in the [working paper](#) *(link forthcoming)*.

## Data access

The aggregated weekly CSVs in `data/` are released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). If you use them in published work, please cite the project.

## Contact

Kevin DeLuca · <Kevin.DeLuca@yale.edu>
Zoe Kava · <zoe.kava@yale.edu>
