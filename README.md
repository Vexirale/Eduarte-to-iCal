# Eduarte-to-iCal

Turns your Summa College Eduarte timetable (subject, room, teacher, duration)
into a calendar feed you can subscribe to from Google Calendar, Apple
Calendar, or Outlook, refreshed twice a day. When a lesson's room changes
between two fetches, that lesson gets a "⚠️" prefix and a "Location changed:
was X, now Y" note for one cycle, so a last-minute room swap doesn't get lost
in a page you weren't about to re-check. A cancelled lesson gets a "❌"
prefix, a "Cancelled" note, and `STATUS:CANCELLED` (which Apple/Google
Calendar typically render with strikethrough).

## How it works

Summa's student portal (`summacollege-student.educus.nl`) doesn't expose a
public API or a built-in iCal export. Logging in also isn't a plain
username/password form: it's OAuth2 through `login.educus.nl`, which
federates out to your school's Microsoft account (SAML via
`login.microsoftonline.com`) -- almost certainly with MFA enforced. That
means a script cannot fully automate the login itself, so this project
splits the work in two:

On top of that, the agenda page itself isn't a JSON API either -- it's a
server-rendered Apache Wicket app. Each lesson's start time and duration
aren't sent as data at all; they're encoded as pixel positions (`top`/
`height`) in inline CSS against an hourly axis. `fetch_roster.py` converts
those pixel positions back into real times, and pages through weeks using
the same links the site's own "next week" button uses.

1. **You log in once, yourself**, in a real browser window that
   `scripts/capture_session.py` opens locally. You handle the Microsoft
   login and any MFA prompt like you always do. The script then saves the
   resulting session.
2. **A GitHub Actions workflow runs twice daily** (08:25 and 11:00 CEST),
   reuses that saved session (no login needed), reads your timetable, and
   (re)writes `docs/roster.ics`. GitHub Pages serves that file at a stable
   URL your calendar app subscribes to and refreshes on its own schedule.

When the saved session eventually expires (Educus bounces it back to the
Microsoft login page), the daily workflow run fails on purpose instead of
silently going stale -- GitHub will show the run as red / email you, and
that's your signal to redo step 1.

## One-time setup

### 1. Capture a session

```bash
pip install -r requirements.txt
playwright install chromium
python scripts/capture_session.py
```

A browser window opens. Log in with your Summa Microsoft account, wait
until your real timetable is visible, then press Enter in the terminal.
This produces `session_state.json`.

`session_state.json` is a login credential. **Never commit it.**

### 2. Store it as a GitHub secret

In this repo: **Settings -> Secrets and variables -> Actions -> New repository
secret**, name it `EDUARTE_SESSION_STATE`, and paste the entire contents of
`session_state.json` as the value.

### 3. Enable GitHub Pages

**Settings -> Pages -> Source: Deploy from a branch -> Branch: `main`,
folder: `/docs`.**

Your feed will then be published at:

```
https://<your-github-username>.github.io/Eduarte-to-iCal/roster.ics
```

### 4. Subscribe to it

- **Google Calendar**: Other calendars (+) -> From URL -> paste the link above.
- **Apple Calendar**: File -> New Calendar Subscription -> paste the link.
- **Outlook**: Add calendar -> Subscribe from web -> paste the link.

Each app refreshes subscribed calendars on its own interval (typically
every few hours), on top of the daily GitHub Actions run that keeps
`roster.ics` itself current.

### 5. Run it once manually

Actions tab -> "Update roster.ics" -> Run workflow, to confirm it works
before waiting for the first scheduled run.

## When the session expires

Re-run step 1 and update the `EDUARTE_SESSION_STATE` secret with the new
`session_state.json` contents. Everything else keeps working as-is.

## Notes on the scraper

- Subjects, rooms and teachers show up exactly as abbreviated codes (e.g.
  `WISB`, `LB-0.25`, `BEIS`) -- the same shorthand the Eduarte web UI shows
  you, since that's all the page ever renders.
- Lesson duration is derived from a pixel-position CSS rule the site
  generates (`PX_PER_HOUR = 108` in `fetch_roster.py`, reverse engineered
  from Summa's own stylesheet). If a future redesign changes that scale,
  lesson times would all be off by the same factor -- that's the first
  thing to check if something looks wrong.
- `WEEKS_AHEAD` (default 4) controls how many weeks out `roster.ics`
  covers; set it via a repo variable/env var if you want more or less.
- Room-change detection compares each lesson's room against the
  previously published `docs/roster.ics` (matched by subject + start/end
  time, not by room, so a room change updates the same calendar event
  instead of creating a duplicate). It only has one prior fetch to compare
  against, so the "⚠️" note lasts exactly one cycle before clearing on its
  own -- if you don't check your calendar between two fetches, you'd only
  ever see the latest room, not the flag.
- The twice-daily schedule is fixed UTC and doesn't shift for Dutch DST --
  see the comment in `.github/workflows/update-roster.yml` if the run
  times drift an hour after a clock change.
- Cancellation detection (`is_cancelled` / `CANCELLED_MARKERS` in
  `fetch_roster.py`) looks for "vervallen" (and a few synonyms) in the
  lesson's own classes, any of its descendants' classes, or its text --
  this is the standard Dutch term across school scheduling systems, but it
  hasn't been confirmed against a real cancelled lesson on this specific
  site (there wasn't one in any session used while building this). If a
  cancellation doesn't get flagged, check what the site actually shows for
  one and adjust `CANCELLED_MARKERS` accordingly.
