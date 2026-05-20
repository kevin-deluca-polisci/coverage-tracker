#!/usr/bin/env Rscript
# build_aggregates.R
#
# Reads the raw per-segment chunks file and the raw per-headline file,
# computes weekly aggregates (counts + % positive/negative/net score) by
# network and by outlet, and runs LOESS smoothing to add smoothed columns
# with 95% confidence intervals. Writes 4 small CSVs into the repo's
# data/ folder:
#
#   weekly_tv.csv       (per network, by week)
#   weekly_tv_agg.csv   (across-network aggregate, by week)
#   weekly_news.csv     (per outlet, by week)
#   weekly_news_agg.csv (across-outlet aggregate, by week)
#
# Each file contains both raw weekly values (net_score, pct_negative,
# total_segments) and LOESS-smoothed values with 95% CI bounds.
#
# The static page (index.html) reads these directly. Raw chunk/headline
# CSVs never need to leave the cluster.
#
# Usage:
#   Rscript build_aggregates.R \
#     --chunks    /path/to/trump_performance_chunks.csv \
#     --headlines /path/to/trump_headlines_analyzed.csv \
#     --out-dir   /path/to/coverage-tracker/data \
#     --span      0.5

suppressPackageStartupMessages({
  library(dplyr)
  library(lubridate)
  library(optparse)
})

# ---- CLI ---------------------------------------------------------------

option_list <- list(
  make_option("--chunks",    type = "character", default = NULL,
              help = "Path to trump_performance_chunks.csv (raw TV segments)"),
  make_option("--headlines", type = "character", default = NULL,
              help = "Path to trump_headlines_analyzed.csv (raw headlines)"),
  make_option("--out-dir",   type = "character", default = "data",
              help = "Output directory [default: %default]"),
  make_option("--span",      type = "double",    default = 0.5,
              help = "LOESS smoothing span [default: %default]")
)
opt <- parse_args(OptionParser(option_list = option_list))

if (is.null(opt$chunks) || is.null(opt$headlines)) {
  stop("Both --chunks and --headlines paths are required.")
}
if (!dir.exists(opt[["out-dir"]])) dir.create(opt[["out-dir"]], recursive = TRUE)

LOESS_SPAN <- opt$span
Z95 <- qnorm(0.975)  # 1.96 for 95% CI

# ---- Network / outlet maps --------------------------------------------

CBS_SHOWS <- c("CBS Evening News","CBS News Mornings","Face the Nation",
               "CBS Evening News Plus","CBS Evening News With Norah O'Donnell",
               "CBS Overnight News","CBS News Sunday Morning","60 Minutes",
               "The Late Show with Stephen Colbert","CBS News Roundup",
               "CBS Weekend News","CBS Morning News","CBS News 24/7")
CNN_SHOWS <- c("CNN News Central","CNN Newsroom With Wolf Blitzer",
               "CNN This Morning","CNN This Morning Weekend",
               "The Source With Kaitlan Collins","CNN Newsroom With Jim Acosta",
               "Anderson Cooper 360","Erin Burnett OutFront",
               "The Lead With Jake Tapper","CNN NewsNight With Abby Phillip",
               "State of the Union With Jake Tapper and Dana Bash",
               "Inside Politics With Dana Bash","Early Start")
FOX_SHOWS <- c("Fox News Sunday","FOX News Saturday Night","Fox News Live",
               "FOX and Friends Sunday","Fox Report With Jon Scott",
               "FOX News Saturday Night With Jimmy Failla","The Ingraham Angle",
               "Hannity","The Five","Fox News at Night","America's Newsroom",
               "FOX and Friends","Sunday Morning Futures With Maria Bartiromo",
               "Special Report With Bret Baier","Jesse Watters Primetime",
               "Gutfeld!","One Nation With Brian Kilmeade")
ABC_SHOWS <- c("This Week With George Stephanopoulos","Good Morning America",
               "Jimmy Kimmel Live!","ABC World News Saturday",
               "ABC World News Sunday","ABC World News Tonight With David Muir",
               "Nightline","ABC World News Now")
