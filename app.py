#!/usr/bin/env python3
"""
SmartOLT Web Dashboard + Alert System
"""

import sqlite3
import threading
import time
import logging
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, jsonify, request

# ── Paths & constants ─────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
DB_PATH     = BASE / "smartolt.db"
LOG_FILE    = BASE / "app.log"
SMARTOLT_API   = "https://giganticconnection.smartolt.com/api"
SMARTOLT_TOKEN = "c2f5007b0fbe4585805b60d550e87e7c"
POLL_INTERVAL  = 300   # 5 minutes
DOWN_STATUSES  = {"Offline", "LOS", "Power fail"}

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

        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
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

# ── Config helpers ────────────────────────────────────────────────────────────
def cfg_get(key: str, default="") -> str:
    with get_db() as db:
        row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def cfg_set(key: str, value: str):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, value))

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
    r = requests.get(
        f"{SMARTOLT_API}/onu/get_all_onus_details",
        headers={"X-Token": SMARTOLT_TOKEN},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data.get("status"), bool) and not data["status"]:
        raise RuntimeError(data.get("error", "API error"))
    return data["onus"]

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
    events = []

    for onu in onus:
        uid        = onu.get("unique_external_id", "")
        cur_status = onu.get("status", "Unknown")
        cur_signal = onu.get("signal")

        # Count
        if cur_status == "Online":
            counts["online"] += 1
        elif cur_status == "Offline":
            counts["offline"] += 1
        elif cur_status == "LOS":
            counts["los"] += 1
        elif cur_status == "Power fail":
            counts["power_fail"] += 1

        old_status = _prev_state.get(uid)
        if old_status is not None and old_status != cur_status:
            events.append({
                "ts": ts, "uid": uid,
                "name":    onu.get("name"),
                "zone":    onu.get("zone_name"),
                "olt":     onu.get("olt_name"),
                "port":    f"{onu.get('board')}/{onu.get('port')}/{onu.get('onu')}",
                "sn":      onu.get("sn"),
                "address": onu.get("address"),
                "old":     old_status,
                "new":     cur_status,
                "signal":  cur_signal,
                "sig_dbm": onu.get("signal_1490") or onu.get("signal_1310"),
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
        for e in events:
            db.execute(
                "INSERT INTO status_events(ts,onu_uid,onu_name,zone_name,olt_name,port_info,sn,address,old_status,new_status,signal,signal_dbm) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (e["ts"], e["uid"], e["name"], e["zone"], e["olt"], e["port"],
                 e["sn"], e["address"], e["old"], e["new"], e["signal"], e["sig_dbm"]),
            )
        # Persist current state so it survives restarts
        for onu in onus:
            uid = onu.get("unique_external_id", "")
            if uid:
                db.execute(
                    "INSERT OR REPLACE INTO onu_state(uid, status, signal) VALUES(?,?,?)",
                    (uid, onu.get("status"), onu.get("signal")),
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

# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template("dashboard.html")

@app.route("/report")
def report():
    return render_template("report.html")

@app.route("/settings")
def settings():
    tg_token   = cfg_get("tg_token")
    tg_chat_id = cfg_get("tg_chat_id")
    return render_template("settings.html", tg_token=tg_token, tg_chat_id=tg_chat_id)

# ── API: live stats ───────────────────────────────────────────────────────────
@app.route("/api/stats")
def api_stats():
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    if row:
        return jsonify(dict(row))
    return jsonify({"total": 0, "online": 0, "offline": 0, "los": 0, "power_fail": 0})

@app.route("/api/current-issues")
def api_current_issues():
    """Live current non-Online ONUs (always fresh from last poll state)."""
    with get_db() as db:
        # Get last known status for every ONU from status_events
        rows = db.execute("""
            SELECT e.onu_name, e.zone_name, e.olt_name, e.port_info, e.sn,
                   e.address, e.new_status, e.signal, e.signal_dbm, e.ts
            FROM status_events e
            INNER JOIN (
                SELECT onu_uid, MAX(id) AS max_id FROM status_events GROUP BY onu_uid
            ) latest ON e.id = latest.max_id
            WHERE e.new_status != 'Online'
            ORDER BY e.ts DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])

# ── API: chart data (last 24h / 7d) ──────────────────────────────────────────
@app.route("/api/chart")
def api_chart():
    period = request.args.get("period", "24h")
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
        rows = db.execute(f"""
            SELECT {group} AS label,
                   AVG(online) AS online, AVG(offline) AS offline,
                   AVG(los) AS los, AVG(power_fail) AS power_fail
            FROM snapshots
            WHERE ts >= ?
            GROUP BY label
            ORDER BY label
        """, (since,)).fetchall()

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
    global _prev_state
    _prev_state = load_prev_state()
    log.info("Restored %d ONU states from DB", len(_prev_state))
    t = threading.Thread(target=poller_thread, daemon=True)
    t.start()
    log.info("Poller thread started")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    start()
