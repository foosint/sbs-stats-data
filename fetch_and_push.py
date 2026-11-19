#!/usr/bin/env python3
"""
fetch_and_push.py
-----------------
Fetches latest SBS battlefield stats (overall daily/monthly and monthly per
subdivision), writes them into data/sbs.db in this repo, then commits the
DB and pushes it over SSH with the repo's deploy key (only with PUBLISH=1,
which is set on the Pi only).

Run via cron every 10 minutes:
    */10 * * * * cd /home/pi/sbs-stats-data && uv run fetch_and_push.py >> cron.log 2>&1

Configuration via .env (see .env.example).
"""

import os
import sqlite3
import subprocess
import time
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Load .env ────────────────────────────────────────────────────────────────

_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ─── Config ───────────────────────────────────────────────────────────────────

HERE    = Path(__file__).parent
DB_PATH = os.environ.get("SBS_DB_PATH", str(HERE / "data" / "sbs.db"))
# Commit and push the DB after each run. Set PUBLISH=1 only on the Pi,
# so that one machine writes the data.
PUBLISH = os.environ.get("PUBLISH") == "1"

KYIV_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")

# ─── SBS API endpoints ────────────────────────────────────────────────────────

DAILY_URL = "https://sbs-group.army/api/public/statistics/68b0c85589944c4bfb2a5edc/68fa98652f31834f2e051459"
YESTERDAY_URL = "https://sbs-group.army/api/public/statistics/68b0c85589944c4bfb2a5edc/68b4852e792cdf918400daf1"

SBS_API = "https://sbs-group.army/api/public"

# Monthly period urls are discovered via the API (see get_monthly_periods()).
# Division 0 is the whole USF grouping (monthly_stats).
OVERALL_DIVISION_ID = 0

# Subdivision slug (as on sbs-group.army/en/subdivision/<slug>) → division ids.
# cusd_stss is shown on the website as the sum of divisions 14 and 15.
SUBDIVISIONS: dict[str, list[int]] = {
    "magyars_birds":    [1],
    "nemesis":          [2],
    "phoenix":          [3],
    "steppe_predators": [4],
    "rarog":            [5],
    "achilles":         [6],
    "k_2":              [7],
    "raid":             [8],
    "svarog":           [10],
    "9_susbr":          [12],
    "hawks":            [13],
    "cusd_stss":        [14, 15],
}
MONTHLY_START_MONTH = "2025-11"
# Number of recent months (incl. the current one) refetched on every run.
# Older months don't change anymore and are only fetched once if missing in the DB.
MONTHLY_REFRESH_MONTHS = 2
# Minimum pause between two SBS requests (the API answers bursts with HTTP 429)
REQUEST_DELAY = 1.0
SUBDIVISION_REQUEST_DELAY = 2.0

