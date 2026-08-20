# Daily headline increments

The `Daily headline collection` GitHub Action drops one file here per day:
`YYYY-MM-DD.csv`, holding whatever Trump headlines the collectors found in the
preceding few days. Files are small — typically tens of KB.

This exists because the local pipeline can't run in CI (classification needs
PyTorch and Apple Silicon MPS) and CI can't touch the headlines master (it's
~65 MB and gitignored). So CI collects, commits an increment, and the local
pipeline merges.

**Do not edit these by hand.** `scripts/pipeline/merge_incoming.py` folds them
into `data/raw/mediacloud_data/trump_headlines_master.csv`, deduping on both
normalized URL and `(normalized title, date, outlet)` — the second key matters
because Google News gives redirect URLs that never match a canonical article
URL. Consumed files move to `archive/`, which is gitignored since git history
already preserves them.

`update_local.sh` runs the merge automatically at the start of its headline
stage, so under normal use there's nothing to do here.
