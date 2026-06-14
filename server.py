#!/usr/bin/env python3
"""
Morpheus Mission Control — Backend Server
ThreadingHTTPServer on 127.0.0.1:51763
Serves index.html, /api/snapshot, /events (SSE), and /api/board CRUD.
"""

import json
import os
import sqlite3
import time
import uuid
import re
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta

# ─── PATHS ───────────────────────────────────────────────────────────────────
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(PROJECT_DIR, "index.html")
BOARD_DB = os.path.join(PROJECT_DIR, "board.db")
CVE_REPORTS_DIR = os.path.expanduser("~/Brain/cve-reports")
AGENT_LOGS_DB = os.path.expanduser("~/.hermes/agent-logs.db")
STATE_DB = os.path.expanduser("~/.hermes/state.db")
GATEWAY_STATE_PATH = os.path.expanduser("~/.hermes/gateway_state.json")

HOST = "0.0.0.0"
PORT = 3333
SSE_INTERVAL = 5  # seconds

# ═══════════════════════════════════════════════════════════════════════════════
# BOARD DB — init + helpers
# ═══════════════════════════════════════════════════════════════════════════════

def init_board_db():
    """Create board.db and pre-seed 8 tasks if empty."""
    conn = sqlite3.connect(BOARD_DB)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        priority TEXT DEFAULT 'medium',
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT
    )""")
    cur.execute("SELECT COUNT(*) FROM tasks")
    if cur.fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        seeds = [
            ("task-01", "Renew cybersamurai.co.uk SSL certificate",       "pending",  "high",   "Expires 2026-07-15. Check Let Encrypt auto-renew.", now),
            ("task-02", "Write kill-chain blog post for CVE-2024-XXXX",   "pending",  "medium", "Draft on recon → exploit → pivot pipeline.", now),
            ("task-03", "Harden Morpheus dashboard auth",                 "in_progress","high", "Add JWT + rate limiting to :51763.", now),
            ("task-04", "Deploy Pivot agent container on VPS",            "in_progress","medium","Check Dockerfile + expose SSH tunnel.", now),
            ("task-05", "Integrate HexStrike MCP into Morpheus pipeline", "completed", "critical","BOAZ evasion + 12 encoders wired.", now),
            ("task-06", "Set up Discord bot webhook for /redteam output", "completed", "high",  "Morpheus#2908 connected + DM auth.", now),
            ("task-07", "Audit agent-logs.db retention policy",           "pending",  "low",    "Decide 30-day rotate vs archive.", now),
            ("task-08", "Add SSE reconnect backoff to dashboard JS",       "completed", "medium","Exponential backoff, max 30s.", now),
        ]
        cur.executemany(
            "INSERT INTO tasks (id,title,status,priority,notes,created_at) VALUES (?,?,?,?,?,?)",
            seeds
        )
    conn.commit()
    conn.close()


def board_list():
    conn = sqlite3.connect(BOARD_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks ORDER BY created_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def board_create(title, status, priority, notes):
    tid = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(BOARD_DB)
    conn.execute(
        "INSERT INTO tasks (id,title,status,priority,notes,created_at) VALUES (?,?,?,?,?,?)",
        (tid, title, status, priority, notes, now)
    )
    conn.commit()
    conn.close()
    return {"id": tid, "title": title, "status": status, "priority": priority, "notes": notes, "created_at": now}


def board_update(tid, fields):
    allowed = {"title", "status", "priority", "notes"}
    sets = []
    vals = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return None
    sets.append("updated_at=?")
    vals.append(datetime.now(timezone.utc).isoformat())
    vals.append(tid)
    conn = sqlite3.connect(BOARD_DB)
    conn.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE id=?", (tid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def board_delete(tid):
    conn = sqlite3.connect(BOARD_DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id=?", (tid,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# ═══════════════════════════════════════════════════════════════════════════════
# DATA FUNCTIONS — each wrapped in try/except so one failure never crashes
# ═══════════════════════════════════════════════════════════════════════════════

def gateway_data():
    """Read gateway_state.json and return parsed state."""
    try:
        with open(GATEWAY_STATE_PATH, "r") as f:
            raw = json.load(f)
        return {
            "ok": True,
            "state": raw.get("gateway_state", "unknown"),
            "pid": raw.get("pid"),
            "active_agents": raw.get("active_agents", 0),
            "platforms": raw.get("platforms", {}),
            "updated_at": raw.get("updated_at"),
            "uptime_seconds": None  # computed below if start_time available
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "state": "unavailable", "platforms": {}}


def gateway_uptime():
    """Compute gateway uptime from start_time (integer minutes since epoch?)."""
    try:
        with open(GATEWAY_STATE_PATH, "r") as f:
            raw = json.load(f)
        st = raw.get("start_time")
        if st is None:
            return "unknown"
        # start_time is stored as an integer — try interpreting as seconds first
        # The value 21830609 could be minutes-since-epoch or seconds
        # 21830609 / 60 / 24 / 365 ≈ 4.15 years — likely minutes
        # 21830609 seconds ≈ 252 days — also plausible
        # Let's try: if > 1e9 it's seconds, else minutes
        if st > 1_000_000_000:
            delta = time.time() - st
        else:
            delta = time.time() - (st * 60)
        if delta < 0:
            delta = 0
        hours = int(delta // 3600)
        mins = int((delta % 3600) // 60)
        return f"{hours}h {mins}m"
    except Exception:
        return "unknown"


def activity_data():
    """Query agent-logs.db for last 50 entries, per-agent stats, totals, 7-day breakdown."""
    try:
        conn = sqlite3.connect(AGENT_LOGS_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Last 50 entries
        cur.execute("SELECT * FROM agent_logs ORDER BY created_at DESC, id DESC LIMIT 50")
        recent = [dict(r) for r in cur.fetchall()]

        # Per-agent stats
        cur.execute("""
            SELECT agent_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                   MAX(created_at) as last_seen
            FROM agent_logs GROUP BY agent_name ORDER BY agent_name
        """)
        agent_stats = {}
        for row in cur.fetchall():
            d = dict(row)
            # Last task for this agent
            cur.execute(
                "SELECT task_description FROM agent_logs WHERE agent_name=? ORDER BY created_at DESC, id DESC LIMIT 1",
                (d["agent_name"],)
            )
            r = cur.fetchone()
            d["last_task"] = r["task_description"] if r else None
            # Model used (most recent)
            cur.execute(
                "SELECT model_used FROM agent_logs WHERE agent_name=? AND model_used IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                (d["agent_name"],)
            )
            r = cur.fetchone()
            d["model"] = r["model_used"] if r else None
            agent_stats[d["agent_name"]] = d

        # Overall totals
        cur.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
            FROM agent_logs
        """)
        row = cur.fetchone()
        totals = dict(row) if row else {"total": 0, "completed": 0, "failed": 0}

        # 7-day daily breakdown
        cur.execute("""
            SELECT DATE(created_at) as day, COUNT(*) as count
            FROM agent_logs
            WHERE created_at >= DATE('now', '-7 days')
            GROUP BY day ORDER BY day
        """)
        daily = [{"day": r["day"], "count": r["count"]} for r in cur.fetchall()]

        conn.close()
        return {
            "ok": True,
            "recent": recent,
            "agents": agent_stats,
            "totals": totals,
            "daily": daily
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "recent": [], "agents": {}, "totals": {}, "daily": []}


