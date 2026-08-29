import os
import sqlite3
import atexit
from pathlib import Path
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
import libsql
from dotenv import dotenv_values

# ============================================================
# CONFIG (Loaded directly from .env)
# ============================================================
env_path = Path(__file__).resolve().parent / ".env"
config = dotenv_values(env_path)

TOKEN = config.get("TELEGRAM_BOT_TOKEN")
ACCESS_PASSWORD = config.get("BOT_ACCESS_PASSWORD", "")
TURSO_DATABASE_URL = config.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = config.get("TURSO_AUTH_TOKEN")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TIMEZONE = ZoneInfo("Asia/Kolkata")
DB_FILE = "study_rpg.db"

# Performance: Turso uses one persistent process-level connection.
PERFORMANCE_VERSION = "2026-08-16-fast-v3-auth"

# Performance: avoid repeating the same remote cleanup work on every update.
_USER_DAY_CACHE = {}
_USER_CACHE = {}

# Personal Study RPG identity
BOT_NAME = "Bipin's Study Buddy"
CYCLE_HOURS = 24

MOTIVATIONAL_QUOTES = [
    "Discipline beats motivation. Show up and do the work.",
    "One focused session today makes tomorrow easier.",
    "You do not need a perfect day. You need a consistent one.",
    "Small progress, repeated every day, becomes a big result.",
    "Protect your focus. Your future is built in these hours.",
    "Do the hard thing first. Then keep going.",
    "Your only competition is the version of you from yesterday.",
    "Consistency is the real superpower.",
]

XP = {
    "easy": 10,
    "medium": 25,
    "hard": 50,
}

RANKS = [
    (0, "🌱 Beginner"),
    (2500, "⚡ Learner"),
    (7500, "🔥 Focused"),
    (15000, "🧠 Scholar"),
    (30000, "💎 Elite Scholar"),
    (50000, "👑 Master"),
]

SUBJECTS = {
    "physics": "⚡ Physics",
    "chemistry": "🧪 Chemistry",
    "math": "📐 Mathematics",
}

# ============================================================
# DATABASE
# ============================================================

class TursoRow:
    def __init__(self, values, columns):
        self._values = tuple(values)
        self._columns = [col[0] for col in columns]

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._columns.index(key)]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __bool__(self):
        return bool(self._values)


class TursoCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return TursoRow(row, self._cursor.description)

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [
            TursoRow(row, self._cursor.description)
            for row in rows
        ]

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class TursoConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, *args, **kwargs):
        cursor = self._connection.execute(*args, **kwargs)
        return TursoCursor(cursor)

    def commit(self):
        return self._connection.commit()

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._connection, name)


_DB_CONNECTION = None


def db():
    """Return one reusable Turso connection for this bot process."""
    global _DB_CONNECTION

    if _DB_CONNECTION is None:
        db_url = TURSO_DATABASE_URL or os.getenv("TURSO_DATABASE_URL")
        auth_token = TURSO_AUTH_TOKEN or os.getenv("TURSO_AUTH_TOKEN")
        _DB_CONNECTION = TursoConnection(
            libsql.connect(
                database=db_url,
                auth_token=auth_token,
            )
        )
    return _DB_CONNECTION


def close_db():
    global _DB_CONNECTION
    if _DB_CONNECTION is not None:
        try:
            _DB_CONNECTION._connection.close()
        except Exception:
            pass
        _DB_CONNECTION = None


atexit.register(close_db)


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            last_completion TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            subject TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            xp INTEGER NOT NULL,
            due TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            minutes INTEGER NOT NULL,
            subject TEXT,
            started_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            earned_at TEXT NOT NULL,
            PRIMARY KEY(user_id, code)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_summaries (
            user_id INTEGER NOT NULL,
            summary_date TEXT NOT NULL,
            total_tasks INTEGER NOT NULL DEFAULT 0,
            completed_tasks INTEGER NOT NULL DEFAULT 0,
            incomplete_tasks INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, summary_date)
        )
    """)

    for statement in [
        "ALTER TABLE users ADD COLUMN reminder_hour INTEGER NOT NULL DEFAULT 8",
        "ALTER TABLE users ADD COLUMN reminder_minute INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN cycle_started_at TEXT",
        "ALTER TABLE users ADD COLUMN cycle_number INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE users ADD COLUMN display_name TEXT",
        "ALTER TABLE users ADD COLUMN access_granted INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(statement)
        except (sqlite3.OperationalError, ValueError):
            pass

    for statement in [
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_status_created ON tasks(user_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_created ON tasks(user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_due_status ON tasks(user_id, due, status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_completed ON tasks(user_id, status, completed_at)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user_started ON study_sessions(user_id, started_at)",
        "CREATE INDEX IF NOT EXISTS idx_users_xp_streak ON users(xp DESC, streak DESC, user_id ASC)",
        "CREATE INDEX IF NOT EXISTS idx_daily_summaries_user_date ON daily_summaries(user_id, summary_date)",
    ]:
        try:
            conn.execute(statement)
        except (sqlite3.OperationalError, ValueError):
            pass

    conn.commit()
    conn.close()


def ensure_user(user_id: int, display_name: str | None = None):
    safe_name = (display_name or "").strip()[:100] or None
    if user_id in _USER_CACHE:
        if safe_name is None or _USER_CACHE[user_id] == safe_name:
            return
        conn = db()
        conn.execute("UPDATE users SET display_name=? WHERE user_id=?", (safe_name, user_id))
        conn.commit()
        conn.close()
        _USER_CACHE[user_id] = safe_name
        return

    conn = db()
    now_iso = datetime.now(TIMEZONE).isoformat()
    conn.execute(
        """
        INSERT INTO users(user_id, cycle_started_at, cycle_number, display_name)
        VALUES(?,?,1,?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name=CASE
                WHEN excluded.display_name IS NOT NULL THEN excluded.display_name
                ELSE users.display_name
            END
        """,
        (user_id, now_iso, safe_name),
    )

    conn.commit()
    conn.close()
    _USER_CACHE[user_id] = safe_name

    today_key = datetime.now(TIMEZONE).date().isoformat()
    if _USER_DAY_CACHE.get(user_id) != today_key:
        _USER_DAY_CACHE[user_id] = today_key
        try:
            cleanup_old_task_rows(user_id)
        except Exception as exc:
            print(f"Deferred task cleanup failed for {user_id}: {exc}")


def refresh_24h_cycle(user_id: int):
    now = datetime.now(TIMEZONE)
    conn = db()
    row = conn.execute(
        "SELECT cycle_started_at, cycle_number FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    if not row:
        conn.close()
        ensure_user(user_id)
        return 1, now

    started = row["cycle_started_at"]
    cycle_number = row["cycle_number"] or 1

    try:
        started_at = datetime.fromisoformat(started).astimezone(TIMEZONE)
    except (TypeError, ValueError):
        started_at = now

    if now - started_at >= timedelta(hours=CYCLE_HOURS):
        elapsed_cycles = int((now - started_at).total_seconds() // (CYCLE_HOURS * 3600))
        cycle_number += max(1, elapsed_cycles)
        started_at = started_at + timedelta(hours=CYCLE_HOURS * max(1, elapsed_cycles))
        conn.execute(
            "UPDATE users SET cycle_started_at=?, cycle_number=? WHERE user_id=?",
            (started_at.isoformat(), cycle_number, user_id),
        )
        conn.commit()

    conn.close()
    return cycle_number, started_at


def next_cycle_task_number(user_id: int) -> int:
    cycle_number, cycle_start = refresh_24h_cycle(user_id)
    conn = db()
    count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id=? AND created_at>=?",
        (user_id, cycle_start.isoformat()),
    ).fetchone()[0]
    conn.close()
    return count + 1


def archive_day_and_delete_tasks(user_id: int, day):
    day_start = datetime.combine(day, time.min, tzinfo=TIMEZONE)
    day_end = day_start + timedelta(days=1)

    conn = db()
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_tasks,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_tasks,
            SUM(CASE WHEN status IN ('pending','skipped') THEN 1 ELSE 0 END) AS incomplete_tasks
        FROM tasks
        WHERE user_id=? AND created_at>=? AND created_at<?
        """,
        (user_id, day_start.isoformat(), day_end.isoformat()),
    ).fetchone()

    total = int(row["total_tasks"] or 0)
    completed = int(row["completed_tasks"] or 0)
    incomplete = int(row["incomplete_tasks"] or 0)

    if total:
        conn.execute(
            """
            INSERT INTO daily_summaries(
                user_id, summary_date, total_tasks, completed_tasks, incomplete_tasks
            )
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id, summary_date) DO UPDATE SET
                total_tasks=excluded.total_tasks,
                completed_tasks=excluded.completed_tasks,
                incomplete_tasks=excluded.incomplete_tasks
            """,
            (user_id, day.isoformat(), total, completed, incomplete),
        )
        conn.execute(
            "DELETE FROM tasks WHERE user_id=? AND created_at>=? AND created_at<?",
            (user_id, day_start.isoformat(), day_end.isoformat()),
        )

    conn.commit()
    conn.close()


