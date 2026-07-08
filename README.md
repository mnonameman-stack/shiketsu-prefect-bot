# Shiketsu Prefect

Custom moderation bot for the **Shiketsu High** Discord server. It's already registered as a
Discord application (client ID `1524479220028276786`) and already invited to your server with
the right permissions — the only thing left is to run it somewhere.

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
- `/host_training time notes` — posts a training announcement embed to `#training`
- `/cancel_training` — cancels your most recently announced session

## Why I can't just run this for you

I built and tested this bot from a sandboxed environment that only exists for the length of
our conversation — it has no way to stay online after we're done talking, and it can't reach
the internet to connect to Discord at all (that's a deliberate restriction). A Discord bot has
to be a program running continuously somewhere, 24/7, listening for events. That "somewhere"
has to be a real computer or server that stays on. Here are your options, cheapest/easiest first.

## Option 1: Run it on your own computer

Simplest if you're okay leaving a terminal window open (or your PC on) while you want the bot active.

1. Install [Python 3.10+](https://www.python.org/downloads/) if you don't have it.
2. Open a terminal in this folder.
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. The `.env` file already has your bot token and server IDs filled in. Just run:
   ```
   python bot.py
   ```
5. You should see `Shiketsu Prefect is online and ready.` in the terminal. Closing the terminal
   stops the bot.

## Option 2: A free/cheap always-on host (recommended for 24/7 uptime)

These run your bot continuously without you needing to keep a computer on. All of them require
*you* to create the account (I can't sign up for third-party services on your behalf) — but I'm
happy to walk you through the setup step by step once you pick one.

- **[Railway](https://railway.app)** — free trial credit, then ~$5/month. Deploy by connecting
  a GitHub repo or uploading this folder directly. Easiest of the three.
- **[Render](https://render.com)** — has a free tier for "Background Workers" (may sleep after
  inactivity on the free plan, upgrade removes that).
- **A cheap VPS** (Oracle Cloud has a genuinely free tier, or DigitalOcean/Linode for a few
  dollars/month) — most control, requires a bit more comfort with the command line.

Whichever you pick, the steps are basically: create the account, upload/connect this folder,
set the same environment variables that are in `.env`, and tell it to run `python bot.py`.

## Files in this folder

- `bot.py` — the bot itself
- `requirements.txt` — Python packages it needs (`pip install -r requirements.txt`)
- `.env` — your bot token and server/channel/role IDs, already filled in
- `.env.example` — blank template, useful if you ever rebuild this from scratch
- `warnings.json` — where warning history is stored (auto-created/updated)
- `active_trainings.json` — tracks currently-announced training sessions (auto-created/updated)

## Security note

`.env` contains your bot's live token. Anyone with that token can control the bot as if they
were you. Don't post it publicly, and don't commit it to a public GitHub repo (if you do use
GitHub for hosting, add `.env` to a `.gitignore` file and set the token as an environment
variable in the host's dashboard instead).

## Customizing

- To change which roles can moderate, edit `MOD_ROLE_IDS` in `.env` (comma-separated role IDs).
- To change which roles can host training, edit `TRAINER_ROLE_IDS`.
- To point at different channels for logs/training, edit `LOG_CHANNEL_ID` / `TRAINING_CHANNEL_ID`.
- Automod thresholds (spam count/window, caps ratio) are constants near the top of `bot.py`
  under "Automod" if you want to tune sensitivity.
