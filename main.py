import os
import json
import asyncio
import sqlite3
import io
import math
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import process, fuzz

# =========================
# CONFIG
# =========================
PRIVATE_GUILD_ID = 1391768913632559296
PUBLIC_GUILD_ID = 1360621902674006176
ALLOWED_GUILD_IDS = [PRIVATE_GUILD_ID, PUBLIC_GUILD_ID]

TIMER_CHANNEL_ID = 1490304994975416451
GRACE_CHANNEL_ID = 1560603781551362098
COMMAND_GUIDE_CHANNEL_ID = 1560604669829582860
COMPOUND_STATUS_CHANNEL_ID = 1490340715761242292
KOS_CHANNEL_ID = 1522965909256540190
GANG_STATUS_CHANNEL_ID = 1503068250248839248
GANG_STATUS_MESSAGE_ID = 1560593625761579009

PUBLIC_PRICE_CATEGORY_ID = 1361004589444239431

WARNING_MINUTES = 5
DEFAULT_DURATION = "02:00:00"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")

SPREADSHEET_ID = "15NdYUrKpDQ8_gVktxUvOJ-dWyObgoN4cPpN28XQy8N8"
SHEET_NAME = "warehouse"
VENDOR_SHEET_NAME = "Vendor Locations"
VENDOR_CALIBRATION_SHEET = "Vendor Map Calibration"
VENDOR_MAP_PATH = os.getenv("VENDOR_MAP_PATH", "vendor_map.png")
VENDOR_MAP_CHANNEL_ID = int(os.getenv("VENDOR_MAP_CHANNEL_ID", "1565636448290480220"))
VENDOR_MAP_MESSAGE_ID = 1565642668271206491
# Optional: use a separate PRIVATE channel for door screenshot storage.
VENDOR_IMAGE_CHANNEL_ID = int(os.getenv("VENDOR_IMAGE_CHANNEL_ID", "1565705331076505680"))

# Column B = item name, Column D = sale price
ITEM_COLUMN_INDEX = 1
PRICE_COLUMN_INDEX = 3