def cleanup_old_task_rows(user_id: int):
    today = datetime.now(TIMEZONE).date()

    conn = db()
    old_days = conn.execute(
        """
        SELECT DISTINCT date(created_at) AS day
        FROM tasks
        WHERE user_id=? AND date(created_at) < ?
        ORDER BY day
        """,
        (user_id, today.isoformat()),
    ).fetchall()
    conn.close()

    for row in old_days:
        if row["day"]:
            try:
                archive_day_and_delete_tasks(
                    user_id,
                    datetime.fromisoformat(row["day"]).date(),
                )
            except ValueError:
                pass


def calendar_day_bounds(reference_date=None, offset: int = 0):
    day = reference_date or datetime.now(TIMEZONE).date()
    day = day - timedelta(days=offset)
    start = datetime.combine(day, time.min, tzinfo=TIMEZONE)
    end = start + timedelta(days=1)
    return start, end, day


def daily_task_number(user_id: int, created_at: str | None = None) -> int:
    if created_at:
        try:
            task_time = datetime.fromisoformat(created_at).astimezone(TIMEZONE)
            day = task_time.date()
        except (TypeError, ValueError):
            day = datetime.now(TIMEZONE).date()
    else:
        day = datetime.now(TIMEZONE).date()

    start, end, _ = calendar_day_bounds(day)
    conn = db()
    count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id=? AND created_at>=? AND created_at<?",
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    conn.close()
    return count + 1


_MOTIVATION_CACHE = (None, None)


def motivation():
    global _MOTIVATION_CACHE
    today = datetime.now(TIMEZONE).timetuple().tm_yday
    if _MOTIVATION_CACHE[0] != today:
        _MOTIVATION_CACHE = (
            today,
            MOTIVATIONAL_QUOTES[today % len(MOTIVATIONAL_QUOTES)],
        )
    return _MOTIVATION_CACHE[1]


# ============================================================
# EXTRA FEATURES
# ============================================================

ACHIEVEMENTS = {
    "first_task": ("🎯 First Mission", "Completed your first mission"),
    "streak_7": ("🔥 7 Day Streak", "Maintained a 7-day streak"),
    "tasks_100": ("💯 100 Missions", "Completed 100 missions"),
    "perfect_day": ("🌟 Perfect Day", "Completed every mission for the day"),
    "study_10h": ("⏱ 10 Hour Study", "Logged 10 total study hours"),
}


def award_achievement(user_id, code):
    conn = db()
    exists = conn.execute(
        "SELECT 1 FROM achievements WHERE user_id=? AND code=?",
        (user_id, code),
    ).fetchone()
    if exists:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO achievements(user_id, code, earned_at) VALUES(?,?,?)",
        (user_id, code, datetime.now(TIMEZONE).isoformat()),
    )
    conn.commit()
    conn.close()
    return True


def get_achievements(user_id):
    conn = db()
    rows = conn.execute(
        "SELECT code FROM achievements WHERE user_id=? ORDER BY earned_at",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def total_study_minutes(user_id):
    conn = db()
    value = conn.execute(
        "SELECT COALESCE(SUM(minutes),0) FROM study_sessions WHERE user_id=?",
        (user_id,),
    ).fetchone()[0]
    conn.close()
    return value


def completed_count(user_id):
    conn = db()
    value = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE user_id=? AND status='completed'",
        (user_id,),
    ).fetchone()[0]
    conn.close()
    return value


