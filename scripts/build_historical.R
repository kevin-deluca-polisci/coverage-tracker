#!/usr/bin/env Rscript
# build_historical.R
#
# Builds the historical newspaper series for the Coverage Tracker's Historical
# tab: the Performance Cues Index for every president since Truman, computed
# from newspaper headlines and aligned to time-in-term so administrations can
# be compared at the same point in their presidency.
#
# This is a THIRD corpus, deliberately kept separate from the broadcast and
# digital series that build_aggregates.R produces. Newspapers, television and
# digital outlets are different media with different production logics, and
# averaging across them would manufacture exactly the composition artifacts the
# index is built to avoid. Same classifier, same hypotheses, different corpus.
#
# Two things differ from build_aggregates.R, both forced by the corpus:
#
#   * MONTHLY, not weekly. Mid-century papers ran a few headlines a week about
#     a president where Reuters now runs hundreds. At a 30-per-week floor only
#     0.3-21% of historical outlet-weeks survive; monthly at a floor of 5 keeps
#     ~85% of observations and gives every president complete coverage.
#
#   * Floor of 5 per outlet-month rather than 30. Same purpose — stop a
#     near-empty cell scoring +/-100 and swinging the aggregate — recalibrated
#     to what newspapers actually published.
#
# Output: data/historical_pci.csv, carrying both the full-corpus series and a
# fixed-basket series over the seven papers present for all fifteen
# presidencies. The basket answers the obvious "your sample changed" objection.
#
# It is a robustness check, not a second estimate of the same quantity: the two
# rank the presidencies almost identically (0.99 on term averages) but track
# each other at only 0.92 month to month, and the seven legacy papers run about
# 2 points less negative than the full corpus — 5.7 points for Obama. An
# earlier 0.987 figure quoted here predated the volume floor and the
# fixed-effects change and no longer describes the output.
#
# Usage:
#   Rscript build_historical.R \
#     --headlines data/raw/historical/headlines_scored_extended.csv \
#     --out-dir   data

suppressPackageStartupMessages({
  library(dplyr)
  library(lubridate)
})

# ---- CLI (same minimal base-R parser as build_aggregates.R) -------------
parse_args_simple <- function(args, defaults = list()) {
  out <- defaults; i <- 1
  while (i <= length(args)) {
    a <- args[i]
    if (startsWith(a, "--")) {
      eq <- regexpr("=", a, fixed = TRUE)
      if (eq > 0) {
        out[[substr(a, 3, eq - 1)]] <- substr(a, eq + 1, nchar(a)); i <- i + 1
      } else {
        key <- substr(a, 3, nchar(a))
        if (i + 1 <= length(args) && !startsWith(args[i + 1], "--")) {
          out[[key]] <- args[i + 1]; i <- i + 2
        } else { out[[key]] <- TRUE; i <- i + 1 }
      }
    } else i <- i + 1
  }
  out
}

opt <- parse_args_simple(commandArgs(trailingOnly = TRUE), defaults = list(
  headlines       = NULL,
  `out-dir`       = "data",
  `min-month`     = "5",
  `min-outlets`   = "2",
  `smooth-months` = "7"
))
if (is.null(opt$headlines))
  stop("Usage: Rscript build_historical.R --headlines PATH [--out-dir DIR] [--min-month N] [--min-outlets N] [--smooth-months N]")
if (!dir.exists(opt[["out-dir"]])) dir.create(opt[["out-dir"]], recursive = TRUE)

MIN_MONTH     <- as.numeric(opt[["min-month"]])
# Minimum papers contributing to a published month. See build_series().
MIN_OUTLETS   <- as.numeric(opt[["min-outlets"]])
# Smoothing window in months. Seven is about half a year — long enough to
# damp month-to-month news-cycle noise, short enough to leave the shape of a
# first year intact, which is the part people most want to compare.
SMOOTH_MONTHS <- as.numeric(opt[["smooth-months"]])
Z95 <- qnorm(0.975)

