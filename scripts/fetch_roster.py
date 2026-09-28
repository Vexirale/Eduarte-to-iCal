"""
Pull the Summa/Eduarte timetable from the Eduarte Student app's own REST API
and write it out as roster.ics.

How it signs in: the app logs in once through login.educus.nl (OAuth2 +
PKCE, see app_login.py) and then stays signed in with a refresh token. That
refresh is a plain POST to login.educus.nl and never touches Microsoft, so
there's no MFA prompt and no one-hour web session to outlive. The refresh
token lasts 60 days and rotates: every refresh hands out a new one, which
this script saves straight away (to tokens.json locally, or back into the
EDUARTE_REFRESH_TOKEN secret in CI) before it does anything else.

Where the data comes from (found in the app itself):
  /account/me                            -> your deelnemer id
  /afspraak?deelnemer=..&isoWeek=..      -> the week's lessons (paged)
  /deelnemer/{id}/afspraakwijzigingen    -> cancellations, room and time changes

Privacy: every lesson the API returns also lists all classmates in it
(names, birth dates, photos). This script reads only the lesson's own
fields and the teacher's abbreviation. Classmate data is never read into
the calendar, printed or written to disk.

Usage:
    python scripts/fetch_roster.py            # locally, uses tokens.json
In GitHub Actions it reads EDUARTE_REFRESH_TOKEN and, with ROTATE_SECRET=1
and a GH_TOKEN that may write secrets, stores the rotated token back.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar, Event

TOKEN_URL = "https://login.educus.nl/oauth2/token"
CLIENT_ID = "c42c95ad-1dc4-43f0-b975-63f0cc184cb1"
API = "https://summacollege-rest.educus.nl/eduario/rest/v1"
SECRET_NAME = "EDUARTE_REFRESH_TOKEN"

TZ = ZoneInfo("Europe/Amsterdam")
WEEKS_AHEAD = int(os.environ.get("WEEKS_AHEAD", "4"))
HISTORY_WEEKS = int(os.environ.get("HISTORY_WEEKS", "8"))
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", "docs/roster.ics"))
TOKENS_PATH = Path(os.environ.get("TOKENS_PATH", "tokens.json"))
PAGE_SIZE = 100
STABLE_DTSTAMP = datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC"))

# Same set the app asks for.
CATEGORIES = ["INDIVIDUEEL", "BPV", "PRIVE", "BESCHERMD", "EXTERN", "BESCHIKBAARHEID_OLS", "ROOSTER"]


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- auth

def load_refresh_token() -> tuple[str, str]:
    env = os.environ.get(SECRET_NAME, "").strip()
    if env:
        return env, "env"
    if TOKENS_PATH.exists():
        return json.loads(TOKENS_PATH.read_text())["refresh_token"], "file"
    die(f"No refresh token. Set {SECRET_NAME}, or run scripts/app_login.py to create {TOKENS_PATH}.")


def store_refresh_token(token: str, source: str) -> None:
    """Save the rotated token right away; the old one is no longer valid."""
    if source == "file":
        data = json.loads(TOKENS_PATH.read_text())
        data["refresh_token"] = token
        TOKENS_PATH.write_text(json.dumps(data, indent=2))
        return
    if os.environ.get("ROTATE_SECRET") != "1":
        print("WARNING: refresh token rotated but ROTATE_SECRET isn't set; the secret is now stale.")
        return
    try:
        subprocess.run(["gh", "secret", "set", SECRET_NAME], input=token, text=True,
                       check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        die(f"Couldn't save the rotated refresh token to the {SECRET_NAME} secret ({detail.strip()}). "
            "Check the SECRETS_PAT secret, then run scripts/app_login.py once to start fresh.")
    print(f"Saved the rotated refresh token to the {SECRET_NAME} secret.")


def check_can_save_secret() -> None:
    """Fail before refreshing if the rotated token couldn't be saved afterwards.

    Refreshing revokes the old token, so finding out only afterwards that
    the secret can't be written leaves nothing valid behind.
    """
    if not os.environ.get("GH_TOKEN"):
        die("GH_TOKEN is empty, so the rotated token couldn't be saved. Add the SECRETS_PAT "
            "secret (fine-grained token, Secrets: Read and write). The stored token is untouched.")
    try:
        subprocess.run(["gh", "secret", "list"], check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        die(f"SECRETS_PAT can't read this repo's secrets ({detail.strip()}). Give it "
            "'Secrets: Read and write' on this repo. The stored token is untouched.")


def access_token() -> str:
    refresh, source = load_refresh_token()
    if source == "env" and os.environ.get("ROTATE_SECRET") == "1":
        check_can_save_secret()
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLIENT_ID,
    }, timeout=30)
    if r.status_code != 200:
        die(f"Refreshing failed (HTTP {r.status_code}: {r.text[:200]}). "
            "The refresh token expired or was used elsewhere. Run scripts/app_login.py again.")
    tok = r.json()
    new_refresh = tok.get("refresh_token")
    if new_refresh and new_refresh != refresh:
        store_refresh_token(new_refresh, source)
    return tok["access_token"]


# ---------------------------------------------------------------- api

def api_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {access_token()}", "Accept": "application/json"})
    return s


def get_json(s: requests.Session, path: str, params=None, headers=None):
    r = s.get(f"{API}{path}", params=params, headers=headers, timeout=30)
    if r.status_code == 416:  # asked for a page past the end, e.g. an empty week
        return r, []
    if r.status_code not in (200, 206):
        die(f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
    return r, r.json()


def get_all_items(s: requests.Session, path: str, params) -> list[dict]:
    """The API pages with 'Range: items=a-b' and answers 206 + Content-Range."""
    items: list[dict] = []
    start = 0
    while True:
        r, body = get_json(s, path, params, {"Range": f"items={start}-{start + PAGE_SIZE - 1}"})
        page = body.get("items", []) if isinstance(body, dict) else body
        items.extend(page)
        m = re.match(r"items (\d+)-(\d+)/(\d+)", r.headers.get("Content-Range", ""))
        if not page or not m or int(m.group(2)) + 1 >= int(m.group(3)):
            return items
        if int(m.group(1)) != start:
            die(f"The server ignored paging on {path} (asked from {start}, got {m.group(0)}).")
        start = int(m.group(2)) + 1


def deelnemer_id(s: requests.Session) -> int:
    _, me = get_json(s, "/account/me")
    for link in (me.get("deelnemer") or {}).get("links", []):
        if link.get("rel") == "self":
            return link["id"]
    die("Couldn't find the deelnemer id in /account/me.")


# ---------------------------------------------------------------- lessons

@dataclass
class Lesson:
    id: int
    subject: str
    start: datetime
    end: datetime
    room: str | None = None
    teachers: list[str] = field(default_factory=list)
    full_name: str | None = None
    description: str | None = None
    cancelled: bool = False
    notes: list[str] = field(default_factory=list)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(TZ)


def self_id(obj: dict) -> int | None:
    for link in obj.get("links", []):
        if link.get("rel") == "self":
            return link.get("id")
    return None


def to_lesson(item: dict) -> Lesson | None:
    lid = self_id(item)
    if lid is None or not item.get("beginDatumTijd") or not item.get("eindDatumTijd"):
        return None
    # Only the teacher's abbreviation is read from the participant list.
    teachers = []
    for p in item.get("participanten", []):
        code = (p.get("medewerker") or {}).get("afkorting")
        if code and code not in teachers:
            teachers.append(code)
    product = item.get("onderwijsproduct") or {}
    return Lesson(
        id=lid,
        subject=item.get("titel") or product.get("afkorting") or "Les",
        start=parse_dt(item["beginDatumTijd"]),
        end=parse_dt(item["eindDatumTijd"]),
        room=item.get("afspraakLocatie") or None,
        teachers=teachers,
        full_name=product.get("titel") or None,
        description=(item.get("omschrijving") or "").strip() or None,
    )


def fetch_lessons(s: requests.Session, did: int, monday: date) -> dict[int, Lesson]:
    lessons: dict[int, Lesson] = {}
    for w in range(WEEKS_AHEAD):
        week_start = monday + timedelta(weeks=w)
        year, week, _ = week_start.isocalendar()
        params = [
            ("deelnemer", did),
            ("beginDatumTijdAfter", week_start.isoformat()),
            ("beginDatumTijdBefore", (week_start + timedelta(days=7)).isoformat()),
            *[("categories", c) for c in CATEGORIES],
            ("geoorloofdAfwezig", "BOTH"),
            ("isoWeek", week),
            ("isoJaar", year),
            ("sort", "asc-beginDatumTijd"),
        ]
        for item in get_all_items(s, "/afspraak", params):
            lesson = to_lesson(item)
            if lesson:
                lessons[lesson.id] = lesson
    return lessons


def fetch_changes(s: requests.Session, did: int, monday: date) -> list[dict]:
    last = monday + timedelta(weeks=WEEKS_AHEAD, days=-1)
    _, body = get_json(s, f"/deelnemer/{did}/afspraakwijzigingen",
                       {"datumVanaf": monday.isoformat(), "datumTot": last.isoformat()})
    return body if isinstance(body, list) else body.get("items", [])


# ---------------------------------------------------------------- calendar

def uid_for(lesson_id: int) -> str:
    return f"{lesson_id}@eduarte-to-ical"


def load_previous(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return list(Calendar.from_ical(path.read_bytes()).walk("VEVENT"))
    except ValueError:
        return []


def fmt_time(value: str) -> str:
    try:
        return parse_dt(value).strftime("%a %d %b %H:%M")
    except ValueError:
        return value


def apply_changes(lessons: dict[int, Lesson], changes: list[dict], previous_uids: set[str]) -> int:
    """Mark room/time changes, and turn removed lessons into cancelled events.

    A removal only becomes a cancelled event if that lesson was in the feed
    before, because the change list also mentions appointments that were
    never yours (it's per deelnemer, but not only your own lessons).
    """
    added = 0
    for c in changes:
        aid, kind = c.get("afspraakId"), c.get("afspraakWijzigingType")
        old, new = c.get("waardeOud") or "?", c.get("waardeNieuw") or "?"
        lesson = lessons.get(aid)
        if kind == "AFSPRAAK_VERWIJDERD":
            if lesson:
                lesson.cancelled = True
            elif uid_for(aid) in previous_uids and c.get("begindatumAfspraak") and c.get("einddatumAfspraak"):
                lessons[aid] = Lesson(
                    id=aid, subject=c.get("afspraakTitel") or "Les",
                    start=parse_dt(c["begindatumAfspraak"]), end=parse_dt(c["einddatumAfspraak"]),
                    cancelled=True)
                added += 1
        elif not lesson:
            continue
        elif kind == "AFSPRAAKLOCATIE_GEWIJZIGD":
            lesson.notes.append(f"⚠️ Location changed: {old} → {new}")
        elif kind == "BEGINDATUM_TIJD_GEWIJZIGD":
            lesson.notes.append(f"⚠️ Start time changed, was {fmt_time(old)}")
        elif kind == "EINDDATUM_TIJD_GEWIJZIGD":
            lesson.notes.append(f"⚠️ End time changed, was {fmt_time(old)}")
        elif kind == "AFSPRAAKTYPE_GEWIJZIGD":
            lesson.notes.append(f"⚠️ Type changed: {old} → {new}")
    return added


def build_calendar(lessons: dict[int, Lesson], previous: list, monday: date) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//eduarte-to-ical//summacollege//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Summa rooster")
    cal.add("x-wr-timezone", "Europe/Amsterdam")
    cal.add("method", "PUBLISH")

    # Keep recent past lessons, so last week doesn't vanish from the calendar.
    window_start = datetime.combine(monday, datetime.min.time(), TZ)
    history_start = window_start - timedelta(weeks=HISTORY_WEEKS)
    new_uids = {uid_for(i) for i in lessons}
    kept = 0
    for ev in previous:
        start = ev.decoded("dtstart", None)
        if isinstance(start, datetime) and start.tzinfo and history_start <= start < window_start \
                and str(ev.get("uid")) not in new_uids:
            cal.add_component(ev)
            kept += 1

    for lesson in sorted(lessons.values(), key=lambda l: l.start):
        prefix = ("❌ " if lesson.cancelled else "") + ("⚠️ " if lesson.notes else "")
        event = Event()
        event.add("uid", uid_for(lesson.id))
        event.add("summary", f"{prefix}{lesson.subject}")
        event.add("dtstart", lesson.start)
        event.add("dtend", lesson.end)
        # Fixed on purpose: a timestamp that changes every run would make the
        # file differ every time and fill the repo with empty commits.
        event.add("dtstamp", STABLE_DTSTAMP)
        event.add("status", "CANCELLED" if lesson.cancelled else "CONFIRMED")
        if lesson.room:
            event.add("location", lesson.room)

        parts = []
        if lesson.cancelled:
            parts.append("❌ Cancelled")
        parts.extend(lesson.notes)
        if lesson.full_name and lesson.full_name != lesson.subject:
            parts.append(lesson.full_name)
        if lesson.teachers:
            parts.append("Docent: " + ", ".join(lesson.teachers))
        if lesson.description:
            parts.append(lesson.description)
        if parts:
            event.add("description", "\n".join(parts))
        cal.add_component(event)

    print(f"Kept {kept} past lessons from the previous feed.")
    return cal


def main() -> None:
    today = datetime.now(tz=TZ).date()
    monday = today - timedelta(days=today.weekday())

    s = api_session()
    did = deelnemer_id(s)
    lessons = fetch_lessons(s, did, monday)
    changes = fetch_changes(s, did, monday)

    previous = load_previous(OUTPUT_PATH)
    previous_uids = {str(ev.get("uid")) for ev in previous}
    added = apply_changes(lessons, changes, previous_uids)

    if not lessons:
        print(f"WARNING: no lessons in the next {WEEKS_AHEAD} weeks (holiday?).")

    calendar = build_calendar(lessons, previous, monday)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_bytes(calendar.to_ical())
    cancelled = sum(l.cancelled for l in lessons.values())
    changed = sum(bool(l.notes) for l in lessons.values())
    print(f"Wrote {len(lessons)} lessons to {OUTPUT_PATH} "
          f"({cancelled} cancelled, {added} of them removed from the roster, {changed} changed).")


if __name__ == "__main__":
    main()