def check_achievements(user_id, streak):
    now = datetime.now(TIMEZONE)
    today = now.date().isoformat()

    conn = db()
    row = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM tasks
             WHERE user_id=? AND status='completed') AS completed_count,
            (SELECT COALESCE(SUM(minutes),0) FROM study_sessions
             WHERE user_id=?) AS study_minutes,
            (SELECT COUNT(*) FROM tasks
             WHERE user_id=? AND date(created_at)=?) AS today_total,
            (SELECT COUNT(*) FROM tasks
             WHERE user_id=? AND status='completed' AND date(created_at)=?) AS today_completed
        """,
        (user_id, user_id, user_id, today, user_id, today),
    ).fetchone()

    earned_rows = conn.execute(
        "SELECT code FROM achievements WHERE user_id=?",
        (user_id,),
    ).fetchall()
    earned_codes = {r["code"] for r in earned_rows}

    count = row["completed_count"] or 0
    minutes = row["study_minutes"] or 0
    perfect_day = (row["today_total"] or 0) > 0 and (row["today_total"] or 0) == (row["today_completed"] or 0)

    checks = [
        ("first_task", count >= 1),
        ("streak_7", streak >= 7),
        ("tasks_100", count >= 100),
        ("study_10h", minutes >= 600),
        ("perfect_day", perfect_day),
    ]

    unlocked = []
    for code, condition in checks:
        if condition and code not in earned_codes:
            conn.execute(
                "INSERT OR IGNORE INTO achievements(user_id, code, earned_at) VALUES(?,?,?)",
                (user_id, code, now.isoformat()),
            )
            unlocked.append(ACHIEVEMENTS[code][0])

    conn.commit()
    conn.close()
    return unlocked


async def study_command(update, context):
    context.user_data.clear()
    context.user_data["flow"] = "study_minutes"
    await update.message.reply_text(
        "⏱ STUDY TIME\n\nHow many minutes did you study?\n\nExample: 45"
    )


async def reminder_command(update, context):
    context.user_data.clear()
    context.user_data["flow"] = "reminder_time"
    await update.message.reply_text(
        "⏰ DAILY REMINDER\n\nSet your daily reminder time.\n\n"
        "Use 24-hour format, for example: 08:00"
    )


async def achievements_command(update, context):
    user_id = update.effective_user.id

    conn = db()
    conn.execute(
        """
        INSERT OR IGNORE INTO users(user_id, cycle_started_at, cycle_number)
        VALUES(?,?,1)
        """,
        (user_id, datetime.now(TIMEZONE).isoformat()),
    )
    earned_rows = conn.execute(
        "SELECT code FROM achievements WHERE user_id=? ORDER BY earned_at",
        (user_id,),
    ).fetchall()
    earned = {r["code"] for r in earned_rows}

    count_row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN status IN ('pending','skipped') THEN 1 ELSE 0 END) AS incomplete
        FROM tasks
        WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()
    conn.close()

    total_tasks = count_row["total"] or 0
    completed_tasks = count_row["completed"] or 0
    incomplete_tasks = count_row["incomplete"] or 0

    lines = [
        "🏅 ACHIEVEMENTS",
        "",
        "📚 TASK SUMMARY",
        f"✅ Completed: {completed_tasks}",
        f"⏳ Incomplete: {incomplete_tasks}",
        f"📋 Total: {total_tasks}",
        "",
        "Milestones:",
    ]
    for code, (name, description) in ACHIEVEMENTS.items():
        mark = "✅" if code in earned else "🔒"
        lines.append(f"{mark} {name}\n   {description}")

    lines.extend([
        "",
        "🏆 XP → LEVEL",
        "",
    ])
    for i, (minimum, name) in enumerate(RANKS):
        if i + 1 < len(RANKS):
            maximum = RANKS[i + 1][0] - 1
            lines.append(f"{minimum:,}–{maximum:,} XP  →  {name}")
        else:
            lines.append(f"{minimum:,}+ XP  →  {name}")

    lines.extend([
        "",
        "🔥 Streak bonuses",
        "3 days: +25 XP",
        "7 days: +75 XP",
        "14 days: +150 XP",
        "30 days: +300 XP",
        "",
        f"💬 {motivation()}",
    ])

    message = update.effective_message
    if message is None:
        return

    await message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")]
        ]),
    )


def get_week_dates(reference_date=None):
    day = reference_date or datetime.now(TIMEZONE).date()
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def get_consistency_stats(user_id, start_date, end_date):
    conn = db()

    rows = conn.execute(
        """
        SELECT DISTINCT date(completed_at) AS day
        FROM tasks
        WHERE user_id=? AND status='completed'
          AND completed_at IS NOT NULL
          AND date(completed_at) BETWEEN ? AND ?
        ORDER BY day
        """,
        (user_id, start_date.isoformat(), end_date.isoformat()),
    ).fetchall()

    active_dates = {
        datetime.fromisoformat(row["day"]).date()
        for row in rows
        if row["day"]
    }

    total_days = (end_date - start_date).days + 1
    active_days = len(active_dates)
    consistency = int(active_days / total_days * 100) if total_days else 0

    all_rows = conn.execute(
        """
        SELECT DISTINCT date(completed_at) AS day
        FROM tasks
        WHERE user_id=? AND status='completed' AND completed_at IS NOT NULL
        ORDER BY day
        """,
        (user_id,),
    ).fetchall()

    conn.close()

    all_dates = [
        datetime.fromisoformat(row["day"]).date()
        for row in all_rows
        if row["day"]
    ]

    best_streak = 0
    running = 0
    previous = None

    for day in all_dates:
        if previous is not None and day == previous + timedelta(days=1):
            running += 1
        else:
            running = 1
        best_streak = max(best_streak, running)
        previous = day

    return active_days, consistency, best_streak


async def analytics_command(update, context):
    user_id = update.effective_user.id
    ensure_user(user_id)

    week_start, week_end = get_week_dates()
    today = datetime.now(TIMEZONE).date()

    conn = db()

    stats_row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending
        FROM tasks
        WHERE user_id=? AND date(created_at) BETWEEN ? AND ?
        """,
        (user_id, week_start.isoformat(), week_end.isoformat()),
    ).fetchone()

    total = stats_row["total"] or 0
    completed = stats_row["completed"] or 0
    skipped = stats_row["skipped"] or 0
    pending = stats_row["pending"] or 0

    minutes = conn.execute(
        """
        SELECT COALESCE(SUM(minutes),0) FROM study_sessions
        WHERE user_id=? AND date(started_at) BETWEEN ? AND ?
        """,
        (user_id, week_start.isoformat(), week_end.isoformat()),
    ).fetchone()[0]

    subjects = conn.execute(
        """
        SELECT subject, COUNT(*) AS n FROM tasks
        WHERE user_id=? AND status='completed'
        AND date(completed_at) BETWEEN ? AND ?
        GROUP BY subject ORDER BY n DESC
        """,
        (user_id, week_start.isoformat(), week_end.isoformat()),
    ).fetchall()

    user = conn.execute(
        "SELECT streak FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    conn.close()

    consistency_end = min(today, week_end)
    active_days, consistency, best_streak = get_consistency_stats(
        user_id, week_start, consistency_end
    )

    streak = user["streak"] if user else 0
    percent = int(completed / total * 100) if total else 0

    filled = consistency // 10
    consistency_bar = "🟩" * filled + "⬜" * (10 - filled)

    elapsed_days = (consistency_end - week_start).days + 1

    text = (
        f"📈 ANALYTICS\n\n"
        f"📅 {week_start.strftime('%d %b')} → {week_end.strftime('%d %b')}\n\n"
        f"🔥 CURRENT STREAK\n"
        f"{streak} days\n\n"
        f"🏆 BEST STREAK\n"
        f"{best_streak} days\n\n"
        f"📅 CONSISTENCY\n"
        f"{consistency}%  ({active_days}/{elapsed_days} active days)\n"
        f"{consistency_bar}\n\n"
        f"📚 TASK PROGRESS\n"
        f"Completed: {completed}/{total} ({percent}%)\n"
        f"⏳ Pending: {pending}\n"
        f"⏭ Skipped: {skipped}\n"
        f"⏱ Study time: {minutes // 60}h {minutes % 60}m\n\n"
        f"📖 SUBJECTS\n"
    )

    if subjects:
        text += "\n".join(
            f"• {r['subject']}: {r['n']} completed" for r in subjects
        )
    else:
        text += "• No completed missions yet."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Stats", callback_data="menu:stats")],
        [InlineKeyboardButton("📅 Weekly", callback_data="menu:weekly")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
    ])

    if update.callback_query:
        await update.callback_query.message.reply_text(
            text, reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            text, reply_markup=keyboard
        )


async def extra_text_handler(update, context):
    flow = context.user_data.get("flow")

    if flow == "study_minutes":
        try:
            minutes = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("Please enter the study time as a number. Example: 45")
            return True
        if not 1 <= minutes <= 1440:
            await update.message.reply_text("Enter a value between 1 and 1440 minutes.")
            return True

        conn = db()
        conn.execute(
            "INSERT INTO study_sessions(user_id, minutes, started_at) VALUES(?,?,?)",
            (update.effective_user.id, minutes, datetime.now(TIMEZONE).isoformat()),
        )
        conn.commit()
        conn.close()
        context.user_data.clear()

        total = total_study_minutes(update.effective_user.id)
        unlocked = check_achievements(update.effective_user.id, 0)
        text = f"⏱ STUDY TIME LOGGED\n\n+{minutes} minutes\n📚 Total: {total // 60}h {total % 60}m\n\n💬 {motivation()}"
        if unlocked:
            text += "\n\n🏅 NEW\n" + "\n".join(f"• {x}" for x in unlocked)
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())
        return True

    if flow == "reminder_time":
        try:
            parsed = datetime.strptime(update.message.text.strip(), "%H:%M")
        except ValueError:
            await update.message.reply_text("Use HH:MM format. Example: 08:00")
            return True

        conn = db()
        conn.execute(
            "UPDATE users SET reminder_hour=?, reminder_minute=? WHERE user_id=?",
            (parsed.hour, parsed.minute, update.effective_user.id),
        )
        conn.commit()
        conn.close()
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Daily reminder set for {parsed.strftime('%I:%M %p')}.",
            reply_markup=main_menu_keyboard(),
        )
        return True

    return False


# ============================================================
# GAME / PROGRESS
# ============================================================


def get_rank(xp: int):
    rank = RANKS[0][1]
    for minimum, name in RANKS:
        if xp >= minimum:
            rank = name
    return rank