# Warehouse reset columns
# E = Item Count, I = Daily Reset Count
WAREHOUSE_ITEM_COUNT_COLUMN = 5
WAREHOUSE_DAILY_RESET_COLUMN = 9
WAREHOUSE_RESET_HOUR = 2
WAREHOUSE_RESET_MINUTE = 0
last_warehouse_reset_date: str | None = None

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
# Set DATABASE_PATH=/data/timers.db when a Railway volume is mounted at /data.
# Never place this database inside a directory replaced during deployment.
DATABASE_PATH = os.getenv("DATABASE_PATH", "timers.db")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)
conn = sqlite3.connect(DATABASE_PATH, timeout=30)
conn.execute("PRAGMA busy_timeout=30000")
print(f"SQLite database: {os.path.abspath(DATABASE_PATH)}")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS timers (
    guild_id INTEGER NOT NULL,
    gang_name TEXT NOT NULL,
    end_time TEXT NOT NULL,
    channel_id INTEGER NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS grace_timers (
    guild_id INTEGER NOT NULL,
    gang_name TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    channel_id INTEGER NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bot_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL
)
""")
cursor.execute(
    "INSERT OR IGNORE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
    ("ev_available", "no")
)
conn.commit()

# Gang status board persistence
cursor.execute("""CREATE TABLE IF NOT EXISTS gang_statuses (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    sort_order INTEGER NOT NULL
)""")
initial_gangs = [
    ("GenCorp", "syndicated", "SHOOTING"),
    ("Sinners MC", "syndicated", "SHOOTING"),
    ("Wraith MC", "syndicated", "SHOOTING"),
    ("Santos Cartel", "syndicated", "SHOOTING"),
    ("Outcasts", "syndicated", "SHOOTING"),
    ("Triads", "syndicated", "SHOOTING"),
    ("LaFamilia", "syndicated", "SHOOTING"),
    ("Error", "syndicated", "SHOOTING"),
    ("Vandals", "syndicated", "SHOOTING"),
    ("Kimura", "syndicated", "SHOOTING"),
    ("Devils", "registered", "NO BEEF"),
    ("Balkans", "registered", "NO BEEF"),
]
# Seed only on first-ever database initialization. If leadership removes all gangs,
# restarting the bot must not silently recreate the default list.
seeded = conn.execute(
    "SELECT 1 FROM bot_settings WHERE setting_key='gang_statuses_initialized'"
).fetchone()
if not seeded:
    if conn.execute("SELECT COUNT(*) FROM gang_statuses").fetchone()[0] == 0:
        conn.executemany(
        "INSERT INTO gang_statuses (name, category, status, sort_order) VALUES (?, ?, ?, ?)",
            [(name, category, status, i) for i, (name, category, status) in enumerate(initial_gangs)]
        )
    conn.execute(
        "INSERT INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
        ("gang_statuses_initialized", "yes")
    )
conn.commit()

def is_ev_available() -> bool:
    row = conn.execute(
        "SELECT setting_value FROM bot_settings WHERE setting_key=?",
        ("ev_available",)
    ).fetchone()
    return bool(row and row[0] == "yes")


timer_tasks: dict[tuple[int, str], asyncio.Task] = {}
warning_tasks: dict[tuple[int, str], asyncio.Task] = {}

# =========================
# GOOGLE SHEETS
# =========================
def get_gspread_client() -> gspread.Client:
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
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


def reset_warehouse_daily_counts() -> int:
    client = get_gspread_client()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    worksheet = spreadsheet.worksheet(SHEET_NAME)

    rows = worksheet.get_all_values()

    updates = []
    updated_count = 0

    for row_num, row in enumerate(rows[1:], start=2):
        daily_reset = (
            row[WAREHOUSE_DAILY_RESET_COLUMN - 1].strip()
            if len(row) >= WAREHOUSE_DAILY_RESET_COLUMN
            else ""
        )

        if not daily_reset:
            continue

        updates.append({
            "range": f"E{row_num}",
            "values": [[daily_reset]]
        })
        updated_count += 1

    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")

    return updated_count


async def run_warehouse_daily_reset() -> int:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, reset_warehouse_daily_counts)


@tasks.loop(minutes=1)
async def warehouse_daily_reset_loop() -> None:
    global last_warehouse_reset_date

    now_sydney = datetime.now(SYDNEY_TZ)
    today_key = now_sydney.date().isoformat()

    if (
        now_sydney.hour == WAREHOUSE_RESET_HOUR
        and now_sydney.minute == WAREHOUSE_RESET_MINUTE
        and last_warehouse_reset_date != today_key
    ):
        try:
            updated_count = await run_warehouse_daily_reset()
            last_warehouse_reset_date = today_key
            print(f"Daily warehouse reset complete. Rows updated: {updated_count}")
        except Exception as e:
            print(f"Daily warehouse reset failed: {e}")

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


def get_kos_channel(guild: discord.Guild) -> discord.TextChannel | None:
    channel = guild.get_channel(KOS_CHANNEL_ID)
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
# GANG STATUS BOARD
# =========================
GANG_STATUSES = ["SHOOTING", "NO BEEF", "WAR", "GRACE"]
GANG_CATEGORIES = ["syndicated", "registered"]

def normalized_gang_name(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())

def build_gang_board() -> str:
    white = "\x1b[1;37m"
    grey = "\x1b[1;30m"
    reset = "\x1b[0m"
    colors = {"SHOOTING": "\x1b[1;33m", "NO BEEF": "\x1b[1;32m", "WAR": "\x1b[1;31m", "GRACE": "\x1b[1;34m"}
    lines = []
    # Fixed column widths keep both sections aligned, including longer gang names.
    gang_width = max(16, min(32, max((len(row[0]) for row in conn.execute(
        "SELECT name FROM gang_statuses").fetchall()), default=0) + 2))
    status_width = 8
    divider = "─" * (gang_width + status_width)
    for category, title in [("syndicated", "SYNDICATED GANGS"), ("registered", "REGISTERED GANGS")]:
        lines.extend([
            f"{white}╔══════════════════════════════╗{reset}",
            f"{white}║{title.center(30)}║{reset}",
            f"{white}╚══════════════════════════════╝{reset}",
            "",
            f"{white}{'Gang':<{gang_width}}Status{reset}",
            f"{grey}{divider}{reset}"
        ])
        rows = conn.execute(
            "SELECT name, status FROM gang_statuses WHERE category=? ORDER BY sort_order, name",
            (category,)
        ).fetchall()
        for name, status in rows:
            # Grace is a temporary display overlay; the saved beef status is untouched.
            grace_rows = conn.execute(
                """SELECT gang_name, start_time, end_time FROM grace_timers
                   WHERE guild_id=? ORDER BY end_time DESC""",
                (PRIVATE_GUILD_ID,)
            ).fetchall()
            grace_row = next(
                ((start, end) for stored_name, start, end in grace_rows
                 if normalized_gang_name(stored_name) == normalized_gang_name(name)),
                None
            )
            if grace_row:
                now = datetime.now(timezone.utc)
                if parse_db_time(grace_row[0]) <= now < parse_db_time(grace_row[1]):
                    status = "GRACE"
            lines.append(f"{name:<{gang_width}}{colors.get(status, white)}{status}{reset}")
        lines.append("")
    result = "```ansi\n" + "\n".join(lines).rstrip() + "\n```"
    if len(result) > 2000:
        raise ValueError("The gang status board exceeds Discord's 2000-character message limit.")
    return result

# Update only when the visible grace state changes, including after restarts.
_last_gang_board_content: str | None = None

@tasks.loop(seconds=15)
async def gang_grace_board_loop() -> None:
    global _last_gang_board_content
    try:
        content = build_gang_board()
        if content == _last_gang_board_content:
            return
        guild = bot.get_guild(PRIVATE_GUILD_ID)
        if guild is None:
            return
        await publish_gang_board(guild)
        _last_gang_board_content = content
    except Exception as exc:
        print(f"Grace status board update failed: {exc}")

async def require_gang_leadership(interaction: discord.Interaction) -> bool:
    if not await ensure_private_guild(interaction):
        return False
    member = interaction.user
    if not isinstance(member, discord.Member) or not any(
        role.name.casefold() == "leadership" for role in member.roles
    ):
        await interaction.response.send_message(
            "❌ Only members with the Leadership role can manage gang statuses.", ephemeral=True
        )
        return False
    return True

async def gang_name_autocomplete(interaction: discord.Interaction, current: str):
    rows = conn.execute("SELECT name FROM gang_statuses ORDER BY category DESC, sort_order").fetchall()
    return [app_commands.Choice(name=name, value=name) for (name,) in rows
            if current.casefold() in name.casefold()][:25]

async def publish_gang_board(guild: discord.Guild) -> discord.Message:
    channel = guild.get_channel(GANG_STATUS_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        channel = await bot.fetch_channel(GANG_STATUS_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Gang status channel is not a text channel.")
    content = build_gang_board()
    # Always edit the existing, fixed message. Never post a replacement.
    try:
        message = await channel.fetch_message(GANG_STATUS_MESSAGE_ID)
    except discord.NotFound as exc:
        raise ValueError(
            f"Gang board message {GANG_STATUS_MESSAGE_ID} was not found in this channel. "
            "The bot will not create a replacement."
        ) from exc
    await message.edit(content=content)
    return message

@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="gangadd", description="Add a syndicated or registered gang")
@app_commands.choices(category=[app_commands.Choice(name="Syndicated", value="syndicated"), app_commands.Choice(name="Registered", value="registered")],
                      status=[app_commands.Choice(name="Shooting", value="SHOOTING"), app_commands.Choice(name="No Beef", value="NO BEEF"), app_commands.Choice(name="War", value="WAR")])
async def gangadd_cmd(interaction: discord.Interaction, name: str, category: str, status: str = "NO BEEF"):
    if not await require_gang_leadership(interaction): return
    name = name.strip()
    if not name or len(name) > 16 or any(c in name for c in "\n\r\x1b`"):
        await interaction.response.send_message("❌ Gang name must be 1–16 characters with no newlines or formatting codes.", ephemeral=True)
        return
    if conn.execute("SELECT 1 FROM gang_statuses WHERE name=?", (name,)).fetchone():
        await interaction.response.send_message("❌ That gang already exists.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        conn.execute("INSERT INTO gang_statuses VALUES (?, ?, ?, ?)", (name, category, status,
            conn.execute("SELECT COALESCE(MAX(sort_order), -1)+1 FROM gang_statuses").fetchone()[0]))
        conn.commit()
        msg = await publish_gang_board(interaction.guild)
        await interaction.followup.send(f"✅ Added **{name}** ({category}, {status}). Updated {msg.jump_url}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Saved gang, but could not update board: {e}", ephemeral=True)

@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="gangstatus", description="Change a gang's beef status")
@app_commands.autocomplete(gang=gang_name_autocomplete)
@app_commands.choices(status=[app_commands.Choice(name="Shooting", value="SHOOTING"), app_commands.Choice(name="No Beef", value="NO BEEF"), app_commands.Choice(name="War", value="WAR")])
async def gangstatus_cmd(interaction: discord.Interaction, gang: str, status: str):
    if not await require_gang_leadership(interaction): return
    if not conn.execute("SELECT 1 FROM gang_statuses WHERE name=?", (gang,)).fetchone():
        await interaction.response.send_message("❌ Gang not found.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    conn.execute("UPDATE gang_statuses SET status=? WHERE name=?", (status, gang))
    conn.commit()
    try:
        msg = await publish_gang_board(interaction.guild)
        await interaction.followup.send(f"✅ **{gang}** is now **{status}**. Updated {msg.jump_url}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Status saved, but board update failed: {e}", ephemeral=True)

@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="gangremove", description="Remove a gang from the status board")
@app_commands.autocomplete(gang=gang_name_autocomplete)
async def gangremove_cmd(interaction: discord.Interaction, gang: str):
    if not await require_gang_leadership(interaction): return
    if not conn.execute("SELECT 1 FROM gang_statuses WHERE name=?", (gang,)).fetchone():
        await interaction.response.send_message("❌ Gang not found.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    conn.execute("DELETE FROM gang_statuses WHERE name=?", (gang,))
    conn.commit()
    try:
        msg = await publish_gang_board(interaction.guild)
        await interaction.followup.send(f"✅ Removed **{gang}**. Updated {msg.jump_url}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Gang removed, but board update failed: {e}", ephemeral=True)

@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="gangstatusrefresh", description="Refresh the existing gang status board")
async def gangstatusrefresh_cmd(interaction: discord.Interaction):
    if not await require_gang_leadership(interaction): return
    await interaction.response.defer(ephemeral=True)
    try:
        msg = await publish_gang_board(interaction.guild)
        await interaction.followup.send(f"✅ Refreshed {msg.jump_url}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ {e}", ephemeral=True)

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
            title="⚠️ Breaching Warning",
            description=f"**{gang}** can breach again soon.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Time Remaining", value=f"{WARNING_MINUTES} minutes", inline=True)
        embed.add_field(name="Finishes", value=format_discord_time(end_time), inline=True)
        embed.set_footer(text="Breaching cooldown alert")

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
                title="⏰ Breaching Cooldown Finished",
                description=f"**{gang}** can now breach again.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="Finished At", value=format_discord_time(end_time), inline=False)
            embed.set_footer(text="Breaching cooldown complete")

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
# COMMAND GUIDE (ONE MESSAGE, UPDATED ON STARTUP)
# =========================
def build_command_guide() -> discord.Embed:
    embed = discord.Embed(
        title="📖 Virello Bot — Command Guide",
        description="Available slash commands and what they do. Commands may be restricted by server, channel, or role.",
        color=discord.Color.green(),
    )
    embed.add_field(name="⏱️ Breaching Timers", value=(
        "`/time` — Start a gang breaching cooldown (2 hours by default).\n"
        "`/timecheck` — Check a gang's remaining cooldown.\n"
        "`/timecancel` — Cancel a gang's cooldown.\n"
        "`/timelist` — Post all active cooldowns and grace periods.\n"
        f"Cooldown messages: <#{TIMER_CHANNEL_ID}>"
    ), inline=False)
    embed.add_field(name="🔵 Grace", value=(
        "`/grace` — Schedule a gang's grace period; the status board shows GRACE while active and restores its prior status afterward.\n"
        f"Grace announcements: <#{GRACE_CHANNEL_ID}>"
    ), inline=False)
    embed.add_field(name="🚨 Gang Operations", value=(
        "`/kos` — Announce Kill on Sight for a gang, with optional grace before it starts.\n"
        "`/safe` — Announce that the compound is safe.\n"
        "`/unsafe` — Announce that the compound is unsafe."
    ), inline=False)
    embed.add_field(name="📋 Gang Status Board (Leadership)", value=(
        "`/gangadd` — Add a Syndicated or Registered gang.\n"
        "`/gangstatus` — Change a gang to SHOOTING, NO BEEF, or WAR.\n"
        "`/gangremove` — Remove a gang from the board.\n"
        "`/gangstatusrefresh` — Refresh the existing status message.\n"
        f"Board: <#{GANG_STATUS_CHANNEL_ID}>"
    ), inline=False)
    embed.add_field(name="📦 Warehouse & EV", value=(
        "`/pricelist` — Show current warehouse sale prices (public price category; EV must be available).\n"
        "`/warehousecost` — Calculate the sale price of requested items (public price category).\n"
        "`/evavailable` — Enable or disable public price list access (Leadership; private server).\n"
        "`/warehousereset` — Manually reset warehouse counts (private server)."
    ), inline=False)
    embed.add_field(name="📍 Historical Vendor Map", value=(
        "`/vendormap` — Display saved vendor locations on the map.\n"
        "`/vendorlist` — List vendor coordinates.\n"
        "`/vendoradd`, `/vendoredit`, `/vendorremove` — Manage vendors (Leadership).\n"
        "`/vendorcalibrate` — Set map reference points (Leadership).\n"
        "`/vendor` — View a vendor door screenshot and coordinates.\n"
        "`/vendorimage` — Upload or replace a door screenshot (Leadership).\n"
        "`/vendorimageremove` — Remove a door screenshot (Leadership)."
    ), inline=False)
    embed.set_footer(text="This guide is maintained automatically by the bot")
    return embed


async def publish_command_guide() -> str:
    """Create or update the command guide; return a human-readable result."""
    channel = bot.get_channel(COMMAND_GUIDE_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(COMMAND_GUIDE_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError(f"Command guide destination {COMMAND_GUIDE_CHANNEL_ID} is not a text channel")
    if channel.guild.id != PRIVATE_GUILD_ID:
        raise RuntimeError("Command guide channel is not in the configured private guild")

    embed = build_command_guide()
    row = conn.execute(
        "SELECT setting_value FROM bot_settings WHERE setting_key=?",
        ("command_guide_message_id",),
    ).fetchone()
    if row:
        try:
            message = await channel.fetch_message(int(row[0]))
            if bot.user and message.author.id == bot.user.id:
                await message.edit(embed=embed)
                print(f"Command guide updated: {message.id}")
                return f"Updated existing command guide ({message.id})"
        except (discord.NotFound, ValueError):
            pass

    # Recover the guide if the local database was reset.
    async for message in channel.history(limit=100):
        if bot.user and message.author.id == bot.user.id and any(
            e.title == embed.title for e in message.embeds
        ):
            await message.edit(embed=embed)
            message_id = message.id
            break
    else:
        message = await channel.send(embed=embed)
        message_id = message.id

    conn.execute(
        "INSERT INTO bot_settings(setting_key,setting_value) VALUES(?,?) "
        "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
        ("command_guide_message_id", str(message_id)),
    )
    conn.commit()
    print(f"Command guide published: {message_id} in {channel.id}")
    return f"Command guide published/updated in {channel.mention} ({message_id})"


@app_commands.command(name="commandguide", description="Publish or refresh the bot command guide (Leadership only)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
async def commandguide_cmd(interaction: discord.Interaction) -> None:
    if not await ensure_private_guild(interaction):
        return
    member = interaction.user
    if not isinstance(member, discord.Member) or not any(
        role.name.casefold() == "leadership" for role in member.roles
    ):
        await interaction.response.send_message("❌ Leadership role required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = await publish_command_guide()
        await interaction.followup.send(f"✅ {result}", ephemeral=True)
    except Exception as exc:
        print(f"Command guide manual publish failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send(
            f"❌ Could not publish command guide: `{type(exc).__name__}: {exc}`",
            ephemeral=True,
        )



# =========================
# HISTORICAL VENDOR MAP
# =========================
# Uses the same service account and spreadsheet as the warehouse.
# Map calibration is intentionally required before rendering markers.
VENDOR_HEADERS = ["Vendor Name", "X", "Y", "Z", "Description", "Image Channel ID", "Image Message ID", "Door Type", "Location"]
CALIBRATION_HEADERS = ["World X", "World Y", "Pixel X", "Pixel Y"]


def vendor_sheet():
    spreadsheet = get_gspread_client().open_by_key(SPREADSHEET_ID)
    try:
        ws = spreadsheet.worksheet(VENDOR_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=VENDOR_SHEET_NAME, rows=500, cols=9)
        ws.update(range_name="A1:I1", values=[VENDOR_HEADERS])
    return ws


def calibration_sheet():
    spreadsheet = get_gspread_client().open_by_key(SPREADSHEET_ID)
    try:
        ws = spreadsheet.worksheet(VENDOR_CALIBRATION_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=VENDOR_CALIBRATION_SHEET, rows=20, cols=4)
        ws.update(range_name="A1:D1", values=[CALIBRATION_HEADERS])
    return ws


def vendor_records():
    records = []
    for index, row in enumerate(vendor_sheet().get_all_values()[1:], start=2):
        if not row or not row[0].strip():
            continue
        values = (row + [""] * 9)[:9]
        try:
            records.append({"row": index, "name": values[0].strip(),
                            "x": float(values[1]), "y": float(values[2]),
                            "z": float(values[3]) if values[3] else 0.0,
                            "description": values[4],
                            "image_channel_id": values[5].strip(),
                            "image_message_id": values[6].strip(),
                            "door_type": values[7].strip(),
                            "location": values[8].strip()})
        except ValueError:
            print(f"Skipping invalid vendor row {index}")
    return records


def find_vendor(records, name):
    return next((v for v in records if v["name"].casefold() == name.strip().casefold()), None)


def get_calibration():
    anchors = []
    for row in calibration_sheet().get_all_values()[1:]:
        if len(row) >= 4 and all(v.strip() for v in row[:4]):
            anchors.append(tuple(float(v) for v in row[:4]))
    if len(anchors) < 2:
        raise ValueError("Map not calibrated. Leadership must run /vendorcalibrate with two known points.")
    a, b = anchors[:2]
    if abs(a[0] - b[0]) < 1 or abs(a[1] - b[1]) < 1:
        raise ValueError("Calibration anchors need different world X and Y coordinates.")
    return a, b


def vendor_pixel(x, y, anchors):
    a, b = anchors
    px = a[2] + (x - a[0]) * (b[2] - a[2]) / (b[0] - a[0])
    py = a[3] + (y - a[1]) * (b[3] - a[3]) / (b[1] - a[1])
    return round(px), round(py)


def draw_vendor_map(records):
    anchors = get_calibration()
    with Image.open(VENDOR_MAP_PATH) as source:
        canvas = source.convert("RGBA")
    # Render markers at 4x resolution, then downsample to the ORIGINAL map size.
    # This smooths the tiny circles and numbers without resizing the final PNG.
    scale = 4
    hi = Image.new("RGBA", (canvas.width * scale, canvas.height * scale), (0, 0, 0, 0))
    overlay = ImageDraw.Draw(hi)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 10 * scale)
    except OSError:
        font = ImageFont.load_default(size=10 * scale)
    placed, outside = [], []
    for i, vendor in enumerate(records, start=1):
        px, py = vendor_pixel(vendor["x"], vendor["y"], anchors)
        if not (0 <= px < canvas.width and 0 <= py < canvas.height):
            outside.append(vendor["name"])
            continue
        cx, cy = px * scale, py * scale
        radius = 6 * scale  # unchanged final marker diameter (12 pixels)
        overlay.ellipse((cx-radius, cy-radius, cx+radius, cy+radius),
                        fill=(8, 12, 8, 255), outline=(210, 255, 65, 255), width=2*scale)
        label = str(i)
        marker_font = font
        if len(label) > 1:
            try:
                marker_font = ImageFont.truetype("DejaVuSans-Bold.ttf", 7 * scale)
            except OSError:
                marker_font = ImageFont.load_default(size=7 * scale)
        bounds = overlay.textbbox((0, 0), label, font=marker_font, stroke_width=0)
        text_x = cx - (bounds[2]-bounds[0])/2
        text_y = cy - (bounds[3]-bounds[1])/2 - bounds[1]
        overlay.text((text_x, text_y), label, font=marker_font, fill=(255, 255, 255, 255),
                     stroke_width=0)
        placed.append((i, vendor))
    marker_layer = hi.resize(canvas.size, Image.Resampling.LANCZOS)
    canvas = Image.alpha_composite(canvas, marker_layer)
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    output.seek(0)
    return output, placed, outside


def build_vendor_embeds(placed, outside, total_saved):
    """Put the numbered legend in a second embed, underneath the map image."""
    map_embed = discord.Embed(
        title="📍 Historical Vendor Map",
        description="Historical vendor positions plotted from Google Sheets.",
        color=discord.Color.green(),
    )
    map_embed.set_image(url="attachment://vendor_locations.png")

    legend_lines = [
        f"**{number}** — {discord.utils.escape_markdown(vendor['name'])}"
        for number, vendor in placed
    ]
    if not legend_lines:
        legend_lines = ["No visible vendor markers."]
    if outside:
        legend_lines.append(f"⚠️ {len(outside)} location(s) outside the map.")

    # Discord permits up to 10 embeds per message and 4096 chars per description.
    # Use up to 9 legend pages, retaining all marker numbers in the list.
    pages = []
    current = []
    current_length = 0
    for line in legend_lines:
        if current and current_length + len(line) + 1 > 3900:
            pages.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + 1
    if current:
        pages.append("\n".join(current))
    if len(pages) > 9:
        pages = pages[:9]
        pages[-1] += "\n⚠️ Additional entries omitted from legend."

    embeds = [map_embed]
    for page_number, page in enumerate(pages, 1):
        title = "🔢 Vendor Legend" if page_number == 1 else f"🔢 Vendor Legend ({page_number})"
        legend_embed = discord.Embed(title=title, description=page, color=discord.Color.green())
        if page_number == len(pages):
            legend_embed.set_footer(text=f"{len(placed)} plotted • {total_saved} saved • Google Sheets")
        embeds.append(legend_embed)
    return embeds


async def publish_vendor_map() -> str:
    """Maintain a single map post in the configured private-server channel."""
    if not VENDOR_MAP_CHANNEL_ID:
        return "Map saved, but VENDOR_MAP_CHANNEL_ID is not configured."
    channel = bot.get_channel(VENDOR_MAP_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(VENDOR_MAP_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != PRIVATE_GUILD_ID:
        raise ValueError("Vendor map channel must be a text channel in the private Discord.")
    records = await asyncio.to_thread(vendor_records)
    if records:
        output, placed, outside = await asyncio.to_thread(draw_vendor_map, records)
    else:
        # Empty maps still replace the previous image after the last vendor is removed.
        def blank_map():
            with Image.open(VENDOR_MAP_PATH) as im:
                buffer = io.BytesIO()
                im.convert("RGBA").save(buffer, "PNG")
                buffer.seek(0)
                return buffer
        output = await asyncio.to_thread(blank_map)
        placed, outside = [], []
    embeds = build_vendor_embeds(placed, outside, len(records))
    # Edit only the explicitly configured message; never create duplicates.
    try:
        message = await channel.fetch_message(VENDOR_MAP_MESSAGE_ID)
    except discord.NotFound as exc:
        raise ValueError(
            f"Vendor map message {VENDOR_MAP_MESSAGE_ID} was not found in "
            f"channel {VENDOR_MAP_CHANNEL_ID}. No replacement was posted."
        ) from exc
    if bot.user is None or message.author.id != bot.user.id:
        raise ValueError("Configured vendor map message was not authored by this bot.")
    attachment = discord.File(output, filename="vendor_locations.png")
    await message.edit(embeds=embeds, attachments=[attachment])
    conn.execute("INSERT INTO bot_settings(setting_key, setting_value) VALUES (?, ?) "
                 "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                 ("vendor_map_message_id", str(message.id)))
    conn.commit()
    return f"Updated vendor map: {message.jump_url}"


async def notify_vendor_map_update(interaction: discord.Interaction, result: str):
    try:
        result += "\n" + await publish_vendor_map()
    except Exception as exc:
        print(f"Vendor map publishing failed: {type(exc).__name__}: {exc}")
        result += f"\n⚠️ Vendor saved in Google Sheets, but map publishing failed: {exc}"
    await interaction.followup.send(result, ephemeral=True)


async def vendor_leadership(interaction: discord.Interaction) -> bool:
    return await require_gang_leadership(interaction)


async def vendor_choices(interaction: discord.Interaction, current: str):
    if interaction.guild_id != PRIVATE_GUILD_ID:
        return []
    try:
        records = await asyncio.to_thread(vendor_records)
        return [app_commands.Choice(name=v["name"][:100], value=v["name"][:100])
                for v in records if current.casefold() in v["name"].casefold()][:25]
    except Exception as exc:
        print(f"Vendor autocomplete failed: {exc}")
        return []


@app_commands.command(name="vendoradd", description="Save an old vendor location (Leadership)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.describe(name="Unique vendor name", x="GTA world X", y="GTA world Y",
                       z="GTA world Z (optional)", description="Optional location notes",
                       door_type="Door type (e.g. Single, Double)", location="Suburb or area")
async def vendoradd_cmd(interaction: discord.Interaction, name: str, x: float, y: float,
                        z: float = 0.0, description: str = "",
                        door_type: str = "", location: str = ""):
    if not await vendor_leadership(interaction):
        return
    name = name.strip()
    if not 1 <= len(name) <= 80 or len(description) > 300 or len(door_type) > 100 or len(location) > 100 or not all(map(math.isfinite, (x, y, z))):
        await interaction.response.send_message("❌ Invalid name, description or coordinates.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        def save():
            records = vendor_records()
            if find_vendor(records, name):
                return False
            vendor_sheet().append_row([name, x, y, z, description, "", "", door_type.strip(), location.strip()], value_input_option="RAW")
            return True
        created = await asyncio.to_thread(save)
        if created:
            await notify_vendor_map_update(interaction, f"✅ Saved **{discord.utils.escape_markdown(name)}** at ({x}, {y}, {z}).")
        else:
            await interaction.followup.send("❌ A vendor with that name already exists.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Could not save vendor: {exc}", ephemeral=True)


@app_commands.command(name="vendorlist", description="List historical vendor locations")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
async def vendorlist_cmd(interaction: discord.Interaction):
    if not await ensure_private_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        records = await asyncio.to_thread(vendor_records)
        if not records:
            await interaction.followup.send("No vendors have been saved yet.", ephemeral=True)
            return
        lines = [f"**{i}. {discord.utils.escape_markdown(v['name'])}** — X: {v['x']:g}, Y: {v['y']:g}, Z: {v['z']:g}"
                 for i, v in enumerate(records, 1)]
        chunks, chunk = [], ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1800:
                chunks.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            chunks.append(chunk)
        for text in chunks[:10]:
            await interaction.followup.send(text, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Could not load vendors: {exc}", ephemeral=True)


@app_commands.command(name="vendorremove", description="Delete a saved vendor (Leadership)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.autocomplete(name=vendor_choices)
async def vendorremove_cmd(interaction: discord.Interaction, name: str):
    if not await vendor_leadership(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        def remove():
            record = find_vendor(vendor_records(), name)
            if not record:
                return False
            vendor_sheet().delete_rows(record["row"])
            return True
        removed = await asyncio.to_thread(remove)
        if removed:
            await notify_vendor_map_update(interaction, "✅ Vendor removed.")
        else:
            await interaction.followup.send("❌ Vendor not found.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Could not remove vendor: {exc}", ephemeral=True)


@app_commands.command(name="vendoredit", description="Update an existing vendor (Leadership)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.autocomplete(name=vendor_choices)
async def vendoredit_cmd(interaction: discord.Interaction, name: str, x: float, y: float,
                         z: float = 0.0, description: str = "",
                         door_type: str | None = None, location: str | None = None):
    if not await vendor_leadership(interaction):
        return
    if (len(description) > 300 or (door_type is not None and len(door_type) > 100)
            or (location is not None and len(location) > 100)
            or not all(map(math.isfinite, (x, y, z)))):
        await interaction.response.send_message("❌ Invalid coordinates or description.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        def update():
            record = find_vendor(vendor_records(), name)
            if not record:
                return False
            vendor_sheet().update(range_name=f"A{record['row']}:E{record['row']}",
                                  values=[[record["name"], x, y, z, description]],
                                  value_input_option="RAW")
            if door_type is not None or location is not None:
                vendor_sheet().update(
                    range_name=f"H{record['row']}:I{record['row']}",
                    values=[[record["door_type"] if door_type is None else door_type.strip(),
                             record["location"] if location is None else location.strip()]],
                    value_input_option="RAW")
            return True
        updated = await asyncio.to_thread(update)
        if updated:
            await notify_vendor_map_update(interaction, "✅ Vendor updated.")
        else:
            await interaction.followup.send("❌ Vendor not found.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Could not update vendor: {exc}", ephemeral=True)


@app_commands.command(name="vendorcalibrate", description="Set two GTA-to-map reference points (Leadership)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.describe(x1="First GTA X", y1="First GTA Y", px1="First map pixel X",
                       py1="First map pixel Y", x2="Second GTA X", y2="Second GTA Y",
                       px2="Second map pixel X", py2="Second map pixel Y")
async def vendorcalibrate_cmd(interaction: discord.Interaction,
                              x1: float, y1: float, px1: int, py1: int,
                              x2: float, y2: float, px2: int, py2: int):
    if not await vendor_leadership(interaction):
        return
    if (abs(x2-x1) < 1 or abs(y2-y1) < 1 or
            not all(map(math.isfinite, (x1, y1, x2, y2)))):
        await interaction.response.send_message("❌ Reference points need distinct world X and Y values.", ephemeral=True)
        return
    with Image.open(VENDOR_MAP_PATH) as im:
        width, height = im.size
    if not all(0 <= px < width and 0 <= py < height for px, py in ((px1, py1), (px2, py2))):
        await interaction.response.send_message(f"❌ Pixel points must fit within {width}×{height}.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        def save():
            ws = calibration_sheet()
            ws.clear()
            ws.update(range_name="A1:D3", values=[CALIBRATION_HEADERS,
                [x1, y1, px1, py1], [x2, y2, px2, py2]])
        await asyncio.to_thread(save)
        await notify_vendor_map_update(interaction, "✅ Calibration saved. Check marker alignment against landmarks.")
    except Exception as exc:
        await interaction.followup.send(f"❌ Calibration failed: {exc}", ephemeral=True)


async def vendor_image_channel():
    channel = bot.get_channel(VENDOR_IMAGE_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(VENDOR_IMAGE_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != PRIVATE_GUILD_ID:
        raise ValueError("VENDOR_IMAGE_CHANNEL_ID must be a text channel in the private server.")
    return channel


@app_commands.command(name="vendor", description="View a vendor's door screenshot and location")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.autocomplete(name=vendor_choices)
async def vendor_cmd(interaction: discord.Interaction, name: str):
    if not await ensure_private_guild(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        vendor = find_vendor(await asyncio.to_thread(vendor_records), name)
        if not vendor:
            await interaction.followup.send("❌ Vendor not found.", ephemeral=True)
            return
        embed = discord.Embed(title=f"📍 {vendor['name']}", color=discord.Color.green())
        embed.add_field(name="Coordinates", value=f"X: `{vendor['x']:g}`  Y: `{vendor['y']:g}`  Z: `{vendor['z']:g}`", inline=False)
        if vendor['door_type']:
            embed.add_field(name="Door Type", value=vendor['door_type'], inline=True)
        if vendor['location']:
            embed.add_field(name="Location / Suburb", value=vendor['location'], inline=True)
        if vendor['description']:
            embed.add_field(name="Notes", value=vendor['description'][:1024], inline=False)
        if not vendor['image_message_id'] or not vendor['image_channel_id']:
            embed.add_field(name="Door Screenshot", value="No screenshot uploaded yet.", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        channel = bot.get_channel(int(vendor['image_channel_id']))
        if channel is None:
            channel = await bot.fetch_channel(int(vendor['image_channel_id']))
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != PRIVATE_GUILD_ID:
            raise ValueError("Saved image channel is unavailable or not private.")
        message = await channel.fetch_message(int(vendor['image_message_id']))
        if not message.attachments:
            raise ValueError("Saved screenshot message has no attachment.")
        attachment = message.attachments[0]
        image_bytes = await attachment.read()
        filename = attachment.filename
        embed.set_image(url=f"attachment://{filename}")
        await interaction.followup.send(embed=embed, file=discord.File(io.BytesIO(image_bytes), filename=filename), ephemeral=True)
    except (discord.NotFound, discord.Forbidden, ValueError) as exc:
        await interaction.followup.send(f"⚠️ Screenshot unavailable: {exc}", ephemeral=True)
    except Exception as exc:
        print(f"Vendor lookup failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send("❌ Could not retrieve vendor details.", ephemeral=True)


async def vendor_filter_choices(interaction: discord.Interaction, current: str, field: str):
    """Autocomplete distinct values actually present in the Vendor Locations sheet."""
    if interaction.guild_id != PRIVATE_GUILD_ID:
        return []
    try:
        records = await asyncio.to_thread(vendor_records)
        unique = {}
        for vendor in records:
            value = vendor[field].strip()
            if value and current.casefold() in value.casefold():
                unique.setdefault(value.casefold(), value)
        return [app_commands.Choice(name=value[:100], value=value[:100])
                for value in sorted(unique.values(), key=str.casefold)[:25]]
    except Exception as exc:
        print(f"Vendor filter autocomplete failed: {type(exc).__name__}: {exc}")
        return []


async def vendor_door_type_choices(interaction: discord.Interaction, current: str):
    return await vendor_filter_choices(interaction, current, "door_type")


async def vendor_location_choices(interaction: discord.Interaction, current: str):
    return await vendor_filter_choices(interaction, current, "location")


@app_commands.command(name="vendorrefresh", description="Regenerate the existing vendor map and legend (Leadership)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
async def vendorrefresh_cmd(interaction: discord.Interaction):
    if not await vendor_leadership(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = await publish_vendor_map()
        await interaction.followup.send(f"✅ {result}", ephemeral=True)
    except Exception as exc:
        print(f"Vendor map refresh failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send(f"❌ Could not refresh vendor map: {exc}", ephemeral=True)


@app_commands.command(name="findvendor", description="Find vendors by door type, location, or both")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.describe(door_type="Door type, e.g. Single or Double (optional if location provided)",
                       location="Suburb/location, e.g. Pacific Ocean (optional if door type provided)")
@app_commands.autocomplete(door_type=vendor_door_type_choices, location=vendor_location_choices)
async def findvendor_cmd(interaction: discord.Interaction,
                         door_type: str | None = None, location: str | None = None):
    if not await ensure_private_guild(interaction):
        return
    door_filter = (door_type or "").strip()
    location_filter = (location or "").strip()
    if not door_filter and not location_filter:
        await interaction.response.send_message(
            "❌ Provide **door_type**, **location**, or both to search.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        records = await asyncio.to_thread(vendor_records)
        matches = [v for v in records
                   if (not door_filter or door_filter.casefold() in v["door_type"].casefold())
                   and (not location_filter or location_filter.casefold() in v["location"].casefold())]
        criteria = []
        if door_filter:
            criteria.append(f"Door: {discord.utils.escape_markdown(door_filter)}")
        if location_filter:
            criteria.append(f"Location: {discord.utils.escape_markdown(location_filter)}")
        heading = " · ".join(criteria)
        if not matches:
            await interaction.followup.send(f"🔎 No vendors found for **{heading}**.", ephemeral=True)
            return
        lines = [f"**{discord.utils.escape_markdown(v['name'])}** — "
                 f"{discord.utils.escape_markdown(v['door_type'] or 'Unspecified')} door · "
                 f"{discord.utils.escape_markdown(v['location'] or 'Unknown location')} · "
                 f"`{v['x']:g}, {v['y']:g}, {v['z']:g}`" for v in matches]
        chunks, chunk = [], ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 1800 and chunk:
                chunks.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            chunks.append(chunk)
        for index, body in enumerate(chunks):
            header = f"🔎 **{len(matches)} vendor(s) found** — {heading}\n" if index == 0 else ""
            await interaction.followup.send(header + body, ephemeral=True)
    except Exception as exc:
        print(f"Find vendor failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send("❌ Could not search vendor locations.", ephemeral=True)


@app_commands.command(name="vendorimage", description="Upload or replace a vendor door screenshot (Leadership)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.autocomplete(name=vendor_choices)
@app_commands.describe(name="Existing vendor name", image="Screenshot of the vendor entrance")
async def vendorimage_cmd(interaction: discord.Interaction, name: str, image: discord.Attachment):
    if not await vendor_leadership(interaction):
        return
    valid_types = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    if image.content_type not in valid_types or image.size > 8 * 1024 * 1024:
        await interaction.response.send_message("❌ Upload a PNG, JPG, WEBP or GIF no larger than 8 MB.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        vendor = find_vendor(await asyncio.to_thread(vendor_records), name)
        if not vendor:
            await interaction.followup.send("❌ Vendor not found. Use /vendoradd first.", ephemeral=True)
            return
        channel = await vendor_image_channel()
        data = await image.read()
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}[image.content_type]
        filename = f"vendor_door.{extension}"
        new_message = await channel.send(
            content=f"Door screenshot for vendor: {discord.utils.escape_mentions(vendor['name'])}",
            file=discord.File(io.BytesIO(data), filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            def save_image_ref():
                current = find_vendor(vendor_records(), name)
                if current is None:
                    raise ValueError("Vendor was removed during upload.")
                vendor_sheet().update(
                    range_name=f"F{current['row']}:G{current['row']}",
                    values=[[str(channel.id), str(new_message.id)]],
                    value_input_option="RAW",
                )
            await asyncio.to_thread(save_image_ref)
        except Exception:
            await new_message.delete()
            raise
        # Clean up the old image only after the new reference has been saved.
        if vendor['image_message_id'] and vendor['image_channel_id']:
            try:
                old_channel = bot.get_channel(int(vendor['image_channel_id'])) or await bot.fetch_channel(int(vendor['image_channel_id']))
                if isinstance(old_channel, discord.TextChannel) and old_channel.guild.id == PRIVATE_GUILD_ID:
                    old_msg = await old_channel.fetch_message(int(vendor['image_message_id']))
                    if old_msg.author.id == bot.user.id:
                        await old_msg.delete()
            except (discord.NotFound, discord.Forbidden, ValueError):
                pass
        await interaction.followup.send(f"✅ Screenshot saved for **{discord.utils.escape_markdown(vendor['name'])}**. Members can use `/vendor` to view it.", ephemeral=True)
    except Exception as exc:
        print(f"Vendor image upload failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send(f"❌ Screenshot upload failed: {exc}", ephemeral=True)


@app_commands.command(name="vendorimageremove", description="Remove a vendor door screenshot (Leadership)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.autocomplete(name=vendor_choices)
async def vendorimageremove_cmd(interaction: discord.Interaction, name: str):
    if not await vendor_leadership(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        vendor = find_vendor(await asyncio.to_thread(vendor_records), name)
        if not vendor:
            await interaction.followup.send("❌ Vendor not found.", ephemeral=True)
            return
        if not vendor['image_message_id']:
            await interaction.followup.send("ℹ️ No screenshot is saved for this vendor.", ephemeral=True)
            return
        def clear_ref():
            current = find_vendor(vendor_records(), name)
            if not current:
                raise ValueError("Vendor no longer exists.")
            vendor_sheet().update(range_name=f"F{current['row']}:G{current['row']}", values=[["", ""]], value_input_option="RAW")
        await asyncio.to_thread(clear_ref)
        try:
            channel = bot.get_channel(int(vendor['image_channel_id'])) or await bot.fetch_channel(int(vendor['image_channel_id']))
            if isinstance(channel, discord.TextChannel) and channel.guild.id == PRIVATE_GUILD_ID:
                old_msg = await channel.fetch_message(int(vendor['image_message_id']))
                if old_msg.author.id == bot.user.id:
                    await old_msg.delete()
        except (discord.NotFound, discord.Forbidden, ValueError):
            pass
        await interaction.followup.send("✅ Vendor screenshot removed.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"❌ Could not remove screenshot: {exc}", ephemeral=True)


@app_commands.command(name="vendormap", description="Show old vendor locations on the custom GTA map")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
async def vendormap_cmd(interaction: discord.Interaction):
    if not await ensure_private_guild(interaction):
        return
    await interaction.response.defer()
    try:
        records = await asyncio.to_thread(vendor_records)
        if not records:
            await interaction.followup.send("No vendors saved yet. Leadership can use /vendoradd.", ephemeral=True)
            return
        output, placed, outside = await asyncio.to_thread(draw_vendor_map, records)
        embeds = build_vendor_embeds(placed, outside, len(records))
        await interaction.followup.send(
            embeds=embeds,
            file=discord.File(output, filename="vendor_locations.png"),
        )
    except FileNotFoundError:
        await interaction.followup.send("❌ Map file missing. Set VENDOR_MAP_PATH to your deployed PNG.", ephemeral=True)
    except ValueError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
    except Exception as exc:
        print(f"Vendor map failed: {type(exc).__name__}: {exc}")
        await interaction.followup.send(f"❌ Map generation failed: {exc}", ephemeral=True)

# =========================
# READY EVENT
# =========================
@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")

    try:
        await publish_command_guide()
    except Exception as e:
        print(f"Unable to publish command guide: {type(e).__name__}: {e}")

    if not gang_grace_board_loop.is_running():
        gang_grace_board_loop.start()

    if not warehouse_daily_reset_loop.is_running():
        warehouse_daily_reset_loop.start()
        print("Warehouse daily reset loop started.")

    try:
        # Keep all standard commands global, but register EV availability
        # directly in the private guild for immediate visibility.
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} global commands: {[c.name for c in synced]}")

        for guild_id in ALLOWED_GUILD_IDS:
            guild = discord.Object(id=guild_id)
            bot.tree.clear_commands(guild=guild)
            if guild_id == PRIVATE_GUILD_ID:
                bot.tree.add_command(evavailable_cmd, guild=guild)
                bot.tree.add_command(commandguide_cmd, guild=guild)
                for command in (gangadd_cmd, gangstatus_cmd, gangremove_cmd, gangstatusrefresh_cmd,
                                vendoradd_cmd, vendorlist_cmd, vendorremove_cmd, vendoredit_cmd,
                                vendorcalibrate_cmd, vendormap_cmd, vendor_cmd, findvendor_cmd, vendorrefresh_cmd, vendorimage_cmd, vendorimageremove_cmd):
                    bot.tree.add_command(command, guild=guild)
            guild_synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(guild_synced)} guild commands for {guild_id}: {[c.name for c in guild_synced]}")
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
@bot.tree.command(name="time", description="Start a breaching cooldown")
@app_commands.choices(gang=[
    app_commands.Choice(name="GenCorp", value="GenCorp"),
    app_commands.Choice(name="Sinners MC", value="Sinners MC"),
    app_commands.Choice(name="Wraith MC", value="Wraith MC"),
    app_commands.Choice(name="Santos Cartel", value="Santos Cartel"),
    app_commands.Choice(name="Outcasts", value="Outcasts"),
    app_commands.Choice(name="Triads", value="Triads"),
    app_commands.Choice(name="La Familia", value="La Familia"),
    app_commands.Choice(name="Error", value="Error"),
    app_commands.Choice(name="Vandals", value="Vandals"),
    app_commands.Choice(name="Kimura", value="Kimura"),
])
@app_commands.describe(
    gang="Gang name",
    duration="HH:MM:SS format. Leave blank for 2 hours"
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
                description=f"**{gang}** already has an active breaching cooldown.",
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
        title="✅ Breaching Cooldown Started",
        description=f"Breaching cooldown started for **{gang}**.",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Warning", value=f"{WARNING_MINUTES} mins before end", inline=True)
    embed.add_field(name="Ends", value=format_discord_time(end_time), inline=False)
    embed.set_footer(text=f"Started by {interaction.user.display_name}")

    await timer_channel.send(embed=embed)
    await acknowledge_redirect(interaction, "Posted breaching cooldown")


@bot.tree.command(name="grace", description="Start a grace timer")
@app_commands.choices(gang=[
    app_commands.Choice(name="GenCorp", value="GenCorp"),
    app_commands.Choice(name="Sinners MC", value="Sinners MC"),
    app_commands.Choice(name="Wraith MC", value="Wraith MC"),
    app_commands.Choice(name="Santos Cartel", value="Santos Cartel"),
    app_commands.Choice(name="Outcasts", value="Outcasts"),
    app_commands.Choice(name="Triads", value="Triads"),
    app_commands.Choice(name="La Familia", value="La Familia"),
    app_commands.Choice(name="Error", value="Error"),
    app_commands.Choice(name="Vandals", value="Vandals"),
    app_commands.Choice(name="Kimura", value="Kimura"),
])
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
    if not await ensure_private_guild(interaction):
        return

    guild = interaction.guild
    grace_channel = guild.get_channel(GRACE_CHANNEL_ID)
    if not isinstance(grace_channel, discord.TextChannel):
        await interaction.response.send_message(
            embed=make_error_embed("❌ Missing Grace Channel", f"I couldn't find <#{GRACE_CHANNEL_ID}>."),
            ephemeral=True
        )
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

    now_sydney = datetime.now(SYDNEY_TZ)

    start_datetime_sydney = now_sydney.replace(
        hour=parsed_start.hour,
        minute=parsed_start.minute,
        second=0,
        microsecond=0
    )

    # A start time in the current minute begins immediately rather than tomorrow.
    if start_datetime_sydney < now_sydney:
        if (now_sydney - start_datetime_sydney).total_seconds() < 60:
            start_datetime_sydney = now_sydney
        else:
            start_datetime_sydney += timedelta(days=1)

    end_datetime_sydney = start_datetime_sydney + delta

    start_datetime = start_datetime_sydney.astimezone(timezone.utc)
    end_datetime = end_datetime_sydney.astimezone(timezone.utc)

    cursor.execute(
        "DELETE FROM grace_timers WHERE guild_id=? AND gang_name=?",
        (guild.id, gang.lower())
    )

    cursor.execute(
        """
        INSERT INTO grace_timers
        (guild_id, gang_name, start_time, end_time, channel_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            guild.id,
            gang.lower(),
            start_datetime.isoformat(),
            end_datetime.isoformat(),
            GRACE_CHANNEL_ID
        )
    )
    conn.commit()

    # Refresh immediately when the grace period is already active.
    try:
        await publish_gang_board(guild)
    except Exception as exc:
        print(f"Could not refresh gang board after /grace: {exc}")

    embed = discord.Embed(
        title="✅ Grace Started",
        description=(f"Grace period active for **{gang}**." if start_datetime <= datetime.now(timezone.utc) else f"Grace period scheduled for **{gang}**."),
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Warning", value="No warning", inline=True)
    embed.add_field(name="Starts", value=format_discord_time(start_datetime), inline=False)
    embed.add_field(name="Ends", value=format_discord_time(end_datetime), inline=False)
    embed.set_footer(text=f"Started by {interaction.user.display_name}")

    await grace_channel.send(embed=embed)

    await interaction.response.send_message(
        f"✅ Posted grace timer for **{gang}** in <#{GRACE_CHANNEL_ID}>.",
        ephemeral=True
    )


@bot.tree.command(name="timecheck", description="Check breaching timer")
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
                description=f"No active breaching cooldown was found for **{gang}**.",
                color=discord.Color.light_grey()
            ),
            ephemeral=True
        )
        return

    end_time = parse_db_time(row[0])
    remaining = end_time - datetime.now(timezone.utc)

    if remaining.total_seconds() <= 0:
        embed = discord.Embed(
            title="⏰ Breaching Cooldown Finished",
            description=f"**{gang}** can already breach again.",
            color=discord.Color.green()
        )
        await timer_channel.send(embed=embed)
        await acknowledge_redirect(interaction, "Posted breaching cooldown status")
        return

    embed = discord.Embed(
        title="⏳ Breaching Cooldown Status",
        description=f"Current breaching cooldown for **{gang}**.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Remaining", value=format_remaining(remaining), inline=True)
    embed.add_field(name="Ends", value=format_discord_time(end_time), inline=True)

    await timer_channel.send(embed=embed)
    await acknowledge_redirect(interaction, "Posted breaching cooldown status")


