#!/usr/bin/env python3
"""check_coverage.py

Fails the workflow when an outlet has gone dark, so that GitHub sends an email.

Why this exists: every collection step in the daily workflow is
`continue-on-error: true`, and the only hard gate is "did we get more than zero
rows overall". A source can therefore stop returning one outlet entirely and
the run still goes green, still commits, and simply carries fewer rows. That is
not hypothetical — it is how Media Cloud coverage of Reuters, ABC News,
Bloomberg and Politico degraded over twelve months without anything surfacing.
GitHub only emails on workflow *failure*, so the check has to actually fail.

It runs against the RAW collected batch, before dedup, deliberately:

  * The deduped increment is a poor health signal. It contains only what was
    new since the last increment, so a healthy outlet legitimately shows 2 or 3
    rows on a quiet day, and the count depends on how recently the increments
    were archived.
  * The raw batch is the whole lookback window (3 days) from every source. An
    outlet absent from that is absent from the internet as far as this pipeline
    can tell, which is exactly the condition worth an email.

It also cannot consult increment history, because data/incoming/archive/ is
gitignored and prior increments are removed from the working tree once a local
merge consumes them. Stateless is not a compromise here; it is the only option
that works in CI.

Exit codes:
  0  every expected outlet present
  1  one or more outlets missing  (fails the job -> GitHub emails)

Usage:
  python3 check_coverage.py --collected /tmp/collected.csv [--min-rows 1]
"""

import argparse
import os
import sys

import pandas as pd

# Outlets the pipeline is expected to return. Keep in sync with the outlet
# list in build_aggregates.R and the About section of index.html.
EXPECTED = [
    "Reuters", "Fox News", "CBS News", "Bloomberg", "CNN", "ABC News",
    "USA Today", "New York Times", "NBC News", "Los Angeles Times", "NPR",
    "Washington Post", "Politico",
]


def main():
    p = argparse.ArgumentParser(description="Alert when an outlet stops returning results")
    p.add_argument("--collected", required=True,
                   help="Raw collected CSV for this run (pre-dedup)")
    p.add_argument("--min-rows", type=int, default=1,
                   help="Rows an outlet needs in the window to count as alive "
                        "(default 1 — absence, not thinness, is the signal)")
    args = p.parse_args()

    if not os.path.exists(args.collected):
        # Every source failed. The workflow's own row check already handles
        # this, and failing twice for one cause makes the email ambiguous.
        print(f"No collected file at {args.collected} — nothing to check.")
        return 0

    df = pd.read_csv(args.collected, dtype=str, low_memory=False)
    if df.empty or "outlet" not in df.columns:
        print("Collected file is empty or has no outlet column — nothing to check.")
        return 0

    counts = df["outlet"].value_counts()
    total = len(df)

    print(f"Raw collected batch: {total:,} rows across {counts.size} outlets\n")
    width = max(len(o) for o in EXPECTED) + 2
    missing = []
    for outlet in EXPECTED:
        n = int(counts.get(outlet, 0))
        if n < args.min_rows:
            missing.append(outlet)
            print(f"  {outlet:<{width}} {n:>6}   <-- MISSING")
        else:
            print(f"  {outlet:<{width}} {n:>6}")

    unexpected = [o for o in counts.index if o not in EXPECTED]
    if unexpected:
        print("\n  Outlets returned but not in EXPECTED (harmless, but check "
              "the list is current):")
        for o in unexpected:
            print(f"    {o} ({int(counts[o])})")

    if not missing:
        print(f"\nAll {len(EXPECTED)} outlets returned results.")
        return 0

    print(f"\n{'='*66}")
    print(f"{len(missing)} outlet(s) returned nothing over the whole collection window:")
    for o in missing:
        print(f"  - {o}")
    print("""
The increment was still committed, so no data has been lost — this is an
alert, not a failure to collect.

Likely causes, cheapest to check first:
  * A source dropped the outlet. Media Cloud has done this repeatedly. The
    fix is usually to add or adjust an entry in gnews_feeds.yaml so Google
    News RSS covers it instead.
  * A publisher changed its RSS path (Washington Post, Politico and ABC News
    depend on RSS or Google News rather than Media Cloud).
  * A transient upstream outage. If the next run is clean, ignore this.

Check the per-source logs in the steps above to see which one stopped
returning the outlet.""")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(main())
