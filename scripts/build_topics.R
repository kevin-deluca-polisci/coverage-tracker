#!/usr/bin/env Rscript
# build_topics.R
#
# Reads raw headlines + TV transcript chunks, applies keyword-based topic
# matching from topics.yaml, and writes data/topics_weekly.csv with weekly
# topic volume and share for both digital headlines and TV segments.
#
# Output columns:
#   week, topic,
#   count_news, count_tv,        # number of headlines/segments matching the topic that week
#   total_news, total_tv,        # total headlines/segments that week (denominator)
#   share_news, share_tv         # count / total * 100
#
# Usage:
#   Rscript build_topics.R \
#     --chunks    /path/to/trump_performance_chunks.csv \
#     --headlines /path/to/trump_headlines_analyzed.csv \
#     --topics    scripts/topics.yaml \
#     --out-dir   data \
#     --news-text-col title \
#     --tv-text-col   text

suppressPackageStartupMessages({
  library(dplyr)
  library(lubridate)
  library(stringr)
})

# yaml is the one external (non-base) dep we need. Friendlier error if missing.
if (!requireNamespace("yaml", quietly = TRUE)) {
  stop("This script needs the 'yaml' R package. Install it once with:\n",
       "  Rscript -e 'install.packages(\"yaml\", repos=\"https://cloud.r-project.org\")'")
}

# ---- CLI ---------------------------------------------------------------
# Minimal base-R argument parser. No optparse/getopt dependency.

parse_args_simple <- function(args, defaults = list()) {
  out <- defaults
  i <- 1
  while (i <= length(args)) {
    a <- args[i]
    if (startsWith(a, "--")) {
      eq <- regexpr("=", a, fixed = TRUE)
      if (eq > 0) {
        key <- substr(a, 3, eq - 1)
        val <- substr(a, eq + 1, nchar(a))
        out[[key]] <- val
        i <- i + 1
      } else {
        key <- substr(a, 3, nchar(a))
        if (i + 1 <= length(args) && !startsWith(args[i + 1], "--")) {
          out[[key]] <- args[i + 1]
          i <- i + 2
        } else {
          out[[key]] <- TRUE
          i <- i + 1
        }
      }
    } else {
      i <- i + 1
    }
  }
  out
}

opt <- parse_args_simple(commandArgs(trailingOnly = TRUE), defaults = list(
  chunks          = NULL,
  headlines       = NULL,
  topics          = "scripts/topics.yaml",
  `out-dir`       = "data",
  `news-text-col` = NULL,
  `tv-text-col`   = NULL
))

if (is.null(opt$chunks) || is.null(opt$headlines)) {
  stop("Usage: Rscript build_topics.R --chunks PATH --headlines PATH [--topics PATH] [--out-dir DIR]")
}
if (!dir.exists(opt[["out-dir"]])) dir.create(opt[["out-dir"]], recursive = TRUE)

# ---- Load topics ------------------------------------------------------

topics_cfg <- yaml::read_yaml(opt$topics)
topics <- topics_cfg$topics
if (is.null(topics) || length(topics) == 0) {
  stop("topics.yaml has no topics defined.")
}

# Build one regex per topic: \b(kw1|kw2|...)\b, case-insensitive
build_pattern <- function(keywords) {
  esc <- gsub('([\\^$.|?*+()\\[\\]{}])', '\\\\\\1', keywords)
  paste0("(?i)\\b(", paste(esc, collapse = "|"), ")\\b")
}
topic_patterns <- setNames(
  lapply(topics, function(t) build_pattern(t$keywords)),
  sapply(topics, function(t) t$name)
)
message("Loaded ", length(topic_patterns), " topics: ",
        paste(names(topic_patterns), collapse = ", "))

# ---- Helpers ----------------------------------------------------------

parse_dates <- function(x) {
  as.Date(x, tryFormats = c("%Y-%m-%d","%Y/%m/%d","%m/%d/%Y"))
}

