#!/usr/bin/env python3
"""
SmartOLT Web Dashboard + Alert System
"""

import sqlite3
import threading
import time
import logging
import json
import secrets
import requests
from functools import wraps
from datetime import datetime, timedelta
from pathlib import Path
from flask import (Flask, render_template, jsonify, request,
                   session, redirect, url_for)
from werkzeug.security import generate_password_hash, check_password_hash

# ── Paths & constants ─────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
DB_PATH     = BASE / "smartolt.db"
LOG_FILE    = BASE / "app.log"
POLL_INTERVAL  = 300   # 5 minutes
DOWN_STATUSES  = {"Offline", "LOS", "Power fail"}

# Seeded as the first OLT on a fresh database (kept for backward compatibility).
DEFAULT_OLT_NAME   = "GiganticConnection"
DEFAULT_OLT_DOMAIN = "giganticconnection.smartolt.com"
DEFAULT_OLT_TOKEN  = "c2f5007b0fbe4585805b60d550e87e7c"

app = Flask(__name__)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS status_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            onu_uid     TEXT NOT NULL,
            onu_name    TEXT,
            zone_name   TEXT,
            olt_name    TEXT,
            port_info   TEXT,
            sn          TEXT,
            address     TEXT,
            old_status  TEXT,
            new_status  TEXT,
            signal      TEXT,
            signal_dbm  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts      ON status_events(ts);
        CREATE INDEX IF NOT EXISTS idx_events_status  ON status_events(new_status);
        CREATE INDEX IF NOT EXISTS idx_events_uid     ON status_events(onu_uid);

        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            total       INTEGER,
            online      INTEGER,
            offline     INTEGER,
            los         INTEGER,
            power_fail  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts);

        -- Per-server / per-OLT snapshot, written each poll cycle for filtering
        CREATE TABLE IF NOT EXISTS olt_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            server_id   INTEGER,
            server_name TEXT,
            olt_name    TEXT,
            total       INTEGER,
            online      INTEGER,
            offline     INTEGER,
            los         INTEGER,
            power_fail  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_oltsnap_ts  ON olt_snapshots(ts);
        CREATE INDEX IF NOT EXISTS idx_oltsnap_grp ON olt_snapshots(server_id, olt_name);

        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS olts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            domain     TEXT NOT NULL,
            api_key    TEXT NOT NULL,
            enabled    INTEGER DEFAULT 1,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS onu_state (
            uid     TEXT PRIMARY KEY,
            status  TEXT,
            signal  TEXT
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            onu_uid      TEXT,
            onu_name     TEXT,
            zone_name    TEXT,
            olt_name     TEXT,
            port_info    TEXT,
            sn           TEXT,
            address      TEXT,
            old_status   TEXT,
            new_status   TEXT,
            signal       TEXT,
            signal_dbm   TEXT,
            tg_delivered INTEGER DEFAULT 0,
            tg_error     TEXT,
            acknowledged INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_ts  ON alerts(ts);
        CREATE INDEX IF NOT EXISTS idx_alerts_ack ON alerts(acknowledged);
        """)

def _table_columns(db, table: str) -> set[str]:
    return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}

def migrate_db():
    """Add columns introduced after the original schema, for existing databases."""
    with get_db() as db:
        cols = _table_columns(db, "onu_state")
        for col, decl in (("olt_name", "TEXT"), ("server_id", "INTEGER"), ("server_name", "TEXT")):
            if col not in cols:
                db.execute(f"ALTER TABLE onu_state ADD COLUMN {col} {decl}")
        cols = _table_columns(db, "status_events")
        for col, decl in (("server_id", "INTEGER"), ("server_name", "TEXT")):
            if col not in cols:
                db.execute(f"ALTER TABLE status_events ADD COLUMN {col} {decl}")

# ── Config helpers ────────────────────────────────────────────────────────────
def cfg_get(key: str, default="") -> str:
    with get_db() as db:
        row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def cfg_set(key: str, value: str):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, value))

# ── First-run seeding ─────────────────────────────────────────────────────────
def seed_defaults():
    """On a fresh database, create the default OLT and the admin account."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        if db.execute("SELECT COUNT(*) AS c FROM olts").fetchone()["c"] == 0:
            db.execute(
                "INSERT INTO olts(name,domain,api_key,enabled,created_at) VALUES(?,?,?,1,?)",
                (DEFAULT_OLT_NAME, DEFAULT_OLT_DOMAIN, DEFAULT_OLT_TOKEN, now),
            )
            log.info("Seeded default OLT: %s", DEFAULT_OLT_DOMAIN)
        if db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            db.execute(
                "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                ("admin", generate_password_hash("admin"), now),
            )
            log.info("Created default admin account (username: admin / password: admin)")

def get_secret_key() -> str:
    key = cfg_get("secret_key")
    if not key:
        key = secrets.token_hex(32)
        cfg_set("secret_key", key)
    return key

# ── OLT helpers ───────────────────────────────────────────────────────────────
def olt_api_url(domain: str) -> str:
    """Normalise a stored domain into a SmartOLT API base URL."""
    d = domain.strip().rstrip("/")
    d = d.replace("https://", "").replace("http://", "")
    if d.endswith("/api"):
        d = d[:-4]
    return f"https://{d}/api"

def get_enabled_olts() -> list[dict]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM olts WHERE enabled=1 ORDER BY id").fetchall()
    return [dict(r) for r in rows]

def probe_olt(domain: str, api_key: str) -> tuple[bool, str]:
    """Validate a domain + API key against the SmartOLT API. Returns (ok, message)."""
    try:
        r = requests.get(
            f"{olt_api_url(domain)}/onu/get_all_onus_details",
            headers={"X-Token": api_key},
            timeout=30,
        )
    except Exception as exc:
        return False, str(exc)
    if r.status_code in (401, 403):
        return False, "Invalid API key (unauthorized)"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    try:
        data = r.json()
    except ValueError:
        return False, "Unexpected response (is the domain correct?)"
    if isinstance(data.get("status"), bool) and not data["status"]:
        return False, data.get("error", "API error")
    return True, f"{len(data.get('onus', []))} ONUs found"

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> tuple[bool, str]:
    token   = cfg_get("tg_token")
    chat_id = cfg_get("tg_chat_id")
    if not token or not chat_id:
        return False, "Telegram not configured"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            return True, "ok"
        return False, data.get("description", "unknown error")
    except Exception as exc:
        return False, str(exc)

def save_alert(onu: dict, new_status: str, old_status: str, delivered: bool, tg_error: str, ts: str):
    event_type = {
        "Offline":    "OFFLINE",
        "LOS":        "LOS",
        "Power fail": "POWER_FAIL",
        "Online":     "RECOVERY",
    }.get(new_status, new_status.upper())
    with get_db() as db:
        db.execute(
            "INSERT INTO alerts(ts,event_type,onu_uid,onu_name,zone_name,olt_name,port_info,"
            "sn,address,old_status,new_status,signal,signal_dbm,tg_delivered,tg_error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, event_type,
             onu.get("unique_external_id"), onu.get("name"), onu.get("zone_name"),
             onu.get("olt_name"),
             f"{onu.get('board')}/{onu.get('port')}/{onu.get('onu')}",
             onu.get("sn"), onu.get("address"),
             old_status, new_status,
             onu.get("signal"),
             onu.get("signal_1490") or onu.get("signal_1310"),
             1 if delivered else 0,
             tg_error or None),
        )

