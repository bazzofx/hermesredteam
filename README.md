# Morpheus — Red Team Command Dashboard

A lightweight mission control dashboard for the Morpheus red team pipeline. Monitors agent status, task board, and live events via SSE.

## Stack

- **Backend:** Python 3 `ThreadingHTTPServer` (no external dependencies)
- **Frontend:** Single HTML file with Tailwind CSS (CDN)
- **Database:** SQLite (`board.db` for tasks, reads from `~/.hermes/` for agent state)
- **Port:** `127.0.0.1:51763`

## Quick Start

```bash
# Clone
git clone https://github.com/<your-username>/morpheus-dashboard.git
cd morpheus-dashboard

# Start
./start.sh
```

The dashboard will be available at **http://127.0.0.1:51763**.

## Manual Start

If you prefer to run without the launcher script:

```bash
python3 server.py
```

Logs are written to `server.log`.

## Stopping

```bash
kill $(cat .server.pid)
```

Or just re-run `./start.sh` — it handles stopping any existing instance first.

## API Endpoints

| Endpoint          | Method | Description                    |
|-------------------|--------|--------------------------------|
| `/`               | GET    | Dashboard UI (index.html)      |
| `/api/snapshot`   | GET    | Full system state (JSON)       |
| `/events`         | GET    | Server-Sent Events stream      |
| `/api/board`      | GET    | List all tasks                 |
| `/api/board`      | POST   | Create a new task              |
| `/api/board/<id>` | PUT    | Update a task                  |
| `/api/board/<id>` | DELETE| Delete a task                  |

## Deployment (Production)

For a persistent deployment on a VPS:

```bash
# Clone to your server
git clone https://github.com/<your-username>/morpheus-dashboard.git /opt/morpheus-dashboard
cd /opt/morpheus-dashboard

# Start in background
nohup python3 server.py > server.log 2>&1 &
echo $! > .server.pid
```

### Systemd Service (Recommended)

Create `/etc/systemd/system/morpheus-dashboard.service`:

```ini
[Unit]
Description=Morpheus Red Team Dashboard
After=network.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/opt/morpheus-dashboard
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable morpheus-dashboard
sudo systemctl start morpheus-dashboard
```

### Nginx Reverse Proxy (Optional)

To expose via HTTPS:

```nginx
server {
    listen 443 ssl;
    server_name dashboard.example.com;

    ssl_certificate     /etc/letsencrypt/live/dashboard.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dashboard.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:51763;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /events {
        proxy_pass http://127.0.0.1:51763/events;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_set_header Cache-Control 'no-cache';
        proxy_buffering off;
        chunked_transfer_encoding on;
    }
}
```

> **Note:** The `/events` endpoint requires `proxy_buffering off` for SSE to work correctly.

## Project Structure

```
morpheus-dashboard/
├── index.html      # Frontend (Tailwind CSS, SSE client)
├── server.py       # Backend (ThreadingHTTPServer, SQLite, SSE)
├── start.sh        # Launch script with readiness check
├── board.db        # SQLite task database (auto-created)
├── server.log      # Runtime log (auto-created)
└── .server.pid     # PID file (auto-created)
```

## Requirements

- Python 3.10+
- No pip packages needed — uses only stdlib (`http.server`, `sqlite3`, `json`, etc.)
