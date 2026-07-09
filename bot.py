"""
Shiketsu Prefect - Custom moderation bot for the Shiketsu High Discord server.

Features:
  - Core moderation: /kick, /ban, /timeout, /untimeout, /warn, /warnings, /clearwarnings, /purge
    (restricted to Hall Monitor / Student Council / Administrator-tier staff)
  - Mod-log: every moderation action is posted as an embed to the #logs channel
  - Automod: spam, invite-link, and excessive-caps filtering with warn/delete actions
  - Training system: /host_training and /cancel_training for trainers (Faculty Head /
    Pro Hero Instructor and above) to announce training sessions with an RSVP button
    ("Wish to Attend") that DMs attendees 5 minutes before the session starts.
  - Call-help system: /call_help lets any member post a help request (server link + what's
    happening) to #ask-for-help with an "I Want to Help" button. Volunteers get matched via
    DM to the requester, who can /cancel_help or click Cancel Help Request once sorted.
  - Owner shutdown-warning: DMs the server owner ~30 minutes before the ~6h GitHub Actions
    cycle ends, with a "Reboot Now" button to restart early instead of waiting.

Run with:  python bot.py
Requires a .env file (see .env.example) with DISCORD_TOKEN and the IDs below filled in.
"""

import os
import re
import json
import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration - loaded from environment variables (see .env)
# ---------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
TRAINING_CHANNEL_ID = int(os.getenv("TRAINING_CHANNEL_ID", "0"))
ASK_FOR_HELP_CHANNEL_ID = int(os.getenv("ASK_FOR_HELP_CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0")) or None

# How long a single GitHub Actions run is expected to last before it's killed by the
# workflow's `timeout` command. Used to time the "restarting soon" DM to the owner.
TIMEOUT_MINUTES = int(os.getenv("TIMEOUT_MINUTES", "350"))
SHUTDOWN_WARNING_MINUTES_BEFORE = 30

MOD_ROLE_IDS = {
    int(x) for x in os.getenv("MOD_ROLE_IDS", "").split(",") if x.strip()
}
TRAINER_ROLE_IDS = {
    int(x) for x in os.getenv("TRAINER_ROLE_IDS", "").split(",") if x.strip()
}

WARNINGS_FILE = os.path.join(os.path.dirname(__file__), "warnings.json")
ACTIVE_TRAININGS_FILE = os.path.join(os.path.dirname(__file__), "active_trainings.json")
HELP_REQUESTS_FILE = os.path.join(os.path.dirname(__file__), "help_requests.json")

GUILD_OBJ = discord.Object(id=GUILD_ID) if GUILD_ID else None

# ---------------------------------------------------------------------------
# Persistence helpers (simple JSON files - no database needed for this scale)
# ---------------------------------------------------------------------------


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_warnings():
    return _load_json(WARNINGS_FILE, {})


def save_warnings(data):
    _save_json(WARNINGS_FILE, data)


def load_active_trainings():
    return _load_json(ACTIVE_TRAININGS_FILE, {})


def save_active_trainings(data):
    _save_json(ACTIVE_TRAININGS_FILE, data)


def load_help_requests():
    return _load_json(HELP_REQUESTS_FILE, {})


def save_help_requests(data):
    _save_json(HELP_REQUESTS_FILE, data)


def normalize_link(link: str) -> str:
    """Make sure a user-supplied server link has a proper scheme so Discord
    will actually treat it as a clickable URL (bare domains like
    'discord.gg/xyz' are not auto-linked)."""
    link = link.strip()
    if not link.lower().startswith(("http://", "https://")):
        link = "https://" + link
    return link


def link_field(link: str) -> str:
    """Render a server link as a clickable masked markdown link for use in
    embed fields."""
    return f"[Click to join]({normalize_link(link)})"


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

_process_start = datetime.now(timezone.utc)
_shutdown_warning_sent = False
_ready_once = False


def is_staff(member: discord.Member) -> bool:
    """Staff = has one of the configured mod roles, or Administrator permission
    (covers Principal / Vice Principal / School Board automatically)."""
    if member.guild_permissions.administrator:
        return True
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & MOD_ROLE_IDS)


def is_trainer(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & TRAINER_ROLE_IDS) or is_staff(member)


