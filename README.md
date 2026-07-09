# Shiketsu Prefect

Custom moderation bot for the **Shiketsu High** Discord server (client ID `1524479220028276786`).
It's live, invited to the server, and hosted for free on GitHub Actions — see "How it's hosted"
below.

## What it does

**Moderation** (usable by Hall Monitor, Student Council, or higher — including Principal /
Vice Principal / School Board, who bypass via Administrator):
- `/kick`, `/ban`, `/timeout`, `/untimeout`
- `/warn`, `/warnings`, `/clearwarnings` — persisted warning history per member
- `/purge` — bulk delete recent messages

**Mod-log** — every action above is posted as an embed to `#logs` automatically.

**Automod** — runs on every message from non-staff members:
- Deletes Discord invite links
- Deletes messages that are mostly CAPS
- Detects spam (5+ messages in 5 seconds), purges them, and applies a 5-minute timeout

**Training system** (usable by Pro Hero Instructor, Faculty Head, or higher):
- `/host_training starts_in server_link notes` — posts an embed to `#「⭐」training` with the
  time (shown as a live countdown), the private server link, and notes. Members click
  **Wish to Attend** to RSVP; the bot DMs every attendee 5 minutes before it starts.
- `/cancel_training` — cancels your most recently announced session and notifies attendees.

**Call-help system** (usable by anyone):
- `/call_help server_link issue` — posts an embed to `#「🆘」ask-for-help` with your server link
  and what's happening. Anyone can click **I Want to Help** to volunteer — you get DMed their
  name immediately.
- `/cancel_help` (or the **Cancel Help Request** button on the post) — closes out the request
  once you're sorted.

**Owner reboot control** — about 30 minutes before each ~6-hour hosting cycle ends, the bot DMs
the server owner with a **Reboot Now** button to restart early instead of waiting.

## How it's hosted

The bot runs for free, forever, on **GitHub Actions** using a self-restarting workflow
(`.github/workflows/keep-alive.yml`):

- GitHub gives *unlimited* free Actions minutes to public repos, but caps a single run at 6
  hours. So each run executes the bot for ~5h50m, then exits.
- The moment a run starts, it immediately queues the *next* run via the GitHub API. Because of
  the workflow's concurrency setting, that queued run just waits until the current one finishes,
  then starts within seconds — so restarts aren't tied to a fixed clock time and don't drift
  into multi-hour gaps like a plain cron schedule would.
- Bot state (`warnings.json`, `active_trainings.json`, `help_requests.json`) is committed back to
  the repo at the end of every run, so nothing resets between cycles.
- A cron trigger (`0 */6 * * *`) stays on purely as a safety net in case the self-restart chain
  ever breaks.

**Expect a brief (usually under a minute, worst case ~10 minutes) gap in the bot's availability
every ~6 hours** while one run hands off to the next. The owner gets a heads-up DM 30 minutes
before each handoff with the option to trigger it early.

To manually trigger a run: open the repo on GitHub → **Actions** tab → **Run Shiketsu Prefect
Bot** → **Run workflow**.

## Running it yourself instead (optional)

If you ever want to run this locally for testing:

1. Install [Python 3.10+](https://www.python.org/downloads/).
2. Open a terminal in this folder and run `pip install -r requirements.txt`.
3. Make sure `.env` has your bot token and IDs filled in (see `.env.example`).
4. Run `python bot.py`. You should see `Shiketsu Prefect is online and ready.`

## Files in this folder

- `bot.py` — the bot itself
- `requirements.txt` — Python packages it needs
- `.env` — bot token and server/channel/role IDs (never committed — see Security note)
- `.env.example` — blank template
- `.github/workflows/keep-alive.yml` — the self-restarting GitHub Actions hosting workflow
- `warnings.json` — warning history per member (auto-updated, committed by the workflow)
- `active_trainings.json` — active/past training sessions + RSVPs (auto-updated, committed)
- `help_requests.json` — active/past help requests + helpers (auto-updated, committed)

## Security note

`.env` (and the equivalent GitHub Actions secrets) contain your bot's live token. Anyone with
that token can control the bot as if they were you. The token is stored as a GitHub Actions
**secret**, not in the repo's code — secrets are encrypted and never shown in logs. The repo
itself is public (required for free Actions minutes) but contains no credentials.

## Customizing

- To change which roles can moderate, edit `MOD_ROLE_IDS` (comma-separated role IDs) as a repo
  secret (or in `.env` for local runs).
- To change which roles can host training, edit `TRAINER_ROLE_IDS`.
- To point at different channels, edit `LOG_CHANNEL_ID` / `TRAINING_CHANNEL_ID` /
  `ASK_FOR_HELP_CHANNEL_ID`.
- To change who gets the pre-restart reboot DM, edit `OWNER_ID`.
- Automod thresholds (spam count/window, caps ratio) are constants near the top of `bot.py`
  under "Automod" if you want to tune sensitivity.
