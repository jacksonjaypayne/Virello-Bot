import os
import json
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials

# =========================
# CONFIG
# =========================
PRIVATE_GUILD_ID = 1391768913632559296
PUBLIC_GUILD_ID = 1360621902674006176
ALLOWED_GUILD_IDS = [PRIVATE_GUILD_ID, PUBLIC_GUILD_ID]

TIMER_CHANNEL_ID = 1490304994975416451
COMPOUND_STATUS_CHANNEL_ID = 1490340715761242292

PUBLIC_PRICE_CATEGORY_ID = 1361004589444239431

WARNING_MINUTES = 5
DEFAULT_DURATION = "01:00:00"

# Google Sheets
SPREADSHEET_ID = "15NdYUrKpDQ8_gVktxUvOJ-dWyObgoN4cPpN28XQy8N8"
SHEET_NAME = "selling items"

# Column B = item name, Column D = price
ITEM_COLUMN_INDEX = 1
PRICE_COLUMN_INDEX = 3

# =========================
# LOAD ENV
# =========================
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN not found")

if not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not found")

# =========================
# BOT SETUP
# =========================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# DATABASE
# =========================
conn = sqlite3.connect("timers.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS timers (
    guild_id INTEGER NOT NULL,
    gang_name TEXT NOT NULL,
    end_time TEXT NOT NULL,
    channel_id INTEGER NOT NULL
)
""")
conn.commit()

timer_tasks: dict[tuple[int, str], asyncio.Task] = {}
warning_tasks: dict[tuple[int, str], asyncio.Task] = {}

# =========================
# GOOGLE SHEETS
# =========================
def get_gspread_client() -> gspread.Client:
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    creds = Credentials.from_service_account_info(
        service_account_info,
        scopes=scopes
    )

    return gspread.authorize(creds)


def read_price_list() -> list[tuple[str, str]]:
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.worksheet(SHEET_NAME)

    rows = worksheet.get_all_values()
    items: list[tuple[str, str]] = []

    for row in rows[1:]:
        item = row[ITEM_COLUMN_INDEX].strip() if len(row) > ITEM_COLUMN_INDEX else ""
        price = row[PRICE_COLUMN_INDEX].strip() if len(row) > PRICE_COLUMN_INDEX else ""

        if item and price:
            items.append((item, price))

    return items


async def get_price_list() -> list[tuple[str, str]]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, read_price_list)

# =========================
# HELPERS
# =========================
def parse_duration(duration: str) -> timedelta:
    parts = duration.split(":")

    if len(parts) != 3:
        raise ValueError("Use HH:MM:SS format, for example 01:00:00.")

    try:
        hours, minutes, seconds = map(int, parts)
    except ValueError:
        raise ValueError("Hours, minutes, and seconds must all be numbers.")

    if hours < 0 or minutes < 0 or seconds < 0:
        raise ValueError("Time values cannot be negative.")

    if minutes > 59 or seconds > 59:
        raise ValueError("Minutes and seconds must be between 00 and 59.")

    td = timedelta(hours=hours, minutes=minutes, seconds=seconds)

    if td.total_seconds() <= 0:
        raise ValueError("Duration must be greater than 00:00:00.")

    return td


def timer_key(guild_id: int, gang: str) -> tuple[int, str]:
    return guild_id, gang.strip().lower()


def parse_db_time(end_time_str: str) -> datetime:
    dt = datetime.fromisoformat(end_time_str)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def format_remaining(td: timedelta) -> str:
    total_seconds = max(0, int(td.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{hours:02}:{minutes:02}:{seconds:02}"


def format_discord_time(dt: datetime) -> str:
    return f"<t:{int(dt.timestamp())}:F>"


def get_timer_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel = guild.get_channel(TIMER_CHANNEL_ID)
    return channel if isinstance(channel, discord.TextChannel) else None


def get_compound_status_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel = guild.get_channel(COMPOUND_STATUS_CHANNEL_ID)
    return channel if isinstance(channel, discord.TextChannel) else None


def is_public_price_channel(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False

    if interaction.guild.id != PUBLIC_GUILD_ID:
        return False

    if not isinstance(interaction.channel, discord.TextChannel):
        return False

    return interaction.channel.category_id == PUBLIC_PRICE_CATEGORY_ID


def make_error_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=discord.Color.red()
    )


async def ensure_private_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Invalid Location",
                "This command can only be used in a server."
            ),
            ephemeral=True
        )
        return False

    if interaction.guild.id != PRIVATE_GUILD_ID:
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Command Not Allowed Here",
                "This command can only be used in the private server."
            ),
            ephemeral=True
        )
        return False

    return True


async def acknowledge_redirect(interaction: discord.Interaction, action_text: str) -> None:
    await interaction.response.send_message(
        f"✅ {action_text} in <#{TIMER_CHANNEL_ID}>.",
        ephemeral=True
    )


async def ensure_private_guild_and_timer_channel(
    interaction: discord.Interaction,
) -> tuple[discord.Guild | None, discord.TextChannel | None]:
    if not await ensure_private_guild(interaction):
        return None, None

    timer_channel = get_timer_channel(interaction.guild)

    if timer_channel is None:
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Missing Timer Channel",
                f"I couldn't find <#{TIMER_CHANNEL_ID}>."
            ),
            ephemeral=True
        )
        return interaction.guild, None

    return interaction.guild, timer_channel


def build_price_embeds(items: list[tuple[str, str]]) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []

    if not items:
        embed = discord.Embed(
            title="📋 Price List",
            description="No priced items were found in the sheet.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Source: Google Sheets")
        return [embed]

    lines = [f"**{item}** — {price}" for item, price in items]

    current_chunk: list[str] = []
    current_length = 0
    max_description_length = 3800

    for line in lines:
        line_length = len(line) + 1

        if current_length + line_length > max_description_length and current_chunk:
            embed = discord.Embed(
                title="📋 Price List" if not embeds else "📋 Price List (cont.)",
                description="\n".join(current_chunk),
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text="Source: Google Sheets")
            embeds.append(embed)

            current_chunk = [line]
            current_length = line_length
        else:
            current_chunk.append(line)
            current_length += line_length

    if current_chunk:
        embed = discord.Embed(
            title="📋 Price List" if not embeds else "📋 Price List (cont.)",
            description="\n".join(current_chunk),
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="Source: Google Sheets")
        embeds.append(embed)

    total = len(embeds)

    for i, embed in enumerate(embeds, start=1):
        embed.set_author(name=f"Page {i}/{total} • {len(items)} priced items")

    return embeds

# =========================
# TIMER TASKS
# =========================
async def send_warning(
    guild_id: int,
    channel_id: int,
    gang: str,
    warning_time: datetime,
) -> None:
    try:
        await discord.utils.sleep_until(warning_time)

        guild = bot.get_guild(guild_id)

        if guild is None:
            return

        channel = guild.get_channel(channel_id)

        if not isinstance(channel, discord.TextChannel):
            return

        end_time = warning_time + timedelta(minutes=WARNING_MINUTES)

        embed = discord.Embed(
            title="⚠️ Cooldown Warning",
            description=f"**{gang}** is almost off cooldown.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(name="Time Remaining", value=f"{WARNING_MINUTES} minutes", inline=True)
        embed.add_field(name="Finishes", value=format_discord_time(end_time), inline=True)
        embed.set_footer(text="Cooldown alert")

        await channel.send(embed=embed)

    except asyncio.CancelledError:
        pass

    except Exception as e:
        print(f"Warning task error for {gang}: {e}")

    finally:
        warning_tasks.pop(timer_key(guild_id, gang), None)


async def finish_timer(
    guild_id: int,
    channel_id: int,
    gang: str,
    end_time: datetime,
) -> None:
    try:
        await discord.utils.sleep_until(end_time)

        guild = bot.get_guild(guild_id)

        if guild is None:
            return

        channel = guild.get_channel(channel_id)

        if isinstance(channel, discord.TextChannel):
            embed = discord.Embed(
                title="⏰ Cooldown Finished",
                description=f"**{gang}** is now off cooldown.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )

            embed.add_field(name="Finished At", value=format_discord_time(end_time), inline=False)
            embed.set_footer(text="Cooldown complete")

            await channel.send(embed=embed)

        cursor.execute(
            "DELETE FROM timers WHERE guild_id=? AND gang_name=?",
            (guild_id, gang.lower())
        )
        conn.commit()

    except asyncio.CancelledError:
        pass

    except Exception as e:
        print(f"Finish timer error for {gang}: {e}")

    finally:
        timer_tasks.pop(timer_key(guild_id, gang), None)


def schedule_timer_tasks(
    guild_id: int,
    channel_id: int,
    gang: str,
    end_time: datetime,
) -> None:
    key = timer_key(guild_id, gang)

    old_timer = timer_tasks.get(key)

    if old_timer and not old_timer.done():
        old_timer.cancel()

    old_warning = warning_tasks.get(key)

    if old_warning and not old_warning.done():
        old_warning.cancel()

    timer_tasks[key] = asyncio.create_task(
        finish_timer(guild_id, channel_id, gang, end_time)
    )

    warning_time = end_time - timedelta(minutes=WARNING_MINUTES)

    if warning_time > datetime.now(timezone.utc):
        warning_tasks[key] = asyncio.create_task(
            send_warning(guild_id, channel_id, gang, warning_time)
        )

# =========================
# READY EVENT
# =========================
@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")

    try:
        for guild_id in ALLOWED_GUILD_IDS:
            guild = discord.Object(id=guild_id)

            bot.tree.clear_commands(guild=guild)
            bot.tree.copy_global_to(guild=guild)

            synced = await bot.tree.sync(guild=guild)

            print(
                f"Synced {len(synced)} commands to guild {guild_id}: "
                f"{[c.name for c in synced]}"
            )

    except Exception as e:
        print(f"Sync error: {e}")

    cursor.execute("SELECT guild_id, gang_name, end_time, channel_id FROM timers")
    rows = cursor.fetchall()

    now = datetime.now(timezone.utc)

    for guild_id, gang_name, end_time_str, channel_id in rows:
        try:
            end_time = parse_db_time(end_time_str)

            if end_time > now:
                schedule_timer_tasks(guild_id, channel_id, gang_name, end_time)
            else:
                cursor.execute(
                    "DELETE FROM timers WHERE guild_id=? AND gang_name=?",
                    (guild_id, gang_name.lower())
                )
                conn.commit()

        except Exception as e:
            print(f"Failed to reload timer for {gang_name}: {e}")

# =========================
# COMMANDS - PRIVATE ONLY
# =========================
@bot.tree.command(name="time", description="Start cooldown")
@app_commands.describe(
    gang="Gang name",
    duration="HH:MM:SS format. Leave blank for 1 hour"
)
async def time_cmd(
    interaction: discord.Interaction,
    gang: str,
    duration: str = DEFAULT_DURATION,
) -> None:
    guild, timer_channel = await ensure_private_guild_and_timer_channel(interaction)

    if guild is None or timer_channel is None:
        return

    gang = gang.strip()

    try:
        delta = parse_duration(duration)

    except ValueError as e:
        await interaction.response.send_message(
            embed=make_error_embed("❌ Invalid Duration", str(e)),
            ephemeral=True
        )
        return

    cursor.execute(
        "SELECT end_time FROM timers WHERE guild_id=? AND gang_name=?",
        (guild.id, gang.lower())
    )
    existing = cursor.fetchone()

    if existing:
        existing_end = parse_db_time(existing[0])
        remaining = existing_end - datetime.now(timezone.utc)

        if remaining.total_seconds() > 0:
            embed = discord.Embed(
                title="⚠️ Timer Already Active",
                description=f"**{gang}** already has an active cooldown.",
                color=discord.Color.orange()
            )
            embed.add_field(name="Remaining", value=format_remaining(remaining), inline=True)
            embed.add_field(name="Ends", value=format_discord_time(existing_end), inline=True)

            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        cursor.execute(
            "DELETE FROM timers WHERE guild_id=? AND gang_name=?",
            (guild.id, gang.lower())
        )
        conn.commit()

    end_time = datetime.now(timezone.utc) + delta

    cursor.execute(
        "INSERT INTO timers (guild_id, gang_name, end_time, channel_id) VALUES (?, ?, ?, ?)",
        (guild.id, gang.lower(), end_time.isoformat(), TIMER_CHANNEL_ID)
    )
    conn.commit()

    schedule_timer_tasks(guild.id, TIMER_CHANNEL_ID, gang, end_time)

    embed = discord.Embed(
        title="✅ Cooldown Started",
        description=f"Cooldown started for **{gang}**.",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Warning", value=f"{WARNING_MINUTES} mins before end", inline=True)
    embed.add_field(name="Ends", value=format_discord_time(end_time), inline=False)
    embed.set_footer(text=f"Started by {interaction.user.display_name}")

    await timer_channel.send(embed=embed)
    await acknowledge_redirect(interaction, "Posted cooldown")


@bot.tree.command(name="grace", description="Start a grace timer")
@app_commands.describe(
    gang="Gang name",
    start_time="Example: 7:00PM",
    duration="HH:MM:SS format"
)
async def grace_cmd(
    interaction: discord.Interaction,
    gang: str,
    start_time: str,
    duration: str
) -> None:
    guild, timer_channel = await ensure_private_guild_and_timer_channel(interaction)

    if guild is None or timer_channel is None:
        return

    try:
        delta = parse_duration(duration)

    except ValueError as e:
        await interaction.response.send_message(
            embed=make_error_embed("❌ Invalid Duration", str(e)),
            ephemeral=True
        )
        return

    try:
        parsed_start = datetime.strptime(start_time.upper(), "%I:%M%p")

    except ValueError:
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Invalid Start Time",
                "Use format like `7:00PM` or `11:30AM`."
            ),
            ephemeral=True
        )
        return

    now = datetime.now(timezone.utc)

    start_datetime = now.replace(
        hour=parsed_start.hour,
        minute=parsed_start.minute,
        second=0,
        microsecond=0
    )

    end_datetime = start_datetime + delta

    embed = discord.Embed(
        title="✅ Grace Started",
        description=f"Grace period started for **{gang}**.",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Warning", value="No warning", inline=True)
    embed.add_field(name="Starts", value=format_discord_time(start_datetime), inline=False)
    embed.add_field(name="Ends", value=format_discord_time(end_datetime), inline=False)

    embed.set_footer(text=f"Started by {interaction.user.display_name}")

    await timer_channel.send(embed=embed)

    await interaction.response.send_message(
        f"✅ Posted grace timer for **{gang}** in <#{TIMER_CHANNEL_ID}>.",
        ephemeral=True
    )


@bot.tree.command(name="timecheck", description="Check timer")
@app_commands.describe(gang="Gang name")
async def timecheck_cmd(interaction: discord.Interaction, gang: str) -> None:
    guild, timer_channel = await ensure_private_guild_and_timer_channel(interaction)

    if guild is None or timer_channel is None:
        return

    cursor.execute(
        "SELECT end_time FROM timers WHERE guild_id=? AND gang_name=?",
        (guild.id, gang.lower())
    )
    row = cursor.fetchone()

    if not row:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="📭 No Timer Found",
                description=f"No active cooldown was found for **{gang}**.",
                color=discord.Color.light_grey()
            ),
            ephemeral=True
        )
        return

    end_time = parse_db_time(row[0])
    remaining = end_time - datetime.now(timezone.utc)

    if remaining.total_seconds() <= 0:
        embed = discord.Embed(
            title="⏰ Cooldown Finished",
            description=f"**{gang}** is already off cooldown.",
            color=discord.Color.green()
        )
        await timer_channel.send(embed=embed)
        await acknowledge_redirect(interaction, "Posted cooldown status")
        return

    embed = discord.Embed(
        title="⏳ Cooldown Status",
        description=f"Current cooldown for **{gang}**.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Remaining", value=format_remaining(remaining), inline=True)
    embed.add_field(name="Ends", value=format_discord_time(end_time), inline=True)

    await timer_channel.send(embed=embed)
    await acknowledge_redirect(interaction, "Posted cooldown status")


@bot.tree.command(name="timecancel", description="Cancel timer")
@app_commands.describe(gang="Gang name")
async def timecancel_cmd(interaction: discord.Interaction, gang: str) -> None:
    guild, timer_channel = await ensure_private_guild_and_timer_channel(interaction)

    if guild is None or timer_channel is None:
        return

    cursor.execute(
        "SELECT 1 FROM timers WHERE guild_id=? AND gang_name=?",
        (guild.id, gang.lower())
    )
    exists = cursor.fetchone()

    if not exists:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="📭 No Timer Found",
                description=f"No active cooldown was found for **{gang}**.",
                color=discord.Color.light_grey()
            ),
            ephemeral=True
        )
        return

    key = timer_key(guild.id, gang)

    if key in timer_tasks:
        timer_tasks[key].cancel()
        del timer_tasks[key]

    if key in warning_tasks:
        warning_tasks[key].cancel()
        del warning_tasks[key]

    cursor.execute(
        "DELETE FROM timers WHERE guild_id=? AND gang_name=?",
        (guild.id, gang.lower())
    )
    conn.commit()

    embed = discord.Embed(
        title="🛑 Cooldown Cancelled",
        description=f"Cooldown for **{gang}** has been cancelled.",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Cancelled by {interaction.user.display_name}")

    await timer_channel.send(embed=embed)
    await acknowledge_redirect(interaction, "Posted cancellation")


@bot.tree.command(name="timelist", description="Show all timers")
async def timelist_cmd(interaction: discord.Interaction) -> None:
    guild, timer_channel = await ensure_private_guild_and_timer_channel(interaction)

    if guild is None or timer_channel is None:
        return

    cursor.execute(
        "SELECT gang_name, end_time FROM timers WHERE guild_id=? ORDER BY end_time ASC",
        (guild.id,)
    )
    rows = cursor.fetchall()

    now = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="⏳ Active Cooldowns",
        description="Current cooldown timers in this server.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    active_found = False

    for gang_name, end_time_str in rows:
        end_time = parse_db_time(end_time_str)
        remaining = end_time - now

        if remaining.total_seconds() > 0:
            active_found = True
            embed.add_field(
                name=gang_name.capitalize(),
                value=f"Remaining: **{format_remaining(remaining)}**\nEnds: {format_discord_time(end_time)}",
                inline=False
            )

    if not active_found:
        embed = discord.Embed(
            title="📭 No Active Timers",
            description="There are currently no cooldowns running.",
            color=discord.Color.light_grey()
        )

    await timer_channel.send(embed=embed)
    await acknowledge_redirect(interaction, "Posted timer list")


@bot.tree.command(name="unsafe", description="Mark the compound as unsafe")
async def unsafe_cmd(interaction: discord.Interaction) -> None:
    if not await ensure_private_guild(interaction):
        return

    status_channel = get_compound_status_channel(interaction.guild)

    if status_channel is None:
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Missing Status Channel",
                f"I couldn't find <#{COMPOUND_STATUS_CHANNEL_ID}>."
            ),
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🚨 COMPOUND UNSAFE",
        description="Hostiles are currently breaching the compound.",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="Warning",
        value="Do **not** fly into the compound until further notice.",
        inline=False
    )
    embed.set_footer(text=f"Marked unsafe by {interaction.user.display_name}")

    await status_channel.send(embed=embed)
    await interaction.response.send_message(
        f"✅ Posted unsafe alert in <#{COMPOUND_STATUS_CHANNEL_ID}>.",
        ephemeral=True
    )


@bot.tree.command(name="safe", description="Mark the compound as safe")
async def safe_cmd(interaction: discord.Interaction) -> None:
    if not await ensure_private_guild(interaction):
        return

    status_channel = get_compound_status_channel(interaction.guild)

    if status_channel is None:
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Missing Status Channel",
                f"I couldn't find <#{COMPOUND_STATUS_CHANNEL_ID}>."
            ),
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="✅ COMPOUND SAFE",
        description="The compound is now safe for members.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="Status",
        value="Members are clear to fly in.",
        inline=False
    )
    embed.set_footer(text=f"Marked safe by {interaction.user.display_name}")

    await status_channel.send(embed=embed)
    await interaction.response.send_message(
        f"✅ Posted safe alert in <#{COMPOUND_STATUS_CHANNEL_ID}>.",
        ephemeral=True
    )

# =========================
# COMMANDS - PUBLIC ONLY
# =========================
@bot.tree.command(name="pricelist", description="Post the current selling items price list")
async def pricelist_cmd(interaction: discord.Interaction) -> None:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Invalid Location",
                "This command can only be used in a server text channel."
            ),
            ephemeral=True
        )
        return

    if not is_public_price_channel(interaction):
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Command Not Allowed Here",
                "The `/pricelist` command can only be used in the public server inside the allowed category."
            ),
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        items = await get_price_list()
        embeds = build_price_embeds(items)

        for embed in embeds:
            await interaction.channel.send(embed=embed)

        await interaction.followup.send(
            f"✅ Posted price list in {interaction.channel.mention}.",
            ephemeral=True
        )

    except json.JSONDecodeError:
        await interaction.followup.send(
            "❌ GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.",
            ephemeral=True
        )

    except gspread.exceptions.SpreadsheetNotFound:
        await interaction.followup.send(
            "❌ Spreadsheet not found. Check the spreadsheet ID and make sure the sheet is shared with the service account email.",
            ephemeral=True
        )

    except gspread.exceptions.WorksheetNotFound:
        await interaction.followup.send(
            f"❌ Worksheet `{SHEET_NAME}` was not found. Check the tab name exactly.",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ Failed to read the price list: {e}",
            ephemeral=True
        )

# =========================
# RUN
# =========================
bot.run(TOKEN)