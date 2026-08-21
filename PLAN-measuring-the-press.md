# Measuring the Press — rebuild plan for the Coverage Tracker

Working document. Drafted against §C of the AI & Search Visibility brief.
Status: proposal, nothing implemented yet.

---

## What §C actually changes

The current page is a dashboard: it shows numbers. The memo asks for something
different — the canonical public home of a **named measure**, with the argument
for that measure on the page itself, a citable methodology, and a historical
series that makes the current president one point in a long record rather than
the whole story.

That reframing implies work in three categories, and they are not equally
urgent. Renaming and layout are cheap and reversible. Two structural problems
are neither, and both should be settled before anything cosmetic happens,
because changing the headline number after people cite it is expensive.

---

## Problem 1 — The iframe defeats the entire visibility goal

Not in the memo, and it may be the highest-leverage item in it.

`kevinmdeluca.com/media-tracker/` contains **five words of body content and
eight iframe references.** The whole tracker is served from
`kevin-deluca-polisci.github.io` inside an `<iframe>`.

Crawlers and AI retrievers generally do not attribute iframed content to the
parent page. So the situation right now is:

- the URL that looks canonical is an empty shell
- the URL with the actual content sits on a bare project-page domain carrying
  no authorial identity
- neither is a good answer to "what does Kevin DeLuca's tracker say"

This also cuts against Session 1's name-disambiguation priority. Content on
`kevin-deluca-polisci.github.io` does not obviously accrue to Kevin DeLuca at
Yale, which is precisely the association the brief is trying to strengthen
against the Utah communication scholar.

**Recommendation: serve the tracker from the main domain.** The dashboard is a
single self-contained `index.html` plus ~150 KB of CSVs, so this is a small
migration, not a rewrite. The data pipeline and raw data stay in the
`coverage-tracker` repo — that repo becomes the data home, which matches how
the brief already treats ANED ("the GitHub repo is the data").

---

## Problem 2 — The aggregation rule (the memo's "one true blocker")

The memo is right that this needs settling first. Having now measured it, the
picture is more specific than the memo assumes, and the most serious issue is
not the one it leads with.

### 2a. Equal weighting across wildly unequal outlets — the urgent one

Median Trump headlines per week, current corpus:

| Outlet | Median/wk | Mean PCI |
|---|---:|---:|
| Politico | **5** | **−50.0** |
| Washington Post | 17 | — |
| NPR | 143 | −19.9 |
| New York Times | 187 | −21.1 |
| NBC News | 192 | −10.4 |
| Los Angeles Times | 194 | −19.5 |
| USA Today | 198 | −15.1 |
| ABC News | 267 | −11.3 |
| CNN | 338 | −19.5 |
| CBS News | 356 | −8.2 |
| Bloomberg | 359 | −15.9 |
| Fox News | 399 | −10.8 |
| Reuters | **834** | −6.0 |

The current headline is an unweighted mean of outlet-level percentages. So
**Politico's ~5 articles a week carry the same weight as Reuters' ~834**, and
because Politico's small sample sits at −50, it drags the headline down by
roughly 2–3 points on its own. That is indefensible on its face and is the
first thing a critic will find.

This is separable from the weighted-vs-unweighted question the memo poses.
Even if the answer is "unweighted, we want the average *outlet*," an outlet
needs a minimum volume to be a meaningful observation at all.

### 2b. Corpus composition drift — real, and worse than it looks

The memo's concern is live, not hypothetical. Outlets present in the weekly
average over the last quarter:

```
2026-06-08  n=13   —
2026-06-22  n=12   missing: Reuters
2026-06-29  n= 8   missing: ABC, Bloomberg, Politico, Reuters, WaPo
2026-07-06  n= 9   missing: ABC, Bloomberg, Reuters, WaPo
2026-07-20  n=10   missing: ABC, Reuters, WaPo
2026-08-10  n=13   —
```

The count swings between 8 and 13 week to week. Since outlet means span 44
points, that composition churn moves the headline on its own. Reuters
disappearing alone — it was the *least* negative outlet — biases the average
**−1.0 points on average, up to −2.6 in a single week**, with no change in
press behaviour whatsoever.