# ── SmartOLT API ──────────────────────────────────────────────────────────────
def fetch_onus() -> list[dict]:
    """Fetch ONUs from every enabled OLT and merge them into one list.

    Each ONU is tagged with `_olt_id` so its state key stays unique even if two
    SmartOLT accounts happen to reuse the same unique_external_id."""
    olts = get_enabled_olts()
    if not olts:
        log.warning("No enabled OLTs configured — nothing to poll")
        return []

    merged: list[dict] = []
    for olt in olts:
        try:
            r = requests.get(
                f"{olt_api_url(olt['domain'])}/onu/get_all_onus_details",
                headers={"X-Token": olt["api_key"]},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data.get("status"), bool) and not data["status"]:
                log.error("OLT '%s' API error: %s", olt["name"], data.get("error", "unknown"))
                continue
            onus = data.get("onus", [])
            for onu in onus:
                onu["_olt_id"]  = olt["id"]
                onu["_olt_src"] = olt["name"]
            merged.extend(onus)
            log.info("OLT '%s' returned %d ONUs", olt["name"], len(onus))
        except Exception as exc:
            log.error("OLT '%s' fetch failed: %s", olt["name"], exc)
    return merged

def onu_key(onu: dict) -> str:
    """Stable per-ONU state key, namespaced by source OLT to avoid collisions."""
    return f"{onu.get('_olt_id', 0)}:{onu.get('unique_external_id', '')}"

