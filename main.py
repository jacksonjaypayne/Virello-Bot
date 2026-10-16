import os
import json
import asyncio
import sqlite3
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
if conn.execute("SELECT COUNT(*) FROM gang_statuses").fetchone()[0] == 0:
    conn.executemany(
        "INSERT INTO gang_statuses (name, category, status, sort_order) VALUES (?, ?, ?, ?)",
        [(name, category, status, i) for i, (name, category, status) in enumerate(initial_gangs)]
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
# READY EVENT
# =========================
@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")

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
                for command in (gangadd_cmd, gangstatus_cmd, gangremove_cmd, gangstatusrefresh_cmd):
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
            TIMER_CHANNEL_ID
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

    await timer_channel.send(embed=embed)

    await interaction.response.send_message(
        f"✅ Posted grace timer for **{gang}** in <#{TIMER_CHANNEL_ID}>.",
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