def sessions_data():
    """Query state.db for session/message counts, token totals, 25 recent sessions."""
    try:
        conn = sqlite3.connect(STATE_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) as cnt FROM sessions")
        session_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM messages")
        message_count = cur.fetchone()["cnt"]

        cur.execute("""
            SELECT COALESCE(SUM(input_tokens),0) as input_tokens,
                   COALESCE(SUM(output_tokens),0) as output_tokens,
                   COALESCE(SUM(cache_read_tokens),0) as cache_read
            FROM sessions
        """)
        row = cur.fetchone()
        tokens = {"input": row["input_tokens"], "output": row["output_tokens"], "cache": row["cache_read"]}

        # 25 most recent sessions — timestamps pass through as-is (Unix float seconds)
        cur.execute("""
            SELECT id, source, model, message_count, input_tokens, output_tokens,
                   cache_read_tokens, started_at, ended_at, title
            FROM sessions
            ORDER BY started_at DESC
            LIMIT 25
        """)
        sessions = [dict(r) for r in cur.fetchall()]

        conn.close()
        return {
            "ok": True,
            "session_count": session_count,
            "message_count": message_count,
            "tokens": tokens,
            "recent_sessions": sessions
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "session_count": 0, "message_count": 0,
                "tokens": {}, "recent_sessions": []}