# ── Alert message builder ─────────────────────────────────────────────────────
STATUS_EMOJI = {"Offline": "🔴", "LOS": "📡", "Power fail": "⚡", "Online": "🟢"}
EVENT_HDR    = {
    "Offline":    "🔴 ONU OFFLINE",
    "LOS":        "📡 ONU LOSS OF SIGNAL",
    "Power fail": "⚡ ONU POWER FAILURE",
    "Online":     "🟢 ONU RECOVERED",
}

def build_msg(onu: dict, new_status: str, old_status: str) -> str:
    hdr     = EVENT_HDR.get(new_status, f"⚠️ {new_status}")
    status  = new_status
    signal  = onu.get("signal") or "N/A"
    sig_dbm = onu.get("signal_1490") or onu.get("signal_1310") or "N/A"
    return (
        f"{hdr}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"ONU    : <b>{onu.get('name','N/A')}</b>\n"
        f"Zone   : {onu.get('zone_name','N/A')}\n"
        f"OLT    : {onu.get('olt_name','N/A')}  "
        f"Port: {onu.get('board')}/{onu.get('port')}/{onu.get('onu')}\n"
        f"SN     : <code>{onu.get('sn','N/A')}</code>\n"
        f"Status : {STATUS_EMOJI.get(status,'❓')} {status}  (was: {old_status})\n"
        f"Signal : {signal}  ({sig_dbm} dBm)\n"
        f"Address: {onu.get('address','N/A')}\n"
        f"Time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

# ── Background poller ─────────────────────────────────────────────────────────
_prev_state: dict[str, str] = {}   # uid → status

def load_prev_state() -> dict[str, str]:
    with get_db() as db:
        rows = db.execute("SELECT uid, status FROM onu_state").fetchall()
    return {r["uid"]: r["status"] for r in rows}

def poll_once():
    global _prev_state
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    onus = fetch_onus()

    counts = {"total": len(onus), "online": 0, "offline": 0, "los": 0, "power_fail": 0}
    groups: dict[tuple, dict] = {}   # (server_id, server_name, olt_name) → counts
    events = []

    def _tally(bucket: dict, status: str):
        if status == "Online":
            bucket["online"] += 1
        elif status == "Offline":
            bucket["offline"] += 1
        elif status == "LOS":
            bucket["los"] += 1
        elif status == "Power fail":
            bucket["power_fail"] += 1

    for onu in onus:
        uid         = onu_key(onu)
        cur_status  = onu.get("status", "Unknown")
        cur_signal  = onu.get("signal")
        server_id   = onu.get("_olt_id")
        server_name = onu.get("_olt_src")
        olt_name    = onu.get("olt_name")

        # Global count
        _tally(counts, cur_status)

        # Per server/OLT count
        gkey = (server_id, server_name, olt_name)
        g = groups.setdefault(gkey, {"total": 0, "online": 0, "offline": 0, "los": 0, "power_fail": 0})
        g["total"] += 1
        _tally(g, cur_status)

        old_status = _prev_state.get(uid)
        if old_status is not None and old_status != cur_status:
            events.append({
                "ts": ts, "uid": uid,
                "name":    onu.get("name"),
                "zone":    onu.get("zone_name"),
                "olt":     olt_name,
                "port":    f"{onu.get('board')}/{onu.get('port')}/{onu.get('onu')}",
                "sn":      onu.get("sn"),
                "address": onu.get("address"),
                "old":     old_status,
                "new":     cur_status,
                "signal":  cur_signal,
                "sig_dbm": onu.get("signal_1490") or onu.get("signal_1310"),
                "server_id":   server_id,
                "server_name": server_name,
            })

            # Send alert and log it
            alert_worthy = (cur_status in DOWN_STATUSES) or \
                           (cur_status == "Online" and old_status in DOWN_STATUSES)
            if alert_worthy:
                ok, err = send_telegram(build_msg(onu, cur_status, old_status))
                save_alert(onu, cur_status, old_status, ok, err, ts)

        _prev_state[uid] = cur_status

    # Write to DB
    with get_db() as db:
        db.execute(
            "INSERT INTO snapshots(ts,total,online,offline,los,power_fail) VALUES(?,?,?,?,?,?)",
            (ts, counts["total"], counts["online"], counts["offline"], counts["los"], counts["power_fail"]),
        )
        for (sid, sname, oname), g in groups.items():
            db.execute(
                "INSERT INTO olt_snapshots(ts,server_id,server_name,olt_name,total,online,offline,los,power_fail) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (ts, sid, sname, oname, g["total"], g["online"], g["offline"], g["los"], g["power_fail"]),
            )
        for e in events:
            db.execute(
                "INSERT INTO status_events(ts,onu_uid,onu_name,zone_name,olt_name,port_info,sn,address,old_status,new_status,signal,signal_dbm,server_id,server_name) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e["ts"], e["uid"], e["name"], e["zone"], e["olt"], e["port"],
                 e["sn"], e["address"], e["old"], e["new"], e["signal"], e["sig_dbm"],
                 e["server_id"], e["server_name"]),
            )
        # Persist current state so it survives restarts
        for onu in onus:
            uid = onu_key(onu)
            if uid:
                db.execute(
                    "INSERT OR REPLACE INTO onu_state(uid, status, signal, olt_name, server_id, server_name) "
                    "VALUES(?,?,?,?,?,?)",
                    (uid, onu.get("status"), onu.get("signal"),
                     onu.get("olt_name"), onu.get("_olt_id"), onu.get("_olt_src")),
                )

    log.info("Poll done — total:%d online:%d offline:%d los:%d pf:%d events:%d",
             counts["total"], counts["online"], counts["offline"],
             counts["los"], counts["power_fail"], len(events))

