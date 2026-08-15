import datetime
import json

from anthropic import Anthropic

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

_SEARCH_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search",
     "max_uses": config.SEARCH_MAX_SEARCHES, "allowed_callers": ["direct"]},
    {"type": "web_fetch_20260209", "name": "web_fetch",
     "allowed_callers": ["direct"]},
]


def _call(user_content, profile, tools=None):
    """Run one extraction request; returns (job dicts, usage dict)."""
    config.load_env()
    client = Anthropic()
    messages = [{"role": "user", "content": user_content}]
    usage = {"input": 0, "output": 0, "searches": 0}
    output_config = {"format": {"type": "json_schema", "schema": _SCHEMA}}
    if profile.get("effort"):
        output_config["effort"] = profile["effort"]  # omitted for models like Haiku
    for _ in range(5):  # server-tool turns can pause; resume until finished
        with client.messages.stream(
            model=profile["model"],
            max_tokens=config.MAX_OUTPUT_TOKENS,
            system=_SYSTEM,
            messages=messages,
            output_config=output_config,
            **({"tools": tools} if tools else {}),
        ) as stream:
            message = stream.get_final_message()
        u = message.usage
        usage["input"] += (u.input_tokens + (u.cache_read_input_tokens or 0)
                          + (u.cache_creation_input_tokens or 0))
        usage["output"] += u.output_tokens
        server_tools = getattr(u, "server_tool_use", None)
        if server_tools is not None:
            usage["searches"] += getattr(server_tools, "web_search_requests", 0) or 0
        if message.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": message.content}]
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"output truncated at {config.MAX_OUTPUT_TOKENS} tokens")
    payload = next(b.text for b in message.content if b.type == "text")
    return json.loads(payload)["jobs"], usage


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


def extract_jobs_via_search(source_name, listing_url, expected_count=0, profile=None):
    """Bot-walled path: Claude's server-side web search/fetch enumerates the
    listings, since the site blocks our own downloads."""
    expectation = (
        f"The board currently tracks roughly {expected_count} listings from "
        f"this source, so a comparable number likely exists now. "
    ) if expected_count else ""
    return _call(
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
    )  # returns (jobs, usage)