# Pick the first column from `candidates` that exists in df. Errors out
# with a helpful message if none match.
pick_text_col <- function(df, candidates, override, source) {
  if (!is.null(override)) {
    if (!override %in% names(df)) {
      stop(source, ": column '", override, "' not found. Available: ",
           paste(names(df), collapse = ", "))
    }
    return(override)
  }
  hit <- candidates[candidates %in% names(df)]
  if (length(hit) == 0) {
    stop(source, ": no text column found. Tried: ",
         paste(candidates, collapse = ", "),
         ". Available: ", paste(names(df), collapse = ", "))
  }
  hit[1]
}

count_topics <- function(df, text_col, count_col) {
  out_list <- list()
  for (topic_name in names(topic_patterns)) {
    pat <- topic_patterns[[topic_name]]
    hits <- str_detect(df[[text_col]], pat)
    sub  <- df[hits & !is.na(hits), c("week"), drop = FALSE]
    if (nrow(sub) == 0) next
    grouped <- sub %>% count(week, name = count_col) %>% mutate(topic = topic_name)
    out_list[[topic_name]] <- grouped
  }
  if (length(out_list) == 0) {
    return(data.frame(week = as.Date(character()), topic = character(),
                      stringsAsFactors = FALSE) %>% mutate(!!count_col := integer()))
  }
  bind_rows(out_list)
}

# ---- Headlines --------------------------------------------------------

message("Reading headlines: ", opt$headlines)
news <- read.csv(opt$headlines, stringsAsFactors = FALSE)
news_text_col <- pick_text_col(news,
  candidates = c("title", "headline", "text", "story", "content"),
  override   = opt[["news-text-col"]],
  source     = "headlines")
message("Using '", news_text_col, "' as headline text column")

news$date <- parse_dates(news$date)
news <- news[!is.na(news$date) & !is.na(news[[news_text_col]]), ]
news$week <- floor_date(news$date, "week", week_start = 1)

news_topic <- count_topics(news, news_text_col, "count_news")
total_news <- news %>% count(week, name = "total_news")

# ---- TV chunks --------------------------------------------------------

message("Reading chunks: ", opt$chunks)
chunks <- read.csv(opt$chunks, stringsAsFactors = FALSE)
tv_text_col <- pick_text_col(chunks,
  candidates = c("text", "chunk_text", "segment_text", "transcript", "snippet"),
  override   = opt[["tv-text-col"]],
  source     = "chunks")
message("Using '", tv_text_col, "' as TV chunk text column")

chunks$date <- parse_dates(chunks$date)
chunks <- chunks[!is.na(chunks$date) & !is.na(chunks[[tv_text_col]]), ]
chunks$week <- floor_date(chunks$date, "week", week_start = 1)

tv_topic <- count_topics(chunks, tv_text_col, "count_tv")
total_tv <- chunks %>% count(week, name = "total_tv")

# ---- Combine into a dense (week × topic) grid -------------------------

all_weeks  <- sort(unique(c(news$week, chunks$week)))
all_topics <- names(topic_patterns)
grid <- expand.grid(week = all_weeks, topic = all_topics,
                    stringsAsFactors = FALSE) %>%
        mutate(week = as.Date(week))

result <- grid %>%
  left_join(news_topic, by = c("week", "topic")) %>%
  left_join(tv_topic,   by = c("week", "topic")) %>%
  left_join(total_news, by = "week") %>%
  left_join(total_tv,   by = "week") %>%
  mutate(
    count_news = ifelse(is.na(count_news), 0, count_news),
    count_tv   = ifelse(is.na(count_tv),   0, count_tv),
    total_news = ifelse(is.na(total_news), 0, total_news),
    total_tv   = ifelse(is.na(total_tv),   0, total_tv),
    share_news = ifelse(total_news > 0,
                        round(count_news / total_news * 100, 2), 0),
    share_tv   = ifelse(total_tv > 0,
                        round(count_tv / total_tv * 100, 2), 0)
  ) %>%
  arrange(topic, week)

out_path <- file.path(opt[["out-dir"]], "topics_weekly.csv")
write.csv(result, out_path, row.names = FALSE)
message("Wrote ", out_path, " (", nrow(result), " rows, ",
        length(all_topics), " topics × ", length(all_weeks), " weeks)")
