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

### Why the scheduled run needs a browser too

Educus' own session cookie is a server-side session with a short idle
timeout -- measured, it dies within a few hours of not being used, so
between two scheduled runs it's always dead. The Microsoft half of the
login is the durable part: its persistent auth cookie lasts months.

So `fetch_roster.py` doesn't replay the saved cookies directly. It hands
them to a headless browser and lets it redo the OAuth/SAML handshake,
which completes **silently** -- no password, no MFA prompt -- because
Microsoft still considers the account signed in. It has to be a real
browser: that handoff is driven by Microsoft's client-side JavaScript,
and replaying it over plain HTTP just bounces between redirects forever
(verified -- it loops on `/reprocess` indefinitely). Once through, the
browser's fresh cookies are handed to `requests` and the actual scrape
runs over plain HTTP as before.

The practical effect: one capture should last until the Microsoft
persistent cookie expires (on the order of ~90 days) rather than hours.

### Keeping the session from dying in the first place

`.github/workflows/keep-session-alive.yml` pings the agenda page every 10
minutes so the session never goes idle between the twice-daily fetches.
It's deliberately tiny (one GET, `requests` only, no browser) since it
runs a lot.

On this account this is not a nicety, it's the whole strategy. MFA here
is Microsoft Authenticator push approval, and Summa doesn't permit
third-party authenticator apps, so there's no TOTP secret to store and a
script can never answer the prompt. Measured: a fresh session signed in
fine, and ~12 hours later every run was hitting the push prompt. Keeping
the session from ever dying is the only way to avoid needing to sign in
at all.

Its limits are worth knowing, because it is a mitigation and not a fix:

- **It can only keep alive a session it was given.** The cookie lives in
  a secret and a CI job can't write a fresh one back, so once the session
  does die, every later ping is talking to a dead session. Recovery is
  the roster workflow's job, via the browser sign-in.
- **GitHub does not honour this schedule, and measurably so.** With
  `*/10` configured, the first observed window delivered **2 runs in 4.5
  hours** (19:27 and 21:37) where ~28 were due: roughly 7%, with a
  130-minute gap. GitHub delays and drops scheduled workflows under load,
  and short intervals fare worst.

  That gap is far beyond the idle timeout (somewhere between ~22 minutes
  and ~6 hours based on observed runs, quite possibly the usual 30). So
  **on GitHub Actions this approach does not work** -- it cannot ping
  often enough to keep a session warm, whatever interval is configured.

  It's kept because it costs nothing and does no harm when a session is
  live. But anything depending on the session surviving needs a scheduler
  that actually fires: real cron, systemd timers, or launchd on a machine
  you control.

It exits successfully even when the session is dead, on purpose: on this
schedule, failing here would mean dozens of red runs a day drowning out
the roster workflow, which is the one that actually matters.

### Optional: let it recover on its own

With only a saved session, an expiry means a red run and a manual
re-capture. Set these extra secrets and the job signs itself back in
instead, so it never needs you:

| secret | needed? |
|---|---|
| `EDUARTE_EMAIL` | to answer "who's signing in" |
| `EDUARTE_PASSWORD` | to answer the password prompt |
| `TOTP_SECRET` | only if the account uses authenticator-app 2FA |

**About Microsoft Authenticator.** Its default "approve on your phone /
type the matching number" prompt **cannot be automated** -- there is no
secret to store, the approval happens on the device. But the same app
also exposes a rotating 6-digit *verification code*, which is ordinary
TOTP and does work here. To get its seed: **[mysignins.microsoft.com/security-info](https://mysignins.microsoft.com/security-info)**
-> Add sign-in method -> Authenticator app -> "I want to use a different
authenticator app" -> **"Can't scan image?"**, which prints the secret as
text. That's the value for `TOTP_SECRET`.

Microsoft normally *shows* the push prompt first, so with a TOTP secret
set the login clicks through "I can't use my Microsoft Authenticator app
right now" -> "Use a verification code" to reach the code field. If your
school restricts MFA to push approval only, that option won't exist and
the session-capture route is the only one available.

The saved session is still tried first and normally covers everything;
these are only touched when Microsoft actually asks. The login step is
detected from whatever is on screen at the time (account tile, 2FA code,
password, email) rather than assuming a fixed sequence, since how far the
saved session gets varies.

Trade-off worth being deliberate about: `EDUARTE_PASSWORD` is your whole
school Microsoft account, not just this timetable. Without it the worst
case is a red run and five minutes of re-capturing.

If sign-in can't complete, the run fails on purpose instead of silently
going stale -- GitHub shows the run red / emails you.

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

### 2b. Make `main` the default branch

**Settings -> General -> Default branch**, set it to `main`.

Scheduled workflows only ever run from the repo's default branch. If it
isn't `main`, the scheduled runs execute a different branch's code, and
GitHub Pages (serving `main/docs`) never sees the updated `roster.ics`.
Both workflows now check out `main` explicitly and push to it, so a stale
default branch can't silently publish to the wrong place, but setting the
default correctly avoids the confusion entirely.

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
