"""
Daily job: reuse a previously captured session to pull the Summa/Eduarte
timetable for the next few weeks and write it out as roster.ics.

Requires a saved session (see capture_session.py). In CI this is read from
the EDUARTE_SESSION_STATE environment variable (the raw JSON content of
session_state.json, e.g. from a GitHub Actions secret).

How the site actually works (reverse engineered from a real logged-in
session -- there is no documented API):

The agenda page is server-rendered by Apache Wicket
(nl.topicus.eduario), not a JSON SPA. Each lesson is an <li
class="js-agenda-detail"> carrying only the subject/room/teacher text; its
start time and duration are encoded as an inline "top"/"height" (in
pixels) CSS rule keyed by the li's id, positioned against a shared hour
axis (.agenda--time) whose first visible label is the day's start hour.
Reverse engineered scale: 108px == 1 hour (from the site's own
".agenda--time li { height: 108px }" rule). Paging to the next week is a
plain link (a.time-filter--right) that must be re-read from each response
in turn (Wicket bumps a version counter in the URL every time), and the
displayed week's Monday is available as the "filter:datum" field so day
dates don't need to be guessed from abbreviated, year-less text like
"ma 7 sep".

Signing in is handled by a headless browser (see build_session) because
the app's own session cookie only survives a few hours of inactivity,
while the Microsoft side of the login stays valid for months. If even the
Microsoft session is gone, we fail hard (non-zero exit) rather than
silently producing an empty/stale calendar, so the scheduled workflow
shows up red and you know it's time to re-run capture_session.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import pyotp
import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from playwright.sync_api import TimeoutError as PlaywrightTimeout, sync_playwright

BASE_URL = "https://summacollege-student.educus.nl/agenda"
LOGIN_HOSTS = ("login.educus.nl", "login.microsoftonline.com")
TZ = ZoneInfo("Europe/Amsterdam")
WEEKS_AHEAD = int(os.environ.get("WEEKS_AHEAD", "4"))
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", "docs/roster.ics"))

# Reverse engineered from the site's own CSS (.agenda--time li { height: 108px }).
PX_PER_HOUR = 108

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Origin": "https://summacollege-student.educus.nl",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}


@dataclass
class Lesson:
    subject: str
    teacher: str | None
    room: str | None
    start: datetime
    end: datetime
    cancelled: bool = False


# "vervallen" is the standard Dutch term for a cancelled lesson across
# school scheduling systems (Zermelo, Magister, and this one included), but
# this hasn't actually been confirmed against a real cancelled lesson on
# this site -- there wasn't one in any captured session. Checked against the
# li's own classes, any descendant's classes, and its text, to allow for
# whichever of those the site actually uses. Worst case if this doesn't
# match the real markup: cancellations just don't get flagged, nothing
# breaks -- fix the marker list here once you've seen a real one.
CANCELLED_MARKERS = ("vervallen", "geannuleerd", "afgelast", "cancelled", "canceled")


def is_cancelled(li) -> bool:
    def has_marker(classes) -> bool:
        joined = " ".join(classes).lower()
        return any(marker in joined for marker in CANCELLED_MARKERS)

    if has_marker(li.get("class", [])):
        return True
    if any(has_marker(el.get("class", [])) for el in li.find_all(True)):
        return True
    text = li.get_text(" ", strip=True).lower()
    return any(marker in text for marker in CANCELLED_MARKERS)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


SIGNED_IN_URL = re.compile(r"educus\.nl/agenda")


def _visible(page, selector: str, timeout: int = 2500):
    """Return the locator if it's actually on screen right now, else None."""
    locator = page.locator(selector).first
    try:
        locator.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeout:
        return None
    return locator


# Microsoft's own wording for "the app is asking you to approve on your phone".
PUSH_PROMPT_TEXT = re.compile(
    r"approve|goedkeur|open your|authenticator app|verifi(?:cation|catie)verzoek|"
    r"enter the number|nummer",
    re.I,
)
# The escape hatch on that screen, and the option to pick once it opens.
OTHER_WAY_SELECTORS = "#idA_PWD_SwitchToCredPicker, #signInAnotherWay"
OTHER_WAY_TEXT = re.compile(
    r"can't use|kan .*niet gebruiken|another way|andere manier|different (?:method|way)",
    re.I,
)
VERIFICATION_CODE_TEXT = re.compile(
    r"verification code|verificatiecode|use a code|code from",
    re.I,
)


def _looks_like_push_prompt(page) -> bool:
    """True when the screen is an approve-on-your-phone prompt, not a form."""
    if _visible(page, 'input[name="otc"], input[type="password"]', timeout=600):
        return False
    try:
        body = page.inner_text("body", timeout=2000)
    except PlaywrightTimeout:
        return False
    return bool(PUSH_PROMPT_TEXT.search(body))


