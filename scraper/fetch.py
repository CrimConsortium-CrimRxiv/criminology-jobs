"""Fetch job-board pages and reduce them to plain text for extraction"""

import gzip
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

from . import config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# A bare UA is enough for an ordinary site but not for the ones behind a bot
# check, which also look at the rest of a real navigation request.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class FetchError(Exception):
    pass


def _http_with_status(url):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.status, _decompress(
            response.read(300_000), response.headers.get("Content-Encoding", ""))


def _http(url):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return _decompress(r.read(), r.headers.get("Content-Encoding", ""))


def _curl(url):
    """curl negotiates HTTP/2 and a real TLS fingerprint, which urllib cannot."""
    cmd = ["curl", "-s", "-L", "--compressed", "--http2", "--max-time", "40"]
    for key, value in BROWSER_HEADERS.items():
        cmd += ["-H", f"{key}: {value}"]
    r = subprocess.run(cmd + [url], capture_output=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise FetchError(f"curl failed for {url}")
    return r.stdout


def _decompress(body, encoding):
    if "gzip" in encoding:
        return gzip.decompress(body)
    if "deflate" in encoding:
        return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def _blocked(body):
    markers = (
        b"Incapsula",
        b"_Incapsula_Resource",
        b"challenges.cloudflare.com",
        b"<title>Just a moment...</title>",
    )
    return len(body) < 2000 or any(marker in body for marker in markers)


def _get(url):
    last = None
    for method in (_http, _curl):
        try:
            body = method(url)
            if not _blocked(body):
                return body
            last = FetchError(f"{method.__name__} returned a blocked/empty page for {url}")
        except FetchError as e:
            last = e
        except Exception as e:  # network errors, HTTP 403s, timeouts
            last = e
    raise FetchError(f"all fetch methods failed for {url}: {last}")


class _TextExtractor(HTMLParser):
    """HTML to text"""
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self, base_url):
        super().__init__()
        self.base = base_url
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "a" and not self._skip:
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "#", "mailto:")):
                self.parts.append(f" [{urllib.parse.urljoin(self.base, href)}] ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw_html, base_url):
    parser = _TextExtractor(base_url)
    parser.feed(raw_html)
    lines = (line.strip() for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _jmajax(url):
    """WP Job Manager AJAX listings (TSPA): POST once, get every listing's HTML."""
    data = urllib.parse.urlencode({"per_page": "200", "page": "1"}).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    if not payload.get("found_jobs"):
        raise FetchError(f"jm-ajax reported no jobs at {url}")
    return html_to_text(payload["html"], url)


def fetch_source(name):
    """Return (text, note), errors if nothing usable came back."""
    cfg = config.SOURCES[name]
    texts = []
    for url in cfg["urls"]:
        if cfg.get("kind") == "jmajax":
            texts.append(_jmajax(url))
        else:
            body = _get(url)
            texts.append(html_to_text(body.decode("utf-8", errors="ignore"), url))
    text = "\n\n=== NEXT PAGE ===\n\n".join(texts)
    if len(text.strip()) < 200:
        raise FetchError(f"extracted page text was empty for {name}")
    note = ""
    if len(text) > config.MAX_INPUT_CHARS:
        text = text[:config.MAX_INPUT_CHARS]
        note = f"(truncated to {config.MAX_INPUT_CHARS} chars)"
    return text, note


# --- Liveness ---------------------------------------------------------------
# A posting stays on the board until it is *known* dead. Boards do not take a
# listing down with a 404: HigherEdJobs serves "Position Deleted on 1/02/2026"
# with HTTP 200, so status alone tells us nothing and the page has to be read.
DEAD_MARKERS = (
    "position deleted",
    "position has been deleted",
    "this position is no longer",
    "no longer accepting applications",
    "no longer available",
    "posting has expired",
    "job posting has been removed",
    "this job has expired",
    "vacancy has closed",
    "position has been filled",
    "listing is no longer active",
    "job is no longer posted",
)

HOST_REQUEST_DELAY = 0.7  # seconds between requests to the same host

# Never conclude "dead" from these — they mean we could not see the page.
LIVE_UNKNOWN_STATUSES = (401, 403, 405, 429, 500, 502, 503, 504)


def _curl_with_status(url):
    """(status, body) via curl, which gets past bot checks urllib cannot."""
    cmd = ["curl", "-s", "-L", "--compressed", "--http2", "--max-time", "25",
           "-w", "\n%{http_code}"]
    for key, value in BROWSER_HEADERS.items():
        cmd += ["-H", f"{key}: {value}"]
    result = subprocess.run(cmd + [url], capture_output=True, timeout=45)
    if result.returncode != 0:
        raise FetchError(f"curl failed for {url}")
    body, _, status = result.stdout.rpartition(b"\n")
    return int(status or 0), body


def listing_state(url):
    """Return "live", "dead", or "unknown" for one posting URL.

    Only a definite signal retires a posting: 404/410, or a dead marker in the
    page text. A bot wall, a timeout or a 5xx is "unknown" and keeps the row,
    because a board we cannot read is not a board without jobs. urllib is tried
    first and curl second for the same reason fetching does — Incapsula answers
    urllib with a 403, which would make every HigherEdJobs posting unverifiable.
    """
    if not url:
        return "unknown"
    status, body = 0, b""
    for attempt in (_http_with_status, _curl_with_status):
        try:
            status, body = attempt(url)
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                return "dead"
            continue
        except Exception:
            continue
        if status in (404, 410):
            return "dead"
        if status in LIVE_UNKNOWN_STATUSES or _blocked(body):
            continue  # try the next method before giving up
        break
    if not body or status in (404, 410):
        return "dead" if status in (404, 410) else "unknown"
    if status in LIVE_UNKNOWN_STATUSES or _blocked(body):
        return "unknown"
    text = html_to_text(body.decode("utf-8", errors="ignore"), url).lower()
    if any(marker in text for marker in DEAD_MARKERS):
        return "dead"
    return "live"


def listing_states(urls, workers=8, max_per_host=None):
    """Check posting URLs; returns {url: state} for the ones checked.

    Parallel across hosts but serial within one, with a pause between requests
    to the same host, and at most max_per_host per run. Both limits are there
    because volume is what gets us refused: 8 concurrent requests made
    higheredjobs.com report 144 of 154 postings unverifiable, and a few hundred
    checks in one day made it refuse all 154. Callers pass the URLs in the order
    they want them spent, and anything past the cap is simply left unchecked —
    absent from the result rather than reported as unknown.
    """
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return {}
    by_host = {}
    for url in urls:
        host = urllib.parse.urlparse(url).hostname or ""
        chosen = by_host.setdefault(host, [])
        if max_per_host is None or len(chosen) < max_per_host:
            chosen.append(url)

    def check_host(host_urls):
        out = {}
        for index, url in enumerate(host_urls):
            if index:
                time.sleep(HOST_REQUEST_DELAY)
            out[url] = listing_state(url)
        return out

    states = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(check_host, by_host.values()):
            states.update(result)
    return states
