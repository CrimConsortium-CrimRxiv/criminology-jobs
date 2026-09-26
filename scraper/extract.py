"""Extract structured job rows from board content using the OpenAI Responses API."""

import datetime
import json
import urllib.parse

from openai import OpenAI

from . import config

# The 15 job fields the LLM fills, plus confidence + reason
_JOB_PROPS = {
    "job_title": {"type": "string"},
    "institution": {"type": "string"},
    "department_or_school": {"type": "string"},
    "country": {"type": "string"},
    "city_or_region": {"type": "string"},
    "rank_type": {"type": "string"},
    "area_specialization": {"type": "string"},
    "contract_type": {"type": "string"},
    "teaching_expectations": {"type": "string"},
    "research_expectations": {"type": "string"},
    "posted_date": {"type": "string"},
    "deadline_or_review_date": {"type": "string"},
    "salary_currency": {"type": "string"},
    "salary_range": {"type": "string"},
    "job_url": {"type": "string"},
    "confidence": {"type": "number"},
    "reason": {"type": "string"},
}

# Strict json_schema: every field required, no extra properties at either level.
_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _JOB_PROPS,
                "required": list(_JOB_PROPS),
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}

_SYSTEM = f"""\
You extract job listings from job-board content for a criminology jobs
dashboard. Extract EVERY distinct job listing. For each job fill the schema
fields; use "" for anything the source does not state — never guess or invent
values. Dates as YYYY-MM-DD when given.

{config.CRITERIA}"""


# Server-side search for the bot-walled sources. The Responses API runs the tool
# loop itself, so one create() call returns the finished answer.
# return_token_budget "unlimited" is load-bearing: under the default budget the
# model runs out of room mid-enumeration and returns no jobs at all for these
# boards. The search is deliberately NOT domain-filtered — these listings are
# only reachable through the wider web, and restricting it to the source's own
# domain returns nothing at all. _keep_own_urls guards the output instead.
_SEARCH_TOOLS = [{
    "type": "web_search",
    "search_context_size": "high",
    "return_token_budget": "unlimited",
}]


def _keep_own_urls(jobs, domain):
    """Blank any job_url that is not on the source's own domain.

    Enumerating a walled board through general search turns up the same posting
    on mirror and aggregator sites, and the model will happily report one of
    those as the listing's URL. A wrong link is worse than none, and the board
    already carries these rows with an empty job_url."""
    if not domain:
        return jobs
    for job in jobs:
        host = urllib.parse.urlparse(job.get("job_url", "")).hostname or ""
        if host != domain and not host.endswith("." + domain):
            job["job_url"] = ""
    return jobs


def _call(user_content, profile, tools=None):
    """Run one extraction request; returns (job dicts, usage dict)."""
    config.load_env()
    client = OpenAI(timeout=config.REQUEST_TIMEOUT, max_retries=config.MAX_RETRIES)
    request = {
        "model": profile["model"],
        "instructions": _SYSTEM,
        "input": user_content,
        "max_output_tokens": config.MAX_OUTPUT_TOKENS,
        "text": {"format": {"type": "json_schema", "name": "criminology_jobs",
                            "strict": True, "schema": _SCHEMA}},
    }
    if profile.get("effort"):
        request["reasoning"] = {"effort": profile["effort"]}
    if tools:
        request["tools"] = tools
        request["max_tool_calls"] = config.SEARCH_MAX_SEARCHES
    response = client.responses.create(**request)

    if response.status != "completed":
        reason = getattr(response.incomplete_details, "reason", None)
        if reason == "max_output_tokens":
            raise RuntimeError(f"output truncated at {config.MAX_OUTPUT_TOKENS} tokens")
        raise RuntimeError(f"response {response.status}: {reason or 'no detail given'}")

    u = response.usage
    # input_tokens is the total; cached tokens are a discounted subset of it.
    cached = getattr(u.input_tokens_details, "cached_tokens", 0) or 0
    usage = {
        "input": u.input_tokens,
        "cached": cached,
        "output": u.output_tokens,
        "searches": sum(1 for item in response.output if item.type == "web_search_call"),
    }
    return json.loads(response.output_text)["jobs"], usage


def extract_jobs(source_name, text):
    """Fetched-page path: extract jobs from page text we downloaded ourselves."""
    return _call(
        f"Source site: {source_name}\n"
        f"Today's date: {datetime.date.today().isoformat()} "
        f"(use it to resolve relative dates like 'Posted 4 days ago')\n\n"
        f"job_url must be a URL that actually appears in the text (they are "
        f"inlined in [brackets]); pick the one linking to that job's detail "
        f"page.\n\nPage text:\n\n{text}",
        config.EXTRACT,
    )  # returns (jobs, usage)


def extract_jobs_via_search(source_name, listing_url, expected_count=0, profile=None,
                            domain=None):
    """Bot-walled path: the model's server-side web search enumerates the
    listings, since the site blocks our own downloads."""
    expectation = (
        f"The board currently tracks roughly {expected_count} listings from "
        f"this source, so a comparable number likely exists now. "
    ) if expected_count else ""
    jobs, usage = _call(
        f"Source site: {source_name}\n"
        f"Today's date: {datetime.date.today().isoformat()}\n\n"
        f"Enumerate EVERY job posting currently listed in this job-board "
        f"category:\n{listing_url}\n\n"
        f"The direct fetch of that page is usually blocked by bot protection — "
        f"if so, the site's individual job-detail pages ARE indexed by search "
        f"engines, so enumerate them via web search. Run MANY searches with "
        f"varied queries (different role words: professor, lecturer, faculty, "
        f"instructor, postdoc, criminology, criminal justice; site-specific "
        f"queries) and keep going until new searches stop surfacing listings "
        f"you haven't already collected. {expectation}One or two searches is "
        f"not enough. Only include postings on the source site itself, not "
        f"other job boards that appear in results. Report only real postings "
        f"you actually saw in a fetch or search result — finding fewer than "
        f"expected is acceptable, inventing or padding is not. job_url should "
        f"be the listing's page on the source site.",
        profile or config.SEARCH,
        tools=_SEARCH_TOOLS,
    )
    return _keep_own_urls(jobs, domain), usage