def poller_thread():
    while True:
        try:
            poll_once()
        except Exception as exc:
            log.error("Poll error: %s", exc)
        time.sleep(POLL_INTERVAL)

# ── Authentication ────────────────────────────────────────────────────────────
PUBLIC_ENDPOINTS = {"login", "static"}

@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if session.get("user"):
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for("login", next=request.path))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        with get_db() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session["user"] = username
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt)
        return render_template("login.html", error="Invalid username or password")
    if session.get("user"):
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template("dashboard.html")

@app.route("/olts")
def olts_page():
    return render_template("olts.html")

@app.route("/report")
def report():
    return render_template("report.html")

@app.route("/settings")
def settings():
    tg_token   = cfg_get("tg_token")
    tg_chat_id = cfg_get("tg_chat_id")
    return render_template("settings.html", tg_token=tg_token, tg_chat_id=tg_chat_id)

# ── API: dashboard filters (domains + OLTs) ───────────────────────────────────
@app.route("/api/filters")
def api_filters():
    """Domains (SmartOLT servers) and the OLTs seen under each, for the dropdowns."""
    with get_db() as db:
        servers = [dict(r) for r in db.execute(
            "SELECT id, name FROM olts ORDER BY name"
        ).fetchall()]
        olts = [dict(r) for r in db.execute("""
            SELECT DISTINCT server_id, server_name, olt_name
            FROM olt_snapshots
            WHERE ts = (SELECT MAX(ts) FROM olt_snapshots)
              AND olt_name IS NOT NULL
            ORDER BY server_name, olt_name
        """).fetchall()]
    return jsonify({"servers": servers, "olts": olts})

def _dash_filters():
    """Read domain/olt query params (domain = server id)."""
    return request.args.get("domain", "").strip(), request.args.get("olt", "").strip()

