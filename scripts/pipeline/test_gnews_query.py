#!/usr/bin/env python3
"""test_gnews_query.py — try several Google News query shapes and report
how many items each returns, plus a sample headline and the publisher Google
attributes it to.

Built to solve the ABC News case: `site:abcnews.go.com` returns nothing, most
likely because ABC lives on a subdomain of an unrelated registered domain
(go.com). Rather than guess, this probes candidate query forms so we can put
the winner into gnews_feeds.yaml as a per-outlet `query:` override.

Usage:
  # probe the built-in ABC candidates
  python3 scripts/pipeline/test_gnews_query.py

  # probe your own query strings
  python3 scripts/pipeline/test_gnews_query.py "trump site:cnn.com when:7d" "trump CNN when:7d"
"""

import sys
import time
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET

GNEWS_URL = ("https://news.google.com/rss/search"
             "?q={query}&hl=en-US&gl=US&ceid=US:en")
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36")

# Candidate query shapes for ABC News, roughly in order of preference.
DEFAULT_CANDIDATES = [
    'trump site:abcnews.go.com when:7d',
    'trump site:go.com when:7d',
    'trump "ABC News" when:7d',
    'trump abcnews when:7d',
    'trump site:abcnews.go.com/politics when:7d',
    'trump source:"ABC News" when:7d',
]


def probe(query, timeout=30):
    url = GNEWS_URL.format(query=quote_plus(query))
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (URLError, HTTPError, TimeoutError) as e:
        return None, f"fetch error: {e}", []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        return None, f"parse error: {e}", []

    items = list(root.iter("item"))
    samples = []
    for it in items[:3]:
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        pub = (src_el.text or "").strip() if src_el is not None else ""
        samples.append((title, pub))
    return len(items), "ok", samples


def main():
    candidates = sys.argv[1:] or DEFAULT_CANDIDATES
    print(f"Probing {len(candidates)} query shape(s) against Google News RSS\n")
    for q in candidates:
        n, status, samples = probe(q)
        n_str = "ERR" if n is None else str(n)
        print(f"{'─'*72}")
        print(f"query : {q}")
        print(f"items : {n_str}   ({status})")
        for t, pub in samples:
            print(f"        · [{pub}] {t[:90]}")
        if n == 0:
            print("        (no results — this query shape does not work)")
        time.sleep(3)  # be polite
    print(f"{'─'*72}")
    print("\nPut the winning query into gnews_feeds.yaml as a per-outlet override:")
    print('  - name: ABC News')
    print('    domain: abcnews.go.com')
    print('    query: "trump <winning form> {when}"')
    print("\n({when} is substituted with the configured when: window.)")


if __name__ == "__main__":
    main()
