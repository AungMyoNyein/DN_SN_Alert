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

> **Important:** install the packages for the **same** Python interpreter the systemd
> service uses (`/usr/bin/python3`). Using a different `pip` is the most common cause of
> a `ModuleNotFoundError: No module named 'flask'` crash loop after deploying.

```bash
sudo /usr/bin/python3 -m pip install -r requirements.txt
```

If you see `error: externally-managed-environment` (Ubuntu 23.04+ / Debian 12+), use one
of these instead:

```bash
# Option A — install via apt (cleanest)
sudo apt update && sudo apt install -y python3-flask python3-requests

# Option B — force pip to install system-wide
sudo /usr/bin/python3 -m pip install --break-system-packages -r requirements.txt
```

Verify the install against the interpreter the service runs:

```bash
/usr/bin/python3 -c "import flask, werkzeug, requests; print('flask', flask.__version__)"
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

## Updating to a New Version

Run these on the server, in the directory the **service** uses (`/root/DN_SN_Alert` by default):

```bash
cd /root/DN_SN_Alert
cp smartolt.db smartolt.db.bak          # back up the database first
git pull origin master                  # get the new code
sudo /usr/bin/python3 -m pip install -r requirements.txt   # install any new deps
sudo systemctl restart smartolt-alert
sudo systemctl status smartolt-alert --no-pager
```

New database tables are created automatically on startup — no manual migration needed.

> If you edit the code in a different folder (e.g. your home directory), remember the
> service still loads from the path in `ExecStart`. Pull in that path, or update the
> service file to point at your folder.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'flask'` / service crash-loops**
Flask is not installed for the interpreter in `ExecStart` (`/usr/bin/python3`). Install it
for that exact interpreter — see [Install Python dependencies](#2-install-python-dependencies).

**`Address already in use` / `Port 5000 is in use`**
Another process (often a leftover manual `python3 app.py`) is holding the port:

```bash
ss -ltnp | grep :5000
sudo pkill -f "python3 /root/DN_SN_Alert/app.py"
sudo systemctl restart smartolt-alert
```

**See the real error behind a `status=1/FAILURE`**

```bash
sudo journalctl -u smartolt-alert -n 40 --no-pager
```

**Forgot the dashboard password** — reset it from the server:

```bash
/usr/bin/python3 - <<'PY'
import sqlite3
from werkzeug.security import generate_password_hash
db = sqlite3.connect("/root/DN_SN_Alert/smartolt.db")
db.execute("UPDATE users SET password_hash=? WHERE username='admin'",
           (generate_password_hash("admin"),))
db.commit(); print("admin password reset to: admin")
PY
```

---

## Accessing the Dashboard

| Page             | URL                          | Description                          |
|------------------|------------------------------|--------------------------------------|
| Login            | `http://<ip>:5000/login`     | Sign in (default **admin / admin**)  |
| Dashboard        | `http://<ip>:5000/`          | Live stats, trend chart, issues      |
| Alerts           | `http://<ip>:5000/alerts`    | Full alert log with ack support      |
| Reports          | `http://<ip>:5000/report`    | Historical breakdown by date range   |
| SmartOLT Servers | `http://<ip>:5000/olts`      | Add/test/enable/delete OLT domains   |
| Settings         | `http://<ip>:5000/settings`  | Telegram config + change password    |

---

## File Overview

```
DN_SN_Alert/
├── app.py                   # Main app: Flask dashboard + background poller
├── alert.py                 # Standalone reference script (NOT used by service)
├── smartolt-alert.service   # systemd service unit file
├── smartolt.db              # SQLite database (auto-created on first run)
├── app.log                  # Application log
├── requirements.txt         # Python dependencies
├── templates/
│   ├── base.html            # Shared layout, sidebar, sound alerts
│   ├── login.html           # Login page
│   ├── dashboard.html       # Live stats and current issues
│   ├── alerts.html          # Alert log with filtering
│   ├── report.html          # Historical reports
│   ├── olts.html            # SmartOLT server (domain + API key) management
│   └── settings.html        # Telegram config + change password
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
