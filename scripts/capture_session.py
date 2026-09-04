"""
Interactive login helper. Run this whenever the roster workflow starts
failing with an expired session.

It opens a real, visible browser window, lets you log in yourself
(including the Microsoft Authenticator prompt), saves the resulting
session, and -- if the GitHub CLI is available -- uploads it straight to
the EDUARTE_SESSION_STATE secret and kicks off a roster run, so the whole
recovery is one command rather than a copy-paste round trip.

This step needs a human because Summa's MFA is Authenticator push
approval and third-party authenticator apps aren't permitted, so there's
no code a script can generate. Approving on your phone is the one part
that can't be automated.

Usage:
    pip install -r requirements.txt
    playwright install chromium
    python scripts/capture_session.py              # capture, upload, run
    python scripts/capture_session.py --no-upload  # just write the file

Produces:
    session_state.json   -- cookies for your logged-in session. This is a
                             login credential. Never commit it (it's
                             gitignored).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PORTAL_URL = "https://summacollege-student.educus.nl/agenda"
STATE_PATH = Path("session_state.json")
SECRET_NAME = "EDUARTE_SESSION_STATE"
ROSTER_WORKFLOW = "update-roster.yml"


def manual_instructions() -> None:
    print(
        f"\nTo finish by hand: copy the contents of {STATE_PATH} into the "
        f"{SECRET_NAME} secret (Settings -> Secrets and variables -> Actions), "
        f"then run the roster workflow from the Actions tab."
    )


def upload(state_text: str) -> None:
    """Push the fresh session to the secret and trigger a roster run."""
    if shutil.which("gh") is None:
        print("\nGitHub CLI (gh) not found, so the secret wasn't updated.")
        print("Install it from https://cli.github.com to make this one step.")
        manual_instructions()
        return

    try:
        subprocess.run(
            ["gh", "secret", "set", SECRET_NAME],
            input=state_text,
            text=True,
            check=True,
        )
        print(f"Updated the {SECRET_NAME} secret.")
    except subprocess.CalledProcessError:
        print(f"\nCouldn't update {SECRET_NAME} (is `gh auth login` done?).")
        manual_instructions()
        return

    try:
        subprocess.run(
            ["gh", "workflow", "run", ROSTER_WORKFLOW, "--ref", "main"],
            check=True,
        )
        print("Started a roster run; the calendar should refresh shortly.")
    except subprocess.CalledProcessError:
        print(
            "\nSecret updated, but starting the roster run failed. Run it "
            "yourself from the Actions tab."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="only write session_state.json; don't touch the secret",
    )
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        print(f"Opening {PORTAL_URL} ...")
        print("Log in with your Summa Microsoft account, approve the prompt on")
        print("your phone, and wait until your timetable is visible on screen.")
        page.goto(PORTAL_URL)

        input("\nPress Enter here once your schedule is fully loaded and visible... ")

        context.storage_state(path=str(STATE_PATH))
        browser.close()

    print(f"\nSaved session to {STATE_PATH.resolve()}")

    if args.no_upload:
        manual_instructions()
        return

    upload(STATE_PATH.read_text())


if __name__ == "__main__":
    sys.exit(main())