def get_next_rank(xp: int):
    for minimum, name in RANKS:
        if xp < minimum:
            return minimum, name
    return None, None


def progress_bar(xp: int):
    next_xp, next_rank = get_next_rank(xp)
    if next_xp is None:
        return "██████████", 100, None

    previous_xp = 0
    for minimum, _ in RANKS:
        if minimum <= xp:
            previous_xp = minimum

    total = next_xp - previous_xp
    current = xp - previous_xp
    percentage = max(0, min(100, int(current / total * 100)))
    filled = percentage // 10
    return "█" * filled + "░" * (10 - filled), percentage, next_rank


def streak_update(user_row, now):
    last = user_row["last_completion"]
    today = now.date()

    if not last:
        return 1, True

    last_date = datetime.fromisoformat(last).astimezone(TIMEZONE).date()

    if last_date == today:
        return user_row["streak"], False

    if last_date == today - timedelta(days=1):
        return user_row["streak"] + 1, True

    return 1, True


async def custom_daily_reminders(context):
    now = datetime.now(TIMEZONE)
    conn = db()
    users = conn.execute(
        "SELECT user_id, reminder_hour, reminder_minute FROM users WHERE reminder_hour=? AND reminder_minute=?",
        (now.hour, now.minute),
    ).fetchall()

    for user in users:
        tasks = conn.execute(
            """
            SELECT * FROM tasks
            WHERE user_id=? AND status='pending' AND due IS NOT NULL AND date(due)=?
            ORDER BY due ASC LIMIT 10
            """,
            (user["user_id"], now.date().isoformat()),
        ).fetchall()

        if not tasks:
            continue

        text = "🌅 TODAY'S MISSIONS\n\n"
        for task in tasks:
            due = datetime.fromisoformat(task["due"]).astimezone(TIMEZONE)
            text += f"📚 {task['title']}\n⚡ +{task['xp']} XP • ⏰ {due.strftime('%I:%M %p')}\n\n"

        try:
            await context.bot.send_message(
                chat_id=user["user_id"],
                text=text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 My Missions", callback_data="menu:tasks")]
                ]),
            )
        except Exception as exc:
            print(f"Daily reminder failed: {exc}")

    conn.close()


async def deadline_reminders(context):
    now = datetime.now(TIMEZONE)
    end = now + timedelta(minutes=15)

    conn = db()
    rows = conn.execute(
        """
        SELECT * FROM tasks
        WHERE status='pending' AND due IS NOT NULL AND reminder_sent=0
        """
    ).fetchall()

    for task in rows:
        try:
            due = datetime.fromisoformat(task["due"]).astimezone(TIMEZONE)
        except ValueError:
            continue

        if now <= due <= end:
            try:
                await context.bot.send_message(
                    chat_id=task["user_id"],
                    text=(
                        f"⏰ MISSION REMINDER\n\n"
                        f"📚 {task['title']}\n"
                        f"📖 {task['subject']}\n"
                        f"⚡ +{task['xp']} XP\n\n"
                        f"Due within 15 minutes."
                    ),
                )
                conn.execute("UPDATE tasks SET reminder_sent=1 WHERE id=?", (task["id"],))
            except Exception as exc:
                print(f"Deadline reminder failed: {exc}")

    conn.commit()
    conn.close()


async def daily_task_cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()

    for row in users:
        try:
            cleanup_old_task_rows(row["user_id"])
        except Exception as exc:
            print(f"Daily task cleanup failed for {row['user_id']}: {exc}")


async def cycle_rollover_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    users = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    for row in users:
        try:
            refresh_24h_cycle(row["user_id"])
        except Exception as exc:
            print(f"Cycle refresh failed for {row['user_id']}: {exc}")


# ============================================================
# LEADERBOARD
# ============================================================


