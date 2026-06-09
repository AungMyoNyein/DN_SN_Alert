# GCN Alert — SmartOLT Monitor

Web dashboard that polls SmartOLT every 5 minutes, detects ONU status changes, sends Telegram alerts, and stores history in a local SQLite database.

---

## Requirements

- Python 3.10 or newer
- pip packages: `flask`, `requests`
- A Telegram bot token and group/channel chat ID (see [Telegram setup](#telegram-setup))

---

## Installation

### 1. Clone / copy the project

```bash
cp -r DN_SN_Alert /opt/DN_SN_Alert
cd /opt/DN_SN_Alert
```

Or if using git:

```bash
git clone https://github.com/AungMyoNyein/DN_SN_Alert /opt/DN_SN_Alert
cd /opt/DN_SN_Alert
```

### 2. Install Python dependencies

```bash
pip3 install flask requests
```

### 3. Run the app (manual / test)

```bash
python3 app.py
```

The dashboard will be available at `http://<server-ip>:5000`.

On first start the SQLite database (`smartolt.db`) is created automatically and the poller runs one cycle immediately.

---

## Telegram Setup

You need a Telegram bot and a group or channel to receive alerts.

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts — copy the **Bot Token** it gives you
3. Add the bot to your group or channel
4. Send a message in that group, then open:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
5. Find the `"id"` inside `"chat"` — that is your **Chat ID** (groups have a negative number)

Once the app is running, go to **Settings** in the dashboard sidebar, paste the token and chat ID, and click **Save** then **Send Test Message** to verify.

---

## Run as a systemd Service (Linux)

This keeps the app running after reboots and auto-restarts on crashes.

### 1. Copy the service file

```bash
cp smartolt-alert.service /etc/systemd/system/smartolt-alert.service
```

If you installed to a path other than `/root/DN_SN_Alert`, edit the service file first:

```bash
nano /etc/systemd/system/smartolt-alert.service
```

Update `ExecStart` and `WorkingDirectory` to match your install path.

### 2. Enable and start the service

```bash
systemctl daemon-reload
systemctl enable smartolt-alert
systemctl start smartolt-alert
```

### 3. Check status

```bash
systemctl status smartolt-alert
```

### 4. View live logs

```bash
journalctl -u smartolt-alert -f
```

---

## Accessing the Dashboard

| Page       | URL                          | Description                        |
|------------|------------------------------|------------------------------------|
| Dashboard  | `http://<ip>:5000/`          | Live stats, trend chart, issues    |
| Alerts     | `http://<ip>:5000/alerts`    | Full alert log with ack support    |
| Reports    | `http://<ip>:5000/report`    | Historical breakdown by date range |
| Settings   | `http://<ip>:5000/settings`  | Telegram token and chat ID         |

---

## File Overview

```
DN_SN_Alert/
├── app.py                   # Main app: Flask dashboard + background poller
├── alert.py                 # Standalone reference script (NOT used by service)
├── smartolt-alert.service   # systemd service unit file
├── smartolt.db              # SQLite database (auto-created on first run)
├── app.log                  # Application log
├── templates/
│   ├── base.html            # Shared layout, sidebar, sound alerts
│   ├── dashboard.html       # Live stats and current issues
│   ├── alerts.html          # Alert log with filtering
│   ├── report.html          # Historical reports
│   └── settings.html        # Telegram configuration
└── static/                  # Static assets
```

---

## Configuration Reference

All configuration is stored in the `config` table in `smartolt.db` and managed through the Settings page.

| Key          | Description                  |
|--------------|------------------------------|
| `tg_token`   | Telegram bot token           |
| `tg_chat_id` | Telegram group/channel ID    |

The SmartOLT API token and endpoint are set at the top of `app.py`:

```python
SMARTOLT_API   = "https://giganticconnection.smartolt.com/api"
SMARTOLT_TOKEN = "<your-smartolt-token>"
POLL_INTERVAL  = 300   # seconds (5 minutes)
```

---

## Stopping / Uninstalling the Service

```bash
systemctl stop smartolt-alert
systemctl disable smartolt-alert
rm /etc/systemd/system/smartolt-alert.service
systemctl daemon-reload
```