def _click_first(page, *candidates) -> bool:
    """Click the first candidate that's actually there. Each is a callable."""
    for candidate in candidates:
        try:
            locator = candidate()
            locator.wait_for(state="visible", timeout=1500)
            locator.click()
            return True
        except Exception:
            continue
    return False


def _switch_to_verification_code(page) -> bool:
    """Get from the push prompt to the 'enter a code' field.

    Two clicks in Microsoft's UI: an escape-hatch link, then picking the
    verification-code option from the list it opens. Selectors are matched
    both by id and by wording (English and Dutch) because the id names have
    moved around over the years and the portal renders in Dutch here.
    """
    opened = _click_first(
        page,
        lambda: page.locator(OTHER_WAY_SELECTORS).first,
        lambda: page.get_by_role("link", name=OTHER_WAY_TEXT).first,
        lambda: page.get_by_text(OTHER_WAY_TEXT).first,
    )
    if not opened:
        return False
    page.wait_for_load_state("networkidle")

    return _click_first(
        page,
        lambda: page.get_by_text(VERIFICATION_CODE_TEXT).first,
        lambda: page.get_by_role("button", name=VERIFICATION_CODE_TEXT).first,
    )


def sign_in(page, email: str | None, password: str | None, totp_secret: str | None) -> str | None:
    """Complete whichever login steps Microsoft actually asks for.

    The flow isn't a fixed sequence: depending on what the saved session
    still covers, Microsoft may ask for everything (email, password, 2FA
    code), only some of it, or nothing at all. So rather than marching
    through fixed steps, look at what's on screen each time round and
    handle just that -- the same approach reneax/eduarte-bot uses.

    Returns None on success, or a string explaining what it got stuck on, so
    the caller can say something more useful than "still on the login page".
    """
    for _ in range(15):
        if SIGNED_IN_URL.search(page.url):
            return

        # Already-known account tile ("pick an account").
        if email:
            tile = _visible(page, f'div[data-test-id="{email}"]', timeout=1200)
            if tile:
                tile.click()
                page.wait_for_load_state("networkidle")
                continue

        # 2FA code. Checked before password: when a saved session covers the
        # password but not the second factor, this is the only field shown.
        otc = _visible(page, 'input[name="otc"]', timeout=1200)
        if otc:
            if not totp_secret:
                die(
                    "Microsoft is asking for a 2FA code and no TOTP_SECRET is set. "
                    "Add the TOTP secret as a secret, or re-run capture_session.py "
                    "locally and refresh EDUARTE_SESSION_STATE."
                )
            otc.fill(pyotp.TOTP(totp_secret).now())
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle")
            continue

        # Microsoft usually defaults to a push / number-match prompt ("Approve
        # sign-in request"), which a script fundamentally cannot answer -- that
        # approval happens on the phone. If a TOTP secret is configured, switch
        # over to the "enter a verification code" option, which lands on the
        # otc field handled above.
        if _looks_like_push_prompt(page):
            hint = (
                "Microsoft is asking for approval in the Authenticator app. That "
                "can't be automated -- approval happens on your phone. "
            )
            hint += (
                "Set TOTP_SECRET (Authenticator also shows a 6-digit verification "
                "code; get its seed at mysignins.microsoft.com/security-info by "
                "adding a 'different authenticator app' and using \"Can't scan "
                "image?\" to read the secret)."
                if not totp_secret
                else "Tried to switch to the verification-code option and could "
                "not find it -- the account may be restricted to push approval."
            )
            if totp_secret and _switch_to_verification_code(page):
                page.wait_for_load_state("networkidle")
                continue
            return hint

        pwd = _visible(page, 'input[type="password"]', timeout=1200)
        if pwd:
            if not password:
                die(
                    "Microsoft is asking for a password and EDUARTE_PASSWORD is not "
                    "set. Add it as a secret, or re-run capture_session.py locally "
                    "and refresh EDUARTE_SESSION_STATE."
                )
            pwd.fill(password)
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle")
            continue

        mail = _visible(page, 'input[type="email"], input[name="loginfmt"]', timeout=1200)
        if mail:
            if not email:
                die(
                    "Microsoft is asking who's signing in and EDUARTE_EMAIL is not "
                    "set. Add it as a secret, or re-run capture_session.py locally "
                    "and refresh EDUARTE_SESSION_STATE."
                )
            mail.fill(email)
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle")
            continue

        # "Stay signed in?" -- saying yes is what makes the session last.
        kmsi = _visible(page, "#idSIButton9", timeout=1200)
        if kmsi:
            kmsi.click()
            page.wait_for_load_state("networkidle")
            continue

        # Nothing actionable on screen; give any in-flight redirect a moment.
        try:
            page.wait_for_url(SIGNED_IN_URL, timeout=8000)
            return
        except PlaywrightTimeout:
            break


