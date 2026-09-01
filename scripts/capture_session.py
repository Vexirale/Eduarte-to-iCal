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
    session_state.json   -- cookies for your logged-in session. This is a
                             login credential. Never commit it. Copy its
                             contents into the EDUARTE_SESSION_STATE GitHub
                             Actions secret instead.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PORTAL_URL = "https://summacollege-student.educus.nl/agenda"
STATE_PATH = Path("session_state.json")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"Opening {PORTAL_URL} ...")
        print("Log in with your Summa Microsoft account, complete MFA if asked,")
        print("and wait until your actual timetable/agenda is visible on screen.")
        page.goto(PORTAL_URL)

        input("\nPress Enter here once your schedule is fully loaded and visible... ")

        context.storage_state(path=str(STATE_PATH))
        print(f"\nSaved session to {STATE_PATH.resolve()}")
        print("Copy its contents into the EDUARTE_SESSION_STATE GitHub Actions secret.")

        browser.close()


if __name__ == "__main__":
    sys.exit(main())