NBC_SHOWS <- c("NBC News Daily","Meet the Press",
               "NBC Nightly News With Lester Holt","Today")
MSNBC_SHOWS <- c("Chris Jansing Reports","The Beat With Ari Melber",
                 "Deadline: White House","Morning Joe",
                 "The Rachel Maddow Show","The Last Word With Lawrence O'Donnell",
                 "Alex Wagner Tonight","Andrea Mitchell Reports",
                 "Inside With Jen Psaki","José Díaz-Balart Reports","The ReidOut")

ALL_SHOWS <- c(CBS_SHOWS, CNN_SHOWS, FOX_SHOWS, ABC_SHOWS, NBC_SHOWS, MSNBC_SHOWS)
SHOW_NETWORK_MAP <- c(
  setNames(rep("CBS",         length(CBS_SHOWS)),   CBS_SHOWS),
  setNames(rep("CNN",         length(CNN_SHOWS)),   CNN_SHOWS),
  setNames(rep("Fox",         length(FOX_SHOWS)),   FOX_SHOWS),
  setNames(rep("ABC",         length(ABC_SHOWS)),   ABC_SHOWS),
  setNames(rep("NBC",         length(NBC_SHOWS)),   NBC_SHOWS),
  setNames(rep("MSNBC/MSNow", length(MSNBC_SHOWS)), MSNBC_SHOWS)
)

KEEP_OUTLETS <- c("Reuters","Fox News","CBS News","Bloomberg","CNN","ABC News",
                  "USA Today","New York Times","NBC News","Los Angeles Times","NPR")

# ---- Helpers ----------------------------------------------------------

parse_dates <- function(x) {
  as.Date(x, tryFormats = c("%Y-%m-%d","%Y/%m/%d","%m/%d/%Y"))
}

# Weekly summary: counts, percentages, net score for one grouping variable
weekly_summary <- function(df, group_col) {
  df %>%
    group_by(week, .data[[group_col]]) %>%
    summarise(
      total_segments = n(),
      n_positive     = sum(debate_performance ==  1, na.rm = TRUE),
      n_negative     = sum(debate_performance == -1, na.rm = TRUE),
      n_neutral      = sum(debate_performance ==  0, na.rm = TRUE),
      pct_positive   = round(mean(debate_performance ==  1, na.rm = TRUE) * 100, 2),
      pct_negative   = round(mean(debate_performance == -1, na.rm = TRUE) * 100, 2),
      net_score      = round(pct_positive - pct_negative, 2),
      .groups        = "drop"
    ) %>%
    rename(!!group_col := !!sym(group_col)) %>%
    arrange(.data[[group_col]], week)
}

# Aggregate across groups (equal-weighted by group, matching methodology text)
weekly_aggregate <- function(weekly_df, group_col) {
  weekly_df %>%
    group_by(week) %>%
    summarise(
      total_segments = sum(total_segments),
      pct_positive   = round(mean(pct_positive, na.rm = TRUE), 2),
      pct_negative   = round(mean(pct_negative, na.rm = TRUE), 2),
      net_score      = round(pct_positive - pct_negative, 2),
      .groups        = "drop"
    ) %>%
    arrange(week)
}

# Run LOESS on a single time series and return fit + 95% CI
loess_smooth <- function(weeks, y, span = LOESS_SPAN) {
  ok <- !is.na(y) & is.finite(y)
  n_ok <- sum(ok)
  if (n_ok < max(4, ceiling(1 / span))) {
    return(list(fit = rep(NA_real_, length(y)),
                lo  = rep(NA_real_, length(y)),
                hi  = rep(NA_real_, length(y))))
  }
  x_num <- as.numeric(weeks)
  fit <- tryCatch(
    loess(y[ok] ~ x_num[ok], span = span, degree = 2, na.action = na.exclude),
    error = function(e) NULL
  )
  if (is.null(fit)) {
    return(list(fit = rep(NA_real_, length(y)),
                lo  = rep(NA_real_, length(y)),
                hi  = rep(NA_real_, length(y))))
  }
  pr <- predict(fit, newdata = data.frame(x_num = x_num), se = TRUE)
  list(
    fit = as.numeric(pr$fit),
    lo  = as.numeric(pr$fit - Z95 * pr$se.fit),
    hi  = as.numeric(pr$fit + Z95 * pr$se.fit)
  )
}

