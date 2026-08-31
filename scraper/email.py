"""Build and send the job-board refresh email.

Usage: python -m scraper.email [refresh_summary.json]
"""

import html
import json
import os
import re
import smtplib
import ssl
import sys
from collections import defaultdict
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from . import config


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUMMARY_PATH = ROOT / "refresh_summary.json"


def _required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def recipients_from_env():
    recipients = [
        address.strip()
        for address in re.split(r"[,;]", _required_env("TO_EMAILS"))
        if address.strip()
    ]
    if not recipients:
        raise RuntimeError("TO_EMAILS does not contain any email addresses")
    return recipients


def _sources(row):
    return [source.strip() for source in row.get("source_site", "").split(",") if source.strip()]


def _group_by_source(rows):
    grouped = defaultdict(list)
    for row in rows:
        sources = _sources(row) or ["Unknown source"]
        for source in sources:
            grouped[source].append(row)
    return dict(sorted(grouped.items()))


def _job_label(row):
    title = row.get("job_title") or "Untitled position"
    institution = row.get("institution") or "Institution not listed"
    location = row.get("city_or_region") or row.get("country") or ""
    return f"{title} — {institution}" + (f" ({location})" if location else "")


def _job_url(row):
    urls = row.get("combined_urls") or row.get("job_url") or ""
    return urls.split(",", 1)[0].strip()


def _text_job(row):
    label = _job_label(row)
    url = _job_url(row)
    return f"- {label}" + (f"\n  {url}" if url else "")


def _html_job(row):
    label = html.escape(_job_label(row))
    url = _job_url(row)
    if url:
        label = f'<a href="{html.escape(url, quote=True)}">{label}</a>'
    return f"<li>{label}</li>"


def build_message(summary, sender, recipients, test=False):
    new_jobs = summary.get("new_jobs", [])
    unverified_jobs = summary.get("unverified_jobs", [])
    failures = summary.get("source_failures", [])
    run_date = summary.get("run_date", "unknown date")
    pending = summary.get("pending_review_count", 0)
    verified = summary.get("verified_existing_count", 0)
    dropped_low = summary.get("dropped_low_confidence_count", 0)
    api_cost = float(summary.get("estimated_api_cost_usd", 0))

    prefix = "[TEST] " if test else ""
    message = EmailMessage()
    message["Subject"] = (
        f"{prefix}Criminology jobs refresh — {run_date} "
        f"(+{len(new_jobs)} new, {len(unverified_jobs)} unverified)"
    )
    message["From"] = formataddr(("CrimConsortium Jobs", sender))
    message["To"] = ", ".join(recipients)

    text_lines = [
        "CrimConsortium job-board refresh",
        f"Run date: {run_date}",
        f"Published jobs on board: {summary.get('published_total', 0)}",
        f"New jobs added: {len(new_jobs)}",
        f"Existing jobs verified: {verified}",
        f"Pending manual review: {pending}",
        f"New results below confidence cutoff: {dropped_low}",
        f"Existing jobs not verified this run: {len(unverified_jobs)}",
        f"Estimated Anthropic API cost: ${api_cost:.2f}",
        "Job board: https://jobs.crimconsortium.com/",
        "",
        "NEW JOBS BY BOARD",
    ]
    new_by_source = _group_by_source(new_jobs)
    if not new_by_source:
        text_lines.append("No new jobs were added.")
    for source, jobs in new_by_source.items():
        text_lines.extend(["", f"{source} ({len(jobs)})"])
        text_lines.extend(_text_job(job) for job in jobs)

    text_lines.extend([
        "",
        "JOBS NOT VERIFIED IN THIS RUN",
        (
            "These existing entries were preserved because they were not matched "
            "in the latest scrape. They may be expired, temporarily unavailable, "
            "or missed by the source extraction."
        ),
    ])
    unverified_by_source = _group_by_source(unverified_jobs)
    if not unverified_by_source:
        text_lines.append("All existing jobs were verified.")
    for source, jobs in unverified_by_source.items():
        text_lines.extend(["", f"{source} ({len(jobs)})"])
        text_lines.extend(_text_job(job) for job in jobs)

    text_lines.extend(["", "SOURCE FAILURES"])
    text_lines.extend(f"- {failure}" for failure in failures)
    if not failures:
        text_lines.append("No source failures.")
    message.set_content("\n".join(text_lines))

    html_parts = [
        "<html><body>",
        "<h2>CrimConsortium job-board refresh</h2>",
        f"<p><strong>Run date:</strong> {html.escape(str(run_date))}<br>",
        f"<strong>Published jobs on board:</strong> {summary.get('published_total', 0)}<br>",
        f"<strong>New jobs added:</strong> {len(new_jobs)}<br>",
        f"<strong>Existing jobs verified:</strong> {verified}<br>",
        f"<strong>Pending manual review:</strong> {pending}<br>",
        f"<strong>New results below confidence cutoff:</strong> {dropped_low}<br>",
        f"<strong>Existing jobs not verified this run:</strong> {len(unverified_jobs)}<br>",
        f"<strong>Estimated Anthropic API cost:</strong> ${api_cost:.2f}<br>",
        '<a href="https://jobs.crimconsortium.com/">Open the job board</a></p>',
        "<h3>New jobs by board</h3>",
    ]
    if not new_by_source:
        html_parts.append("<p>No new jobs were added.</p>")
    for source, jobs in new_by_source.items():
        html_parts.extend([
            f"<h4>{html.escape(source)} ({len(jobs)})</h4>",
            "<ul>",
            *(_html_job(job) for job in jobs),
            "</ul>",
        ])

    html_parts.extend([
        "<h3>Jobs not verified in this run</h3>",
        (
            "<p>These existing entries were preserved because they were not matched "
            "in the latest scrape. They may be expired, temporarily unavailable, "
            "or missed by the source extraction.</p>"
        ),
    ])
    if not unverified_by_source:
        html_parts.append("<p>All existing jobs were verified.</p>")
    for source, jobs in unverified_by_source.items():
        html_parts.extend([
            f"<h4>{html.escape(source)} ({len(jobs)})</h4>",
            "<ul>",
            *(_html_job(job) for job in jobs),
            "</ul>",
        ])

    html_parts.append("<h3>Source failures</h3><ul>")
    html_parts.extend(f"<li>{html.escape(str(failure))}</li>" for failure in failures)
    if not failures:
        html_parts.append("<li>No source failures.</li>")
    html_parts.extend(["</ul>", "</body></html>"])
    message.add_alternative("\n".join(html_parts), subtype="html")
    return message


def send_refresh_email(summary, test=False):
    config.load_env()
    sender = _required_env("SENDER_EMAIL")
    password = _required_env("SENDER_PASSWORD")
    server = _required_env("SENDER_SERVER")
    recipients = recipients_from_env()
    port = int(os.environ.get("SENDER_PORT", "465"))
    message = build_message(summary, sender, recipients, test=test)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(server, port, context=context, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)
    return len(recipients)


def main():
    summary_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SUMMARY_PATH
    with summary_path.open(encoding="utf-8") as handle:
        summary = json.load(handle)
    recipient_count = send_refresh_email(summary)
    print(f"Refresh email sent to {recipient_count} recipient(s).")


if __name__ == "__main__":
    main()
