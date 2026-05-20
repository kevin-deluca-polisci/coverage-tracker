# Presidential Coverage Tracker

A research project from Yale political scientists measuring the tone of presidential coverage across major US broadcast and cable news networks and digital news outlets, 2025–present.

- **Live dashboard:** <https://kevin-deluca-polisci.github.io/coverage-tracker/>
- **Embedded view:** <https://kevinmdeluca.com/media-tracker/>
- **Paper repository:** <https://github.com/kevin-deluca-polisci/presidential_headlines>
- **Working paper:** forthcoming

## What this is

We collect closed-caption transcripts from the [Internet Archive's TV News Archive](https://archive.org/details/tv) and news headlines from [Media Cloud](https://search.mediacloud.org), then classify each transcript segment and headline that mentions the president using a fine-tuned natural language inference (NLI) model. The result is a weekly net coverage score (% positive minus % negative) for each network and outlet, plus topic-level coverage volume.

Unlike dictionary-based sentiment or document-level transformer classifiers, our approach assigns tone to a specific candidate rather than to the document as a whole. A headline like "Biden struggles to contain inflation" carries a different signal than "Inflation eases under Biden," and our method distinguishes them.

## What's in this repo

```
coverage-tracker/
├── index.html                    # The static interactive dashboard (Plotly.js)
├── data/                         # Small aggregated CSVs the page reads
│   ├── weekly_tv.csv                 # Per-week, per-network: counts, %pos/%neg, LOESS smooth + 95% CI
│   ├── weekly_tv_agg.csv             # Same, aggregated across all networks
│   ├── weekly_news.csv               # Per-week, per-outlet, same schema
│   ├── weekly_news_agg.csv           # Aggregated across all outlets
│   └── topics_weekly.csv             # Weekly counts and shares per topic, across TV and headlines
├── scripts/
│   ├── build_aggregates.R            # Builds weekly_* CSVs (LOESS span = 0.5) from raw data
│   ├── build_topics.R                # Builds topics_weekly.csv from raw data + topics.yaml
│   ├── topics.yaml                   # Topic definitions (name + keyword list each)
│   ├── update_local.sh               # Single-command local pipeline driver (Mac)
│   ├── update_all.sh                 # Single-command cluster pipeline driver (SLURM)
│   ├── job_analysis.sh               # SLURM: classify + smooth + aggregate + topics
│   ├── job_combine.sh                # SLURM: combine per-network CSVs into master
│   ├── job_update_<network>.sh       # SLURM: scrape one network for a date window
│   └── pipeline/                     # Python pipeline used by update_local.sh
│       ├── scrape_all_networks.py    # Scrapes Internet Archive + runs classifier
│       ├── scrape_mediacloud_news.py # Scrapes headlines from Media Cloud
│       ├── run_trump_analysis.py     # Classifies TV chunks (DEBATE NLI model)
│       ├── run_headline_analysis.py  # Classifies headlines (DEBATE NLI model)
│       ├── combine_networks.py       # Combines per-network show CSVs into master
│       ├── requirements.txt          # Python dependencies for local runs
│       ├── com.kevinmdeluca.coverage-tracker.plist  # launchd job for automatic updates
│       └── SETUP_LOCAL.md            # One-time setup guide for the local Mac pipeline
├── update.sh                     # Local helper: pull aggregates from cluster, commit, push
├── DEPLOY.md                     # Operator's guide (cluster + local workflows)
└── README.md
```

Raw transcript chunks and analyzed headline CSVs are **not** tracked in this repo (they're large; see `.gitignore`). They live in `data/raw/` on your local machine and/or on the compute cluster. Only the small aggregated CSVs the public-facing page needs are committed.

## Networks and outlets covered

**Broadcast and cable:** CBS, CNN, Fox News, ABC, NBC, MSNBC/MSNow. National programs only — no local affiliates.

**Digital news:** Reuters, Fox News, CBS News, Bloomberg, CNN, ABC News, USA Today, New York Times, NBC News, Los Angeles Times, NPR.

## Methodology summary

Each broadcast transcript is split into 3-sentence windows. Windows mentioning the president are classified using the [Political DEBATE model](https://huggingface.co/mlburnham/Political_DEBATE_large_v1.0) by Michael Burnham, further trained on a positive/negative/neutral performance-cue task. The same approach applies to digital headlines at the headline level.

A segment is positive if the model agrees with "the author of this text believes that [president] is performing/performed/will perform well," and negative if it agrees with the parallel "poorly" hypothesis. The weekly net score is `% positive − % negative`. The dashboard's over-time charts show the raw weekly series in light overlay, with a LOESS smooth (span = 0.5) and 95% confidence band on top.

Full training procedure, validation, and reliability tests live in the [paper repository](https://github.com/kevin-deluca-polisci/presidential_headlines).

## Topics

Topic coverage is computed by case-insensitive whole-word keyword matching against the same raw chunks and headlines. Topic definitions live in [`scripts/topics.yaml`](scripts/topics.yaml). To add a new topic, edit the YAML and rerun `build_topics.R` (or just trigger the next regular refresh — it runs as the last pipeline step).

Initial seed topics: economy, immigration, foreign policy, tariffs, healthcare, crime.

## Data access

The aggregated weekly CSVs in `data/` are released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). If you use them in published work, please cite the project.

## Contact

Kevin DeLuca · <kevin.deluca@yale.edu>
Zoe Kava · <zoe.kava@yale.edu>