### 2c. What the memo's proposed fix actually buys — measured

I implemented the two-way fixed-effects indexing the memo suggests
(`PCI_ot = α_outlet + δ_period + ε`, report δ) and compared it to the current
raw mean over 86 weeks:

| | Result |
|---|---|
| Mean absolute difference | 0.53 points |
| **Max absolute difference** | **3.18 points** |
| Correlation with raw mean | 0.975 |
| Reduction in week-to-week jitter | ~1% |

Read this honestly, in both directions.

**It matters less than the memo implies for de-noising.** Most week-to-week
movement is genuine news-cycle variation, not composition. FE indexing removes
about 1% of the jitter. It is not a volatility fix.

**It matters exactly where you would want it to.** Every one of the largest
divergences is a week with outlets missing — the top six are all n=9–11, all in
the July dropout window, diverging by 1.6–3.2 points. That is the artifact the
memo is worried about, isolated and confirmed.

**And it will matter far more for the back-series**, where composition varies
across decades rather than across weeks. The 2025–26 window is the *easy* case
and it still produces 3-point artifacts.

**Recommendation:** adopt FE indexing as the headline, but adopt it for
correctness rather than for smoothing, and say so. Pair it with a minimum-volume
threshold (2a), which is the larger effect in the current data. Report the raw
unweighted mean as an alternate series so the change is auditable.

### 2d. Credibility weighting

Agree with the memo entirely — if it exists at all it is a toggle, never the
default. The non-partisanship position depends on the headline number requiring
no judgment calls about outlets. Recommend deferring it past v1 rather than
building a toggle nobody has asked for yet.

---

## Problem 3 — No back-series

Currently 2024-12-30 → present, Trump only. The memo is right that a
one-president tracker reads as a bias meter, and that the back-series is
simultaneously the moat and the non-partisanship defence.

This is the largest single piece of work in the plan and the one most
constrained by things outside our control (corpus availability, and the
radio-era problem the memo flags). It should be scoped against what the text
corpora actually support, and the honest boundary stated on the methodology
page rather than hidden.

Sequencing note: the aggregation rule (Problem 2) should be settled *before*
the back-series is built, because the back-series is where composition drift
becomes severe, and rebuilding it under a changed rule is wasted work.

---

## Proposed information architecture

The brief asks to treat this as its own site. Proposed structure, all on
`kevinmdeluca.com`:

```
/measuring-the-press/                  series + project index, editorial rules
/measuring-the-press/tracker/          THE TRACKER — canonical, always current
/measuring-the-press/tracker/method/   PCI construction, validation, limits, citation
/measuring-the-press/tracker/corpus/   what's in the corpus, sources & volume over time
/measuring-the-press/posts/<slug>/     the recurring series
```

Rationale for nesting the tracker under the series rather than the reverse: the
memo names *Measuring the Press* as "the umbrella under which everything else
sits." The tracker is the flagship product within it; future trackers (if the
measure extends past presidents) slot in alongside rather than requiring
reorganisation.

Three things this buys that the current single page cannot:

1. **The methodology page becomes a citable entity.** A named measure with a
   stable methodology URL and citation string is infrastructure other people
   use. A methodology *section* at the bottom of a dashboard is not citable in
   the same way.
2. **The corpus page preempts the obvious objection** and, as the memo notes,
   is a good post in its own right.
3. **Clean HTML for retrieval.** Methodology and corpus pages are prose, which
   is what retrievers actually read. The dashboard is JS-rendered and largely
   invisible to them — meaning the *arguments* for the measure need to live on
   pages that are not the dashboard.

---

## Renaming

| Current | Becomes |
|---|---|
| "Net Coverage Score" | **Performance Cues Index (PCI)** |
| "Net score, last 7 days" | "PCI, last 7 days" |
| Page title "Presidential Coverage Tracker" | unchanged ✓ |
| — | "Measuring the Press" as section/umbrella |

Mechanical, but do it at the same time as the aggregation change, since both
alter what the headline number means. Renaming and redefining in one move is
one explanation to write; doing them separately is two.

Per the memo: the measure does not carry a personal name.

---

## v1 scope