SBS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ─── Schema ───────────────────────────────────────────────────────────────────

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date DATE, hour INTEGER,
            data_collected_at TEXT, last_updated DATETIME,
            personnel_killed INTEGER, personnel_wounded INTEGER,
            total_targets_hit INTEGER, total_targets_destroyed INTEGER,
            total_personnel_casualties INTEGER,
            flights_strike INTEGER, flights_recon INTEGER,
            PRIMARY KEY (date, hour)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_stats (
            date DATE,
            data_collected_at TEXT, last_updated DATETIME,
            personnel_killed INTEGER, personnel_wounded INTEGER,
            total_targets_hit INTEGER, total_targets_destroyed INTEGER,
            total_personnel_casualties INTEGER,
            flights_strike INTEGER, flights_recon INTEGER,
            PRIMARY KEY (date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subdivision_monthly_stats (
            date DATE, subdivision TEXT,
            data_collected_at TEXT, last_updated DATETIME,
            personnel_killed INTEGER, personnel_wounded INTEGER,
            total_targets_hit INTEGER, total_targets_destroyed INTEGER,
            total_personnel_casualties INTEGER,
            flights_strike INTEGER, flights_recon INTEGER,
            PRIMARY KEY (date, subdivision)
        )
    """)
    conn.commit()
    # Migrate existing DBs created before the flights columns were added
    for table in ("daily_stats", "monthly_stats", "subdivision_monthly_stats"):
        add_missing_columns(conn, table, ["flights_strike", "flights_recon"])


def add_missing_columns(conn: sqlite3.Connection, table: str, cols: list[str]) -> None:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    for col in cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER")
    conn.commit()


def ensure_columns(conn: sqlite3.Connection, table: str, tids: list[int]) -> None:
    add_missing_columns(conn, table, [f"{prefix}_{tid}" for tid in tids for prefix in ("hit", "destroyed")])

# ─── SBS HTTP fetch with retries ──────────────────────────────────────────────

_session = requests.Session()
_session.headers.update(SBS_HEADERS)
_last_request = 0.0


def _retry_delay(r: requests.Response, attempt: int, backoff: int) -> float:
    retry_after = r.headers.get("Retry-After", "")
    if retry_after.isdigit():
        return int(retry_after)
    # Rate limiting needs a longer pause than transient server errors
    return (15 if r.status_code == 429 else backoff) * attempt


def fetch_json(url: str, retries: int = 5, backoff: int = 5, delay: float = REQUEST_DELAY) -> dict:
    global _last_request
    for attempt in range(1, retries + 1):
        wait = delay - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            r = _session.get(url, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            if status == 429 or status >= 500:
                log(f"⚠️  HTTP {status}. Attempt {attempt}/{retries}...")
                if attempt < retries:
                    time.sleep(_retry_delay(e.response, attempt, backoff))
                    continue
            raise
        except requests.exceptions.RequestException as e:
            log(f"⚠️  Network error: {e}. Attempt {attempt}/{retries}...")
            if attempt < retries:
                time.sleep(backoff * attempt)
                continue
            raise
    raise RuntimeError(f"Failed after {retries} attempts: {url}")

# ─── Parse SBS response ───────────────────────────────────────────────────────

def parse_response(raw: dict) -> dict:
    data = raw.get("data", {})
    if not data:
        raise ValueError("API response missing 'data' field")
    personnel = data.get("personnel", {})
    flights = data.get("flights") or {}
    targets = {
        t["targetClassId"]: {"hit": t.get("hit"), "destroyed": t.get("destroyed")}
        for t in data.get("targetsByType", [])
        if t.get("targetClassId") is not None
    }
    return {
        "data_collected_at":          data.get("dataCollectedAt"),
        "start_date":          data.get("startDate"),
        "last_updated":               data.get("lastUpdated"),
        "personnel_killed":           personnel.get("killed"),
        "personnel_wounded":          personnel.get("wounded"),
        "total_targets_hit":          data.get("totalTargetsHit"),
        "total_targets_destroyed":    data.get("totalTargetsDestroyed"),
        "total_personnel_casualties": data.get("totalPersonnelCasualties"),
        "flights_strike":             flights.get("strike"),
        "flights_recon":              flights.get("recon"),
        "targets": targets,
    }


def to_kyiv(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(KYIV_TZ)

# ─── Monthly periods / subdivisions ───────────────────────────────────────────

def get_monthly_periods(division_id: int, delay: float = REQUEST_DELAY) -> dict[str, str]:
    """Return {"YYYY-MM": stats_url} for a division's monthly periods.
    Period ids are reused every year, but startDate always reflects the month
    the period currently holds."""
    sub = fetch_json(f"{SBS_API}/subdivisions/by-division-id/{division_id}", delay=delay)["data"]["subdivision"]
    periods: dict[str, str] = {}
    for p in sub.get("periods", []):
        if not p.get("periodType", "").startswith("monthly_") or not p.get("startDate"):
            continue
        month = to_kyiv(p["startDate"]).strftime("%Y-%m")
        if month >= MONTHLY_START_MONTH:
            periods[month] = f"{SBS_API}/statistics/{sub['_id']}/{p['id']}"
    return periods


_SUM_FIELDS = (
    "personnel_killed", "personnel_wounded",
    "total_targets_hit", "total_targets_destroyed", "total_personnel_casualties",
    "flights_strike", "flights_recon",
)


def sum_parsed(parts: list[dict]) -> dict:
    """Combine parsed responses of several divisions (e.g. cusd_stss = 14 + 15)."""
    if len(parts) == 1:
        return parts[0]

    def add(values):
        values = [v for v in values if v is not None]
        return sum(int(v) for v in values) if values else None

    out = {
        "data_collected_at": max((p["data_collected_at"] or "" for p in parts), default=None) or None,
        "start_date":        parts[0]["start_date"],
        "last_updated":      None,
    }
    for f in _SUM_FIELDS:
        out[f] = add(p[f] for p in parts)
    tids = {tid for p in parts for tid in p["targets"]}
    out["targets"] = {
        tid: {k: add(p["targets"].get(tid, {}).get(k) for p in parts) for k in ("hit", "destroyed")}
        for tid in tids
    }
    return out

# ─── Upsert ───────────────────────────────────────────────────────────────────

def _upsert(conn: sqlite3.Connection, table: str, pk_cols: list[str], data: dict) -> None:
    targets: dict[int, dict] = data.pop("targets", {})
    tids = list(targets.keys())
    ensure_columns(conn, table, tids)

    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    for tid in tids:
        data[f"hit_{tid}"]       = targets[tid]["hit"]
        data[f"destroyed_{tid}"] = targets[tid]["destroyed"]

    cols    = list(data.keys())
    ph      = ", ".join("?" * len(cols))
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pk_cols)
    pk      = ", ".join(pk_cols)

    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})"
        f" ON CONFLICT({pk}) DO UPDATE SET {updates}",
        list(data.values())
    )
    conn.commit()


def upsert_daily(conn: sqlite3.Connection, data: dict) -> None:
    date, hour = data["date"], data["hour"]
    del data["start_date"] # remove field
    _upsert(conn, "daily_stats", ["date", "hour"], data)
    log(f"  ✅ daily_stats:   {date} hour {hour:02d}")


def upsert_monthly(conn: sqlite3.Connection, data: dict) -> None:
    date = data["date"]
    del data["start_date"] # remove field
    _upsert(conn, "monthly_stats", ["date"], data)
    log(f"  ✅ monthly_stats: {date}")


def upsert_subdivision_monthly(conn: sqlite3.Connection, data: dict) -> None:
    date, sub = data["date"], data["subdivision"]
    del data["start_date"] # remove field
    _upsert(conn, "subdivision_monthly_stats", ["date", "subdivision"], data)
    log(f"  ✅ subdivision_monthly_stats: {sub} {date}")

# ─── Publish via git (deploy key) ─────────────────────────────────────────────

def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def publish() -> None:
    """
    Commit the DB in this repo if it changed, and push if the clone is ahead of origin.
    Pushes over SSH with the repo's deploy key (PUBLISH_SSH_KEY).
    Runs on every invocation, so a push that failed earlier (no network) is retried
    on the next run even when nothing new changed.
    """
    if key := os.environ.get("PUBLISH_SSH_KEY"):
        os.environ["GIT_SSH_COMMAND"] = (
            f'ssh -i "{Path(key).expanduser()}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new')
    # Bot identity — keeps the data commits off any personal GitHub profile
    name = os.environ.get("PUBLISH_GIT_NAME", "sbs-stats-data")
    email = os.environ.get("PUBLISH_GIT_EMAIL", "sbs-stats-data@bot.invalid")
    for var in ("AUTHOR", "COMMITTER"):
        os.environ.setdefault(f"GIT_{var}_NAME", name)
        os.environ.setdefault(f"GIT_{var}_EMAIL", email)

    db = str(Path(DB_PATH).resolve())
    ts = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    try:
        if git("status", "--porcelain", "--", db):
            git("add", "--", db)
            # DB only, never other local edits
            git("commit", "--quiet", "-m", f"chore: update sbs.db [skip ci]\n\nFetched at {ts}", "--", db)
            log("  ✅ Committed sbs.db")
        else:
            log("  ℹ️  sbs.db unchanged, nothing to commit.")
        if int(git("rev-list", "--count", "@{u}..HEAD")):
            git("pull", "--rebase", "--autostash", "--quiet")  # pick up code commits from elsewhere
            git("push", "--quiet")
            log("  ✅ Pushed to origin")
    except RuntimeError as e:
        log(f"  ⚠️  Publish failed, will retry next run: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    log("Starting fetch...")

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    # Daily
    log("Fetching daily...")
    p = parse_response(fetch_json(DAILY_URL))
    kyiv_dt   = to_kyiv(p["data_collected_at"])
    p["date"] = kyiv_dt.strftime("%Y-%m-%d")
    p["hour"] = kyiv_dt.hour
    upsert_daily(conn, p)

    # Yesterday (add the adjusted value to the last hour of the previous day)
    log("Fetching yesterday...")
    p = parse_response(fetch_json(YESTERDAY_URL))
    kyiv_dt = datetime.fromisoformat(p["start_date"])
    p["date"] = kyiv_dt.strftime("%Y-%m-%d")
    p["hour"] = 23
    upsert_daily(conn, p)

    # Monthly — refresh the last MONTHLY_REFRESH_MONTHS months,
    # older months are only fetched once (backfill)
    now = datetime.now(KYIV_TZ)
    y, m = divmod(now.year * 12 + now.month - 1 - (MONTHLY_REFRESH_MONTHS - 1), 12)
    refresh_from = f"{y}-{m + 1:02d}"

    log("Fetching monthly periods...")
    try:
        periods = get_monthly_periods(OVERALL_DIVISION_ID)
    except Exception as e:
        log(f"  ⚠️  Skipping monthly: {e}")
        periods = {}
    existing = {row[0][:7] for row in conn.execute("SELECT date FROM monthly_stats")}
    for month, url in sorted(periods.items()):
        if month < refresh_from and month in existing:
            continue
        log(f"Fetching monthly {month}...")
        try:
            pm = parse_response(fetch_json(url))
            pm["date"] = f"{month}-01"
            upsert_monthly(conn, pm)
        except Exception as e:
            log(f"  ⚠️  Skipping {month}: {e}")

    # Monthly per subdivision (slower request rate — many requests per run)
    for slug, division_ids in SUBDIVISIONS.items():
        log(f"Fetching subdivision {slug}...")
        try:
            periods = [get_monthly_periods(d, SUBDIVISION_REQUEST_DELAY) for d in division_ids]
        except Exception as e:
            log(f"  ⚠️  Skipping {slug}: {e}")
            continue
        existing = {
            row[0][:7] for row in conn.execute(
                "SELECT date FROM subdivision_monthly_stats WHERE subdivision = ?", (slug,)
            )
        }
        months = sorted(set().union(*periods))
        for month in months:
            if month < refresh_from and month in existing:
                continue
            try:
                parts = [
                    parse_response(fetch_json(p[month], delay=SUBDIVISION_REQUEST_DELAY))
                    for p in periods if month in p
                ]
                pm = sum_parsed(parts)
                pm["date"] = f"{month}-01"
                pm["subdivision"] = slug
                upsert_subdivision_monthly(conn, pm)
            except Exception as e:
                log(f"  ⚠️  Skipping {slug} {month}: {e}")

    # Checkpoint WAL and close — ensures all data is flushed to the DB file
    # before we commit it
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    if PUBLISH:
        log("Publishing...")
        publish()
    else:
        log("Publishing disabled (PUBLISH != 1), not committing.")

    log("Done.")


if __name__ == "__main__":
    main()