# ---- Presidential terms -------------------------------------------------
# Trump appears twice and is treated as two separate series; a president's
# second term is a different presidency for these purposes and merging them
# would put a four-year gap in the middle of one line.
#
# Truman's row starts where the corpus starts (Nov 1947), not at his 1945
# inauguration, so "months into term" is measured from the start of the term
# rather than the start of the data. His early months are simply absent.
TERMS <- tibble::tribble(
  ~figure,              ~label,       ~term_start,   ~term_end,
  "Harry Truman",       "Truman",     "1945-04-12",  "1953-01-20",
  "Dwight Eisenhower",  "Eisenhower", "1953-01-20",  "1961-01-20",
  "John F. Kennedy",    "Kennedy",    "1961-01-20",  "1963-11-22",
  "Lyndon B. Johnson",  "Johnson",    "1963-11-22",  "1969-01-20",
  "Richard Nixon",      "Nixon",      "1969-01-20",  "1974-08-09",
  "Gerald Ford",        "Ford",       "1974-08-09",  "1977-01-20",
  "Jimmy Carter",       "Carter",     "1977-01-20",  "1981-01-20",
  "Ronald Reagan",      "Reagan",     "1981-01-20",  "1989-01-20",
  "George H.W. Bush",   "Bush 41",    "1989-01-20",  "1993-01-20",
  "Bill Clinton",       "Clinton",    "1993-01-20",  "2001-01-20",
  "George W. Bush",     "Bush 43",    "2001-01-20",  "2009-01-20",
  "Barack Obama",       "Obama",      "2009-01-20",  "2017-01-20",
  "Donald Trump",       "Trump I",    "2017-01-20",  "2021-01-20",
  "Joe Biden",          "Biden",      "2021-01-20",  "2025-01-20",
  "Donald Trump",       "Trump II",   "2025-01-20",  "2029-01-20"
) %>% mutate(term_start = as.Date(term_start), term_end = as.Date(term_end))

# Papers publishing across all fifteen presidencies. Reported alongside the
# full corpus so a reader can check that movement in the index isn't movement
# in the sample.
CORE_BASKET <- c("New York Times", "Los Angeles Times", "Boston Globe",
                 "Chicago Tribune", "Hartford Courant", "Wall Street Journal",
                 "Newsday")

# ---- Load ---------------------------------------------------------------
message("Reading: ", opt$headlines)
h <- read.csv(opt$headlines, stringsAsFactors = FALSE)
h$date <- as.Date(h$date, tryFormats = c("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"))
h$debate_performance <- as.numeric(h$debate_performance)
h <- h[!is.na(h$date) & !is.na(h$debate_performance) & !is.na(h$outlet), ]
message("  ", format(nrow(h), big.mark = ","), " scored headlines, ",
        length(unique(h$outlet)), " outlets, ",
        format(min(h$date)), " to ", format(max(h$date)))

# Assign each headline to a presidency (in-term coverage only).
assign_terms <- function(df) {
  out <- list()
  for (i in seq_len(nrow(TERMS))) {
    tr <- TERMS[i, ]
    sel <- df$figure == tr$figure & df$date >= tr$term_start & df$date < tr$term_end
    if (!any(sel)) next
    d <- df[sel, ]
    d$pres       <- tr$label
    d$term_start <- tr$term_start
    out[[length(out) + 1]] <- d
  }
  bind_rows(out)
}
t <- assign_terms(h)
message("  in-term headlines: ", format(nrow(t), big.mark = ","),
        " across ", length(unique(t$pres)), " presidencies")

t$month <- floor_date(t$date, "month")

# ---- Two-way fixed effects, estimated once across the whole panel -------
# Estimating outlet and period effects on the full 1947-present panel (rather
# than per presidency) is what makes the series comparable across presidents:
# every month is expressed on one common scale, net of which papers happened
# to be publishing. Fitting per-term would centre each president on their own
# mean and destroy exactly the comparison the tab exists to make.
fe_monthly <- function(cells) {
  if (nrow(cells) < 3 || length(unique(cells$month)) < 2 ||
      length(unique(cells$outlet)) < 2) {
    return(cells %>% distinct(month) %>% mutate(fit = NA_real_, se = NA_real_))
  }
  cells$.m <- factor(format(cells$month, "%Y-%m"))
  cells$.o <- factor(cells$outlet)
  fit <- tryCatch(lm(pci ~ .m + .o, data = cells), error = function(e) NULL)
  months <- levels(cells$.m)
  if (is.null(fit))
    return(tibble::tibble(month = as.Date(paste0(months, "-01")),
                          fit = NA_real_, se = NA_real_))
  ref <- levels(cells$.o)[1]
  nd  <- data.frame(.m = factor(months, levels = months),
                    .o = factor(ref, levels = levels(cells$.o)))
  pr <- tryCatch(predict(fit, newdata = nd, se.fit = TRUE), error = function(e) NULL)
  if (is.null(pr))
    return(tibble::tibble(month = as.Date(paste0(months, "-01")),
                          fit = NA_real_, se = NA_real_))
  fitted <- as.numeric(pr$fit)
  fitted <- fitted - (mean(fitted, na.rm = TRUE) - mean(cells$pci, na.rm = TRUE))
  tibble::tibble(month = as.Date(paste0(months, "-01")),
                 fit = fitted, se = as.numeric(pr$se.fit))
}