# ── API: live stats ───────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    server, olt = _dash_filters()
    if not server and not olt:
        # Unfiltered: latest global snapshot
        with get_db() as db:
            row = db.execute("SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({"total": 0, "online": 0, "offline": 0, "los": 0, "power_fail": 0})

    # Filtered: sum the most recent per-OLT snapshot batch
    wheres = ["ts = (SELECT MAX(ts) FROM olt_snapshots)"]
    params: list = []
    if server:
        wheres.append("server_id = ?"); params.append(server)
    if olt:
        wheres.append("olt_name = ?"); params.append(olt)
    sql = ("SELECT COALESCE(SUM(total),0) AS total, COALESCE(SUM(online),0) AS online, "
           "COALESCE(SUM(offline),0) AS offline, COALESCE(SUM(los),0) AS los, "
           "COALESCE(SUM(power_fail),0) AS power_fail FROM olt_snapshots WHERE "
           + " AND ".join(wheres))
    with get_db() as db:
        row = db.execute(sql, params).fetchone()
    return jsonify(dict(row))

@app.route("/api/current-issues")
def api_current_issues():
    """Live current non-Online ONUs (always fresh from last poll state)."""
    server, olt = _dash_filters()
    extra, params = "", []
    if server:
        extra += " AND s.server_id = ?"; params.append(server)
    if olt:
        extra += " AND e.olt_name = ?"; params.append(olt)
    with get_db() as db:
        # Latest event per ONU, joined to live state for the server filter
        rows = db.execute(f"""
            SELECT e.onu_name, e.zone_name, e.olt_name, e.port_info, e.sn,
                   e.address, e.new_status, e.signal, e.signal_dbm, e.ts,
                   s.server_id, s.server_name
            FROM status_events e
            INNER JOIN (
                SELECT onu_uid, MAX(id) AS max_id FROM status_events GROUP BY onu_uid
            ) latest ON e.id = latest.max_id
            LEFT JOIN onu_state s ON s.uid = e.onu_uid
            WHERE e.new_status != 'Online' {extra}
            ORDER BY e.ts DESC
        """, params).fetchall()
    return jsonify([dict(r) for r in rows])

# ── API: chart data (last 24h / 7d) ──────────────────────────────────────────
@app.route("/api/chart")
def api_chart():
    period = request.args.get("period", "24h")
    server, olt = _dash_filters()
    if period == "7d":
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        group = "strftime('%Y-%m-%d %H:00', ts)"
    elif period == "30d":
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        group = "strftime('%Y-%m-%d', ts)"
    else:  # 24h
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        group = "strftime('%Y-%m-%d %H:00', ts)"

    with get_db() as db:
        if not server and not olt:
            # Unfiltered: global snapshots (full history)
            rows = db.execute(f"""
                SELECT {group} AS label,
                       AVG(online) AS online, AVG(offline) AS offline,
                       AVG(los) AS los, AVG(power_fail) AS power_fail
                FROM snapshots
                WHERE ts >= ?
                GROUP BY label
                ORDER BY label
            """, (since,)).fetchall()
        else:
            # Filtered: sum matching OLTs per instant, then average over the bucket
            wheres = ["ts >= ?"]; params = [since]
            if server:
                wheres.append("server_id = ?"); params.append(server)
            if olt:
                wheres.append("olt_name = ?"); params.append(olt)
            rows = db.execute(f"""
                SELECT {group} AS label,
                       AVG(online) AS online, AVG(offline) AS offline,
                       AVG(los) AS los, AVG(power_fail) AS power_fail
                FROM (
                    SELECT ts,
                           SUM(online) AS online, SUM(offline) AS offline,
                           SUM(los) AS los, SUM(power_fail) AS power_fail
                    FROM olt_snapshots
                    WHERE {' AND '.join(wheres)}
                    GROUP BY ts
                )
                GROUP BY label
                ORDER BY label
            """, params).fetchall()

    return jsonify([dict(r) for r in rows])

# ── API: report ───────────────────────────────────────────────────────────────
@app.route("/api/report")
def api_report():
    rng = request.args.get("range", "yesterday")
    now = datetime.now()

    if rng == "yesterday":
        d = now - timedelta(days=1)
        since = d.strftime("%Y-%m-%d 00:00:00")
        until = d.strftime("%Y-%m-%d 23:59:59")
    elif rng == "7d":
        since = (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        until = now.strftime("%Y-%m-%d %H:%M:%S")
    elif rng == "30d":
        since = (now - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
        until = now.strftime("%Y-%m-%d %H:%M:%S")
    elif rng == "1y":
        since = (now - timedelta(days=365)).strftime("%Y-%m-%d 00:00:00")
        until = now.strftime("%Y-%m-%d %H:%M:%S")
    else:
        since = "2000-01-01"
        until = now.strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as db:
        events = db.execute("""
            SELECT ts, onu_name, zone_name, olt_name, port_info, sn,
                   address, old_status, new_status, signal, signal_dbm
            FROM status_events
            WHERE ts BETWEEN ? AND ?
              AND new_status IN ('LOS', 'Power fail', 'Offline')
            ORDER BY ts DESC
        """, (since, until)).fetchall()

        summary = db.execute("""
            SELECT new_status, COUNT(*) as cnt, COUNT(DISTINCT onu_uid) as unique_onus
            FROM status_events
            WHERE ts BETWEEN ? AND ?
              AND new_status IN ('LOS', 'Power fail', 'Offline')
            GROUP BY new_status
        """, (since, until)).fetchall()

        # Daily breakdown for chart
        daily = db.execute("""
            SELECT strftime('%Y-%m-%d', ts) AS day,
                   SUM(CASE WHEN new_status='LOS'        THEN 1 ELSE 0 END) AS los,
                   SUM(CASE WHEN new_status='Power fail' THEN 1 ELSE 0 END) AS power_fail,
                   SUM(CASE WHEN new_status='Offline'    THEN 1 ELSE 0 END) AS offline
            FROM status_events
            WHERE ts BETWEEN ? AND ?
              AND new_status IN ('LOS', 'Power fail', 'Offline')
            GROUP BY day
            ORDER BY day
        """, (since, until)).fetchall()

    return jsonify({
        "events":  [dict(r) for r in events],
        "summary": [dict(r) for r in summary],
        "daily":   [dict(r) for r in daily],
        "since":   since,
        "until":   until,
    })

# ── Alerts page & API ─────────────────────────────────────────────────────────
@app.route("/alerts")
def alerts_page():
    return render_template("alerts.html")

@app.route("/api/alerts")
def api_alerts():
    event_type = request.args.get("type", "")        # OFFLINE|LOS|POWER_FAIL|RECOVERY
    delivered  = request.args.get("delivered", "")   # 1|0
    acked      = request.args.get("acked", "")       # 1|0
    limit      = int(request.args.get("limit", 200))

    wheres, params = [], []
    if event_type:
        wheres.append("event_type = ?"); params.append(event_type)
    if delivered != "":
        wheres.append("tg_delivered = ?"); params.append(int(delivered))
    if acked != "":
        wheres.append("acknowledged = ?"); params.append(int(acked))

    where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    params.append(limit)

    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM alerts {where_sql} ORDER BY ts DESC LIMIT ?", params
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts/test-sound", methods=["POST"])
def api_alerts_test_sound():
    """Insert a fake Power Fail and LOS alert so the browser poller fires sounds."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        db.execute(
            "INSERT INTO alerts(ts,event_type,onu_uid,onu_name,zone_name,olt_name,"
            "port_info,sn,address,old_status,new_status,signal,signal_dbm,tg_delivered,tg_error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "POWER_FAIL", "TEST-001", "TEST-ONU-PF", "Test Zone",
             "GCN_NET_OLT01", "1/0/1", "TESTPF001", "Test Address",
             "Online", "Power fail", "Warning", "-24.5 dBm", 0, "test alert"),
        )
        db.execute(
            "INSERT INTO alerts(ts,event_type,onu_uid,onu_name,zone_name,olt_name,"
            "port_info,sn,address,old_status,new_status,signal,signal_dbm,tg_delivered,tg_error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, "LOS", "TEST-002", "TEST-ONU-LOS", "Test Zone",
             "GCN_NET_OLT01", "1/0/2", "TESTLOS002", "Test Address",
             "Online", "LOS", "Critical", "-30.1 dBm", 0, "test alert"),
        )
    return jsonify({"ok": True, "ts": ts})

@app.route("/api/alerts/since/<int:since_id>")
def api_alerts_since(since_id):
    """Return all alerts with id > since_id (for sound/popup polling)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, ts, event_type, onu_name, zone_name, new_status, signal_dbm "
            "FROM alerts WHERE id > ? ORDER BY id ASC",
            (since_id,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alerts/unread-count")
def api_alerts_unread():
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS cnt FROM alerts WHERE acknowledged=0"
        ).fetchone()
    return jsonify({"count": row["cnt"] if row else 0})

@app.route("/api/alerts/<int:alert_id>/ack", methods=["POST"])
def api_alert_ack(alert_id):
    with get_db() as db:
        db.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))
    return jsonify({"ok": True})

@app.route("/api/alerts/ack-all", methods=["POST"])
def api_alert_ack_all():
    with get_db() as db:
        db.execute("UPDATE alerts SET acknowledged=1 WHERE acknowledged=0")
    return jsonify({"ok": True})

# ── API: settings ─────────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.json or {}
    token   = data.get("tg_token", "").strip()
    chat_id = data.get("tg_chat_id", "").strip()
    if token:
        cfg_set("tg_token", token)
    if chat_id:
        cfg_set("tg_chat_id", chat_id)
    return jsonify({"ok": True})

# ── API: OLT (SmartOLT server) management ─────────────────────────────────────
@app.route("/api/olts")
def api_olts_list():
    with get_db() as db:
        rows = db.execute("SELECT * FROM olts ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        key = d.get("api_key") or ""
        d["api_key_masked"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else "••••"
        del d["api_key"]
        out.append(d)
    return jsonify(out)

@app.route("/api/olts", methods=["POST"])
def api_olts_add():
    data    = request.json or {}
    name    = (data.get("name") or "").strip()
    domain  = (data.get("domain") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    if not name or not domain or not api_key:
        return jsonify({"ok": False, "error": "Name, domain and API key are all required"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO olts(name,domain,api_key,enabled,created_at) VALUES(?,?,?,1,?)",
            (name, domain, api_key, now),
        )
        new_id = cur.lastrowid
    log.info("Added OLT '%s' (%s)", name, domain)
    return jsonify({"ok": True, "id": new_id})

@app.route("/api/olts/<int:olt_id>", methods=["DELETE"])
def api_olts_delete(olt_id):
    with get_db() as db:
        db.execute("DELETE FROM olts WHERE id=?", (olt_id,))
    log.info("Deleted OLT id=%d", olt_id)
    return jsonify({"ok": True})

@app.route("/api/olts/<int:olt_id>/toggle", methods=["POST"])
def api_olts_toggle(olt_id):
    with get_db() as db:
        row = db.execute("SELECT enabled FROM olts WHERE id=?", (olt_id,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "Not found"}), 404
        new_val = 0 if row["enabled"] else 1
        db.execute("UPDATE olts SET enabled=? WHERE id=?", (new_val, olt_id))
    return jsonify({"ok": True, "enabled": new_val})

@app.route("/api/olts/test", methods=["POST"])
def api_olts_test():
    data    = request.json or {}
    domain  = (data.get("domain") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    if not domain or not api_key:
        # Allow testing an already-saved OLT by id
        olt_id = data.get("id")
        if olt_id:
            with get_db() as db:
                row = db.execute("SELECT domain, api_key FROM olts WHERE id=?", (olt_id,)).fetchone()
            if row:
                domain, api_key = row["domain"], row["api_key"]
    if not domain or not api_key:
        return jsonify({"ok": False, "error": "Domain and API key required"})
    ok, msg = probe_olt(domain, api_key)
    return jsonify({"ok": ok, "message": msg if ok else None, "error": None if ok else msg})

# ── API: account / password ───────────────────────────────────────────────────
@app.route("/api/account/password", methods=["POST"])
def api_change_password():
    data     = request.json or {}
    current  = data.get("current") or ""
    new_pw   = data.get("new") or ""
    if len(new_pw) < 4:
        return jsonify({"ok": False, "error": "New password must be at least 4 characters"}), 400
    username = session.get("user")
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], current):
            return jsonify({"ok": False, "error": "Current password is incorrect"}), 403
        db.execute("UPDATE users SET password_hash=? WHERE username=?",
                   (generate_password_hash(new_pw), username))
    log.info("Password changed for user '%s'", username)
    return jsonify({"ok": True})

@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    data = request.json or {}
    token   = data.get("tg_token", "").strip() or cfg_get("tg_token")
    chat_id = data.get("tg_chat_id", "").strip() or cfg_get("tg_chat_id")
    if not token or not chat_id:
        return jsonify({"ok": False, "error": "Token or Chat ID missing"})
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id,
                  "text": "✅ <b>SmartOLT Alert System</b>\nTelegram connected successfully!",
                  "parse_mode": "HTML"},
            timeout=10,
        )
        d = r.json()
        return jsonify({"ok": d.get("ok", False), "error": d.get("description", "")})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})

# ── Startup ───────────────────────────────────────────────────────────────────
def start():
    init_db()
    migrate_db()
    seed_defaults()
    app.secret_key = get_secret_key()
    global _prev_state
    _prev_state = load_prev_state()
    log.info("Restored %d ONU states from DB", len(_prev_state))
    t = threading.Thread(target=poller_thread, daemon=True)
    t.start()
    log.info("Poller thread started")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    start()