def leaderboard_data(user_id: int):
    conn = db()

    top_rows = conn.execute(
        """
        SELECT user_id, display_name, xp, streak
        FROM users
        ORDER BY xp DESC, streak DESC, user_id ASC
        LIMIT 10
        """
    ).fetchall()

    user_row = conn.execute(
        "SELECT xp, streak FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    if user_row:
        higher_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE xp > ?
               OR (xp = ? AND streak > ?)
               OR (xp = ? AND streak = ? AND user_id < ?)
            """,
            (
                user_row["xp"], user_row["xp"], user_row["streak"],
                user_row["xp"], user_row["streak"], user_id,
            ),
        ).fetchone()[0]
        user_rank = higher_count + 1
    else:
        user_rank = None

    conn.close()
    return top_rows, user_row, user_rank


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    display_name = update.effective_user.full_name or "Student"
    ensure_user(user_id, display_name)

    rows, user_row, user_rank = leaderboard_data(user_id)

    lines = [
        "🏆 GLOBAL LEADERBOARD",
        "",
        "Top students ranked by XP",
        "",
    ]

    medals = ["🥇", "🥈", "🥉"]

    for index, row in enumerate(rows, start=1):
        name = (row["display_name"] or "Student").replace("\n", " ")[:24]
        prefix = medals[index - 1] if index <= 3 else f"{index}."
        lines.append(
            f"{prefix} {name}\n"
            f"   ⚡ {row['xp']:,} XP • 🔥 {row['streak']} day streak"
        )

    if not rows:
        lines.append("No students have joined yet.")

    if user_row:
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━",
            f"👤 Your Rank: #{user_rank}",
            f"⚡ Your XP: {user_row['xp']:,}",
            f"🔥 Your Streak: {user_row['streak']} days",
        ])

    lines.extend([
        "",
        f"💬 {motivation()}",
    ])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Refresh", callback_data="menu:leaderboard")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "\n".join(lines), reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            "\n".join(lines), reply_markup=keyboard
        )


# ============================================================
# ACCESS GATE + DAILY HISTORY
# ============================================================


AUTHORIZED_USERS = set()


def user_is_authorized(user_id: int) -> bool:
    if user_id in AUTHORIZED_USERS:
        return True

    conn = db()
    row = conn.execute(
        "SELECT access_granted FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()

    authorized = bool(row and row["access_granted"])
    if authorized:
        AUTHORIZED_USERS.add(user_id)
    return authorized


def grant_access(user_id: int, display_name: str | None = None):
    conn = db()
    now_iso = datetime.now(TIMEZONE).isoformat()
    safe_name = (display_name or "").strip()[:100] or None

    conn.execute(
        """
        INSERT OR IGNORE INTO users(
            user_id, cycle_started_at, cycle_number, display_name, access_granted
        )
        VALUES(?,?,1,?,1)
        """,
        (user_id, now_iso, safe_name),
    )
    conn.execute(
        """
        UPDATE users
        SET access_granted=1,
            display_name=COALESCE(?, display_name)
        WHERE user_id=?
        """,
        (safe_name, user_id),
    )
    conn.commit()
    conn.close()
    AUTHORIZED_USERS.add(user_id)
    _USER_CACHE[user_id] = safe_name


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ACCESS_PASSWORD:
        return

    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    text = (message.text or "").strip()
    command = text.split(maxsplit=1)[0].lower() if text else ""

    is_start = command == "/start" or command.startswith("/start@")
    if is_start and user_is_authorized(user.id):
        conn = db()
        conn.execute(
            "UPDATE users SET access_granted=0 WHERE user_id=?",
            (user.id,),
        )
        conn.commit()
        conn.close()
        AUTHORIZED_USERS.discard(user.id)

    if user_is_authorized(user.id):
        return

    if context.user_data.get("access_flow") == "password" and text:
        if text == ACCESS_PASSWORD:
            grant_access(user.id, user.full_name or "Student")
            context.user_data.pop("access_flow", None)

            await start(update, context)
            raise ApplicationHandlerStop

        await message.reply_text(
            "❌ Incorrect access password.\n\n"
            "Please enter the correct password to continue."
        )
        raise ApplicationHandlerStop

    context.user_data.clear()
    context.user_data["access_flow"] = "password"

    await message.reply_text(
        f"🔐 {BOT_NAME} is private.\n\n"
        "Enter the access password to continue."
    )
    raise ApplicationHandlerStop


async def access_callback_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ACCESS_PASSWORD:
        return

    user = update.effective_user
    if user and user_is_authorized(user.id):
        return

    if update.callback_query:
        await update.callback_query.answer(
            "🔐 Enter the access password first.",
            show_alert=True,
        )
        raise ApplicationHandlerStop


def cycle_bounds(user_id: int, offset: int = 0):
    refresh_24h_cycle(user_id)

    now = datetime.now(TIMEZONE)
    conn = db()
    row = conn.execute(
        "SELECT cycle_started_at, cycle_number FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()

    if not row or not row["cycle_started_at"]:
        start = now
        number = 1
    else:
        try:
            start = datetime.fromisoformat(row["cycle_started_at"]).astimezone(TIMEZONE)
        except (TypeError, ValueError):
            start = now
        number = row["cycle_number"] or 1

    start = start - timedelta(hours=CYCLE_HOURS * offset)
    end = start + timedelta(hours=CYCLE_HOURS)
    return start, end, number - offset


async def daily_history_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    offset: int = 0,
):
    user_id = update.effective_user.id
    ensure_user(user_id, update.effective_user.full_name or "Student")

    start_bound, end_bound, day = calendar_day_bounds(offset=offset)
    date_text = day.strftime("%A, %d %b %Y")

    if offset == 0:
        conn = db()
        rows = conn.execute(
            """
            SELECT id, title, subject, xp, status
            FROM tasks
            WHERE user_id=? AND created_at>=? AND created_at<?
            ORDER BY created_at ASC, id ASC
            """,
            (user_id, start_bound.isoformat(), end_bound.isoformat()),
        ).fetchall()
        conn.close()

        lines = [
            "📜 DAILY HISTORY",
            "",
            "📅 TODAY",
            date_text,
            "",
        ]

        if not rows:
            lines.append("No missions on this date.")
        else:
            for index, task in enumerate(rows, start=1):
                status = (
                    "✅ Completed" if task["status"] == "completed"
                    else "⏭ Skipped" if task["status"] == "skipped"
                    else "⏳ Incomplete"
                )
                lines.append(f"{status}  Task {index:02d} — {task['title']}")
                lines.append(f"   {task['subject']} • +{task['xp']} XP")

        buttons = [[
            InlineKeyboardButton("⬅️ Previous Day", callback_data="menu:history:prev")
        ]]
    else:
        conn = db()
        row = conn.execute(
            """
            SELECT total_tasks, completed_tasks, incomplete_tasks
            FROM daily_summaries
            WHERE user_id=? AND summary_date=?
            """,
            (user_id, day.isoformat()),
        ).fetchone()
        conn.close()

        lines = [
            "📜 DAILY HISTORY",
            "",
            "📅 PREVIOUS DAY",
            date_text,
            "",
        ]

        if row:
            lines.extend([
                f"📋 Total tasks: {row['total_tasks']}",
                f"✅ Completed: {row['completed_tasks']}",
                f"⏳ Incomplete: {row['incomplete_tasks']}",
            ])
        else:
            lines.append("No saved task summary for this date.")

        buttons = [[
            InlineKeyboardButton("➡️ Current Day", callback_data="menu:history:current")
        ]]

    buttons.append([
        InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")
    ])
    keyboard = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "\n".join(lines), reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            "\n".join(lines), reply_markup=keyboard
        )


# ============================================================
# MAIN MENU
# ============================================================


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Task", callback_data="menu:add"),
            InlineKeyboardButton("📋 My Tasks", callback_data="menu:tasks"),
        ],
        [
            InlineKeyboardButton("📊 My Stats", callback_data="menu:stats"),
            InlineKeyboardButton("📅 Weekly", callback_data="menu:weekly"),
        ],
        [
            InlineKeyboardButton("⏱ Study Time", callback_data="menu:study"),
            InlineKeyboardButton("📈 Analytics", callback_data="menu:analytics"),
        ],
        [
            InlineKeyboardButton("🏅 Achievements", callback_data="menu:achievements"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="menu:leaderboard"),
        ],
        [
            InlineKeyboardButton("📜 Daily History", callback_data="menu:history"),
        ],
        [
            InlineKeyboardButton("⏰ Reminder", callback_data="menu:reminder"),
        ],
    ])


async def show_main_menu_message(update: Update, text: str):
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            text, reply_markup=main_menu_keyboard()
        )

# ============================================================
# COMMANDS
# ============================================================


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    display_name = update.effective_user.full_name or "Student"
    ensure_user(update.effective_user.id, display_name)
    context.user_data.clear()
    await show_main_menu_message(
        update,
        f"""
🏠 {BOT_NAME} — MAIN MENU

Welcome back, {display_name}! 👋

📚 Turn study into missions.
⚡ Complete tasks and earn XP.
🔥 Build your streak.
📊 Track your progress.

💬 {motivation()}
""",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    display_name = update.effective_user.full_name or "Student"
    ensure_user(update.effective_user.id, display_name)
    context.user_data.clear()
    await show_main_menu_message(
        update,
        f"""
🎮 {BOT_NAME}

Welcome back, {display_name}! 👋

📚 Turn study into missions.
⚡ Complete tasks and earn XP.
🔥 Build your streak.
📊 Track your progress.

💬 {motivation()}
""",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"""
❓ {BOT_NAME}'s Study RPG

➕ Add Task
Create a study mission, choose its subject, difficulty and deadline.

📋 My Tasks
View pending missions and complete or skip them.

📊 My Stats
See XP, level, streak and your next level.

📅 Weekly
Compare this week's progress with last week.

📈 Analytics
See consistency, best streak, study time and subject progress.

🏅 Achievements
Unlock milestones and see the XP required for every level.

🏆 Leaderboard
See the global XP ranking and your current position.

📜 Daily History
Review completed and incomplete missions from your current or previous 24-hour cycle.

⏱ Study Time
Log your study minutes.

⏰ Reminder
Set your daily task reminder.

Every 24 hours, a new study cycle begins and its first task is Task 1.
Your old history stays safe.

Commands:
 /start  /add  /tasks  /done ID  /skip ID
 /stats  /weekly  /analytics  /achievements  /leaderboard
 /study  /reminder  /help
"""
    )


# ============================================================
# ADD TASK WIZARD
# ============================================================


async def add_menu_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()

    ensure_user(update.effective_user.id)
    context.user_data.clear()
    context.user_data["add_flow"] = "subject"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Physics", callback_data="add:subject:physics"),
            InlineKeyboardButton("🧪 Chemistry", callback_data="add:subject:chemistry"),
        ],
        [InlineKeyboardButton("📐 Mathematics", callback_data="add:subject:math")],
        [InlineKeyboardButton("❌ Cancel", callback_data="add:cancel")],
    ])

    text = "➕ NEW MISSION\n\nChoose a subject:"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


async def handle_task_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await extra_text_handler(update, context):
        return

    if context.user_data.get("add_flow") != "task_name":
        return

    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("Enter the mission name:")
        return
    if len(title) > 200:
        await update.message.reply_text("Keep the mission name under 200 characters.")
        return

    context.user_data["title"] = title
    context.user_data["add_flow"] = "level"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Easy  +10 XP", callback_data="add:level:easy"),
            InlineKeyboardButton("🟡 Normal  +25 XP", callback_data="add:level:medium"),
        ],
        [InlineKeyboardButton("🔴 Important  +50 XP", callback_data="add:level:hard")],
        [InlineKeyboardButton("❌ Cancel", callback_data="add:cancel")],
    ])
    await update.message.reply_text(
        "⚡ Choose the difficulty:",
        reply_markup=keyboard,
    )


async def add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add:cancel":
        context.user_data.clear()
        await query.edit_message_text("❌ Mission creation cancelled.")
        return

    _, action, value = data.split(":", 2)

    if action == "subject":
        context.user_data["subject"] = SUBJECTS[value]
        context.user_data["add_flow"] = "task_name"
        await query.edit_message_text(
            f"""
📖 {SUBJECTS[value]}

Enter your study mission.

Example:
Solve 30 questions
"""
        )
        return

    if action == "level":
        context.user_data["difficulty"] = value
        context.user_data["add_flow"] = "deadline"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📅 Today", callback_data="add:due:today"),
                InlineKeyboardButton("🌅 Tomorrow", callback_data="add:due:tomorrow"),
            ],
            [InlineKeyboardButton("♾️ No deadline", callback_data="add:due:none")],
            [InlineKeyboardButton("❌ Cancel", callback_data="add:cancel")],
        ])
        await query.edit_message_text(
            "⏰ Choose a deadline:",
            reply_markup=keyboard,
        )
        return

    if action == "due":
        title = context.user_data.get("title")
        subject = context.user_data.get("subject")
        difficulty = context.user_data.get("difficulty")

        if not title or not subject or not difficulty:
            context.user_data.clear()
            await query.edit_message_text(
                "❌ Some information is missing. Please start a new mission."
            )
            return

        now = datetime.now(TIMEZONE)
        cycle_number, cycle_start = refresh_24h_cycle(query.from_user.id)

        day_start, day_end, task_date = calendar_day_bounds()
        conn = db()
        daily_number = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id=? AND created_at>=? AND created_at<?",
            (query.from_user.id, day_start.isoformat(), day_end.isoformat()),
        ).fetchone()[0] + 1

        if value == "today":
            due = now.replace(hour=23, minute=59, second=0, microsecond=0)
        elif value == "tomorrow":
            due = (now + timedelta(days=1)).replace(
                hour=23, minute=59, second=0, microsecond=0
            )
        else:
            due = None

        cursor = conn.execute(
            """
            INSERT INTO tasks(
                user_id, title, subject, difficulty, xp, due, status, created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                query.from_user.id,
                title,
                subject,
                difficulty,
                XP[difficulty],
                due.isoformat() if due else None,
                "pending",
                now.isoformat(),
            ),
        )
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()

        context.user_data.clear()

        await query.edit_message_text(
            f"""
✅ MISSION ADDED

📅 {task_date.strftime('%A, %d %b %Y')}
Task {daily_number:02d} • ID #{task_id}

📚 {title}

📖 {subject}
⚡ +{XP[difficulty]} XP
⏰ {due.strftime('%d %b, %I:%M %p') if due else 'No deadline'}

💬 {motivation()}
""",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 My Tasks", callback_data="menu:tasks")],
                [InlineKeyboardButton("➕ Add Another", callback_data="menu:add")],
            ]),
        )

