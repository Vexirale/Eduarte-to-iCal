# Eduarte-to-iCal

Turns your Summa College Eduarte timetable (subject, room, teacher, duration)
into a calendar feed you can subscribe to from Google Calendar, Apple
Calendar, or Outlook. When a lesson's room changes between two fetches,
that lesson gets a "⚠️" prefix and a "Location changed: was X, now Y" note
for one cycle. A cancelled lesson gets a "❌" prefix, a "Cancelled" note,
and `STATUS:CANCELLED` (which Apple/Google Calendar typically render with
strikethrough).

> **Refreshing is manual, by necessity.** Run
> `python scripts/capture_session.py` when you want the calendar brought
> up to date. The feed keeps serving the last fetch in between, which is
> usually fine since a timetable changes rarely, but it won't pick up a
> same-day room change on its own.
>
> This isn't a missing feature, it's a wall. The Educus session lasts
> about **an hour** (measured: signed in 11:12, dead by 12:17), and
> renewing it needs Microsoft Authenticator push approval, which happens
> on a phone and cannot be scripted. Every unattended workaround was tried
> and measured; see [Why it can't run itself](#why-it-cant-run-itself).

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
2. **A GitHub Actions workflow reads your timetable** using that saved
   session and (re)writes `docs/roster.ics`. GitHub Pages serves that file
   at a stable URL your calendar app subscribes to. `capture_session.py`
   starts this run for you, so step 1 is the whole procedure.

### Why the fetch needs a browser too

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

## Why it can't run itself

Every unattended approach was built, run against the real site, and
measured. All four failed, for different reasons:

**1. Reuse the saved session on a schedule.** The Educus session is killed
by inactivity. Signed in at 11:12, already dead by 12:17, so it lasts
about **an hour**. Any schedule sparse enough to be reasonable lands after
it's gone. Sixteen consecutive scheduled runs failed this way.

**2. Let the browser re-do the SSO silently.** Worth a real try: Microsoft
still reports `"isSignedIn": true` long after the Educus session dies, and
its persistent cookie lasts months. But it doesn't hold from CI. A fresh
session signed in fine at 23:19 and by 11:32 the next morning every run
was landing on the login prompt, most likely because a datacenter IP that
changes every run is exactly what risk-based Conditional Access is
looking for.

**3. Sign in with stored credentials.** `EDUARTE_EMAIL` and
`EDUARTE_PASSWORD` are accepted, and then it stops dead at Microsoft
Authenticator **push approval**, which happens on a phone. There is no
secret that answers it.

Authenticator also exposes a rotating 6-digit code, which is ordinary
TOTP and *would* work, via
[mysignins.microsoft.com/security-info](https://mysignins.microsoft.com/security-info)
-> Add sign-in method -> Authenticator app -> "I want to use a different
authenticator app" -> "Can't scan image?". **Summa blocks third-party
authenticator apps**, so that option isn't offered and no seed exists.
(The code still handles this path, in case that policy ever changes.)

**4. Ping constantly so the session never goes idle.** Fails on GitHub's
scheduler, not on the idea. With `*/10` configured, the actual delivery
was 6 runs in 14 hours:

```
19:27 → 21:37 → 23:17 → 01:01 → 05:36 → 09:18
gaps:   2h10m   1h40m   1h44m   4h35m   3h42m
```

Roughly 7% of schedule, with typical gaps 2 to 4 times longer than the
session's entire lifetime, and zero pings during the hour it actually
died. GitHub delays and drops scheduled workflows under load, and short
intervals fare worst. This job was removed once measured.

### What would actually fix it

Running the fetch on a machine you control, which beats all four at once:
real cron fires when it says it will, a residential IP is far less likely
to be challenged (the account's own token showed `amr: ["pwd"]`, no MFA,
from a normal network), the refreshed session can be written straight back
to disk instead of round-tripping through a CI secret, and if it ever does
prompt, the approval lands on your phone while you're right there.

Anything always-on works: a Raspberry Pi, a mini PC, a desktop that stays
awake. A VPS gets the reliable cron but keeps the datacenter-IP problem,
so it may hit wall #2 again.

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

## Refreshing the calendar

```bash
python scripts/capture_session.py
```

Log in, approve on your phone, press Enter. With the [GitHub
CLI](https://cli.github.com) installed and authenticated, that also
updates the `EDUARTE_SESSION_STATE` secret and starts the roster run, so
this one command is the whole procedure. Without `gh` it writes the file
and tells you where to paste it.

Between refreshes the feed keeps serving the last fetch, so your calendar
stays populated with several weeks of lessons; it just won't reflect a
change made at school since. See [Why it can't run
itself](#why-it-cant-run-itself) for why this step can't be automated
away.

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
