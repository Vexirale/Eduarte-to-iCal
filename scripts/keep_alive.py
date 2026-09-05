"""
Touch the agenda page often enough that the Educus session never goes idle.

Educus' session is killed by *inactivity*, not by age, so a cheap request
every so often keeps it alive and saves fetch_roster.py from having to
re-establish it. Measured behaviour: the session survived while runs were
happening ~20 minutes apart, and was dead after a ~6 hour gap.

Deliberately dependency-light -- just requests, no browser, no parsing
libraries -- because this runs many times a day and installing Chromium
each time to send one GET would be absurd. That's also why the few
constants below are duplicated from fetch_roster.py rather than imported:
importing it would drag in playwright, bs4 and icalendar.

Two things this fundamentally cannot do, worth being clear about:

  * It can only keep alive the session it was given. The cookie lives in a
    secret, and a CI job can't write a fresh one back, so once the session
    does die (a delayed run, a GitHub outage, an Educus restart) every
    later ping is talking to a dead session. Recovering is
    fetch_roster.py's job, via the browser sign-in.
  * GitHub's scheduled workflows are delayed under load, often well past
    their interval. So the real gap between pings is not the cron
    interval, and if it ever exceeds the server's idle timeout the session
    is gone.

So this reduces how often the session dies. It does not stop it.

Exits 0 even when the session is dead: this runs on a tight schedule, and
a dead session failing here would mean dozens of red runs a day drowning
out the one that matters. fetch_roster.py is what fails loudly.
"""

import json
import os
import sys

import requests

BASE_URL = "https://summacollege-student.educus.nl/agenda"
LOGIN_HOSTS = ("login.educus.nl", "login.microsoftonline.com")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": BASE_URL,
    "Origin": "https://summacollege-student.educus.nl",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}


def main() -> int:
    raw = os.environ.get("EDUARTE_SESSION_STATE")
    if not raw:
        print("EDUARTE_SESSION_STATE is not set; nothing to keep alive.")
        return 0

    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        print("EDUARTE_SESSION_STATE is not valid JSON; nothing to keep alive.")
        return 0

    session = requests.Session()
    for cookie in state.get("cookies", []):
        session.cookies.set(
            cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"]
        )

    try:
        response = session.get(BASE_URL, headers=REQUEST_HEADERS, timeout=30)
    except requests.RequestException as exc:
        print(f"Ping failed ({exc.__class__.__name__}); leaving it for the next run.")
        return 0

    redirected_to_login = any(host in response.url for host in LOGIN_HOSTS)
    if redirected_to_login or "filter:datum" not in response.text:
        print(
            "Session is no longer alive -- pings can't bring it back. The next "
            "scheduled fetch_roster.py run will try to sign in again in a browser."
        )
        return 0

    print("Session still alive; idle timer reset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
