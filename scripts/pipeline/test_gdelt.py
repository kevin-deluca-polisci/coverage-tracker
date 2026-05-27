#!/usr/bin/env python3
"""test_gdelt.py — simple GDELT health-check.

Runs a tiny query for each tracked outlet over a recent 7-day window. Prints
how long each call took, the HTTP status, and how many results came back.

Useful for diagnosing whether GDELT is reachable / rate-limiting / returning
data, without running the full scraper.

Usage:
  python3 scripts/pipeline/test_gdelt.py            # checks all 10 outlets
  python3 scripts/pipeline/test_gdelt.py nytimes.com reuters.com  # just these
"""

import sys
import time
import requests
from datetime import datetime, timedelta

OUTLETS = [
    "reuters.com", "foxnews.com", "cbsnews.com", "bloomberg.com",
    "cnn.com", "abcnews.go.com", "usatoday.com", "nbcnews.com",
    "latimes.com", "npr.org", "nytimes.com",   # NYT included here for parity
]

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
USER_AGENT = "coverage-tracker health check"


def check_outlet(domain, start_dt, end_dt):
    params = {
        "query":         f"trump domain:{domain}",   # NOTE: domain:, not source:
        "mode":          "ArtList",
        "format":        "json",
        "startdatetime": start_dt.strftime("%Y%m%d000000"),
        "enddatetime":   end_dt.strftime("%Y%m%d235959"),
        "maxrecords":    10,
        "sort":          "DateDesc",
    }
    t0 = time.time()
    try:
        r = requests.get(GDELT_URL, params=params,
                         headers={"User-Agent": USER_AGENT}, timeout=90)
        elapsed = time.time() - t0
    except requests.RequestException as e:
        return ("ERR", time.time() - t0, str(e), 0, None)

    if r.status_code != 200:
        return (r.status_code, elapsed,
                r.text[:100].replace("\n", " "), 0, None)

    # Sometimes GDELT returns HTML even with 200; try parsing JSON
    try:
        data = r.json()
    except ValueError:
        return ("HTML", elapsed,
                r.text[:100].replace("\n", " "), 0, None)

    articles = data.get("articles", []) or []
    sample = articles[0]["title"][:80] if articles else None
    return (r.status_code, elapsed, "OK", len(articles), sample)


def main():
    domains = sys.argv[1:] if len(sys.argv) > 1 else OUTLETS

    # Last 7 calendar days, UTC
    today = datetime.utcnow()
    start = today - timedelta(days=7)

    print(f"GDELT health check — window {start.date()} → {today.date()}")
    print(f"{'domain':<22} {'status':>7}  {'time':>6}  {'n':>4}  sample headline")
    print("-" * 100)

    for d in domains:
        status, elapsed, msg, n, sample = check_outlet(d, start, today)
        status_str = str(status)
        timing = f"{elapsed:.1f}s"
        sample_str = sample if sample else f"({msg})"
        print(f"{d:<22} {status_str:>7}  {timing:>6}  {n:>4}  {sample_str}")
        time.sleep(8)  # GDELT is bursty under load; 8s helps avoid retry storms

    print()
    print("Status legend:")
    print("  200  = OK")
    print("  429  = rate limited (GDELT throttling us)")
    print("  HTML = GDELT returned non-JSON (often overload)")
    print("  ERR  = network error / timeout")
    print("  n    = number of articles returned (cap=10 here, just to keep it light)")


if __name__ == "__main__":
    main()
