"""Fetch a bot-walled listing page through Perplexity's fetch_url tool.

ACJS, ASC and HigherEdJobs answer our own requests with a Cloudflare/Incapsula
JS challenge, but Perplexity's server-side fetch reaches them. Asking it to
hand back the listing text verbatim keeps the pipeline's shape: the page still
goes through the normal extractor, which means real listings, real URLs, and
the same URL-appears-in-the-page check as a page we downloaded ourselves.

This is used in preference to search-based enumeration, which is stochastic —
the same board came back with 33, 25 and 0 listings from identical code.
"""

import json
import os

from openai import OpenAI

from . import config

BASE_URL = "https://api.perplexity.ai/v1"

# Mirrors fetch.html_to_text's output: each link inlined in [brackets] next to
# its text, so the extractor's "job_url must appear in the page" check holds.
# Asking for URLs inline in [brackets] inside free text did not work — a 62
# posting ASC transcription came back with no links at all, so every job_url was
# dropped by the extractor's "must appear in the page" check. Structured pairs
# are reliable, and fetch_listing rebuilds the bracketed form from them.
_SCHEMA = {
    "type": "object",
    "properties": {
        "listing_text": {"type": "string"},
        "postings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
        "postings_found": {"type": "number"},
    },
    "required": ["listing_text", "postings", "postings_found"],
    "additionalProperties": False,
}

_SYSTEM = """\
You are a page-fetching proxy, not a summarizer. Fetch the requested URL(s) and
report what is actually on them. Never invent a posting, a detail or a URL, and
never drop a posting because it looks uninteresting."""


def available():
    config.load_env()
    return bool(os.environ.get("PERPLEXITY_API_KEY", "").strip())


def fetch_listing(url, expected_hint=""):
    """Return (listing_text, usage) for a page we cannot fetch ourselves."""
    config.load_env()
    client = OpenAI(api_key=os.environ["PERPLEXITY_API_KEY"], base_url=BASE_URL,
                    timeout=config.REQUEST_TIMEOUT, max_retries=config.MAX_RETRIES)
    # fetch_url only, deliberately. Adding web_search alongside it turned a
    # complete 61-posting transcription into a single posting — the model starts
    # searching and summarizing instead of reading the page it was given.
    response = client.responses.create(
        model=config.PROXY["model"],
        instructions=_SYSTEM,
        input=(
            f"Fetch {url} and all of its paginated result pages. {expected_hint}"
            f"Return listing_text = the complete listing text you received. "
            f"Return postings = one entry per posting with its title and its own "
            f"detail URL exactly as the page gives it (url = \"\" if the page "
            f"shows none — never guess or build one). "
            f"postings_found = how many postings it contains."
        ),
        tools=[{"type": "fetch_url", "max_urls": config.PROXY_MAX_URLS}],
        max_output_tokens=config.MAX_OUTPUT_TOKENS,
        reasoning={"effort": config.PROXY["effort"]},
        extra_body={"max_steps": config.PROXY_MAX_STEPS},
        text={"format": {"type": "json_schema", "name": "listing_page",
                         "strict": True, "schema": _SCHEMA}},
    )
    if response.status != "completed":
        reason = getattr(response.incomplete_details, "reason", None)
        raise RuntimeError(f"proxy fetch {response.status}: {reason or 'no detail'}")
    payload = json.loads(response.output_text)
    # Rebuild fetch.html_to_text's shape so the extractor sees each posting's URL
    # in the page text and can verify it the same way it does a page we fetched.
    lines = [payload["listing_text"]]
    for posting in payload.get("postings", []):
        title = (posting.get("title") or "").strip()
        link = (posting.get("url") or "").strip()
        if title:
            lines.append(f"{title} [{link}]" if link else title)
    listing_text = "\n".join(lines)
    u = response.usage
    cached = getattr(u.input_tokens_details, "cached_tokens", 0) or 0
    usage = {"input": u.input_tokens, "cached": cached, "output": u.output_tokens,
             "searches": sum(1 for i in response.output
                             if i.type in ("web_search_call", "fetch_url_call"))}
    return listing_text, usage