# ============================================================
# TASK LIST
# ============================================================


async def send_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    day_start, day_end, task_date = calendar_day_bounds()
    conn = db()

    pending_rows = conn.execute(
        """
        SELECT *,
               ROW_NUMBER() OVER (ORDER BY created_at ASC, id ASC) AS daily_number
        FROM tasks
        WHERE user_id=? AND status='pending'
          AND created_at>=? AND created_at<?
        ORDER BY
            CASE WHEN due IS NULL THEN 1 ELSE 0 END,
            due ASC,
            created_at ASC,
            id ASC
        """,
        (user_id, day_start.isoformat(), day_end.isoformat()),
    ).fetchall()

    completed_rows = conn.execute(
        """
        SELECT *,
               ROW_NUMBER() OVER (ORDER BY created_at ASC, id ASC) AS daily_number
        FROM tasks
        WHERE user_id=? AND status='completed'
          AND created_at>=? AND created_at<?
        ORDER BY completed_at ASC, created_at ASC, id ASC
        """,
        (user_id, day_start.isoformat(), day_end.isoformat()),
    ).fetchall()

    conn.close()

    if not pending_rows and not completed_rows:
        text = "📋 MY MISSIONS\n\nNo missions today yet."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add Task", callback_data="menu:add")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text, reply_markup=keyboard)
        return

    for task in pending_rows:
        due_text = "No deadline"
        if task["due"]:
            due = datetime.fromisoformat(task["due"]).astimezone(TIMEZONE)
            due_text = due.strftime("%d %b, %I:%M %p")

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Complete", callback_data=f"task:done:{task['id']}"),
                InlineKeyboardButton("⏭ Skip", callback_data=f"task:skip:{task['id']}"),
            ]
        ])

        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"📚 PENDING • TASK {int(task['daily_number']):02d} • {task_date.strftime('%d %b %Y')}\n\n"
                f"{task['title']}\n\n"
                f"📖 {task['subject']}\n"
                f"⚡ +{task['xp']} XP\n"
                f"⏰ {due_text}"
            ),
            reply_markup=keyboard,
        )

    if completed_rows:
        completed_lines = [
            "✅ COMPLETED TODAY",
            f"📅 {task_date.strftime('%A, %d %b %Y')}",
            "",
        ]

        for task in completed_rows:
            completed_time = ""
            if task["completed_at"]:
                try:
                    completed_dt = datetime.fromisoformat(task["completed_at"]).astimezone(TIMEZONE)
                    completed_time = f" • {completed_dt.strftime('%I:%M %p')}"
                except (TypeError, ValueError):
                    pass

            completed_lines.extend([
                f"✅ Task {int(task['daily_number']):02d} — {task['title']}",
                f"   📖 {task['subject']} • +{task['xp']} XP{completed_time}",
                "",
            ])

        await context.bot.send_message(
            chat_id=user_id,
            text="\n".join(completed_lines).rstrip(),
        )

    summary = [
        "📋 MY MISSIONS",
        "",
        f"⏳ Pending today: {len(pending_rows)}",
        f"✅ Completed today: {len(completed_rows)}",
    ]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Task", callback_data="menu:add")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(
            "\n".join(summary),
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            "\n".join(summary),
            reply_markup=keyboard,
        )

# ============================================================
# COMPLETE / SKIP
# ============================================================


async def complete_task(user_id: int, task_id: int):
    now = datetime.now(TIMEZONE)
    conn = db()

    task = conn.execute(
        "SELECT * FROM tasks WHERE id=? AND user_id=?",
        (task_id, user_id),
    ).fetchone()

    if not task:
        conn.close()
        return None, "not_found"

    if task["status"] != "pending":
        conn.close()
        return task, "already_done"

    user = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    old_xp = user["xp"]
    new_streak, new_day = streak_update(user, now)

    bonus = 0
    if new_day:
        if new_streak == 3:
            bonus = 25
        elif new_streak == 7:
            bonus = 75
        elif new_streak == 14:
            bonus = 150
        elif new_streak == 30:
            bonus = 300

    earned = task["xp"] + bonus
    new_xp = old_xp + earned

    conn.execute(
        "UPDATE tasks SET status='completed', completed_at=? WHERE id=?",
        (now.isoformat(), task_id),
    )
    conn.execute(
        """
        UPDATE users
        SET xp=?, streak=?, last_completion=?
        WHERE user_id=?
        """,
        (new_xp, new_streak, now.isoformat(), user_id),
    )
    conn.commit()
    conn.close()

    unlocked = check_achievements(user_id, new_streak)

    return {
        "task": task,
        "earned": earned,
        "bonus": bonus,
        "xp": new_xp,
        "streak": new_streak,
        "rank": get_rank(new_xp),
        "unlocked": unlocked,
    }, "success"


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /done TASK_ID")
        return

    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("The mission ID must be a number.")
        return

    result, status = await complete_task(update.effective_user.id, task_id)
    if status == "not_found":
        await update.message.reply_text("❌ Mission not found.")
        return
    if status == "already_done":
        await update.message.reply_text("ℹ️ This mission is already complete.")
        return

    bonus_line = f"\n🎁 Streak bonus: +{result['bonus']} XP" if result["bonus"] else ""
    achievement_line = (
        "\n\n🏅 NEW ACHIEVEMENT\n" + "\n".join(f"• {x}" for x in result.get("unlocked", []))
        if result.get("unlocked") else ""
    )
    await update.message.reply_text(
        f"""
✅ MISSION COMPLETE

📚 {result['task']['title']}

✨ +{result['earned']} XP
⚡ Total XP: {result['xp']}
🏆 Level: {result['rank']}
🔥 Streak: {result['streak']} days
{bonus_line}{achievement_line}

💬 {motivation()}
"""
    )


