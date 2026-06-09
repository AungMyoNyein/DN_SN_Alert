#!/usr/bin/env python3
"""
SmartOLT Alert System — standalone script (NOT used by the service).
The service runs app.py which includes all polling and alerting logic.
This file is kept as a reference only. Do NOT run it alongside app.py
or every alert will be sent twice.
"""

import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SMARTOLT_API   = "https://giganticconnection.smartolt.com/api"
SMARTOLT_TOKEN = "c2f5007b0fbe4585805b60d550e87e7c"
TELEGRAM_TOKEN = ""   # set via app.py Settings page
TELEGRAM_CHAT  = ""   # set via app.py Settings page
POLL_INTERVAL  = 300   # seconds (5 min)
STATE_FILE     = Path(__file__).parent / "state.json"
LOG_FILE       = Path(__file__).parent / "alert.log"

# Statuses that are considered "down"
DOWN_STATUSES  = {"Offline", "LOS", "Power fail"}
# OLT considered down when this % of its ONUs are not Online
OLT_DOWN_PCT   = 90.0

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error("Telegram error: %s", exc)
        return False


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
        raise RuntimeError(f"API returned error: {data.get('error')}")
    return data["onus"]


# ── State persistence ─────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"onu": {}, "olt": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Message builders ──────────────────────────────────────────────────────────
STATUS_EMOJI = {
    "Offline":    "🔴",
    "LOS":        "📡",
    "Power fail": "⚡",
    "Online":     "🟢",
}
EVENT_HEADER = {
    "OFFLINE":         "🔴 ONU OFFLINE",
    "LOS":             "📡 ONU LOSS OF SIGNAL",
    "POWER_FAIL":      "⚡ ONU POWER FAILURE",
    "RECOVERY":        "🟢 ONU RECOVERED",
    "SIGNAL_CRITICAL": "⚠️ SIGNAL CRITICAL",
    "SIGNAL_WARNING":  "🟡 SIGNAL WARNING",
    "OLT_DOWN":        "🚨 OLT DOWN",
    "OLT_RECOVERED":   "🟢 OLT RECOVERED",
}


def onu_msg(onu: dict, event: str) -> str:
    status   = onu.get("status", "?")
    signal   = onu.get("signal") or "N/A"
    sig_dbm  = onu.get("signal_1490") or onu.get("signal_1310") or "N/A"
    changed  = onu.get("last_status_change", "N/A")
    return (
        f"{EVENT_HEADER[event]}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"ONU  : <b>{onu.get('name', 'N/A')}</b>\n"
        f"Zone : {onu.get('zone_name', 'N/A')}\n"
        f"OLT  : {onu.get('olt_name', 'N/A')}  "
        f"Port: {onu.get('board')}/{onu.get('port')}/{onu.get('onu')}\n"
        f"SN   : <code>{onu.get('sn', 'N/A')}</code>\n"
        f"Status : {STATUS_EMOJI.get(status, '❓')} {status}\n"
        f"Signal : {signal}  ({sig_dbm} dBm)\n"
        f"Address: {onu.get('address', 'N/A')}\n"
        f"Changed: {changed}"
    )


def olt_msg(olt_name: str, event: str, offline: int, total: int) -> str:
    pct = offline / total * 100 if total else 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if event == "OLT_DOWN":
        return (
            f"🚨 <b>OLT DOWN</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"OLT  : <b>{olt_name}</b>\n"
            f"Down : {offline}/{total} ONUs ({pct:.1f}%)\n"
            f"Time : {now}"
        )
    return (
        f"🟢 <b>OLT RECOVERED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"OLT    : <b>{olt_name}</b>\n"
        f"Online : {total - offline}/{total} ONUs\n"
        f"Time   : {now}"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────
def run():
    log.info("SmartOLT Alert System starting — poll interval %ds", POLL_INTERVAL)
    send_telegram(
        "🚀 <b>SmartOLT Alert System started</b>\n"
        f"Polling every {POLL_INTERVAL // 60} minutes.\n"
        f"Monitoring: Offline · LOS · Power fail · Recovery · Signal · OLT down"
    )

    state = load_state()
    prev_onu = state.get("onu", {})   # uid → {status, signal}
    prev_olt = state.get("olt", {})   # olt_name → offline_pct

    while True:
        try:
            log.info("Fetching ONU data …")
            onus = fetch_onus()
            log.info("Fetched %d ONUs", len(onus))

            cur_onu: dict[str, dict] = {}
            olt_buckets: dict[str, dict] = {}  # olt_name → {total, offline}
            sent = 0

            for onu in onus:
                uid = onu.get("unique_external_id")
                if not uid:
                    continue

                cur_status = onu.get("status")
                cur_signal = onu.get("signal")
                cur_onu[uid] = {"status": cur_status, "signal": cur_signal}

                # OLT bucket
                olt = onu.get("olt_name", "Unknown")
                if olt not in olt_buckets:
                    olt_buckets[olt] = {"total": 0, "offline": 0}
                olt_buckets[olt]["total"] += 1
                if cur_status != "Online":
                    olt_buckets[olt]["offline"] += 1

                # Skip first-run (no previous state)
                prev = prev_onu.get(uid)
                if not prev:
                    continue

                prev_status = prev.get("status")
                prev_signal = prev.get("signal")

                # ── Status change alerts ──────────────────────────────────
                if cur_status != prev_status:
                    if cur_status == "Offline":
                        send_telegram(onu_msg(onu, "OFFLINE")); sent += 1
                    elif cur_status == "LOS":
                        send_telegram(onu_msg(onu, "LOS")); sent += 1
                    elif cur_status == "Power fail":
                        send_telegram(onu_msg(onu, "POWER_FAIL")); sent += 1
                    elif cur_status == "Online" and prev_status in DOWN_STATUSES:
                        send_telegram(onu_msg(onu, "RECOVERY")); sent += 1

                # ── Signal degradation (Online ONUs only) ─────────────────
                if cur_status == "Online" and cur_signal != prev_signal:
                    if cur_signal == "Critical":
                        send_telegram(onu_msg(onu, "SIGNAL_CRITICAL")); sent += 1
                    elif cur_signal == "Warning" and prev_signal in ("Very good", None):
                        send_telegram(onu_msg(onu, "SIGNAL_WARNING")); sent += 1

            # ── OLT health check ──────────────────────────────────────────
            for olt_name, bkt in olt_buckets.items():
                pct = bkt["offline"] / bkt["total"] * 100 if bkt["total"] else 0
                prev_pct = prev_olt.get(olt_name, {}).get("pct", 0)

                if pct >= OLT_DOWN_PCT and prev_pct < OLT_DOWN_PCT:
                    send_telegram(olt_msg(olt_name, "OLT_DOWN", bkt["offline"], bkt["total"]))
                    sent += 1
                elif pct < OLT_DOWN_PCT and prev_pct >= OLT_DOWN_PCT:
                    send_telegram(olt_msg(olt_name, "OLT_RECOVERED", bkt["offline"], bkt["total"]))
                    sent += 1

                prev_olt[olt_name] = {"pct": pct}

            prev_onu = cur_onu
            save_state({"onu": cur_onu, "olt": prev_olt})
            log.info("Cycle done — alerts sent: %d", sent)

        except requests.exceptions.RequestException as exc:
            log.error("API request failed: %s", exc)
            send_telegram(f"⚠️ <b>SmartOLT API unreachable</b>\n{exc}")
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
