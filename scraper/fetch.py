"""Fetch job-board pages and reduce them to plain text for extraction"""

import json
import subprocess
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class FetchError(Exception):
    pass


def _http(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _curl(url):
    r = subprocess.run(["curl", "-s", "-L", "-A", UA, "--max-time", "40", url],
                       capture_output=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        raise FetchError(f"curl failed for {url}")
    return r.stdout


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