@bot.tree.command(name="timecancel", description="Cancel breaching timer")
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
                description=f"No active breaching cooldown was found for **{gang}**.",
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
        title="🛑 Breaching Cooldown Cancelled",
        description=f"Breaching cooldown for **{gang}** has been cancelled.",
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

    now = datetime.now(timezone.utc)

    embed = discord.Embed(
        title="⏳ Active Timers",
        description="Current breaching cooldown and grace timers in this server.",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    active_found = False

    cursor.execute(
        "SELECT gang_name, end_time FROM timers WHERE guild_id=? ORDER BY end_time ASC",
        (guild.id,)
    )
    cooldown_rows = cursor.fetchall()

    for gang_name, end_time_str in cooldown_rows:
        end_time = parse_db_time(end_time_str)
        remaining = end_time - now

        if remaining.total_seconds() > 0:
            active_found = True
            embed.add_field(
                name=f"Breaching — {gang_name.capitalize()}",
                value=(
                    f"Remaining: **{format_remaining(remaining)}**\n"
                    f"Ends: {format_discord_time(end_time)}"
                ),
                inline=False
            )
        else:
            cursor.execute(
                "DELETE FROM timers WHERE guild_id=? AND gang_name=?",
                (guild.id, gang_name.lower())
            )
            conn.commit()

    cursor.execute(
        "SELECT gang_name, start_time, end_time FROM grace_timers WHERE guild_id=? ORDER BY end_time ASC",
        (guild.id,)
    )
    grace_rows = cursor.fetchall()

    for gang_name, start_time_str, end_time_str in grace_rows:
        start_time_dt = parse_db_time(start_time_str)
        end_time = parse_db_time(end_time_str)
        remaining = end_time - now

        if remaining.total_seconds() > 0:
            active_found = True
            embed.add_field(
                name=f"Grace — {gang_name.capitalize()}",
                value=(
                    f"Starts: {format_discord_time(start_time_dt)}\n"
                    f"Remaining: **{format_remaining(remaining)}**\n"
                    f"Ends: {format_discord_time(end_time)}"
                ),
                inline=False
            )
        else:
            cursor.execute(
                "DELETE FROM grace_timers WHERE guild_id=? AND gang_name=?",
                (guild.id, gang_name.lower())
            )
            conn.commit()

    if not active_found:
        embed = discord.Embed(
            title="📭 No Active Timers",
            description="There are currently no breaching cooldowns or grace timers running.",
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


@bot.tree.command(name="kos", description="Post a KOS alert with optional grace")
@app_commands.describe(
    gang="Gang name",
    grace="Grace until KOS starts. HH:MM:SS format. Leave blank for no grace"
)
async def kos_cmd(
    interaction: discord.Interaction,
    gang: str,
    grace: str = "00:00:00"
) -> None:
    if not await ensure_private_guild(interaction):
        return

    kos_channel = get_kos_channel(interaction.guild)

    if kos_channel is None:
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Missing KOS Channel",
                f"I couldn't find <#{KOS_CHANNEL_ID}>."
            ),
            ephemeral=True
        )
        return

    gang = gang.strip()

    try:
        grace_delta = (
            timedelta(seconds=0)
            if grace.strip() == "00:00:00"
            else parse_duration(grace.strip())
        )
    except ValueError as e:
        await interaction.response.send_message(
            embed=make_error_embed("❌ Invalid Grace Time", str(e)),
            ephemeral=True
        )
        return

    starts_at = datetime.now(timezone.utc) + grace_delta

    member_role = discord.utils.get(interaction.guild.roles, name="Member")
    role_ping = member_role.mention if member_role else "@Member"

    embed = discord.Embed(
        title="🚨 KILL ON SIGHT",
        description=f"**{gang}** has been placed on **KOS**.",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Gang", value=gang, inline=False)

    if grace_delta.total_seconds() > 0:
        embed.add_field(name="Grace Until Start", value=grace.strip(), inline=True)
        embed.add_field(name="KOS Starts", value=format_discord_time(starts_at), inline=True)
    else:
        embed.add_field(name="Status", value="KOS starts **now**.", inline=False)

    embed.set_footer(text=f"Called by {interaction.user.display_name}")

    await kos_channel.send(
        content=role_ping,
        embed=embed
    )

    await interaction.response.send_message(
        f"✅ Posted KOS for **{gang}** in <#{KOS_CHANNEL_ID}>.",
        ephemeral=True
    )


@bot.tree.command(name="warehousereset", description="Manually reset warehouse daily counts")
async def warehousereset_cmd(interaction: discord.Interaction) -> None:
    if not await ensure_private_guild(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    try:
        updated_count = await run_warehouse_daily_reset()

        await interaction.followup.send(
            f"✅ Warehouse reset complete. Updated **{updated_count}** item counts from Daily Reset Count.",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(
            f"❌ Warehouse reset failed: {e}",
            ephemeral=True
        )

# =========================
# COMMANDS - PUBLIC ONLY
# =========================
@bot.tree.command(name="warehousecost", description="Calculate total warehouse sale price")
@app_commands.describe(
    items="Example: 5 x Lock Picking Tool, 2 x Bullet Crate (5.5) x 100"
)
async def warehousecost_cmd(interaction: discord.Interaction, items: str) -> None:
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
                "This command can only be used in the public price category."
            ),
            ephemeral=True
        )
        return

    await interaction.response.defer()

    try:
        price_list = await get_price_list()

        price_lookup = {
            item.lower(): (item, price)
            for item, price in price_list
        }

        requested_items = []
        raw_parts = items.replace("\n", ",").split(",")

        for part in raw_parts:
            part = part.strip()

            if not part:
                continue

            if " x " not in part.lower():
                await interaction.followup.send(
                    f"❌ Could not read `{part}`. Use format like `5 x Lock Picking Tool`."
                )
                return

            quantity_text, item_name = part.split(" x ", 1)

            try:
                quantity = int(quantity_text.strip())
            except ValueError:
                await interaction.followup.send(
                    f"❌ Invalid quantity in `{part}`."
                )
                return

            requested_items.append((quantity, item_name.strip()))

        total = 0
        lines = []

        for quantity, item_name in requested_items:
            item_key = item_name.lower()

            matched = process.extractOne(
                item_key,
                price_lookup.keys(),
                scorer=fuzz.WRatio
            )

            if not matched:
                lines.append(f"❌ **{item_name}** not found.")
                continue

            matched_name, score, _ = matched

            if score < 70:
                lines.append(f"❌ **{item_name}** not found.")
                continue

            display_name, price_text = price_lookup[matched_name]

            clean_price = (
                str(price_text)
                .replace("$", "")
                .replace(",", "")
                .strip()
            )

            try:
                unit_price = int(float(clean_price))
            except ValueError:
                lines.append(f"❌ **{display_name}** has invalid price `{price_text}`.")
                continue

            item_total = quantity * unit_price
            total += item_total

            lines.append(
                f"**{quantity}x {display_name}** "
                f"— ${unit_price:,.0f} each = **${item_total:,.0f}**"
            )

        embed = discord.Embed(
            title="🧾 Warehouse Sale Calculator",
            description="\n".join(lines) if lines else "No items calculated.",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="Total Sale Price",
            value=f"**${total:,.0f}**",
            inline=False
        )

        embed.set_footer(text=f"Calculated by {interaction.user.display_name}")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(
            f"❌ Failed to calculate warehouse cost: {e}"
        )


@bot.tree.command(name="evavailable", description="Set EV price list availability (Leadership only)")
@app_commands.guilds(discord.Object(id=PRIVATE_GUILD_ID))
@app_commands.describe(available="Allow public /pricelist access?")
@app_commands.choices(available=[
    app_commands.Choice(name="Yes", value="yes"),
    app_commands.Choice(name="No", value="no"),
])
async def evavailable_cmd(
    interaction: discord.Interaction,
    available: app_commands.Choice[str]
) -> None:
    if not await ensure_private_guild(interaction):
        return

    member = interaction.user
    if not isinstance(member, discord.Member) or not any(
        role.name.casefold() == "leadership" for role in member.roles
    ):
        await interaction.response.send_message(
            embed=make_error_embed(
                "❌ Permission Denied",
                "Only members with the Leadership role can change EV availability."
            ),
            ephemeral=True
        )
        return

    cursor.execute(
        "INSERT INTO bot_settings (setting_key, setting_value) VALUES (?, ?) "
        "ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
        ("ev_available", available.value)
    )
    conn.commit()

    enabled = available.value == "yes"
    embed = discord.Embed(
        title="✅ EV Available" if enabled else "🔴 EV Unavailable",
        description=(
            "The public Discord can now use `/pricelist`." if enabled
            else "**EV is currently unavailable**."
        ),
        color=discord.Color.green() if enabled else discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"Updated by {member.display_name}")
    await interaction.response.defer(ephemeral=True)
    await interaction.channel.send(embed=embed)
    await interaction.followup.send("✅ EV availability announcement posted in this channel.", ephemeral=True)


@bot.tree.command(name="pricelist", description="Post the current warehouse price list")
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

    if not is_ev_available():
        embed = discord.Embed(
            title="🔴 EV Unavailable",
            description="**EV is currently unavailable**.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text="EV availability status")
        await interaction.response.send_message(embed=embed)
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