def staff_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message(
            "You don't have permission to use this command. "
            "This requires Hall Monitor, Student Council, or higher.",
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


def trainer_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if is_trainer(interaction.user):
            return True
        await interaction.response.send_message(
            "You don't have permission to use this command. "
            "This requires Pro Hero Instructor, Faculty Head, or higher.",
            ephemeral=True,
        )
        return False

    return app_commands.check(predicate)


async def send_mod_log(guild: discord.Guild, embed: discord.Embed):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel is not None:
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


def build_mod_embed(action: str, target: discord.abc.User, moderator: discord.abc.User,
                     reason: str, color: discord.Color, extra: dict | None = None) -> discord.Embed:
    embed = discord.Embed(title=f"🛡️ {action}", color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Member", value=f"{target.mention} ({target})", inline=True)
    embed.add_field(name="Moderator", value=f"{moderator.mention}", inline=True)
    embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
    if extra:
        for key, value in extra.items():
            embed.add_field(name=key, value=value, inline=True)
    embed.set_thumbnail(url=target.display_avatar.url if hasattr(target, "display_avatar") else None)
    return embed


# ---------------------------------------------------------------------------
# Persistent Views - training RSVP, help requests, owner reboot control.
# These use static custom_ids (encoding the training/help id) so button clicks
# keep working even after the process restarts, as long as we re-register a
# matching view via bot.add_view() for every still-active entry in on_ready.
# ---------------------------------------------------------------------------


class TrainingView(discord.ui.View):
    def __init__(self, training_id: str):
        super().__init__(timeout=None)
        self.training_id = training_id
        btn = discord.ui.Button(
            label="Wish to Attend", emoji="✅", style=discord.ButtonStyle.success,
            custom_id=f"training_attend:{training_id}",
        )
        btn.callback = self.on_attend
        self.add_item(btn)

    async def on_attend(self, interaction: discord.Interaction):
        trainings = load_active_trainings()
        entry = trainings.get(self.training_id)
        if entry is None or entry.get("cancelled"):
            await interaction.response.send_message("This training is no longer active.", ephemeral=True)
            return

        start_dt = datetime.fromisoformat(entry["start_ts"])
        if datetime.now(timezone.utc) >= start_dt:
            await interaction.response.send_message("This training has already started.", ephemeral=True)
            return

        attendees = entry.setdefault("attendees", [])
        if interaction.user.id in attendees:
            await interaction.response.send_message(
                "You're already signed up — I'll DM you 5 minutes before it starts.", ephemeral=True
            )
            return

        attendees.append(interaction.user.id)
        save_active_trainings(trainings)

        try:
            msg = interaction.message
            embed = msg.embeds[0]
            mentions = ", ".join(f"<@{uid}>" for uid in attendees)
            for i, field in enumerate(embed.fields):
                if field.name == "Attending":
                    embed.set_field_at(i, name="Attending", value=mentions, inline=False)
                    break
            await msg.edit(embed=embed)
        except (discord.HTTPException, IndexError):
            pass

        await interaction.response.send_message(
            "✅ You're in! I'll send you a DM reminder 5 minutes before the training starts. "
            "Make sure your DMs are open.",
            ephemeral=True,
        )


class HelpView(discord.ui.View):
    def __init__(self, help_id: str):
        super().__init__(timeout=None)
        self.help_id = help_id

        help_btn = discord.ui.Button(
            label="I Want to Help", emoji="🙋", style=discord.ButtonStyle.success,
            custom_id=f"help_offer:{help_id}",
        )
        help_btn.callback = self.on_help
        self.add_item(help_btn)

        cancel_btn = discord.ui.Button(
            label="Cancel Help Request", emoji="🚫", style=discord.ButtonStyle.secondary,
            custom_id=f"help_cancel:{help_id}",
        )
        cancel_btn.callback = self.on_cancel
        self.add_item(cancel_btn)

    async def on_help(self, interaction: discord.Interaction):
        requests_ = load_help_requests()
        entry = requests_.get(self.help_id)
        if entry is None or entry.get("cancelled"):
            await interaction.response.send_message("This help request is no longer active.", ephemeral=True)
            return
        if interaction.user.id == entry["requester_id"]:
            await interaction.response.send_message("You can't volunteer to help your own request.", ephemeral=True)
            return

        helpers = entry.setdefault("helpers", [])
        if interaction.user.id in helpers:
            await interaction.response.send_message("You've already offered to help with this one.", ephemeral=True)
            return

        helpers.append(interaction.user.id)
        save_help_requests(requests_)

        requester = interaction.guild.get_member(entry["requester_id"]) if interaction.guild else None
        if requester:
            try:
                await requester.send(
                    f"🙋 **{interaction.user}** said they'll help you out with your request in "
                    f"**{interaction.guild.name}**! Server link you posted: {entry['server_link']}"
                )
            except discord.HTTPException:
                pass

        try:
            msg = interaction.message
            embed = msg.embeds[0]
            helper_mentions = ", ".join(f"<@{uid}>" for uid in helpers)
            for i, field in enumerate(embed.fields):
                if field.name == "Helpers":
                    embed.set_field_at(i, name="Helpers", value=helper_mentions, inline=False)
                    break
            await msg.edit(embed=embed)
        except (discord.HTTPException, IndexError):
            pass

        await interaction.response.send_message("✅ Thanks for helping! The requester has been notified.", ephemeral=True)

    async def on_cancel(self, interaction: discord.Interaction):
        requests_ = load_help_requests()
        entry = requests_.get(self.help_id)
        if entry is None or entry.get("cancelled"):
            await interaction.response.send_message("This help request is already closed.", ephemeral=True)
            return

        is_requester = interaction.user.id == entry["requester_id"]
        is_mod = isinstance(interaction.user, discord.Member) and is_staff(interaction.user)
        if not (is_requester or is_mod):
            await interaction.response.send_message(
                "Only the person who requested help (or staff) can cancel this.", ephemeral=True
            )
            return

        entry["cancelled"] = True
        save_help_requests(requests_)

        try:
            msg = interaction.message
            embed = msg.embeds[0]
            embed.title = "✅ Help Request Resolved"
            embed.color = discord.Color.dark_grey()
            for item in self.children:
                item.disabled = True
            await msg.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

        await interaction.response.send_message("Marked as resolved. Thanks for letting us know!", ephemeral=True)


class RebootView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Reboot Now", emoji="🔄", style=discord.ButtonStyle.danger, custom_id="owner_reboot_now")
    async def reboot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if OWNER_ID and interaction.user.id != OWNER_ID:
            await interaction.response.send_message("Only the server owner can trigger this.", ephemeral=True)
            return
        await interaction.response.send_message(
            "🔄 Rebooting now. A fresh cycle was already queued when this run started, so I should "
            "be back online within a minute or two.",
            ephemeral=True,
        )
        await bot.close()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    global _ready_once
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not _ready_once:
        _ready_once = True

        # Re-register persistent views for every still-active training / help request
        # so their buttons keep working across restarts.
        active = load_active_trainings()
        for tid, entry in active.items():
            if not entry.get("cancelled"):
                bot.add_view(TrainingView(tid))

        help_requests = load_help_requests()
        for hid, entry in help_requests.items():
            if not entry.get("cancelled"):
                bot.add_view(HelpView(hid))

        if OWNER_ID:
            bot.add_view(RebootView())

        if not training_reminder_loop.is_running():
            training_reminder_loop.start()
        if not shutdown_warning_loop.is_running():
            shutdown_warning_loop.start()

    if GUILD_OBJ:
        try:
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            print(f"Synced {len(synced)} slash commands to guild {GUILD_ID}")
        except discord.HTTPException as e:
            print(f"Failed to sync commands: {e}")
    print("Shiketsu Prefect is online and ready.")


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


@tasks.loop(seconds=30)
async def training_reminder_loop():
    """Sends a DM to attendees 5 minutes before a training starts, and cleans up
    old entries. Driven entirely off persisted wall-clock data so it survives
    process restarts cleanly."""
    trainings = load_active_trainings()
    if not trainings:
        return

    now = datetime.now(timezone.utc)
    changed = False
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None

    for entry in trainings.values():
        if entry.get("cancelled") or entry.get("reminded"):
            continue
        try:
            start_dt = datetime.fromisoformat(entry["start_ts"])
        except (KeyError, ValueError):
            continue

        if now >= start_dt - timedelta(minutes=5):
            for uid in entry.get("attendees", []):
                member = guild.get_member(uid) if guild else None
                try:
                    target = member or await bot.fetch_user(uid)
                    start_ts = int(start_dt.timestamp())
                    await target.send(
                        f"⏰ Reminder: the training you signed up for starts <t:{start_ts}:R>!\n"
                        f"Server link: {entry.get('server_link', 'N/A')}\n"
                        f"Notes: {entry.get('notes', '-')}"
                    )
                except discord.HTTPException:
                    pass
            entry["reminded"] = True
            changed = True

    if changed:
        save_active_trainings(trainings)


@training_reminder_loop.before_loop
async def before_training_reminder_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def shutdown_warning_loop():
    """DMs the owner ~30 minutes before this run is expected to hit its GitHub
    Actions timeout, with a button to reboot early instead of waiting it out."""
    global _shutdown_warning_sent
    if _shutdown_warning_sent or not OWNER_ID:
        return

    elapsed_minutes = (datetime.now(timezone.utc) - _process_start).total_seconds() / 60
    if elapsed_minutes < (TIMEOUT_MINUTES - SHUTDOWN_WARNING_MINUTES_BEFORE):
        return

    try:
        owner = bot.get_user(OWNER_ID) or await bot.fetch_user(OWNER_ID)
        embed = discord.Embed(
            title="🔁 Restarting soon",
            description=(
                f"I'll automatically restart in about {SHUTDOWN_WARNING_MINUTES_BEFORE} minutes as part "
                "of my normal ~6-hour cycle. A fresh run is already queued and will pick up right after "
                "this one ends (usually under a minute of downtime).\n\n"
                "You don't need to do anything — but if you'd rather restart right now instead of waiting, "
                "hit the button below."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        await owner.send(embed=embed, view=RebootView())
        _shutdown_warning_sent = True
    except discord.HTTPException:
        pass


@shutdown_warning_loop.before_loop
async def before_shutdown_warning_loop():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Automod
# ---------------------------------------------------------------------------

INVITE_RE = re.compile(r"(discord\.gg/|discord(?:app)?\.com/invite/)[a-zA-Z0-9\-]+", re.IGNORECASE)

# rolling message timestamps per user, for spam detection
_recent_messages: dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
SPAM_COUNT = 5
SPAM_WINDOW_SECONDS = 5

# channels automod should never act in (staff areas)
AUTOMOD_EXEMPT_CHANNEL_NAMES = {"mod-chat", "logs", "tasks", "security-logs", "transcripts", "dev-log"}


async def automod_warn(message: discord.Message, reason: str, delete: bool = True):
    if delete:
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    warnings = load_warnings()
    uid = str(message.author.id)
    warnings.setdefault(uid, []).append({
        "reason": f"[Automod] {reason}",
        "moderator": str(bot.user),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    save_warnings(warnings)

    embed = build_mod_embed(
        "Automod Action", message.author, bot.user, reason,
        discord.Color.orange(), extra={"Channel": message.channel.mention}
    )
    await send_mod_log(message.guild, embed)

    try:
        await message.channel.send(
            f"{message.author.mention} that message was removed by automod ({reason}).",
            delete_after=6,
        )
    except discord.HTTPException:
        pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    if isinstance(message.author, discord.Member) and is_staff(message.author):
        await bot.process_commands(message)
        return

    if getattr(message.channel, "name", "") in AUTOMOD_EXEMPT_CHANNEL_NAMES:
        await bot.process_commands(message)
        return

    # --- invite link filter ---
    if INVITE_RE.search(message.content):
        await automod_warn(message, "posting a Discord invite link")
        return

    # --- excessive caps filter ---
    letters = [c for c in message.content if c.isalpha()]
    if len(letters) >= 12:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.7:
            await automod_warn(message, "excessive caps", delete=True)
            return

    # --- spam filter ---
    now = datetime.now(timezone.utc)
    dq = _recent_messages[message.author.id]
    dq.append(now)
    recent = [t for t in dq if (now - t).total_seconds() <= SPAM_WINDOW_SECONDS]
    if len(recent) >= SPAM_COUNT:
        try:
            await message.channel.purge(
                limit=10,
                check=lambda m: m.author.id == message.author.id,
                after=now - timedelta(seconds=SPAM_WINDOW_SECONDS + 1),
            )
        except discord.HTTPException:
            pass

        if isinstance(message.author, discord.Member):
            try:
                await message.author.timeout(timedelta(minutes=5), reason="Automod: spamming")
            except discord.HTTPException:
                pass

        embed = build_mod_embed(
            "Automod Timeout (Spam)", message.author, bot.user,
            "Sending messages too quickly", discord.Color.red(),
        )
        await send_mod_log(message.guild, embed)
        dq.clear()
        return

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Moderation commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="kick", description="Kick a member from the server.", guild=GUILD_OBJ)
@app_commands.describe(member="The member to kick", reason="Reason for the kick")
@staff_check()
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You can't kick someone with an equal or higher role.", ephemeral=True)
        return
    await member.kick(reason=f"{interaction.user}: {reason}")
    embed = build_mod_embed("Member Kicked", member, interaction.user, reason, discord.Color.red())
    await send_mod_log(interaction.guild, embed)
    await interaction.response.send_message(f"👢 Kicked {member.mention}.", ephemeral=True)


@bot.tree.command(name="ban", description="Ban a member from the server.", guild=GUILD_OBJ)
@app_commands.describe(member="The member to ban", reason="Reason for the ban", delete_days="Days of message history to delete (0-7)")
@staff_check()
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided", delete_days: app_commands.Range[int, 0, 7] = 0):
    if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You can't ban someone with an equal or higher role.", ephemeral=True)
        return
    await member.ban(reason=f"{interaction.user}: {reason}", delete_message_days=delete_days)
    embed = build_mod_embed("Member Banned", member, interaction.user, reason, discord.Color.dark_red())
    await send_mod_log(interaction.guild, embed)
    await interaction.response.send_message(f"🔨 Banned {member.mention}.", ephemeral=True)


@bot.tree.command(name="timeout", description="Timeout (mute) a member for a set duration.", guild=GUILD_OBJ)
@app_commands.describe(member="The member to timeout", minutes="Duration in minutes (max 40320 / 28 days)", reason="Reason for the timeout")
@staff_check()
async def timeout_cmd(interaction: discord.Interaction, member: discord.Member, minutes: app_commands.Range[int, 1, 40320], reason: str = "No reason provided"):
    if member.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("You can't timeout someone with an equal or higher role.", ephemeral=True)
        return
    await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}")
    embed = build_mod_embed("Member Timed Out", member, interaction.user, reason, discord.Color.orange(), extra={"Duration": f"{minutes} minutes"})
    await send_mod_log(interaction.guild, embed)
    await interaction.response.send_message(f"⏱️ Timed out {member.mention} for {minutes} minutes.", ephemeral=True)


@bot.tree.command(name="untimeout", description="Remove an active timeout from a member.", guild=GUILD_OBJ)
@app_commands.describe(member="The member to remove the timeout from")
@staff_check()
async def untimeout_cmd(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None, reason=f"Timeout removed by {interaction.user}")
    embed = build_mod_embed("Timeout Removed", member, interaction.user, "-", discord.Color.green())
    await send_mod_log(interaction.guild, embed)
    await interaction.response.send_message(f"✅ Removed timeout from {member.mention}.", ephemeral=True)


@bot.tree.command(name="warn", description="Issue a formal warning to a member.", guild=GUILD_OBJ)
@app_commands.describe(member="The member to warn", reason="Reason for the warning")
@staff_check()
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    warnings = load_warnings()
    uid = str(member.id)
    warnings.setdefault(uid, []).append({
        "reason": reason,
        "moderator": str(interaction.user),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    save_warnings(warnings)
    count = len(warnings[uid])

    embed = build_mod_embed("Member Warned", member, interaction.user, reason, discord.Color.yellow(), extra={"Total Warnings": str(count)})
    await send_mod_log(interaction.guild, embed)
    await interaction.response.send_message(f"⚠️ Warned {member.mention} (warning #{count}).", ephemeral=True)

    try:
        await member.send(f"You received a warning in **{interaction.guild.name}**: {reason}")
    except discord.HTTPException:
        pass


@bot.tree.command(name="warnings", description="View a member's warning history.", guild=GUILD_OBJ)
@app_commands.describe(member="The member to check")
@staff_check()
async def warnings_cmd(interaction: discord.Interaction, member: discord.Member):
    warnings = load_warnings()
    entries = warnings.get(str(member.id), [])
    if not entries:
        await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
        return

    embed = discord.Embed(title=f"Warnings for {member}", color=discord.Color.yellow())
    for i, w in enumerate(entries[-10:], start=1):
        embed.add_field(
            name=f"#{i} - {w['timestamp'][:10]}",
            value=f"{w['reason']} (by {w['moderator']})",
            inline=False,
        )
    embed.set_footer(text=f"Total warnings: {len(entries)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearwarnings", description="Clear all warnings for a member.", guild=GUILD_OBJ)
@app_commands.describe(member="The member to clear warnings for")
@staff_check()
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    warnings = load_warnings()
    warnings.pop(str(member.id), None)
    save_warnings(warnings)
    embed = build_mod_embed("Warnings Cleared", member, interaction.user, "-", discord.Color.green())
    await send_mod_log(interaction.guild, embed)
    await interaction.response.send_message(f"🧹 Cleared warnings for {member.mention}.", ephemeral=True)


@bot.tree.command(name="purge", description="Bulk delete recent messages in this channel.", guild=GUILD_OBJ)
@app_commands.describe(amount="Number of messages to delete (1-100)")
@staff_check()
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    embed = build_mod_embed(
        "Messages Purged", interaction.user, interaction.user,
        f"{len(deleted)} messages deleted in {interaction.channel.mention}",
        discord.Color.blurple(),
    )
    await send_mod_log(interaction.guild, embed)
    await interaction.followup.send(f"🧹 Deleted {len(deleted)} messages.", ephemeral=True)


# ---------------------------------------------------------------------------
# Training system
# ---------------------------------------------------------------------------


@bot.tree.command(name="host_training", description="Announce a training session with RSVP + reminder.", guild=GUILD_OBJ)
@app_commands.describe(
    starts_in="Minutes from now until the training starts (e.g. 30)",
    server_link="Private server link/code for attendees to join",
    notes="Any extra details for attendees",
)
@trainer_check()
async def host_training(
    interaction: discord.Interaction,
    starts_in: app_commands.Range[int, 1, 10080],
    server_link: str,
    notes: str = "No additional notes.",
):
    channel = interaction.guild.get_channel(TRAINING_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message("Training channel isn't configured or couldn't be found.", ephemeral=True)
        return

    server_link = normalize_link(server_link)
    start_dt = datetime.now(timezone.utc) + timedelta(minutes=starts_in)
    start_ts = int(start_dt.timestamp())
    training_id = f"t{int(datetime.now(timezone.utc).timestamp())}"

    embed = discord.Embed(
        title="📢 Training Session Announced",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Host", value=interaction.user.mention, inline=True)
    embed.add_field(name="Starts", value=f"<t:{start_ts}:F> (<t:{start_ts}:R>)", inline=True)
    embed.add_field(name="Server Link", value=link_field(server_link), inline=False)
    embed.add_field(name="Notes", value=notes, inline=False)
    embed.add_field(name="Attending", value="No one yet — be the first!", inline=False)
    embed.set_footer(text="Click below to get a DM reminder 5 minutes before it starts.")

    view = TrainingView(training_id)
    msg = await channel.send(embed=embed, view=view)

    active = load_active_trainings()
    active[training_id] = {
        "host_id": interaction.user.id,
        "channel_id": channel.id,
        "message_id": msg.id,
        "server_link": server_link,
        "notes": notes,
        "start_ts": start_dt.isoformat(),
        "attendees": [],
        "reminded": False,
        "cancelled": False,
    }
    save_active_trainings(active)

    await interaction.response.send_message(
        f"✅ Training announced in {channel.mention}, starting <t:{start_ts}:R>.", ephemeral=True
    )


@bot.tree.command(name="cancel_training", description="Cancel your most recently announced training session.", guild=GUILD_OBJ)
@trainer_check()
async def cancel_training(interaction: discord.Interaction):
    active = load_active_trainings()
    mine = [
        (tid, e) for tid, e in active.items()
        if e.get("host_id") == interaction.user.id and not e.get("cancelled")
    ]
    if not mine:
        await interaction.response.send_message("You don't have an active training session to cancel.", ephemeral=True)
        return

    tid, entry = max(mine, key=lambda kv: kv[1]["start_ts"])
    entry["cancelled"] = True
    save_active_trainings(active)

    channel = interaction.guild.get_channel(entry["channel_id"])
    if channel is not None:
        try:
            msg = await channel.fetch_message(entry["message_id"])
            embed = msg.embeds[0] if msg.embeds else discord.Embed(title="Training Session")
            embed.title = "❌ Training Session Cancelled"
            embed.color = discord.Color.dark_grey()
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    for uid in entry.get("attendees", []):
        member = interaction.guild.get_member(uid)
        if member:
            try:
                await member.send(f"❌ The training you signed up for in **{interaction.guild.name}** was cancelled by the host.")
            except discord.HTTPException:
                pass

    await interaction.response.send_message("✅ Training session cancelled and attendees notified.", ephemeral=True)


# ---------------------------------------------------------------------------
# Call-help system
# ---------------------------------------------------------------------------


@bot.tree.command(name="call_help", description="Post a help request - your server link and what's happening.", guild=GUILD_OBJ)
@app_commands.describe(
    server_link="Link/code to join your game server",
    issue="What's happening - who's teaming you, being toxic, etc.",
)
async def call_help(interaction: discord.Interaction, server_link: str, issue: str):
    channel = interaction.guild.get_channel(ASK_FOR_HELP_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message("The ask-for-help channel isn't configured or couldn't be found.", ephemeral=True)
        return

    server_link = normalize_link(server_link)
    help_id = f"h{int(datetime.now(timezone.utc).timestamp())}"

    embed = discord.Embed(
        title="🆘 Help Requested",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Requested by", value=interaction.user.mention, inline=True)
    embed.add_field(name="Server Link", value=link_field(server_link), inline=True)
    embed.add_field(name="What's happening", value=issue, inline=False)
    embed.add_field(name="Helpers", value="No one yet — be the first!", inline=False)
    embed.set_footer(text="Click below if you can help. The requester can cancel once it's sorted.")

    help_role = discord.utils.get(interaction.guild.roles, name="Help pings")
    ping_content = f"{help_role.mention} {interaction.user.mention}" if help_role else interaction.user.mention

    view = HelpView(help_id)
    msg = await channel.send(
        content=ping_content,
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(users=True, roles=True),
    )

    requests_ = load_help_requests()
    requests_[help_id] = {
        "requester_id": interaction.user.id,
        "channel_id": channel.id,
        "message_id": msg.id,
        "server_link": server_link,
        "issue": issue,
        "helpers": [],
        "cancelled": False,
    }
    save_help_requests(requests_)

    await interaction.response.send_message(f"✅ Help request posted in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="cancel_help", description="Cancel your most recent active help request.", guild=GUILD_OBJ)
async def cancel_help(interaction: discord.Interaction):
    requests_ = load_help_requests()
    mine = [
        (hid, e) for hid, e in requests_.items()
        if e.get("requester_id") == interaction.user.id and not e.get("cancelled")
    ]
    if not mine:
        await interaction.response.send_message("You don't have an active help request to cancel.", ephemeral=True)
        return

    hid, entry = max(mine, key=lambda kv: kv[0])
    entry["cancelled"] = True
    save_help_requests(requests_)

    channel = interaction.guild.get_channel(entry["channel_id"])
    if channel is not None:
        try:
            msg = await channel.fetch_message(entry["message_id"])
            embed = msg.embeds[0] if msg.embeds else discord.Embed(title="Help Request")
            embed.title = "✅ Help Request Resolved"
            embed.color = discord.Color.dark_grey()
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    await interaction.response.send_message("✅ Help request cancelled. Glad it's sorted!", ephemeral=True)


# ---------------------------------------------------------------------------
# Misc utility
# ---------------------------------------------------------------------------


@bot.tree.command(name="ping", description="Check that the bot is online and responsive.", guild=GUILD_OBJ)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latency: {round(bot.latency * 1000)}ms", ephemeral=True)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
