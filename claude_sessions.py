#!/usr/bin/env python3
"""Claude Sessions Manager (web UI) - quản lý session của Claude Code.

- Đọc thư mục .claude (mặc định ~/.claude, có thể đổi trong giao diện)
- Đồng bộ conversation (projects/*/*.jsonl) vào SQLite khi mở và khi đóng app
- Tạo session mới (claude -n <name>) / resume session trong Terminal

Chạy:  python3 claude_sessions.py [--port 8765] [--no-browser]
"""
import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(os.environ.get("CSM_HOME", Path.home() / ".claude-sessions-manager"))
DB_PATH = APP_DIR / "sessions.db"
DEFAULT_CLAUDE_DIR = Path.home() / ".claude"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    claude_dir TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_mtime REAL NOT NULL,
    file_size INTEGER NOT NULL,
    cwd TEXT,
    title TEXT,
    first_prompt TEXT,
    git_branch TEXT,
    model TEXT,
    message_count INTEGER DEFAULT 0,
    created_at TEXT,
    last_activity TEXT,
    missing INTEGER DEFAULT 0,
    synced_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    ts TEXT,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cwd TEXT NOT NULL,
    prompt TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_last ON sessions(last_activity);
"""

REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
TAG_ONLY_RE = re.compile(r"^\s*<(command-|local-command-|bash-|task-notification)", re.S)


# ---------------------------------------------------------------- database --
def connect():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


# ------------------------------------------------------------------- parse --
def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def parse_session_file(path: Path):
    """Trả về (meta, messages) từ 1 file jsonl."""
    meta = {"cwd": None, "title": None, "git_branch": None, "model": None,
            "created_at": None, "last_activity": None, "first_prompt": None}
    msgs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue  # dòng cuối có thể đang được ghi dở
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            ts = d.get("timestamp")
            if t in ("custom-title", "agent-name"):
                title = d.get("customTitle") or d.get("agentName")
                if title:
                    meta["title"] = title
                continue
            if t not in ("user", "assistant") or d.get("isSidechain"):
                continue
            if d.get("cwd"):
                meta["cwd"] = d["cwd"]
            if d.get("gitBranch"):
                meta["git_branch"] = d["gitBranch"]
            if ts:
                meta["created_at"] = meta["created_at"] or ts
                meta["last_activity"] = ts
            m = d.get("message") or {}
            if t == "assistant" and m.get("model") and m["model"] != "<synthetic>":
                meta["model"] = m["model"]
            text = _text_of(m.get("content"))
            if t == "user":
                if d.get("isMeta"):
                    continue
                text = REMINDER_RE.sub("", text)
                if TAG_ONLY_RE.match(text):
                    continue
            text = text.strip()
            if not text:
                continue
            if t == "user" and not meta["first_prompt"]:
                meta["first_prompt"] = text[:300]
            msgs.append((t, text, ts))
    return meta, msgs


# -------------------------------------------------------------------- sync --
def sync(db, claude_dir: Path):
    """Đồng bộ incremental: chỉ parse lại file có mtime/size đổi."""
    projects = claude_dir / "projects"
    now = datetime.now().isoformat(timespec="seconds")
    cdir = str(claude_dir)
    seen = set()
    stats = {"added": 0, "updated": 0, "missing": 0}

    if projects.is_dir():
        for path in projects.glob("*/*.jsonl"):
            sid = path.stem
            try:
                st = path.stat()
            except OSError:
                continue
            seen.add(sid)
            row = db.execute("SELECT file_mtime,file_size,claude_dir FROM sessions WHERE id=?",
                             (sid,)).fetchone()
            if (row and row["file_mtime"] == st.st_mtime and row["file_size"] == st.st_size
                    and row["claude_dir"] == cdir):
                db.execute("UPDATE sessions SET missing=0 WHERE id=?", (sid,))
                continue
            meta, msgs = parse_session_file(path)
            if not msgs and not meta["title"]:
                continue  # file rỗng / chỉ có metadata
            cwd = meta["cwd"] or "/" + path.parent.name.lstrip("-").replace("-", "/")
            with db:
                db.execute("""INSERT INTO sessions(id,claude_dir,file_path,file_mtime,file_size,cwd,
                        title,first_prompt,git_branch,model,message_count,created_at,last_activity,
                        missing,synced_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                    ON CONFLICT(id) DO UPDATE SET claude_dir=excluded.claude_dir,
                        file_path=excluded.file_path,file_mtime=excluded.file_mtime,
                        file_size=excluded.file_size,cwd=excluded.cwd,title=excluded.title,
                        first_prompt=excluded.first_prompt,git_branch=excluded.git_branch,
                        model=excluded.model,message_count=excluded.message_count,
                        created_at=excluded.created_at,last_activity=excluded.last_activity,
                        missing=0,synced_at=excluded.synced_at""",
                           (sid, cdir, str(path), st.st_mtime, st.st_size, cwd, meta["title"],
                            meta["first_prompt"], meta["git_branch"], meta["model"], len(msgs),
                            meta["created_at"], meta["last_activity"], now))
                db.execute("DELETE FROM messages WHERE session_id=?", (sid,))
                db.executemany("INSERT INTO messages(session_id,seq,role,text,ts) VALUES(?,?,?,?,?)",
                               [(sid, i, r, t, ts) for i, (r, t, ts) in enumerate(msgs)])
            stats["updated" if row else "added"] += 1

    # session có file đã bị xoá -> đánh dấu missing (giữ lại lịch sử trong DB)
    for r in db.execute("SELECT id FROM sessions WHERE claude_dir=? AND missing=0", (cdir,)).fetchall():
        if r["id"] not in seen:
            db.execute("UPDATE sessions SET missing=1 WHERE id=?", (r["id"],))
            stats["missing"] += 1
    db.commit()
    return stats


# ---------------------------------------------------------------- terminal --
def _applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def open_in_terminal(shell_cmd: str, terminal="Terminal"):
    """Mở cửa sổ terminal mới chạy shell_cmd (macOS)."""
    if sys.platform != "darwin":
        for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["xterm", "-e"]):
            try:
                subprocess.Popen(term + ["bash", "-lc", shell_cmd + "; exec bash"])
                return
            except FileNotFoundError:
                continue
        raise RuntimeError("Không tìm thấy terminal emulator")
    if terminal == "iTerm":
        script = ('tell application "iTerm"\n activate\n'
                  ' set w to (create window with default profile)\n'
                  f' tell current session of w to write text {_applescript_str(shell_cmd)}\n'
                  'end tell')
    else:
        script = ('tell application "Terminal"\n activate\n'
                  f' do script {_applescript_str(shell_cmd)}\nend tell')
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)


def build_cmd(cwd, args, claude_dir: Path):
    env = ""
    if claude_dir.resolve() != DEFAULT_CLAUDE_DIR.resolve():
        env = f"CLAUDE_CONFIG_DIR={shlex.quote(str(claude_dir))} "
    return f"cd {shlex.quote(cwd)} && {env}claude " + " ".join(shlex.quote(a) for a in args)


def launch_claude(cwd, args, claude_dir: Path, terminal="Terminal"):
    """Mở terminal mới chạy `claude <args>` trong cwd (macOS / Linux / Windows)."""
    if sys.platform == "win32":
        # Windows: console mới, chạy thẳng không qua shell -> không cần quote
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("Không tìm thấy lệnh `claude` trong PATH")
        env = os.environ.copy()
        if claude_dir.resolve() != DEFAULT_CLAUDE_DIR.resolve():
            env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
        cmd = [exe, *args]
        if exe.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd.exe", "/c", *cmd]
        subprocess.Popen(cmd, cwd=cwd, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    open_in_terminal(build_cmd(cwd, args, claude_dir), terminal)


# ------------------------------------------------------------------ server --
class State:
    """Trạng thái dùng chung giữa các request."""

    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.syncing = False
        self.status = ""
        self.server = None

    def start_sync(self):
        with self.lock:
            if self.syncing:
                return False
            self.syncing = True
            self.status = "Đang đồng bộ…"
        threading.Thread(target=self._run_sync, daemon=True).start()
        return True

    def _run_sync(self):
        try:
            db = connect()
            cdir = Path(get_setting(db, "claude_dir", str(DEFAULT_CLAUDE_DIR)))
            st = sync(db, cdir)
            db.close()
            msg = f"Đã đồng bộ: +{st['added']} mới, {st['updated']} cập nhật"
        except Exception as e:  # noqa: BLE001
            msg = f"Lỗi đồng bộ: {e}"
        with self.lock:
            self.syncing = False
            self.status = msg

    def final_sync(self):
        """Đồng bộ lần cuối khi đóng app."""
        while self.syncing:
            threading.Event().wait(0.05)
        try:
            db = connect()
            sync(db, Path(get_setting(db, "claude_dir", str(DEFAULT_CLAUDE_DIR))))
            db.close()
        except Exception as e:  # noqa: BLE001
            print("final sync lỗi:", e, file=sys.stderr)


STATE = State()


class Handler(BaseHTTPRequestHandler):
    server_version = "CSM/1.0"

    def log_message(self, *a):  # im lặng
        pass

    # --- helpers
    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")  # chặn DNS rebinding

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    # --- GET
    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "bad host")
        u = urlparse(self.path)
        db = connect()
        try:
            if u.path == "/":
                html = INDEX_HTML.replace("__TOKEN__", STATE.token)
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._send(200, {
                    "claude_dir": get_setting(db, "claude_dir", str(DEFAULT_CLAUDE_DIR)),
                    "terminal": get_setting(db, "terminal", "Terminal"),
                    "last_cwd": get_setting(db, "last_cwd", str(Path.home())),
                    "syncing": STATE.syncing, "status": STATE.status})
            if u.path == "/api/sessions":
                qs = parse_qs(u.query)
                q = (qs.get("q", [""])[0]).strip()
                content = qs.get("content", ["1"])[0] == "1"
                cdir = get_setting(db, "claude_dir", str(DEFAULT_CLAUDE_DIR))
                params, sql = [cdir], "SELECT * FROM sessions s WHERE claude_dir=?"
                if q:
                    like = f"%{q}%"
                    cond = "(title LIKE ? OR first_prompt LIKE ? OR cwd LIKE ? OR id LIKE ?"
                    params += [like] * 4
                    if content:
                        cond += (" OR EXISTS(SELECT 1 FROM messages m "
                                 "WHERE m.session_id=s.id AND m.text LIKE ?)")
                        params.append(like)
                    sql += " AND " + cond + ")"
                sql += " ORDER BY last_activity DESC"
                rows = [dict(r) for r in db.execute(sql, params)]
                for r in rows:
                    for k in ("file_path", "file_mtime", "file_size", "synced_at"):
                        r.pop(k, None)
                return self._send(200, rows)
            m = re.fullmatch(r"/api/sessions/([\w-]+)/messages", u.path)
            if m:
                rows = db.execute("SELECT role,text,ts FROM messages WHERE session_id=? ORDER BY seq",
                                  (m.group(1),)).fetchall()
                return self._send(200, [dict(r) for r in rows])
            return self._err(404, "not found")
        finally:
            db.close()

    # --- POST
    def do_POST(self):
        if not self._host_ok():
            return self._err(403, "bad host")
        if not secrets.compare_digest(self.headers.get("X-CSM-Token", ""), STATE.token):
            return self._err(403, "bad token")  # chặn CSRF từ trang web khác
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._err(400, "bad json")
        db = connect()
        try:
            cdir = Path(get_setting(db, "claude_dir", str(DEFAULT_CLAUDE_DIR)))
            terminal = get_setting(db, "terminal", "Terminal")
            p = self.path

            if p == "/api/sync":
                return self._send(200, {"started": STATE.start_sync()})

            if p == "/api/config":
                if "claude_dir" in body:
                    d = Path(str(body["claude_dir"])).expanduser()
                    if not d.is_dir():
                        return self._err(400, f"Không tồn tại: {d}")
                    set_setting(db, "claude_dir", str(d))
                    STATE.start_sync()
                if body.get("terminal") in ("Terminal", "iTerm"):
                    set_setting(db, "terminal", body["terminal"])
                return self._send(200, {"ok": True})

            if p == "/api/resume":
                r = db.execute("SELECT * FROM sessions WHERE id=?", (body.get("id"),)).fetchone()
                if not r:
                    return self._err(404, "Không có session này")
                if r["missing"]:
                    return self._err(400, "File session đã bị xoá khỏi .claude, không thể resume")
                if not r["cwd"] or not os.path.isdir(r["cwd"]):
                    return self._err(400, f"Thư mục làm việc không còn tồn tại: {r['cwd']}")
                launch_claude(r["cwd"], ["--resume", r["id"]], cdir, terminal)
                return self._send(200, {"ok": True})

            if p == "/api/new":
                name = str(body.get("name", "")).strip()
                cwd = str(body.get("cwd", "")).strip()
                prompt = str(body.get("prompt", "")).strip()
                cwd = str(Path(cwd).expanduser())
                if not name:
                    return self._err(400, "Nhập tên session")
                if not os.path.isdir(cwd):
                    return self._err(400, f"Thư mục không tồn tại: {cwd}")
                args = ["-n", name] + ([prompt] if prompt else [])
                launch_claude(cwd, args, cdir, terminal)
                db.execute("INSERT INTO launches(name,cwd,prompt,created_at) VALUES(?,?,?,?)",
                           (name, cwd, prompt or None, datetime.now().isoformat(timespec="seconds")))
                db.commit()
                set_setting(db, "last_cwd", cwd)
                return self._send(200, {"ok": True})

            if p == "/api/quit":
                self._send(200, {"ok": True})
                threading.Thread(target=shutdown, daemon=True).start()
                return
            return self._err(404, "not found")
        except subprocess.CalledProcessError as e:
            return self._err(500, f"Không mở được terminal: {(e.stderr or '').strip() or e}")
        except Exception as e:  # noqa: BLE001
            return self._err(500, str(e))
        finally:
            db.close()


def shutdown(*_):
    """Đồng bộ lần cuối rồi tắt server."""
    STATE.final_sync()
    if STATE.server:
        STATE.server.shutdown()


INDEX_HTML = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Sessions Manager</title>
<style>
:root{--bg:#faf9f7;--fg:#1f1e1d;--mut:#76736e;--line:#e4e1db;--card:#fff;--acc:#c15f3c;--acc2:#a84f30;--sel:#f4e9e3;--user:#0b5cad;--asst:#2e7d32}
@media(prefers-color-scheme:dark){:root{--bg:#1e1d1c;--fg:#ece9e4;--mut:#9a968f;--line:#38362f;--card:#262523;--sel:#3a2d27;--user:#6cb0f5;--asst:#7fcf84}}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg);height:100vh;display:flex;flex-direction:column}
header{display:flex;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:15px;margin:0 8px 0 0}
button,input,select,textarea{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
button{cursor:pointer}button:hover{border-color:var(--acc)}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}button.pri:hover{background:var(--acc2)}
.mut{color:var(--mut)}.grow{flex:1}
main{flex:1;display:grid;grid-template-columns:minmax(420px,1fr) 1.2fr;min-height:0}
#list{overflow:auto;border-right:1px solid var(--line)}
.row{padding:10px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--sel)}.row.sel{background:var(--sel);box-shadow:inset 3px 0 var(--acc)}
.row .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .m{font-size:12px;color:var(--mut);display:flex;gap:10px;white-space:nowrap;overflow:hidden}
.row .m span:first-child{overflow:hidden;text-overflow:ellipsis;flex:0 1 auto}
.row.missing .t{text-decoration:line-through;opacity:.6}
#detail{display:flex;flex-direction:column;min-height:0}
#dhead{padding:12px 20px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center}
#dhead .info{flex:1;min-width:0}#dhead .info b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#msgs{flex:1;overflow:auto;padding:12px 20px}
.msg{margin-bottom:14px}.msg .r{font-size:12px;font-weight:700;margin-bottom:2px}
.msg.user .r{color:var(--user)}.msg.assistant .r{color:var(--asst)}
.msg pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.5 ui-monospace,Menlo,monospace}
.empty{padding:40px;text-align:center;color:var(--mut)}
dialog{border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg);padding:20px;width:520px}
dialog::backdrop{background:rgba(0,0,0,.4)}
dialog label{display:block;margin:10px 0 3px;font-size:12px;color:var(--mut)}
dialog input,dialog textarea{width:100%}
.btns{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:8px 16px;border-radius:8px;display:none}
#toast.err{background:#b3261e}
</style></head><body>
<header>
 <h1>Claude Sessions</h1>
 <button class="pri" id="bNew">＋ New session</button>
 <input id="q" placeholder="Tìm tên, thư mục, nội dung…" size="30">
 <label class="mut"><input type="checkbox" id="content" checked> nội dung</label>
 <span class="grow"></span>
 <span class="mut" id="status"></span>
 <button id="bSync" title="Đồng bộ lại">⟳ Refresh</button>
 <button id="bCfg">⚙ Cấu hình</button>
 <button id="bQuit" title="Đồng bộ lần cuối rồi tắt server">⏻ Thoát</button>
</header>
<main>
 <div id="list"></div>
 <div id="detail"><div class="empty">Chọn một session để xem hội thoại</div></div>
</main>

<dialog id="dNew"><form method="dialog" id="fNew">
 <b>New session</b>
 <label>Tên session (-n)</label><input id="nName" required autofocus>
 <label>Thư mục làm việc</label><input id="nCwd" required>
 <label>Prompt đầu (tuỳ chọn)</label><textarea id="nPrompt" rows="5"></textarea>
 <div class="btns"><button type="button" onclick="dNew.close()">Huỷ</button><button class="pri">Tạo &amp; mở Terminal</button></div>
</form></dialog>

<dialog id="dCfg"><form method="dialog" id="fCfg">
 <b>Cấu hình</b>
 <label>Thư mục .claude</label><input id="cDir" required>
 <label>Terminal</label><select id="cTerm"><option>Terminal</option><option>iTerm</option></select>
 <div class="btns"><button type="button" onclick="dCfg.close()">Huỷ</button><button class="pri">Lưu</button></div>
</form></dialog>
<div id="toast"></div>

<script>
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id);
let sessions=[],selId=null,st={},timer;
async function api(path,body){
  const o=body===undefined?{}:{method:"POST",headers:{"X-CSM-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(body)};
  const r=await fetch(path,o);const j=await r.json();
  if(!r.ok)throw new Error(j.error||r.statusText);return j;
}
function toast(m,err){const t=$("toast");t.textContent=m;t.className=err?"err":"";t.style.display="block";clearTimeout(t._t);t._t=setTimeout(()=>t.style.display="none",3500)}
const fmt=iso=>iso?new Date(iso).toLocaleString("vi-VN",{dateStyle:"short",timeStyle:"short"}):"";
async function loadState(){st=await api("/api/state");$("status").textContent=st.status;return st}
async function loadList(){
  const q=encodeURIComponent($("q").value.trim());
  sessions=await api(`/api/sessions?q=${q}&content=${$("content").checked?1:0}`);
  const box=$("list");box.replaceChildren();
  if(!sessions.length){box.innerHTML='<div class="empty">Không có session nào</div>';return}
  for(const s of sessions){
    const d=document.createElement("div");d.className="row"+(s.id===selId?" sel":"")+(s.missing?" missing":"");
    const t=document.createElement("div");t.className="t";t.textContent=(s.title||s.first_prompt||"(trống)").replace(/\s+/g," ");
    const m=document.createElement("div");m.className="m";
    for(const x of [s.cwd,s.message_count+" msgs",fmt(s.last_activity)]){const sp=document.createElement("span");sp.textContent=x;m.append(sp)}
    d.append(t,m);d.onclick=()=>select(s.id);d.ondblclick=()=>resume(s.id);box.append(d);
  }
}
async function select(id){
  selId=id;[...$("list").children].forEach((c,i)=>c.classList.toggle("sel",sessions[i]?.id===id));
  const s=sessions.find(x=>x.id===id);if(!s)return;
  const msgs=await api(`/api/sessions/${id}/messages`);
  const det=$("detail");det.replaceChildren();
  const h=document.createElement("div");h.id="dhead";
  const info=document.createElement("div");info.className="info";
  const b=document.createElement("b");b.textContent=s.title||s.first_prompt||"(trống)";
  const sub=document.createElement("span");sub.className="mut";
  sub.textContent=[s.cwd,s.git_branch,s.model,id].filter(Boolean).join(" · ");
  info.append(b,sub);
  const btn=document.createElement("button");btn.className="pri";btn.textContent="▶ Resume";btn.onclick=()=>resume(id);
  h.append(info,btn);
  const box=document.createElement("div");box.id="msgs";
  for(const m of msgs){
    const e=document.createElement("div");e.className="msg "+m.role;
    const r=document.createElement("div");r.className="r";r.textContent=m.role.toUpperCase()+"  "+fmt(m.ts);
    const p=document.createElement("pre");p.textContent=m.text.length>6000?m.text.slice(0,6000)+"…":m.text;
    e.append(r,p);box.append(e);
  }
  det.append(h,box);
}
async function resume(id){try{await api("/api/resume",{id});toast("Đã mở Terminal")}catch(e){toast(e.message,true)}}
async function refresh(){await loadState();await loadList();if(selId)select(selId)}
async function waitSync(){
  for(let i=0;i<600;i++){const s=await loadState();if(!s.syncing)break;await new Promise(r=>setTimeout(r,500))}
  await loadList();
}
$("q").oninput=()=>{clearTimeout(timer);timer=setTimeout(loadList,200)};
$("content").onchange=loadList;
$("bSync").onclick=async()=>{await api("/api/sync",{});waitSync()};
$("bNew").onclick=()=>{$("nCwd").value=st.last_cwd||"";$("nName").value="";$("nPrompt").value="";$("dNew").showModal()};
$("fNew").onsubmit=async e=>{
  try{await api("/api/new",{name:$("nName").value,cwd:$("nCwd").value,prompt:$("nPrompt").value});toast("Đã mở Terminal")}
  catch(x){e.preventDefault();toast(x.message,true);$("dNew").showModal()}
};
$("bCfg").onclick=()=>{$("cDir").value=st.claude_dir;$("cTerm").value=st.terminal;$("dCfg").showModal()};
$("fCfg").onsubmit=async e=>{
  try{await api("/api/config",{claude_dir:$("cDir").value,terminal:$("cTerm").value});selId=null;
      $("detail").innerHTML='<div class="empty">Chọn một session để xem hội thoại</div>';waitSync()}
  catch(x){e.preventDefault();toast(x.message,true);$("dCfg").showModal()}
};
$("bQuit").onclick=async()=>{if(!confirm("Đồng bộ lần cuối và tắt app?"))return;
  await api("/api/quit",{});document.body.innerHTML='<div class="empty">Đã lưu và tắt. Có thể đóng tab này.</div>'};
// sync mỗi lần mở trang, và cập nhật danh sách định kỳ khi tab đang hiển thị
loadState().then(()=>{loadList();waitSync()});
setInterval(()=>{if(!document.hidden&&!$("dNew").open&&!$("dCfg").open)loadList()},15000);
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--sync", action="store_true", help="chỉ đồng bộ DB rồi thoát")
    args = ap.parse_args()

    if args.sync:
        db = connect()
        print(sync(db, Path(get_setting(db, "claude_dir", str(DEFAULT_CLAUDE_DIR)))))
        return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port bận -> tự chọn port khác
    STATE.server = server
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    STATE.start_sync()  # cập nhật khi mở app
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=shutdown, daemon=True).start())
    print(f"Claude Sessions Manager: {url}  (Ctrl+C để thoát)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STATE.final_sync()  # cập nhật khi đóng app
    server.server_close()


if __name__ == "__main__":
    main()