The memo says v1 should track fewer things than is tempting: one headline
number plus a small number of subcomponents. Current page has four KPI cards
and four charts of equal visual weight, which reads as four co-equal numbers.

Proposed: one large PCI figure with its trend, then subcomponents (% negative,
% positive, volume) at clearly secondary weight. "Biggest mover" is interesting
but is not a subcomponent of the measure — demote or drop.

The topics section stays a placeholder; it is not v1.

---

## Decisions taken

| Decision | Choice |
|---|---|
| Where the site lives | **Move onto kevinmdeluca.com** as Hugo pages; `coverage-tracker` repo becomes data + pipeline only |
| Headline aggregation | **FE-indexed, unweighted across outlets**; raw mean published as an alternate series |
| Volume floor | **30 headlines/week**; thin outlets excluded from the headline but still charted individually |
| v1 scope | **Trump-only**, shipping with the method and corpus pages; back-series follows |

Still open, non-blocking: credibility toggle (recommend: no), audience-weighted
alternate series (needs the circulation merge built first), whether historical
posts get their own sub-series, one repo per analysis or one for all,
newsletter mirroring.

---

## Measured: what the decided rule does to the published number

Both changes were implemented against the live 86-week series and decomposed,
because the two do very different amounts of work.

| Change | Mean abs. shift | Max abs. shift |
|---|---:|---:|
| FE indexing (on the eligible set) | 0.34 pts | 1.87 pts |
| **Volume floor (≥30/wk)** | **0.77 pts** | **15.42 pts** |

**The floor is the load-bearing fix, and it is correcting something egregious.**
Because PCI is a percentage, a one-article outlet-week yields ±100. Of the 37
outlet-weeks below 30 headlines, **7 sit at exactly ±100** and 8 exceed ±50. Of
the 892 outlet-weeks above the floor, **none** exceed ±50.

The clearest case, week of 2026-05-25:

```
Bloomberg          1 article    PCI = -100.00   <- excluded by floor
Politico           3 articles   PCI = -100.00   <- excluded by floor
Reuters          507 articles   PCI =   -4.34
CBS News         301 articles   PCI =   -2.66
... 7 more substantial outlets
```

Under the current unweighted mean those four articles carry the same weight as
Reuters' 507, and they dragged the published headline from −15.19 to **−30.61**.
The number on the site that week was roughly twice as negative as the corpus
supports, because of four articles.

Two properties make this a safe change to make publicly:

- **It removes noise, not level.** Signed mean shift is +0.11 points — the
  effect has no systematic direction. It cuts both ways (one week moves −9.4,
  another +15.4).
- **It substantially stabilises the series.** Week-to-week volatility drops
  from SD 4.17 to 2.85, a **32% reduction in jitter**, without touching the
  average level.

FE indexing on top is a modest, well-behaved correction that does what the memo
says it should: its largest divergences are precisely the weeks with outlets
missing. Adopt it for correctness, not for smoothing, and say so on the
methodology page.

**Launch communication note:** the level barely moves (−13.83 → −13.69 overall),
so this is defensible as "we fixed a defect," not "we changed the measure."
Worth publishing the old and new series together once, in the methodology page
or a launch post, so the change is on the record before anyone cites either.

---

## Suggested sequencing

1. ~~Settle the blocking decisions~~ — done, see table above
2. **Implement the aggregation change + rename to PCI together.** One change to
   `build_aggregates.R`: apply the 30/wk floor, fit two-way FE, emit the period
   effect as the headline plus the raw mean as an alternate series. Rename in
   `index.html` at the same time.
3. **Migrate to kevinmdeluca.com** and split into the four-page structure
4. **Write the methodology and corpus pages** — these are the retrievable
   assets; the dashboard itself is JS and largely invisible to retrievers
5. Back-series
6. Series infrastructure (archetypes, RSS, editorial about-page)

Steps 2–4 are the launchable unit. The back-series can follow without
invalidating anything, now that the aggregation rule is fixed.

### Note on step 2 and the classifier

Nothing here requires re-running the DEBATE model. The floor and the FE index
both operate on already-classified weekly aggregates, so this is a change to
`build_aggregates.R` only — cheap to implement, cheap to revert, and testable
against the existing series before it goes live.
