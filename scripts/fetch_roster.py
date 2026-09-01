"""
Daily job: log in to Eduarte using a previously captured session, pull the
timetable for the next few weeks, and write it out as roster.ics.

Requires a saved Playwright session (see capture_session.py). In CI this is
read from the EDUARTE_SESSION_STATE environment variable (the raw JSON
content of session_state.json, e.g. from a GitHub Actions secret).

If the saved session has expired, Educus bounces us back to its Microsoft
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
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event
from playwright.sync_api import sync_playwright

PORTAL_URL = "https://summacollege-student.educus.nl/agenda"
LOGIN_HOSTS = ("login.educus.nl", "login.microsoftonline.com")
TZ = ZoneInfo("Europe/Amsterdam")
WEEKS_AHEAD = int(os.environ.get("WEEKS_AHEAD", "4"))
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", "docs/roster.ics"))

INTERESTING_URL = re.compile(r"(agenda|rooster|schedule|lesson|les|planning)", re.I)

# Candidate field names to try, in order, for each piece of a lesson.
# Educus/Eduarte responses are typically Dutch; keep this list easy to extend
# once we've seen the real payload.
FIELD_CANDIDATES = {
    "subject": ["vak", "vaknaam", "vakNaam", "onderwerp", "subject", "naam", "omschrijving", "title"],
    "teacher": ["docent", "docentNaam", "docenten", "teacher", "medewerker", "personeel"],
    "room": ["lokaal", "ruimte", "ruimteNaam", "room", "locatie", "location"],
    "group": ["klas", "groep", "groepNaam", "group", "class"],
    "start": ["start", "begin", "beginDatumTijd", "startDatumTijd", "startTime", "van"],
    "end": ["eind", "einde", "eindDatumTijd", "endDatumTijd", "endTime", "tot"],
}


@dataclass
class Lesson:
    subject: str
    teacher: str | None
    room: str | None
    group: str | None
    start: datetime
    end: datetime


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_session_state() -> Path:
    raw = os.environ.get("EDUARTE_SESSION_STATE")
    if not raw:
        die(
            "EDUARTE_SESSION_STATE is not set. Run scripts/capture_session.py "
            "locally and put session_state.json's contents into that secret."
        )
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(raw)
    return Path(path)


def first_matching(node: dict, keys: list[str]):
    lower_map = {k.lower(): k for k in node.keys()}
    for candidate in keys:
        real_key = lower_map.get(candidate.lower())
        if real_key is not None and node[real_key] not in (None, ""):
            return node[real_key]
    return None


def parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # epoch millis or seconds
        seconds = value / 1000 if value > 10**12 else value
        return datetime.fromtimestamp(seconds, tz=TZ)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    return None


def looks_like_lesson(node: dict) -> bool:
    lower_keys = {k.lower() for k in node.keys()}
    has_subject = any(c.lower() in lower_keys for c in FIELD_CANDIDATES["subject"])
    has_start = any(c.lower() in lower_keys for c in FIELD_CANDIDATES["start"])
    return has_subject and has_start


def extract_lessons(payload) -> list[Lesson]:
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if looks_like_lesson(node):
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)

    lessons = []
    for node in found:
        start = parse_datetime(first_matching(node, FIELD_CANDIDATES["start"]))
        end = parse_datetime(first_matching(node, FIELD_CANDIDATES["end"]))
        subject = first_matching(node, FIELD_CANDIDATES["subject"])
        if not start or not subject:
            continue
        if not end:
            end = start + timedelta(hours=1)
        lessons.append(
            Lesson(
                subject=str(subject),
                teacher=_stringify(first_matching(node, FIELD_CANDIDATES["teacher"])),
                room=_stringify(first_matching(node, FIELD_CANDIDATES["room"])),
                group=_stringify(first_matching(node, FIELD_CANDIDATES["group"])),
                start=start,
                end=end,
            )
        )
    return lessons


def _stringify(value):
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def fetch_lessons(session_state_path: Path) -> list[Lesson]:
    captured_payloads = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(session_state_path))
        page = context.new_page()

        def on_response(response):
            if not INTERESTING_URL.search(response.url):
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                captured_payloads.append(response.json())
            except Exception:
                pass

        page.on("response", on_response)

        page.goto(PORTAL_URL, wait_until="networkidle")

        if any(host in page.url for host in LOGIN_HOSTS):
            browser.close()
            die(
                "Saved session was rejected and Educus redirected to its login "
                "page. The session has expired -- re-run scripts/capture_session.py "
                "locally and update the EDUARTE_SESSION_STATE secret."
            )

        for _ in range(WEEKS_AHEAD - 1):
            advanced = False
            for label in ("volgende", "next", ">"):
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=1500)
                    page.wait_for_load_state("networkidle")
                    advanced = True
                    break
                except Exception:
                    continue
            if not advanced:
                break

        browser.close()

    all_lessons: list[Lesson] = []
    for payload in captured_payloads:
        all_lessons.extend(extract_lessons(payload))

    # de-dupe (pagination/re-renders can repeat the same lesson)
    seen = set()
    unique = []
    for lesson in all_lessons:
        key = (lesson.subject, lesson.start.isoformat(), lesson.end.isoformat())
        if key in seen:
            continue
        seen.add(key)
        unique.append(lesson)

    return unique


def build_calendar(lessons: list[Lesson]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//eduarte-to-ical//summacollege//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Summa rooster")
    cal.add("x-wr-timezone", "Europe/Amsterdam")
    cal.add("method", "PUBLISH")

    for lesson in lessons:
        event = Event()
        uid_source = f"{lesson.subject}|{lesson.start.isoformat()}|{lesson.end.isoformat()}"
        event.add("uid", hashlib.sha1(uid_source.encode()).hexdigest() + "@eduarte-to-ical")
        event.add("summary", lesson.subject)
        event.add("dtstart", lesson.start)
        event.add("dtend", lesson.end)
        event.add("dtstamp", datetime.now(tz=TZ))
        if lesson.room:
            event.add("location", lesson.room)
        description_parts = []
        if lesson.teacher:
            description_parts.append(f"Docent: {lesson.teacher}")
        if lesson.group:
            description_parts.append(f"Groep: {lesson.group}")
        if description_parts:
            event.add("description", "\n".join(description_parts))
        cal.add_component(event)

    return cal


def main() -> None:
    session_state_path = load_session_state()
    lessons = fetch_lessons(session_state_path)

    if not lessons:
        die(
            "Logged in fine but found zero lessons. Either the timetable is "
            "genuinely empty, or the field-name heuristics in FIELD_CANDIDATES "
            "no longer match the real payload -- check a fresh debug_capture.json."
        )

    calendar = build_calendar(lessons)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(calendar.to_ical())
    print(f"Wrote {len(lessons)} lessons to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
