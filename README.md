# GCN Alert — SmartOLT Monitor

Web dashboard that polls SmartOLT every 5 minutes, detects ONU status changes, sends Telegram alerts, and stores history in a local SQLite database.

---

## Requirements

- Python 3.10 or newer
- pip packages: listed in [`requirements.txt`](requirements.txt) (`flask`, `requests`)
- A Telegram bot token and group/channel chat ID (see [Telegram setup](#telegram-setup))

---

## Installation

### 1. Clone / copy the project

```bash
cp -r DN_SN_Alert /root/DN_SN_Alert
cd /root/DN_SN_Alert
```

Or if using git:

```bash
git clone https://github.com/AungMyoNyein/DN_SN_Alert /root/DN_SN_Alert
cd /root/DN_SN_Alert
```

### 2. Install Python dependencies

```bash
pip3 install -r requirements.txt
```

### 3. Run the app (manual / test)

```bash
python3 app.py
```

The dashboard will be available at `http://<server-ip>:5000`.

On first start the SQLite database (`smartolt.db`) is created automatically and the poller runs one cycle immediately.

---

## Login

The dashboard is protected by a login. On first run a default account is created:

| Username | Password |
|----------|----------|
| `admin`  | `admin`  |

**Change the password right after first login** — go to **Settings → Change Login Password**.

---

## SmartOLT Servers (domains & API keys)

The app polls one or more SmartOLT servers. Manage them under **SmartOLT Servers** in the sidebar.

1. Open **SmartOLT Servers**
2. Enter a **Name**, the **Domain** (e.g. `yourcompany.smartolt.com`), and the **API Key** (`X-Token`)
3. Click **Test Connection** to verify, then **Save Server**

Each poll cycle fetches ONUs from **every enabled** server and merges the results. Use **Disable** to keep a server but skip it, or **Delete** to remove it. The API key for the SmartOLT API is found in your SmartOLT panel under **Settings → API**.

> On upgrade, the previously hard-coded OLT is seeded automatically as the first server.

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

The service file expects the app at `/root/DN_SN_Alert`. If you cloned elsewhere, edit the service file to match:

```bash
nano /etc/systemd/system/smartolt-alert.service
```

Update `ExecStart` and `WorkingDirectory` to your actual path.

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
