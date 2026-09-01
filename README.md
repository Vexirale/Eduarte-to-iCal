# Eduarte-to-iCal

Turns your Summa College Eduarte timetable (subject, room, teacher, duration)
into a calendar feed you can subscribe to from Google Calendar, Apple
Calendar, or Outlook, refreshed daily.

## How it works

Summa's student portal (`summacollege-student.educus.nl`) doesn't expose a
public API or a built-in iCal export. Logging in also isn't a plain
username/password form: it's OAuth2 through `login.educus.nl`, which
federates out to your school's Microsoft account (SAML via
`login.microsoftonline.com`) -- almost certainly with MFA enforced. That
means a script cannot fully automate the login itself, so this project
splits the work in two:

1. **You log in once, yourself**, in a real browser window that
   `scripts/capture_session.py` opens locally. You handle the Microsoft
   login and any MFA prompt like you always do. The script then saves the
   resulting session.
2. **A GitHub Actions workflow runs daily**, reuses that saved session
   (no login needed), reads your timetable, and (re)writes `docs/roster.ics`.
   GitHub Pages serves that file at a stable URL your calendar app
   subscribes to and refreshes on its own schedule.

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
This produces `session_state.json` (and, the first time, `debug_capture.json`
-- see below).

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

## Status of the timetable parser

`scripts/fetch_roster.py` intercepts the JSON responses the Eduarte agenda
page loads and pulls lessons out of them using a set of likely Dutch/English
field names (`vak`/`subject`, `docent`/`teacher`, `lokaal`/`room`, ...) --
see `FIELD_CANDIDATES` in that file. This was built without access to a real
logged-in session, so if your actual payload uses different field names, the
heuristic in `extract_lessons` may need a tweak. `debug_capture.json`
(produced by `capture_session.py`) shows the real shape and is the fastest
way to fix any mismatch.
