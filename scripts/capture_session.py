"""
One-time interactive login helper.

Run this LOCALLY (not in CI) whenever your Eduarte session has expired.
It opens a real, visible browser window, lets you log in yourself
(including any Microsoft MFA prompt), and then saves the resulting
session so the daily automation can reuse it without you.

Usage:
    pip install -r requirements.txt
    playwright install chromium
    python scripts/capture_session.py

Produces:
    session_state.json   -- Playwright storage state (cookies etc).
                             This is a login credential. Never commit it.
                             Copy its contents into the EDUARTE_SESSION_STATE
                             GitHub Actions secret instead.
    debug_capture.json    -- Only written the first time, while we're still
                             figuring out the real API shape. Every JSON
                             network response whose URL looks schedule-related
                             gets dumped here so we can inspect the real
                             field names instead of guessing them.
"""

import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PORTAL_URL = "https://summacollege-student.educus.nl/agenda"
STATE_PATH = Path("session_state.json")
DEBUG_PATH = Path("debug_capture.json")

# Loose net: anything that smells like schedule data. We filter for real
# once we've seen what the actual endpoint(s) look like.
INTERESTING_URL = re.compile(r"(agenda|rooster|schedule|lesson|les|planning)", re.I)


def main() -> None:
    captures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        def on_response(response):
            url = response.url
            if not INTERESTING_URL.search(url):
                return
            ctype = response.headers.get("content-type", "")
            if "json" not in ctype:
                return
            try:
                body = response.json()
            except Exception:
                return
            captures.append({"url": url, "status": response.status, "body": body})
            print(f"[captured] {response.status} {url}")

        page.on("response", on_response)

        print(f"Opening {PORTAL_URL} ...")
        print("Log in with your Summa Microsoft account, complete MFA if asked,")
        print("and wait until your actual timetable/agenda is visible on screen.")
        page.goto(PORTAL_URL)

        input("\nPress Enter here once your schedule is fully loaded and visible... ")

        # Give the app a moment to fire any lazy-loaded requests, and nudge it
        # by clicking around (next week) so we also capture pagination calls.
        page.wait_for_timeout(1000)
        for label in ("volgende", "next", ">"):
            try:
                page.get_by_text(label, exact=False).first.click(timeout=1500)
                page.wait_for_timeout(1500)
                break
            except Exception:
                continue

        context.storage_state(path=str(STATE_PATH))
        print(f"\nSaved session to {STATE_PATH.resolve()}")

        if captures:
            DEBUG_PATH.write_text(json.dumps(captures, indent=2, ensure_ascii=False))
            print(f"Saved {len(captures)} captured API response(s) to {DEBUG_PATH.resolve()}")
            print("Share this file so the parser can be built against the real data.")
        else:
            print(
                "No matching JSON responses were captured. The schedule is probably "
                "rendered server-side or fetched under a URL this script didn't "
                "recognise -- that's fine, tell me and we'll widen the net."
            )

        browser.close()


if __name__ == "__main__":
    sys.exit(main())