async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /skip TASK_ID")
        return

    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("The mission ID must be a number.")
        return

    conn = db()
    task = conn.execute(
        "SELECT * FROM tasks WHERE id=? AND user_id=?",
        (task_id, update.effective_user.id),
    ).fetchone()

    if not task:
        conn.close()
        await update.message.reply_text("❌ Mission not found.")
        return

    if task["status"] != "pending":
        conn.close()
        await update.message.reply_text("ℹ️ This mission is not pending.")
        return

    conn.execute("UPDATE tasks SET status='skipped' WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"⏭ MISSION SKIPPED\n\n📚 {task['title']}\n\nNo XP was removed. Keep moving forward."
    )

# ============================================================
# STATS / WEEKLY
# ============================================================


async def send_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.now(TIMEZONE)

    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO users(user_id, cycle_started_at, cycle_number) VALUES(?,?,1)",
        (user_id, now.isoformat()),
    )
    user = conn.execute(
        """
        SELECT *,
               (SELECT COUNT(*) FROM tasks t
                WHERE t.user_id=u.user_id AND t.created_at>=u.cycle_started_at) + 1 AS next_cycle_task,
               (SELECT COUNT(*) FROM tasks t
                WHERE t.user_id=u.user_id AND t.created_at>=? AND t.created_at<?) AS today_total,
               (SELECT COUNT(*) FROM tasks t
                WHERE t.user_id=u.user_id AND t.status='completed'
                  AND t.created_at>=? AND t.created_at<?) AS today_completed,
               (SELECT total_tasks FROM daily_summaries ds
                WHERE ds.user_id=u.user_id AND ds.summary_date=?) AS yesterday_total,
               (SELECT completed_tasks FROM daily_summaries ds
                WHERE ds.user_id=u.user_id AND ds.summary_date=?) AS yesterday_completed,
               (SELECT incomplete_tasks FROM daily_summaries ds
                WHERE ds.user_id=u.user_id AND ds.summary_date=?) AS yesterday_incomplete
        FROM users u WHERE u.user_id=?
        """,
        (
            calendar_day_bounds(offset=0)[0].isoformat(),
            calendar_day_bounds(offset=0)[1].isoformat(),
            calendar_day_bounds(offset=0)[0].isoformat(),
            calendar_day_bounds(offset=0)[1].isoformat(),
            calendar_day_bounds(offset=1)[2].isoformat(),
            calendar_day_bounds(offset=1)[2].isoformat(),
            calendar_day_bounds(offset=1)[2].isoformat(),
            user_id,
        ),
    ).fetchone()

    cycle_start_raw = user["cycle_started_at"]
    cycle_number = user["cycle_number"] or 1
    try:
        cycle_start = datetime.fromisoformat(cycle_start_raw).astimezone(TIMEZONE)
    except (TypeError, ValueError):
        cycle_start = now

    changed = False
    if now - cycle_start >= timedelta(hours=CYCLE_HOURS):
        elapsed_cycles = max(1, int((now - cycle_start).total_seconds() // (CYCLE_HOURS * 3600)))
        cycle_number += elapsed_cycles
        cycle_start += timedelta(hours=CYCLE_HOURS * elapsed_cycles)
        conn.execute(
            "UPDATE users SET cycle_started_at=?, cycle_number=? WHERE user_id=?",
            (cycle_start.isoformat(), cycle_number, user_id),
        )
        changed = True

    conn.commit()
    conn.close()

    today_start, today_end, today_date = calendar_day_bounds(offset=0)
    _, _, yesterday_date = calendar_day_bounds(offset=1)
    today_total = int(user["today_total"] or 0)
    today_completed = int(user["today_completed"] or 0)
    yesterday_total = int(user["yesterday_total"] or 0)
    yesterday_completed = int(user["yesterday_completed"] or 0)
    yesterday_incomplete = int(user["yesterday_incomplete"] or 0)
    next_task = 1 if changed else int(user["next_cycle_task"] or 1)

    bar, percentage, next_rank = progress_bar(user["xp"])
    next_text = f"Next level: {next_rank}" if next_rank else "👑 Highest level reached"
    cycle_end = cycle_start + timedelta(hours=CYCLE_HOURS)
    today_status = f"{today_completed}/{today_total} completed" if today_total else "No tasks yet"

    text = f"""
📊 {BOT_NAME.upper()}'S PROGRESS

⚡ XP: {user['xp']}
🏆 Level: {get_rank(user['xp'])}
🔥 Streak: {user['streak']} days

{bar}  {percentage}%
{next_text}

📅 DAILY TASK STATUS

🟢 TODAY — {today_date.strftime('%d %b %Y')}
Tasks: {today_status}
Next task: Task {today_total + 1:02d}

🔵 YESTERDAY — {yesterday_date.strftime('%d %b %Y')}
Completed: {yesterday_completed}/{yesterday_total}
⏳ Incomplete: {yesterday_incomplete}

🕐 24-HOUR CYCLE
Cycle {cycle_number}
Next mission: Task {next_task}
Ends: {cycle_end.strftime('%d %b, %I:%M %p')}

💬 {motivation()}
"""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Task", callback_data="menu:add")],
        [InlineKeyboardButton("📜 Daily History", callback_data="menu:history")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
    ])

    message = update.effective_message
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif message:
        await message.reply_text(text, reply_markup=keyboard)


async def send_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)

    current_start, current_end = get_week_dates()
    previous_start = current_start - timedelta(days=7)
    previous_end = current_start - timedelta(days=1)

    conn = db()

    def week_stats(start_date, end_date):
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                COALESCE(SUM(CASE WHEN status='completed' THEN xp ELSE 0 END),0) AS xp
            FROM tasks
            WHERE user_id=? AND date(created_at) BETWEEN ? AND ?
            """,
            (user_id, start_date.isoformat(), end_date.isoformat()),
        ).fetchone()

        return row["total"] or 0, row["completed"] or 0, row["xp"] or 0

    total, completed, base_xp = week_stats(current_start, current_end)
    prev_total, prev_completed, prev_xp = week_stats(previous_start, previous_end)

    conn.close()

    percentage = int(completed / total * 100) if total else 0
    prev_percentage = int(prev_completed / prev_total * 100) if prev_total else 0
    filled = percentage // 10
    bar = "█" * filled + "░" * (10 - filled)

    text = f"""
📅 WEEKLY REPORT

🟢 THIS WEEK
{current_start.strftime('%d %b')} → {current_end.strftime('%d %b')}

📚 Completed: {completed}/{total}
📊 Completion: {percentage}%