# LOESS over a fixed number of MONTHS rather than a fixed span fraction.
#
# This matters for comparability. loess() takes span as a proportion of the
# series, so span = 0.5 smooths a 97-month presidency over roughly four years
# and a 13-month one over six months. Two presidents drawn on the same axis
# would then be smoothed by wildly different amounts, and differences in
# wiggliness would read as differences in coverage. Fixing the window in
# months means every administration is smoothed identically.
#
# The raw monthly values are still written out and drawn underneath, so the
# smoother is presented as an aid to reading rather than as the data.
smooth_window <- function(months_in, y, window = SMOOTH_MONTHS) {
  n  <- sum(!is.na(y))
  na <- rep(NA_real_, length(y))
  if (n < 5) return(list(fit = na, lo = na, hi = na))
  # span is a PROPORTION, so converting a month-window to a span is window/n.
  # The floor is expressed in months (never fewer than 5 points in a local
  # fit) rather than as a fixed proportion — a proportional floor would
  # silently widen the window for long presidencies and undo the whole point
  # of holding it constant.
  span <- min(max(window, 5) / n, 0.95)
  ok   <- !is.na(y) & is.finite(y)
  x    <- as.numeric(months_in)

  # Fit on a named data frame rather than on subsetted vectors. loess() takes
  # the term name from the expression, so `loess(y[ok] ~ x[ok])` names its
  # predictor "x[ok]" and predict() then cannot match a newdata column called
  # "x". Degree 1 rather than 2: with a seven-point window a quadratic has
  # little left to fit and overshoots at the ends of a term, which is exactly
  # where people read these lines.
  train <- data.frame(.y = y[ok], .x = x[ok])
  fit <- tryCatch(
    loess(.y ~ .x, data = train, span = span, degree = 1,
          na.action = na.exclude),
    error = function(e) NULL)
  if (is.null(fit)) return(list(fit = na, lo = na, hi = na))

  # se = TRUE requires the default gaussian family; predict.loess silently
  # declines to return standard errors under family = "symmetric".
  pr <- tryCatch(predict(fit, newdata = data.frame(.x = x), se = TRUE),
                 error = function(e) NULL)
  if (is.null(pr) || is.null(pr$se.fit)) return(list(fit = na, lo = na, hi = na))
  list(fit = as.numeric(pr$fit),
       lo  = as.numeric(pr$fit - Z95 * pr$se.fit),
       hi  = as.numeric(pr$fit + Z95 * pr$se.fit))
}