def vps_health():
    """CPU from /proc/stat, RAM from /proc/meminfo, disk from os.statvfs. No subprocess."""
    result = {"ok": True}
    try:
        # ── CPU ──
        def read_cpu():
            with open("/proc/stat") as f:
                line = f.readline()  # first line is aggregate cpu
            fields = line.split()[1:]  # skip "cpu"
            return [int(x) for x in fields]

        s1 = read_cpu()
        time.sleep(0.1)
        s2 = read_cpu()

        idle1, idle2 = s1[3], s2[3]
        total1, total2 = sum(s1), sum(s2)
        delta_total = total2 - total1
        delta_idle = idle2 - idle1
        cpu_pct = round((1 - delta_idle / delta_total) * 100, 1) if delta_total else 0.0
        result["cpu_percent"] = cpu_pct
    except Exception as e:
        result["cpu_percent"] = None
        result["cpu_error"] = str(e)

    try:
        # ── RAM ──
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    mem[key] = int(parts[1])  # in kB
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", 0)
        used = total - available
        pct = round(used / total * 100, 1) if total else 0.0
        result["ram"] = {
            "total_mb": round(total / 1024),
            "used_mb": round(used / 1024),
            "available_mb": round(available / 1024),
            "percent": pct
        }
    except Exception as e:
        result["ram"] = None
        result["ram_error"] = str(e)

    try:
        # ── DISK ──
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = round(used / total * 100, 1) if total else 0.0
        result["disk"] = {
            "total_gb": round(total / (1024**3), 2),
            "used_gb": round(used / (1024**3), 2),
            "available_gb": round(free / (1024**3), 2),
            "percent": pct
        }
    except Exception as e:
        result["disk"] = None
        result["disk_error"] = str(e)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CVE REPORTS — list + read markdown files from ~/Brain/cve-reports/
# ═══════════════════════════════════════════════════════════════════════════════