def build_session() -> requests.Session:
    """Get a requests session that's actually logged in to Educus.

    The app's own session cookie (JSESSIONID) is a server-side session with a
    short idle timeout -- it dies within hours of not being used, so between
    two scheduled runs it's always dead. The Microsoft side of the login,
    though, stays valid for months (a persistent auth cookie).

    So rather than replaying the saved cookies directly, we hand them to a
    real headless browser and let it re-do the OAuth/SAML handshake. Because
    Microsoft still considers the account signed in, that usually completes
    silently with no password and no MFA prompt. It has to be a browser: the
    handoff is driven by Microsoft's client-side JavaScript, and replaying it
    with plain HTTP just bounces between redirects forever.

    If the saved session isn't enough any more, and credentials are
    configured, sign_in() fills in whatever Microsoft still asks for so the
    job can recover on its own instead of waiting for a fresh capture.

    Once the browser is through, its fresh cookies are handed to requests and
    the rest of the scrape runs over plain HTTP as before.
    """
    raw = os.environ.get("EDUARTE_SESSION_STATE")
    email = os.environ.get("EDUARTE_EMAIL") or None
    password = os.environ.get("EDUARTE_PASSWORD") or None
    totp_secret = os.environ.get("TOTP_SECRET") or None

    if not raw and not (email and password):
        die(
            "No way to sign in. Set EDUARTE_SESSION_STATE (from "
            "scripts/capture_session.py), or EDUARTE_EMAIL + EDUARTE_PASSWORD "
            "(plus TOTP_SECRET if the account uses an authenticator app)."
        )

    state_path = None
    if raw:
        try:
            json.loads(raw)
        except json.JSONDecodeError:
            die("EDUARTE_SESSION_STATE does not contain valid JSON.")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write(raw)
            state_path = handle.name

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(storage_state=state_path)
            page = context.new_page()
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)

            # The silent SSO hop can take a couple of redirects to settle.
            hint = None
            try:
                page.wait_for_url(SIGNED_IN_URL, timeout=20000)
            except PlaywrightTimeout:
                hint = sign_in(page, email, password, totp_secret)

            page.wait_for_load_state("networkidle")
            final_url = page.url
            cookies = context.cookies()
            browser.close()
    finally:
        if state_path:
            os.unlink(state_path)

    if any(host in final_url for host in LOGIN_HOSTS):
        die(
            hint
            or (
                "Sign-in did not complete -- still stuck on the login page. If no "
                "credentials are configured, set EDUARTE_EMAIL / EDUARTE_PASSWORD "
                "(and TOTP_SECRET if 2FA is on); otherwise Microsoft may be "
                "challenging this sign-in, and a fresh capture_session.py run plus "
                "an updated EDUARTE_SESSION_STATE is the way back in."
            )
        )

    session = requests.Session()
    for cookie in cookies:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
    return session


def element_text(node) -> str | None:
    if node is None:
        return None
    text = node.get_text(strip=True)
    return text or None


