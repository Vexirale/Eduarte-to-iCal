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

If the saved session has expired, the site redirects to its Microsoft
login page. We treat that as a hard failure (non-zero exit) rather than
silently producing an empty/stale calendar, so the scheduled workflow shows
up red and you know it's time to re-run capture_session.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

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


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def build_session() -> requests.Session:
    raw = os.environ.get("EDUARTE_SESSION_STATE")
    if not raw:
        die(
            "EDUARTE_SESSION_STATE is not set. Run scripts/capture_session.py "
            "locally and put session_state.json's contents into that secret."
        )
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        die("EDUARTE_SESSION_STATE does not contain valid JSON.")

    session = requests.Session()
    for cookie in state["cookies"]:
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

        event = Event()
        event.add("uid", uid)
        event.add("summary", f"⚠️ {lesson.subject}" if room_changed else lesson.subject)
        event.add("dtstart", lesson.start)
        event.add("dtend", lesson.end)
        event.add("dtstamp", datetime.now(tz=TZ))
        if lesson.room:
            event.add("location", lesson.room)

        description_parts = []
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