# Add smoothed columns to a weekly df, optionally grouped by a column
add_smooths <- function(df, group_col = NULL) {
  smooth_one <- function(d) {
    d <- d[order(d$week), ]
    n_sm <- loess_smooth(d$week, d$net_score)
    p_sm <- loess_smooth(d$week, d$pct_negative)
    d$smooth_net       <- round(n_sm$fit, 2)
    d$smooth_net_lo    <- round(n_sm$lo,  2)
    d$smooth_net_hi    <- round(n_sm$hi,  2)
    d$smooth_neg       <- round(p_sm$fit, 2)
    d$smooth_neg_lo    <- round(p_sm$lo,  2)
    d$smooth_neg_hi    <- round(p_sm$hi,  2)
    d
  }
  if (is.null(group_col)) {
    smooth_one(df)
  } else {
    df %>%
      group_split(.data[[group_col]]) %>%
      lapply(smooth_one) %>%
      bind_rows() %>%
      arrange(.data[[group_col]], week)
  }
}

# ---- TV / broadcast ----------------------------------------------------

message("Reading chunks: ", opt$chunks)
tv <- read.csv(opt$chunks, stringsAsFactors = FALSE)
tv$date <- parse_dates(tv$date)
tv$show_name <- trimws(tv$show_name)
tv <- tv[tv$show_name %in% ALL_SHOWS, ]
tv$network <- SHOW_NETWORK_MAP[tv$show_name]
tv$debate_performance <- as.numeric(tv$debate_performance)
tv <- tv[!is.na(tv$date) & !is.na(tv$debate_performance), ]
tv$week <- floor_date(tv$date, "week", week_start = 1)

weekly_tv     <- weekly_summary(tv, "network") %>% add_smooths("network")
weekly_tv_agg <- weekly_aggregate(weekly_tv, "network") %>% add_smooths()

write.csv(weekly_tv,     file.path(opt[["out-dir"]], "weekly_tv.csv"),     row.names = FALSE)
write.csv(weekly_tv_agg, file.path(opt[["out-dir"]], "weekly_tv_agg.csv"), row.names = FALSE)
message("Wrote weekly_tv.csv (", nrow(weekly_tv), " rows, ",
        length(unique(weekly_tv$network)), " networks)")
message("Wrote weekly_tv_agg.csv (", nrow(weekly_tv_agg), " weeks)")

# ---- Digital headlines -------------------------------------------------

message("Reading headlines: ", opt$headlines)
news <- read.csv(opt$headlines, stringsAsFactors = FALSE)
news$date <- parse_dates(news$date)
news$outlet <- trimws(news$outlet)
news <- news[news$outlet %in% KEEP_OUTLETS, ]
news$debate_performance <- as.numeric(news$debate_performance)
news <- news[!is.na(news$date) & !is.na(news$debate_performance), ]
news$week <- floor_date(news$date, "week", week_start = 1)

weekly_news     <- weekly_summary(news, "outlet") %>% add_smooths("outlet")
weekly_news_agg <- weekly_aggregate(weekly_news, "outlet") %>% add_smooths()

write.csv(weekly_news,     file.path(opt[["out-dir"]], "weekly_news.csv"),     row.names = FALSE)
write.csv(weekly_news_agg, file.path(opt[["out-dir"]], "weekly_news_agg.csv"), row.names = FALSE)
message("Wrote weekly_news.csv (", nrow(weekly_news), " rows, ",
        length(unique(weekly_news$outlet)), " outlets)")
message("Wrote weekly_news_agg.csv (", nrow(weekly_news_agg), " weeks)")

message("Done.")