def cve_reports_list():
    """Return list of CVE report .md files with metadata."""
    try:
        if not os.path.isdir(CVE_REPORTS_DIR):
            return {"ok": True, "reports": [], "dir": CVE_REPORTS_DIR}
        reports = []
        for fname in sorted(os.listdir(CVE_REPORTS_DIR), reverse=True):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(CVE_REPORTS_DIR, fname)
            stat = os.stat(fpath)
            # Extract CVE ID from filename (e.g. CVE-2026-41096-assessment.md)
            cve_id = fname.replace("-assessment.md", "").replace("_", " ")
            # Peek first few lines for title / CVSS
            title = cve_id
            cvss = None
            try:
                with open(fpath, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("# "):
                            title = line[2:].strip()
                            break
            except Exception:
                pass
            reports.append({
                "filename": fname,
                "cve_id": cve_id,
                "title": title,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
        return {"ok": True, "reports": reports, "count": len(reports)}
    except Exception as e:
        return {"ok": False, "error": str(e), "reports": []}


def cve_report_read(filename):
    """Return the raw markdown content of a single CVE report file."""
    try:
        # Sanitize: only allow .md files, no path traversal
        if not filename.endswith(".md") or "/" in filename or "\\" in filename or ".." in filename:
            return {"ok": False, "error": "Invalid filename"}
        fpath = os.path.join(CVE_REPORTS_DIR, filename)
        if not os.path.isfile(fpath):
            return {"ok": False, "error": "File not found"}
        with open(fpath, "r") as f:
            content = f.read()
        return {"ok": True, "filename": filename, "content": content}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cron_jobs():
    """
    Parse /var/spool/cron/crontabs/*, /etc/crontab, /etc/cron.d/*.
    Strip the username field in system files (5-field → 6-field).
    Label each job 'hermes' (user crontab) or 'system'.
    Convert schedule to plain English.
    """
    jobs = []

    def schedule_to_english(minute, hour, dom, month, dow):
        """Convert 5 cron fields to a human-readable string."""
        dow_names = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"]
        month_names = ["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

        # Range patterns like 7-23 — check BEFORE simple int() casts
        if minute.isdigit() and hour != "*" and "-" in str(hour) and dom == "*" and month == "*" and dow == "*":
            return f"Every hour ({hour}) at minute {minute}"
        if minute == "*" and hour != "*" and "-" in str(hour) and dom == "*" and month == "*" and dow == "*":
            return f"Every minute during hours {hour}"
        # Simple patterns
        if minute == "*" and hour == "*" and dom == "*" and month == "*" and dow == "*":
            return "Every minute"
        if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
            return f"Every {minute[2:]} minutes"
        if minute != "*" and hour == "*" and dom == "*" and month == "*" and dow == "*":
            return f"Every hour at minute {minute}"
        if minute != "*" and hour != "*" and dom == "*" and month == "*" and dow == "*":
            return f"Daily at {int(hour):02d}:{int(minute):02d}"
        if minute != "*" and hour != "*" and dom == "*" and month == "*" and dow != "*":
            dow_str = dow_names[int(dow)] if dow.isdigit() and int(dow) < 7 else dow
            return f"Every {dow_str} at {int(hour):02d}:{int(minute):02d}"
        if minute != "*" and hour != "*" and dom != "*" and month != "*" and dow == "*":
            return f"{month_names[int(month)] if month.isdigit() and int(month)<=12 else month} {dom} at {int(hour):02d}:{int(minute):02d}"
        if minute != "*" and hour != "*" and dom == "*" and month != "*" and dow == "*":
            m = month_names[int(month)] if month.isdigit() and int(month)<=12 else month
            return f"Every {m} at {int(hour):02d}:{int(minute):02d}"
        if minute.startswith("*/") and hour != "*" and dom == "*" and month == "*" and dow == "*":
            return f"Every {minute[2:]} minutes during hour {hour}"
        # Fallback: raw
        return f"{minute} {hour} {dom} {month} {dow}"

    def parse_crontab_lines(lines, label, has_username=False):
        """
        Parse crontab lines.
        If has_username=True, the 6th field is the username (stripped).
        """
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Handle @replacements
            at_map = {
                "@reboot": "At boot",
                "@yearly": "Once a year (Jan 1 00:00)",
                "@annually": "Once a year (Jan 1 00:00)",
                "@monthly": "Monthly (1st 00:00)",
                "@weekly": "Weekly (Sunday 00:00)",
                "@daily": "Daily at 00:00",
                "@midnight": "Daily at 00:00",
                "@hourly": "Every hour at minute 0",
            }
            for at_key, at_desc in at_map.items():
                if line.startswith(at_key):
                    parts = line.split(None, 1)
                    command = parts[1] if len(parts) > 1 else ""
                    jobs.append({
                        "schedule": at_desc,
                        "command": command,
                        "label": label,
                        "raw": line
                    })
                    break
            else:
                # Standard 5- or 6-field cron line
                parts = line.split()
                if len(parts) < 6:
                    continue
                if has_username:
                    # 6-field: min hour dom month dow username command...
                    # Actually 7+: min hour dom month dow user cmd
                    if len(parts) < 7:
                        continue
                    minute, hour, dom, month, dow = parts[0], parts[1], parts[2], parts[3], parts[4]
                    command = " ".join(parts[6:])
                else:
                    # 5-field: min hour dom month dow command...
                    if len(parts) < 6:
                        continue
                    minute, hour, dom, month, dow = parts[0], parts[1], parts[2], parts[3], parts[4]
                    command = " ".join(parts[5:])
                jobs.append({
                    "schedule": schedule_to_english(minute, hour, dom, month, dow),
                    "command": command,
                    "label": label,
                    "raw": line
                })

    try:
        # User crontabs in /var/spool/cron/crontabs/
        crontabs_dir = "/var/spool/cron/crontabs"
        try:
            if os.path.isdir(crontabs_dir):
                for fname in os.listdir(crontabs_dir):
                    fpath = os.path.join(crontabs_dir, fname)
                    if os.path.isfile(fpath):
                        try:
                            with open(fpath) as f:
                                parse_crontab_lines(f.readlines(), label=fname, has_username=False)
                        except PermissionError:
                            pass
        except PermissionError:
            pass

        # /etc/crontab — has username field (6 fields before command)
        if os.path.isfile("/etc/crontab"):
            try:
                with open("/etc/crontab") as f:
                    parse_crontab_lines(f.readlines(), label="system", has_username=True)
            except PermissionError:
                pass

        # /etc/cron.d/* — also has username field
        cron_d = "/etc/cron.d"
        if os.path.isdir(cron_d):
            for fname in os.listdir(cron_d):
                fpath = os.path.join(cron_d, fname)
                if os.path.isfile(fpath):
                    try:
                        with open(fpath) as f:
                            parse_crontab_lines(f.readlines(), label="system", has_username=True)
                    except PermissionError:
                        pass

        return {"ok": True, "jobs": jobs, "count": len(jobs)}
    except Exception as e:
        return {"ok": False, "error": str(e), "jobs": [], "count": 0}


# ═══════════════════════════════════════════════════════════════════════════════
# SNAPSHOT — aggregates all data functions, each wrapped in try/except
# ═══════════════════════════════════════════════════════════════════════════════

def build_snapshot():
    snapshot = {"ok": True, "timestamp": datetime.now(timezone.utc).isoformat()}

    # Gateway
    try:
        gw = gateway_data()
        gw["uptime"] = gateway_uptime()
        snapshot["gateway"] = gw
    except Exception as e:
        snapshot["gateway"] = {"ok": False, "error": str(e)}

    # Activity
    try:
        snapshot["activity"] = activity_data()
    except Exception as e:
        snapshot["activity"] = {"ok": False, "error": str(e)}

    # Sessions
    try:
        snapshot["sessions"] = sessions_data()
    except Exception as e:
        snapshot["sessions"] = {"ok": False, "error": str(e)}

    # VPS Health
    try:
        snapshot["vps"] = vps_health()
    except Exception as e:
        snapshot["vps"] = {"ok": False, "error": str(e)}

    # Cron Jobs
    try:
        snapshot["cron"] = cron_jobs()
    except Exception as e:
        snapshot["cron"] = {"ok": False, "error": str(e)}

    return snapshot


# ═══════════════════════════════════════════════════════════════════════════════
# SSE CLIENTS
# ═══════════════════════════════════════════════════════════════════════════════

sse_clients = []  # list of (file-like) wfile references


def sse_broadcast(snapshot):
    """Send an SSE event to all connected clients."""
    data = "data: " + json.dumps(snapshot) + "\n\n"
    dead = []
    for i, (rfile, wfile) in enumerate(sse_clients):
        try:
            wfile.write(data.encode("utf-8"))
            wfile.flush()
        except Exception:
            dead.append(i)
    for i in reversed(dead):
        sse_clients.pop(i)


def sse_pusher():
    """Background thread: build snapshot every SSE_INTERVAL and push to all clients."""
    while True:
        time.sleep(SSE_INTERVAL)
        try:
            snapshot = build_snapshot()
            sse_broadcast(snapshot)
        except Exception as e:
            sse_broadcast({"ok": False, "error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════════
# REQUEST HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

class Handler(SimpleHTTPRequestHandler):
    """Custom handler: serves index.json, /api/snapshot, /events (SSE), /api/board CRUD."""

    def log_message(self, format, *args):
        """Suppress default logging to keep output clean."""
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/" or path == "/index.html":
            self.serve_index()
        elif path == "/api/snapshot":
            self.serve_json(build_snapshot())
        elif path == "/api/cve-reports":
            self.serve_json(cve_reports_list())
        elif path.startswith("/api/cve-report/"):
            filename = path[len("/api/cve-report/"):]
            self.serve_json(cve_report_read(filename))
        elif path == "/events":
            self.serve_sse()
        elif path == "/api/board":
            self.serve_json({"ok": True, "tasks": board_list()})
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        content_length = int(self.headers.get("Content-Length", 0))

        if path == "/api/board":
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            title = data.get("title", "").strip()
            if not title:
                self.serve_json({"ok": False, "error": "title required"}, 400)
                return
            result = board_create(
                title=title,
                status=data.get("status", "pending"),
                priority=data.get("priority", "medium"),
                notes=data.get("notes", "")
            )
            self.serve_json({"ok": True, "task": result})
        elif path == "/api/board/update":
            params = parse_qs(parsed.query)
            tid = params.get("id", [None])[0]
            if not tid:
                self.serve_json({"ok": False, "error": "id required"}, 400)
                return
            body = self.rfile.read(content_length)
            try:
                fields = json.loads(body) if body else {}
            except json.JSONDecodeError:
                fields = {}
            result = board_update(tid, fields)
            if result:
                self.serve_json({"ok": True, "task": result})
            else:
                self.serve_json({"ok": False, "error": "task not found or no valid fields"}, 404)
        elif path == "/api/board/delete":
            params = parse_qs(parsed.query)
            tid = params.get("id", [None])[0]
            if not tid:
                self.serve_json({"ok": False, "error": "id required"}, 400)
                return
            if board_delete(tid):
                self.serve_json({"ok": True, "deleted": tid})
            else:
                self.serve_json({"ok": False, "error": "task not found"}, 404)
        else:
            self.send_error(404)

    def serve_index(self):
        try:
            with open(INDEX_PATH, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "index.html not found")

    def serve_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        # Send initial snapshot
        try:
            snapshot = build_snapshot()
            self.wfile.write(("data: " + json.dumps(snapshot) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        except Exception as e:
            self.wfile.write(("data: " + json.dumps({"ok": False, "error": str(e)}) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        # Register client for background pushes
        sse_clients.append((self.rfile, self.wfile))
        # Keep connection alive — block this thread
        try:
            while True:
                time.sleep(30)
                # Send comment to detect dead connections
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            # Remove client on disconnect
            for i, (rf, wf) in enumerate(sse_clients):
                if wf is self.wfile:
                    sse_clients.pop(i)
                    break


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    init_board_db()

    server = ThreadingHTTPServer((HOST, PORT), Handler)

    # Start SSE pusher in a daemon thread
    import threading
    pusher = threading.Thread(target=sse_pusher, daemon=True)
    pusher.start()

    print(f"[Morpheus] Mission Control running on http://{HOST}:{PORT}")
    print(f"[Morpheus] SSE stream:     http://{HOST}:{PORT}/events")
    print(f"[Morpheus] Snapshot API:   http://{HOST}:{PORT}/api/snapshot")
    print(f"[Morpheus] Task Board API: http://{HOST}:{PORT}/api/board")
    print(f"[Morpheus] Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Morpheus] Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
