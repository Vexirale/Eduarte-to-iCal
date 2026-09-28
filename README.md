# Eduarte-to-iCal

Turns your Summa College Eduarte timetable into a calendar feed you can
subscribe to from Apple Calendar, Google Calendar or Outlook. It updates by
itself, every couple of hours, with no logging in.

- A cancelled lesson gets a "❌" prefix and `STATUS:CANCELLED` (most apps show
  it struck through).
- A room or time change gets a "⚠️" prefix, with the old and new value in the
  event notes.
- The notes also hold the full subject name and the teacher's abbreviation.
- Past lessons stay in the calendar for 8 weeks.

## How it works

It uses the same REST API as the Eduarte Student app
(`summacollege-rest.educus.nl/eduario/rest/v1`), found by reading the app
itself.

The app logs in once through `login.educus.nl` (OAuth2 with PKCE, federated to
the school's Microsoft account) and after that stays signed in with a
**refresh token**. Refreshing is a single request to `login.educus.nl` that
never touches Microsoft, so there's no MFA prompt, no one-hour web session and
no datacenter-IP problem. The refresh token lasts 60 days and rotates: every
refresh returns a new one, which the workflow saves back into its secret
straight away.

| Endpoint | Used for |
|---|---|
| `/account/me` | your deelnemer id |
| `/afspraak?deelnemer=…&isoWeek=…` | the week's lessons, paged with `Range: items=a-b` |
| `/deelnemer/{id}/afspraakwijzigingen` | cancellations, room and time changes |

**Privacy.** Every lesson the API returns also lists all classmates in it,
including names, birth dates and photo links. `fetch_roster.py` reads only the
lesson's own fields plus the teacher's abbreviation. Classmate data never
reaches the calendar, the logs or the disk.

## Setup

### 1. Let the workflow save rotated tokens

The built-in `GITHUB_TOKEN` can't write secrets, so the workflow needs a
personal access token for that:

1. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate
   new token.
2. Repository access: **only this repository**.
3. Permissions → Repository → **Secrets: Read and write**. Nothing else.
4. Pick a long expiry (up to a year) and put a reminder in your calendar a
   week before it ends.
5. In this repo: Settings → Secrets and variables → Actions → New repository
   secret, name it `SECRETS_PAT`, paste the token.

### 2. Log in once

Needs the [GitHub CLI](https://cli.github.com), logged in with `gh auth login`.
From the repo folder:

```bash
pip install -r requirements.txt
playwright install chromium
python scripts/app_login.py
```

A browser opens. Log in with your Summa account and approve the prompt on your
phone. The script then stores the refresh token in the
`EDUARTE_REFRESH_TOKEN` secret, deletes the local copy and starts the first
run.

### 3. GitHub Pages

Settings → Pages → Deploy from a branch → `main`, folder `/docs`. The feed is at:

```
https://<your-github-username>.github.io/Eduarte-to-iCal/roster.ics
```

### 4. Subscribe

- **Apple Calendar**: File → New Calendar Subscription → paste the link.
- **Google Calendar**: Other calendars (+) → From URL → paste the link.
- **Outlook**: Add calendar → Subscribe from web → paste the link.

## When it breaks

A failed run shows up red in the Actions tab, and GitHub emails you.

- **"Refreshing failed"**: the refresh token expired or got used twice. Run
  `python scripts/app_login.py` again. This is the only manual step, and only
  needed if the chain ever breaks.
- **"Couldn't save the rotated refresh token"**: the `SECRETS_PAT` token
  expired or lacks the Secrets permission. Make a new one (step 1), then run
  `app_login.py` again, because that run's rotated token was lost.

Don't run `fetch_roster.py` locally against the same token the workflow uses:
both would rotate it and one of them ends up with a revoked token. For local
testing, use `python scripts/app_login.py --no-upload` to get a separate
login in `tokens.json`.

## Settings

Environment variables for `fetch_roster.py`:

| Variable | Default | Meaning |
|---|---|---|
| `WEEKS_AHEAD` | 4 | weeks fetched, starting this week |
| `HISTORY_WEEKS` | 8 | how long past lessons stay in the feed |
| `OUTPUT_PATH` | `docs/roster.ics` | where the feed is written |
