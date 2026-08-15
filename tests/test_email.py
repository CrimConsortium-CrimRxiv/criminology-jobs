import os
import unittest
from unittest.mock import patch

from scraper import email


SUMMARY = {
    "run_date": "2026-08-15",
    "published_total": 12,
    "verified_existing_count": 8,
    "pending_review_count": 2,
    "dropped_low_confidence_count": 3,
    "estimated_api_cost_usd": 0.42,
    "new_jobs": [
        {
            "source_site": "ACJS, ASC",
            "job_title": "Assistant Professor of Criminology",
            "institution": "Example University",
            "city_or_region": "Atlanta, GA",
            "job_url": "https://example.edu/new",
        }
    ],
    "unverified_jobs": [
        {
            "source_site": "HigherEdJobs",
            "job_title": "Research Fellow",
            "institution": "Example Institute",
            "job_url": "https://example.edu/unverified",
        }
    ],
    "source_failures": ["HigherEdJobs: sanity check failed"],
}


class EmailTests(unittest.TestCase):
    def test_message_contains_grouped_updates_and_unverified_note(self):
        message = email.build_message(
            SUMMARY,
            "sender@example.com",
            ["one@example.com", "two@example.com"],
            test=True,
        )

        plain = message.get_body(preferencelist=("plain",)).get_content()
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("[TEST] Criminology jobs refresh", message["Subject"])
        self.assertIn("ACJS (1)", plain)
        self.assertIn("ASC (1)", plain)
        self.assertIn("JOBS NOT VERIFIED IN THIS RUN", plain)
        self.assertIn("Existing jobs verified: 8", plain)
        self.assertIn("New results below confidence cutoff: 3", plain)
        self.assertIn("Estimated Anthropic API cost: $0.42", plain)
        self.assertIn("HigherEdJobs: sanity check failed", plain)
        self.assertIn("https://example.edu/new", html)

    def test_send_uses_ssl_and_accepts_comma_or_semicolon_recipients(self):
        settings = {
            "SENDER_EMAIL": "sender@example.com",
            "SENDER_PASSWORD": "test-password",
            "SENDER_SERVER": "smtp.example.com",
            "TO_EMAILS": "one@example.com; two@example.com,three@example.com",
        }
        with (
            patch.dict(os.environ, settings, clear=True),
            patch.object(email.config, "load_env"),
            patch.object(email.smtplib, "SMTP_SSL") as smtp_ssl,
        ):
            count = email.send_refresh_email(SUMMARY)

        connection = smtp_ssl.return_value.__enter__.return_value
        smtp_ssl.assert_called_once()
        connection.login.assert_called_once_with("sender@example.com", "test-password")
        connection.send_message.assert_called_once()
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
