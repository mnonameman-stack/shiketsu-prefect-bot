"""
Shiketsu Prefect - Custom moderation bot for the Shiketsu High Discord server.

Features:
  - Core moderation: /kick, /ban, /timeout, /untimeout, /warn, /warnings, /clearwarnings, /purge
    (restricted to Hall Monitor / Student Council / Administrator-tier staff)
  - Mod-log: every moderation action is posted as an embed to the #logs channel
  - Automod: spam, invite-link, and excessive-caps filtering with warn/delete actions
  - Training system: /host_training and /cancel_training for trainers (Faculty Head /
    Pro Hero Instructor and above) to announce and cancel training sessions

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
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration - loaded from environment variables (see .env)
# ---------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
TRAINING_CHANNEL_ID = int(os.getenv("TRAINING_CHANNEL_ID", "0"))

MOD_ROLE_IDS = {
    int(x) for x in os.getenv("MOD_ROLE_IDS", "").split(",") if x.strip()
}
TRAINER_ROLE_IDS = {
    int(x) for x in os.getenv("TRAINER_ROLE_IDS", "").split(",") if x.strip()
}

WARNINGS_FILE = os.path.join(os.path.dirname(__file__), "warnings.json")
ACTIVE_TRAININGS_FILE = os.path.join(os.path.dirname(__file__), "active_trainings.json")

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


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


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
# Events
# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if GUILD_OBJ:
        try:
            synced = await bot.tree.sync(guild=GUILD_OBJ)
            print(f"Synced {len(synced)} slash commands to guild {GUILD_ID}")
        except discord.HTTPException as e:
            print(f"Failed to sync commands: {e}")
    print("Shiketsu Prefect is online and ready.")


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


@bot.tree.command(name="host_training", description="Announce a training session.", guild=GUILD_OBJ)
@app_commands.describe(time="When the training is happening (e.g. 'Today 8PM EST')", notes="Any extra details for attendees")
@trainer_check()
async def host_training(interaction: discord.Interaction, time: str, notes: str = "No additional notes."):
    channel = interaction.guild.get_channel(TRAINING_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message("Training channel isn't configured or couldn't be found.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📢 Training Session Announced",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Host", value=interaction.user.mention, inline=True)
    embed.add_field(name="Time", value=time, inline=True)
    embed.add_field(name="Notes", value=notes, inline=False)
    embed.set_footer(text="Use /cancel_training to cancel this session.")

    msg = await channel.send(embed=embed)

    active = load_active_trainings()
    active[str(interaction.user.id)] = {"channel_id": channel.id, "message_id": msg.id}
    save_active_trainings(active)

    await interaction.response.send_message(f"✅ Training announced in {channel.mention}.", ephemeral=True)


@bot.tree.command(name="cancel_training", description="Cancel your most recently announced training session.", guild=GUILD_OBJ)
@trainer_check()
async def cancel_training(interaction: discord.Interaction):
    active = load_active_trainings()
    entry = active.pop(str(interaction.user.id), None)
    save_active_trainings(active)

    if entry is None:
        await interaction.response.send_message("You don't have an active training session to cancel.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(entry["channel_id"])
    if channel is not None:
        try:
            msg = await channel.fetch_message(entry["message_id"])
            embed = msg.embeds[0] if msg.embeds else discord.Embed(title="Training Session")
            embed.title = "❌ Training Session Cancelled"
            embed.color = discord.Color.dark_grey()
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass

    await interaction.response.send_message("✅ Training session cancelled.", ephemeral=True)


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