{bar}

⚡ Task XP: +{base_xp}

━━━━━━━━━━━━━━

🔵 LAST WEEK
{previous_start.strftime('%d %b')} → {previous_end.strftime('%d %b')}

📚 Completed: {prev_completed}/{prev_total}
📊 Completion: {prev_percentage}%
⚡ Task XP: +{prev_xp}

💡 A new week starts every Monday.
Your history stays safe.
"""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Analytics", callback_data="menu:analytics")],
        [InlineKeyboardButton("📋 My Tasks", callback_data="menu:tasks")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


# ============================================================
# CALLBACK ROUTER
# ============================================================


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("menu:"):
        action = data.split(":", 1)[1]

        await query.answer()

        if action == "home":
            context.user_data.clear()
            await show_main_menu_message(update, f"🎮 {BOT_NAME}'s Study RPG\n\nChoose a mission and get started.\n\n💬 {motivation()}")
        elif action == "add":
            await add_menu_start(update, context)
        elif action == "tasks":
            await send_tasks(update, context)
        elif action == "stats":
            await send_stats(update, context)
        elif action == "weekly":
            await send_weekly(update, context)
        elif action == "study":
            context.user_data.clear()
            context.user_data["flow"] = "study_minutes"
            await query.edit_message_text("⏱ STUDY TIME\n\nHow many minutes did you study?\n\nExample: 45")
        elif action == "analytics":
            await query.message.reply_text("📈 Analytics")
            await analytics_command(update, context)
        elif action == "achievements":
            await achievements_command(update, context)
        elif action == "leaderboard":
            await leaderboard_command(update, context)
        elif action == "history":
            await daily_history_command(update, context, 0)
        elif action == "history:prev":
            await daily_history_command(update, context, 1)
        elif action == "history:current":
            await daily_history_command(update, context, 0)
        elif action == "reminder":
            context.user_data.clear()
            context.user_data["flow"] = "reminder_time"
            await query.edit_message_text("⏰ DAILY REMINDER\n\nSet your daily reminder.\n\nUse HH:MM format, for example: 08:00")
        return

    if data.startswith("add:"):
        await add_callback(update, context)
        return

    if data.startswith("task:"):
        await query.answer()
        _, action, task_id_text = data.split(":", 2)
        task_id = int(task_id_text)
        user_id = query.from_user.id

        if action == "done":
            result, status = await complete_task(user_id, task_id)
            if status == "not_found":
                await query.edit_message_text("❌ Mission not found.")
                return
            if status == "already_done":
                await query.answer("This mission is already complete.", show_alert=True)
                return

            bonus_line = f"\n🎁 Streak bonus: +{result['bonus']} XP" if result["bonus"] else ""
            achievement_line = (
                "\n\n🏅 NEW ACHIEVEMENT\n" + "\n".join(f"• {x}" for x in result.get("unlocked", []))
                if result.get("unlocked") else ""
            )
            await query.edit_message_text(
                f"""
✅ TASK COMPLETE

📚 {result['task']['title']}

✨ +{result['earned']} XP
⚡ Total XP: {result['xp']}
🏆 Rank: {result['rank']}
🔥 Streak: {result['streak']} days
{bonus_line}
""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 My Tasks", callback_data="menu:tasks")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
                ]),
            )
            return

        if action == "skip":
            conn = db()
            task = conn.execute(
                "SELECT * FROM tasks WHERE id=? AND user_id=?",
                (task_id, user_id),
            ).fetchone()
            if not task:
                conn.close()
                await query.edit_message_text("❌ Mission not found.")
                return
            if task["status"] != "pending":
                conn.close()
                await query.answer("This mission is not pending.", show_alert=True)
                return
            conn.execute("UPDATE tasks SET status='skipped' WHERE id=?", (task_id,))
            conn.commit()
            conn.close()
            await query.edit_message_text(
                f"⏭ MISSION SKIPPED\n\n📚 {task['title']}\n\nNo XP was removed. Keep moving forward.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 My Tasks", callback_data="menu:tasks")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
                ]),
            )
            return

# ============================================================
# DAILY REMINDER
# ============================================================


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TIMEZONE).date()
    conn = db()
    users = conn.execute("SELECT user_id FROM users").fetchall()

    for row in users:
        user_id = row["user_id"]
        tasks = conn.execute(
            """
            SELECT * FROM tasks
            WHERE user_id=? AND status='pending'
            AND due IS NOT NULL AND date(due)=?
            ORDER BY due ASC
            LIMIT 10
            """,
            (user_id, today.isoformat()),
        ).fetchall()

        if not tasks:
            continue

        lines = ["🌅 TODAY'S TASKS", ""]
        for task in tasks:
            due = datetime.fromisoformat(task["due"]).astimezone(TIMEZONE)
            lines.append(
                f"📚 {task['title']}\n"
                f"⚡ +{task['xp']} XP • ⏰ {due.strftime('%I:%M %p')}"
            )
            lines.append("")

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 My Tasks", callback_data="menu:tasks")]
                ]),
            )
        except Exception as exc:
            print(f"Reminder failed for {user_id}: {exc}")

    conn.close()


# ============================================================
# TELEGRAM COMMAND MENU
# ============================================================


async def setup_commands(application):
    await application.bot.set_my_commands([
        ("menu", "🏠 Main Menu"),
        ("start", "🎮 Bipin's Study Buddy"),
        ("add", "➕ New Mission"),
        ("tasks", "📋 My Missions"),
        ("done", "✅ Complete Mission"),
        ("skip", "⏭ Skip Mission"),
        ("stats", "📊 My Progress"),
        ("weekly", "📅 Weekly Report"),
        ("analytics", "📈 Analytics"),
        ("achievements", "🏅 Achievements"),
        ("leaderboard", "🏆 Global Leaderboard"),
        ("history", "📜 Daily Task History"),
        ("study", "⏱ Log Study Time"),
        ("reminder", "⏰ Set Reminder"),
        ("help", "❓ Help"),
    ])

# ============================================================
# MAIN
# ============================================================


def build_application():
    if not TOKEN:
        raise SystemExit(
            "❌ TELEGRAM_BOT_TOKEN missing. Set it before starting the bot."
        )

    init_db()

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(setup_commands)
        .build()
    )

    application.add_handler(
        MessageHandler(filters.ALL, access_guard),
        group=-1,
    )
    application.add_handler(
        CallbackQueryHandler(access_callback_guard),
        group=-1,
    )

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_text)
    )

    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_menu_start))
    application.add_handler(CommandHandler("tasks", send_tasks))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(CommandHandler("skip", skip))
    application.add_handler(CommandHandler("stats", send_stats))
    application.add_handler(CommandHandler("weekly", send_weekly))
    application.add_handler(CommandHandler("analytics", analytics_command))
    application.add_handler(CommandHandler("achievements", achievements_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    application.add_handler(CommandHandler("history", daily_history_command))
    application.add_handler(CommandHandler("study", study_command))
    application.add_handler(CommandHandler("reminder", reminder_command))
    application.add_handler(CallbackQueryHandler(callback_router))

    application.job_queue.run_repeating(
        custom_daily_reminders,
        interval=60,
        first=5,
        name="custom-daily-reminders",
    )
    application.job_queue.run_repeating(
        deadline_reminders,
        interval=300,
        first=10,
        name="deadline-reminders",
    )
    application.job_queue.run_repeating(
        cycle_rollover_job,
        interval=3600,
        first=20,
        name="24h-cycle-rollover",
    )
    application.job_queue.run_repeating(
        daily_task_cleanup_job,
        interval=3600,
        first=30,
        name="daily-task-cleanup",
    )

    return application


def main():
    application = build_application()
    print("Bypnn's Study Buddy is running...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()