def parse_positions(soup: BeautifulSoup) -> dict[str, tuple[int, int]]:
    """id -> (top_px, height_px) for every #id { top: Npx; ...; height: Mpx; } rule."""
    positions = {}
    for style_tag in soup.find_all("style"):
        css = style_tag.string or ""
        for m in re.finditer(r"#(\w+)\s*\{[^}]*top:\s*(\d+)px;[^}]*height:\s*(\d+)px", css):
            positions[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return positions


def parse_week(html: str, current_url: str) -> tuple[list[Lesson], str | None]:
    soup = BeautifulSoup(html, "html.parser")

    if any(host in current_url for host in LOGIN_HOSTS):
        die(
            "Saved session was rejected and Educus redirected to its login "
            "page. The session has expired -- re-run scripts/capture_session.py "
            "locally and update the EDUARTE_SESSION_STATE secret."
        )

    datum_input = soup.find("input", attrs={"name": "filter:datum"})
    if datum_input is None or not datum_input.get("value"):
        die("Could not find the 'filter:datum' field -- the page layout may have changed.")
    # 'filter:datum' is only Monday-aligned after paging forward/back at least
    # once -- on the initial (current-week) page it holds *today's* date
    # instead, whatever weekday that is. Derive Monday from its weekday so
    # both cases line up with the 7 day columns.
    filter_date = datetime.strptime(datum_input["value"], "%d-%m-%Y").date()
    week_start: date = filter_date - timedelta(days=filter_date.weekday())

    axis_span = soup.select_one(".agenda--time li span")
    if axis_span is None:
        die("Could not find the hour axis (.agenda--time) -- the page layout may have changed.")
    axis_hour, axis_minute = (int(part) for part in axis_span.get_text(strip=True).split(":"))

    positions = parse_positions(soup)

    lessons: list[Lesson] = []
    for day_index, day_div in enumerate(soup.select(".agenda--day")):
        day_date = week_start + timedelta(days=day_index)
        for li in day_div.select("li.js-agenda-detail"):
            li_id = li.get("id")
            position = positions.get(li_id)
            if position is None:
                continue
            top_px, height_px = position
            start_dt = datetime.combine(day_date, dt_time(axis_hour, axis_minute), tzinfo=TZ)
            start_dt += timedelta(minutes=round(top_px / PX_PER_HOUR * 60))
            end_dt = start_dt + timedelta(minutes=round(height_px / PX_PER_HOUR * 60))

            lessons.append(
                Lesson(
                    subject=element_text(li.select_one(".is-subject")) or "Les",
                    room=element_text(li.select_one(".is-location")),
                    teacher=element_text(li.select_one(".is-participant")),
                    start=start_dt,
                    end=end_dt,
                    cancelled=is_cancelled(li),
                )
            )

    next_link = soup.select_one("a.time-filter--right")
    next_href = next_link.get("href") if next_link else None
    next_url = urljoin(current_url, next_href) if next_href else None
    return lessons, next_url


def fetch_lessons(session: requests.Session) -> list[Lesson]:
    all_lessons: list[Lesson] = []
    url = BASE_URL
    referer = BASE_URL

    for _ in range(WEEKS_AHEAD):
        response = session.get(url, headers={**REQUEST_HEADERS, "Referer": referer}, timeout=30)
        response.raise_for_status()
        lessons, next_url = parse_week(response.text, response.url)
        all_lessons.extend(lessons)
        if not next_url:
            break
        referer = response.url
        url = next_url

    return all_lessons


def lesson_uid(lesson: Lesson) -> str:
    # Deliberately excludes room: a lesson's identity is its subject + time
    # slot, not where it happens to be held. Keeping the UID stable across a
    # room change is what lets both a calendar app update the existing event
    # in place (instead of dropping one and adding another) and this script
    # detect the change by comparing against the previously published feed.
    uid_source = f"{lesson.subject}|{lesson.start.isoformat()}|{lesson.end.isoformat()}"
    return hashlib.sha1(uid_source.encode()).hexdigest() + "@eduarte-to-ical"


def load_previous_locations(path: Path) -> dict[str, str | None]:
    if not path.exists():
        return {}
    try:
        previous = Calendar.from_ical(path.read_bytes())
    except ValueError:
        return {}

    locations: dict[str, str | None] = {}
    for component in previous.walk("VEVENT"):
        uid = str(component.get("uid", ""))
        location = component.get("location")
        locations[uid] = str(location) if location else None
    return locations


def build_calendar(lessons: list[Lesson], previous_locations: dict[str, str | None]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//eduarte-to-ical//summacollege//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Summa rooster")
    cal.add("x-wr-timezone", "Europe/Amsterdam")
    cal.add("method", "PUBLISH")

    for lesson in lessons:
        uid = lesson_uid(lesson)
        previous_room = previous_locations.get(uid)
        room_changed = bool(previous_room) and bool(lesson.room) and previous_room != lesson.room

        prefix = ""
        if lesson.cancelled:
            prefix += "❌ "
        if room_changed:
            prefix += "⚠️ "

        event = Event()
        event.add("uid", uid)
        event.add("summary", f"{prefix}{lesson.subject}")
        event.add("dtstart", lesson.start)
        event.add("dtend", lesson.end)
        event.add("dtstamp", datetime.now(tz=TZ))
        event.add("status", "CANCELLED" if lesson.cancelled else "CONFIRMED")
        if lesson.room:
            event.add("location", lesson.room)

        description_parts = []
        if lesson.cancelled:
            description_parts.append("Cancelled")
        if room_changed:
            description_parts.append(f"Location changed: {previous_room} → {lesson.room}")
        if lesson.teacher:
            description_parts.append(f"Docent: {lesson.teacher}")
        if description_parts:
            event.add("description", "\n".join(description_parts))

        cal.add_component(event)

    return cal


def main() -> None:
    session = build_session()
    lessons = fetch_lessons(session)

    if not lessons:
        die(
            "Logged in fine but found zero lessons across the fetched weeks. "
            "Either the timetable is genuinely empty, or the page layout "
            "changed since this scraper was written."
        )

    previous_locations = load_previous_locations(OUTPUT_PATH)
    calendar = build_calendar(lessons, previous_locations)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(calendar.to_ical())
    print(f"Wrote {len(lessons)} lessons to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
