"""Second verification pass: query congress.gov's own website-wide full-text
search (used by www.congress.gov/search) for the exact phrase, and compare the
result set against what our API-driven crawl found.

This is a best-effort cross-check, not a replacement for the API-driven crawl:
congress.gov's website search does not expose a documented/stable API, its
result set composition (which collections it covers) is not fully documented,
and pages are HTML meant for browsers. We scrape it read-only, for verification
only, and cap how many pages we pull.
"""
import csv
import json
import re
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

from . import config

BASE = "https://www.congress.gov/search"
_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})


def _build_url(page: int, congress: int | None = None):
    q = {"source": "all", "search": '"research security"'}
    if congress:
        q["congress"] = str(congress)
    query = {"q": json.dumps(q)}
    if page > 1:
        query["page"] = str(page)
    return BASE + "?" + urllib.parse.urlencode(query)


def run_website_search(congress: int, max_pages: int = 40):
    """Scrape congress.gov search results for the phrase. Returns
    (results, result_count); write_results saves them for all Congresses.
    """
    results = []
    result_count = None
    for page in range(1, max_pages + 1):
        url = _build_url(page, congress)
        try:
            resp = _session.get(url, timeout=30)
        except requests.RequestException:
            break
        if resp.status_code != 200:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        if result_count is None:
            m = re.search(r"of\s+([\d,]+)\s+results", soup.get_text())
            if m:
                result_count = int(m.group(1).replace(",", ""))
        items = soup.select("li.expanded")
        if not items:
            items = soup.select(".search-column-main .basic-search-item, .search-column-main li")
        if not items:
            break
        found_this_page = 0
        for li in items:
            a = li.find("a", href=True)
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.congress.gov" + href
            if not title or "congress.gov" not in href:
                continue
            results.append({"congress": congress, "title": title, "url": href})
            found_this_page += 1
        if found_this_page == 0:
            break
        time.sleep(1.0)  # be polite to the public website
        if result_count is not None and len(results) >= result_count:
            break

    return results, result_count


def write_results(results):
    out_path = config.ARCHIVE_DIR / "website_search_results.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["congress", "title", "url"])
        w.writeheader()
        for r in results:
            w.writerow(r)