build_series <- function(df, basket_label) {
  cells <- df %>%
    group_by(pres, term_start, month, outlet) %>%
    summarise(n   = n(),
              pos = sum(debate_performance ==  1, na.rm = TRUE),
              neg = sum(debate_performance == -1, na.rm = TRUE),
              .groups = "drop") %>%
    mutate(pci = (pos - neg) / n * 100) %>%
    filter(n >= MIN_MONTH)

  eff <- fe_monthly(cells)

  cells %>%
    group_by(pres, term_start, month) %>%
    summarise(pci_raw    = round(mean(pci), 2),
              n_outlets  = n_distinct(outlet),
              n_headlines = sum(n),
              .groups = "drop") %>%
    # A month standing on a single paper is not an index of anything. With one
    # outlet the outlet effect and the month effect are not separable, so the
    # "cross-outlet average" is just that paper — which is the failure mode the
    # whole fixed-effects construction exists to prevent.
    #
    # This costs one month in 953: the trailing month of the corpus, resting on
    # 7 headlines from one paper, which was bending the end of the current
    # president's trend line down by three points. Every month with two or more
    # papers carries at least 35 headlines, so the rule separates the partial
    # trailing month cleanly without a threshold anyone has to defend.
    filter(n_outlets >= MIN_OUTLETS) %>%
    left_join(eff, by = "month") %>%
    mutate(
      basket    = basket_label,
      pci       = round(fit, 2),
      pci_se    = round(se, 2),
      pci_lo    = round(fit - Z95 * se, 2),
      pci_hi    = round(fit + Z95 * se, 2),
      # Months since inauguration. This is the x-axis: it puts every
      # president at the same point in their own term so month 12 of one
      # administration lines up with month 12 of another.
      months_in = (year(month) - year(term_start)) * 12 +
                  (month(month) - month(term_start))
    ) %>%
    filter(months_in >= 0) %>%
    arrange(pres, months_in) %>%
    group_by(pres) %>%
    group_modify(~{
      s <- smooth_window(.x$months_in, .x$pci)
      .x$pci_smooth    <- round(s$fit, 2)
      .x$pci_smooth_lo <- round(s$lo,  2)
      .x$pci_smooth_hi <- round(s$hi,  2)
      .x
    }) %>%
    ungroup() %>%
    select(basket, pres, term_start, month, months_in,
           pci, pci_se, pci_lo, pci_hi, pci_raw,
           pci_smooth, pci_smooth_lo, pci_smooth_hi,
           n_outlets, n_headlines) %>%
    arrange(pres, months_in)
}

message("Building full-corpus series...")
full <- build_series(t, "full")
message("Building fixed-basket series (", length(CORE_BASKET), " papers)...")
core <- build_series(t[t$outlet %in% CORE_BASKET, ], "core")

out <- bind_rows(full, core)
path <- file.path(opt[["out-dir"]], "historical_pci.csv")
write.csv(out, path, row.names = FALSE)
message("Wrote ", path, " (", nrow(out), " rows)")

# ---- Companion metadata -------------------------------------------------
meta_pres <- full %>%
  group_by(pres) %>%
  summarise(term_start   = as.character(min(term_start)),
            first_month  = as.character(min(month)),
            last_month   = as.character(max(month)),
            months       = n(),
            mean_pci     = round(mean(pci, na.rm = TRUE), 2),
            headlines    = sum(n_headlines),
            .groups = "drop") %>%
  arrange(term_start)

meta_path <- file.path(opt[["out-dir"]], "historical_meta.json")
con <- file(meta_path, "w")
writeLines("{", con)
writeLines(paste0('  "corpus": "newspapers",'), con)
writeLines(paste0('  "min_headlines_per_outlet_month": ', MIN_MONTH, ','), con)
writeLines(paste0('  "min_outlets_per_month": ', MIN_OUTLETS, ','), con)
writeLines(paste0('  "smooth_window_months": ', SMOOTH_MONTHS, ','), con)
writeLines(paste0('  "corpus_start": "', as.character(min(t$date)), '",'), con)
writeLines(paste0('  "corpus_end": "',   as.character(max(t$date)), '",'), con)
writeLines(paste0('  "n_outlets": ',     length(unique(t$outlet)), ','), con)
writeLines(paste0('  "core_basket": [',
                  paste0('"', CORE_BASKET, '"', collapse = ", "), '],'), con)
writeLines('  "presidents": [', con)
for (i in seq_len(nrow(meta_pres))) {
  r <- meta_pres[i, ]
  writeLines(paste0('    {"label": "', r$pres,
                    '", "term_start": "', r$term_start,
                    '", "first_month": "', r$first_month,
                    '", "last_month": "', r$last_month,
                    '", "months": ', r$months,
                    ', "mean_pci": ', r$mean_pci,
                    ', "headlines": ', r$headlines, '}',
                    if (i < nrow(meta_pres)) "," else ""), con)
}
writeLines("  ]", con)
writeLines("}", con)
close(con)
message("Wrote ", meta_path)

message("\nPer-president mean PCI (full corpus):")
for (i in seq_len(nrow(meta_pres))) {
  r <- meta_pres[i, ]
  message(sprintf("  %-12s %6.1f   %3d months   %s headlines",
                  r$pres, r$mean_pci, r$months,
                  format(r$headlines, big.mark = ",")))
}
