#!/usr/bin/env python3
"""Private Telegram controller for tmux and the local Codex app-server API."""
import http.client
import json
import os
import select
import shutil
import stat
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse

HOME = os.path.expanduser("~")
CONFIG_PATH = os.environ.get("TELEGRAM_TMUX_CONFIG", os.path.join(HOME, ".config", "telegram-tmux-control.env"))
STATE_PATH = os.environ.get("TELEGRAM_TMUX_STATE", os.path.join(HOME, ".local", "state", "telegram-tmux-control.json"))
SESSION = os.environ.get("TELEGRAM_TMUX_SESSION", "web")
WORKSPACE = os.environ.get("TELEGRAM_TMUX_WORKSPACE", HOME)
CODEX_BIN = os.environ.get("TELEGRAM_TMUX_CODEX_BIN", shutil.which("codex") or os.path.join(HOME, ".local", "bin", "codex"))
NODE_BIN = os.environ.get("TELEGRAM_TMUX_NODE_BIN", shutil.which("node") or "node")
# A wedged app-server turn used to hold the single request lock for ten
# minutes, leaving every later Telegram command silently queued behind it.
TURN_TIMEOUT = int(os.environ.get("TELEGRAM_TMUX_TURN_TIMEOUT", "180"))
# Long tasks are deliberately handled by a single durable worker.  They get a
# separate budget so an interactive request can still retain its short timeout.
TASK_STATE_PATH = os.environ.get("TELEGRAM_TMUX_TASK_STATE", os.path.join(HOME, ".local", "state", "telegram-tmux-tasks.sqlite3"))
TASK_TIMEOUT = int(os.environ.get("TELEGRAM_TMUX_TASK_TIMEOUT", "3600"))
TASK_MAX_QUEUE = int(os.environ.get("TELEGRAM_TMUX_TASK_MAX_QUEUE", "20"))


def config():
    values = {}
    with open(CONFIG_PATH, encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    for required in ("BOT_TOKEN", "PAIR_CODE"):
        if not values.get(required):
            raise RuntimeError(f"Missing {required} in {CONFIG_PATH}")
    return values


CFG = {}
API_HOST = "api.telegram.org"
API_PREFIX = ""


class CodexAppServer:
    """Minimal JSONL client for Codex's supported local app-server protocol."""

    def __init__(self):
        self.process = None
        self.thread_id = None
        self.request_id = 0
        self.lock = threading.RLock()
        self.read_buffer = b""

    def _send(self, message):
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex app-server is not running")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def _receive(self, timeout):
        if not self.process or not self.process.stdout:
            raise RuntimeError("Codex app-server is not running")
        deadline = time.monotonic() + timeout
        while b"\n" not in self.read_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex app-server did not respond in time")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("Codex app-server did not respond in time")
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                raise RuntimeError("Codex app-server stopped unexpectedly")
            self.read_buffer += chunk
        line, self.read_buffer = self.read_buffer.split(b"\n", 1)
        return json.loads(line)

    def _request(self, method, params, timeout=30):
        self.request_id += 1
        request_id = self.request_id
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive(max(0.1, deadline - time.monotonic()))
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(message["error"].get("message", "Codex app-server error"))
            return message.get("result", {})

    def _start(self):
        if self.process and self.process.poll() is None:
            return
        self.process = subprocess.Popen(
            [NODE_BIN, CODEX_BIN, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self.read_buffer = b""
        self._request("initialize", {"clientInfo": {"name": "telegram_tmux_control", "title": "Telegram tmux control", "version": "1.0"}})
        self._send({"method": "initialized", "params": {}})
        result = self._request("thread/start", {"cwd": WORKSPACE})
        self.thread_id = result["thread"]["id"]

    def _stop(self):
        """Discard an unhealthy app-server so the next request starts cleanly."""
        process, self.process = self.process, None
        self.thread_id = None
        self.read_buffer = b""
        if not process or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _text_from(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(CodexAppServer._text_from(part) for part in value)
        if isinstance(value, dict):
            # Agent-message payloads use either `text` or streamed `delta`.
            return str(value.get("text") or value.get("delta") or "")
        return ""

    def run(self, prompt, on_file_changes=None, on_agent_message=None, timeout=TURN_TIMEOUT, cancelled=None):
        """Run a turn and forward patch and completed agent-message events."""
        with self.lock:
            self._start()
            result = self._request("turn/start", {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt}]})
            turn_id = result["turn"]["id"]
            deadline = time.monotonic() + timeout
            answers = []
            reported_paths = set()
            while True:
                remaining = deadline - time.monotonic()
                if cancelled and cancelled():
                    self._stop()
                    raise TaskCancelled("task cancelled")
                if remaining <= 0:
                    self._stop()
                    raise TimeoutError(f"Codex turn timed out after {timeout} seconds; app-server reset")
                try:
                    # Wake regularly to honour cancellation without relying on
                    # unsupported app-server interrupt methods.
                    message = self._receive(min(1.0, max(0.1, remaining)))
                except TimeoutError:
                    if cancelled and cancelled():
                        self._stop()
                        raise TaskCancelled("task cancelled") from None
                    if time.monotonic() < deadline:
                        continue
                    self._stop()
                    raise TimeoutError(f"Codex turn timed out after {timeout} seconds; app-server reset") from None
                if message.get("method") == "item/fileChange/patchUpdated":
                    params = message.get("params", {})
                    if params.get("turnId") == turn_id and on_file_changes:
                        for change in params.get("changes", []):
                            path = change.get("path")
                            if path and path not in reported_paths:
                                # Patch updates repeat prior paths. Notify once,
                                # immediately, when this file first changes.
                                reported_paths.add(path)
                                on_file_changes([change])
                elif message.get("method") == "item/completed":
                    item = message.get("params", {}).get("item", {})
                    if item.get("type") == "agentMessage":
                        text = self._text_from(item).strip()
                        if text:
                            if on_agent_message:
                                on_agent_message(text)
                            else:
                                answers.append(text)
                elif message.get("method") == "turn/completed":
                    turn = message.get("params", {}).get("turn", {})
                    if turn.get("id") == turn_id:
                        if turn.get("status") != "completed":
                            raise RuntimeError(f"Codex turn ended with status: {turn.get('status', 'unknown')}")
                        return "\n\n".join(answers) or "Codex completed without a text response."


CODEX_RPC = CodexAppServer()


class TelegramApi:
    """Small keep-alive Telegram client; one instance per request lane."""

    def __init__(self):
        self.connection = None
        self.lock = threading.RLock()

    def _connect(self):
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(API_HOST, timeout=40)
        return self.connection

    def _discard_connection(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None

    def call(self, method, payload=None):
        data = urllib.parse.urlencode(payload or {}).encode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(data)),
            "Connection": "keep-alive",
        }
        # A server may close an idle keep-alive connection. Reconnect once so
        # that an idle bot does not fail its first reply.
        with self.lock:
            for attempt in range(2):
                try:
                    connection = self._connect()
                    connection.request("POST", API_PREFIX + method, data, headers)
                    response = connection.getresponse()
                    body = response.read()
                    if response.status >= 400:
                        raise RuntimeError(f"Telegram API returned HTTP {response.status}")
                    result = json.loads(body)
                    if not result.get("ok"):
                        raise RuntimeError(result.get("description", "Telegram API request failed"))
                    return result["result"]
                except (http.client.HTTPException, OSError):
                    self._discard_connection()
                    if attempt:
                        raise


# Keep long polling separate: an in-flight getUpdates request must never delay
# acknowledgements or final replies from worker threads.
POLL_API = TelegramApi()
SEND_API = TelegramApi()


def api(method, payload=None):
    client = POLL_API if method == "getUpdates" else SEND_API
    return client.call(method, payload)


def read_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {"offset": 0}


def write_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    temporary = STATE_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file)
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE_PATH)


class TaskCancelled(Exception):
    """Raised when a durable task is cancelled by its Telegram owner."""


class TaskStore:
    """Small, durable, single-user task queue backed by SQLite."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    checkpoint TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )
            """)
            # A process cannot safely know whether an old in-flight turn made
            # changes. Leave it explicit for the owner to inspect and resume.
            self.connection.execute("UPDATE tasks SET status = 'interrupted', updated_at = ? WHERE status = 'running'", (int(time.time()),))

    def _row(self, row):
        return dict(row) if row else None

    def create(self, chat_id, prompt):
        now = int(time.time())
        with self.lock, self.connection:
            queued = self.connection.execute("SELECT count(*) FROM tasks WHERE status IN ('queued', 'running', 'cancelling')").fetchone()[0]
            if queued >= TASK_MAX_QUEUE:
                raise RuntimeError(f"task queue is full ({TASK_MAX_QUEUE})")
            cursor = self.connection.execute("INSERT INTO tasks (chat_id, prompt, status, created_at, updated_at) VALUES (?, ?, 'queued', ?, ?)", (str(chat_id), prompt, now, now))
            return self.get(cursor.lastrowid)

    def get(self, task_id):
        with self.lock:
            return self._row(self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    def recent(self, limit=10):
        with self.lock:
            rows = self.connection.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [self._row(row) for row in rows]

    def claim(self):
        now = int(time.time())
        with self.lock, self.connection:
            row = self.connection.execute("SELECT * FROM tasks WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            self.connection.execute("UPDATE tasks SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ? AND status = 'queued'", (now, now, row['id']))
            return self.get(row['id'])

    def checkpoint(self, task_id, text):
        with self.lock, self.connection:
            self.connection.execute("UPDATE tasks SET checkpoint = ?, updated_at = ? WHERE id = ?", (text[-8000:], int(time.time()), task_id))

    def finish(self, task_id, status, checkpoint="", error=""):
        with self.lock, self.connection:
            self.connection.execute("UPDATE tasks SET status = ?, checkpoint = ?, error = ?, finished_at = ?, updated_at = ? WHERE id = ?", (status, checkpoint[-8000:], error[-4000:], int(time.time()), int(time.time()), task_id))

    def cancel(self, task_id):
        with self.lock, self.connection:
            task = self.get(task_id)
            if not task or task['status'] in ('completed', 'failed', 'cancelled'):
                return task, False
            status = 'cancelled' if task['status'] in ('queued', 'paused', 'interrupted') else 'cancelling'
            self.connection.execute("UPDATE tasks SET status = ?, updated_at = ?, finished_at = CASE WHEN ? = 'cancelled' THEN ? ELSE finished_at END WHERE id = ?", (status, int(time.time()), status, int(time.time()), task_id))
            return self.get(task_id), True

    def pause(self, task_id):
        with self.lock, self.connection:
            task = self.get(task_id)
            if not task or task['status'] != 'queued':
                return task, False
            self.connection.execute("UPDATE tasks SET status = 'paused', updated_at = ? WHERE id = ?", (int(time.time()), task_id))
            return self.get(task_id), True

    def resume(self, task_id):
        with self.lock, self.connection:
            task = self.get(task_id)
            if not task or task['status'] not in ('paused', 'interrupted', 'failed'):
                return task, False
            self.connection.execute("UPDATE tasks SET status = 'queued', error = '', finished_at = NULL, updated_at = ? WHERE id = ?", (int(time.time()), task_id))
            return self.get(task_id), True

    def cancelling(self, task_id):
        task = self.get(task_id)
        return bool(task and task['status'] == 'cancelling')


class TaskWorker:
    """Runs at most one durable Codex task, preventing invisible lock queues."""

    def __init__(self, store):
        self.store = store
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self._run, name="telegram-task-worker", daemon=True)

    def start(self):
        self.thread.start()
        self.wake.set()

    def notify(self):
        self.wake.set()

    def _run(self):
        while True:
            task = self.store.claim()
            if not task:
                self.wake.wait(30)
                self.wake.clear()
                continue
            task_id, chat_id = task['id'], task['chat_id']
            try:
                def changed(changes):
                    for change in changes:
                        reply(chat_id, f"Task T-{task_id}\n{change_summary(change)}")

                def progress(text):
                    self.store.checkpoint(task_id, text)

                answer = CODEX_RPC.run(task['prompt'], on_file_changes=changed,
                                       on_agent_message=progress, timeout=TASK_TIMEOUT,
                                       cancelled=lambda: self.store.cancelling(task_id))
                self.store.finish(task_id, 'completed', answer)
                reply(chat_id, f"Task T-{task_id} completed.\n{answer}")
            except TaskCancelled:
                self.store.finish(task_id, 'cancelled', 'Cancelled by user.')
                reply(chat_id, f"Task T-{task_id} cancelled.")
            except Exception as error:
                message = str(error)
                self.store.finish(task_id, 'failed', error=message)
                print(f"telegram-tmux-control: task T-{task_id} failed: {message}", file=sys.stderr, flush=True)
                reply(chat_id, f"Task T-{task_id} failed: {message}\nUse /task resume {task_id} after reviewing it.")


TASKS = None
TASK_WORKER = None


def tmux(*args, input_text=None):
    return subprocess.run(["tmux", *args], input=input_text, text=True, capture_output=True, timeout=15)


def screen(lines=120):
    result = tmux("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", SESSION)
    if result.returncode:
        return "tmux session is unavailable: " + result.stderr.strip()
    output = result.stdout.strip() or "(terminal is blank)"
    # Telegram messages are capped at 4096 characters.
    return output[-3800:]


def delayed_screen(chat_id):
    """Give interactive tools time to respond, without pausing Telegram polling."""
    time.sleep(20)
    try:
        reply(chat_id, screen(lines=30))
    except Exception as error:
        print(f"telegram-tmux-control: delayed reply failed: {error}", file=sys.stderr, flush=True)


def change_summary(change):
    """Return a compact filename and added/removed line counts from a patch."""
    path = change.get("path", "")
    added = removed = 0
    for line in change.get("diff", "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return f"✏️ Changed\n{os.path.basename(path)}  +{added} -{removed}"


def run_remote_control(chat_id, prompt):
    """Run a Telegram request in the independent direct app-server thread."""
    try:
        def notify_file_changes(changes):
            for change in changes:
                reply(chat_id, change_summary(change))

        def notify_agent_message(text):
            reply(chat_id, text)

        answer = CODEX_RPC.run(
            prompt,
            on_file_changes=notify_file_changes,
            on_agent_message=notify_agent_message,
        )
        if answer != "Codex completed without a text response.":
            reply(chat_id, answer)
    except Exception as error:
        print(f"telegram-tmux-control: app-server request failed: {error}", file=sys.stderr, flush=True)
        try:
            reply(chat_id, f"Codex API request failed: {error}")
        except Exception as reply_error:
            print(f"telegram-tmux-control: failure reply failed: {reply_error}", file=sys.stderr, flush=True)


def send_terminal(text):
    typed = tmux("send-keys", "-t", SESSION, "-l", text)
    entered = tmux("send-keys", "-t", SESSION, "Enter")
    return typed.returncode == 0 and entered.returncode == 0


def reply(chat_id, text):
    # Plain text avoids Telegram markup interpretation of terminal output.
    api("sendMessage", {"chat_id": chat_id, "text": text[:4096]})


def acknowledge(chat_id, message_id):
    """Mark an accepted Codex request without adding a separate chat message."""
    api("setMessageReaction", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": json.dumps([{"type": "emoji", "emoji": "🫡"}]),
    })


def start_remote_control(chat_id, message_id, prompt):
    """Acknowledge promptly, then run the request asynchronously."""
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-tmux-control: reaction failed: {error}", file=sys.stderr, flush=True)
    threading.Thread(target=run_remote_control, args=(chat_id, prompt), daemon=True).start()


def human_duration(seconds):
    """Convert a number of seconds to a short human-readable string."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    seconds %= 60
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours = minutes // 60
    minutes %= 60
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days = hours // 24
    hours %= 24
    if days < 30:
        return f"{days}d {hours}h" if hours else f"{days}d"
    months = days // 30
    days %= 30
    return f"{months}mo {days}d" if days else f"{months}mo"


def task_summary(task, detail=False):
    if not task:
        return "Task not found."
    age = max(0, int(time.time()) - task['created_at'])
    text = f"T-{task['id']} {task['status']} ({human_duration(age)})"
    if detail:
        text += f"\nRequest: {task['prompt'][:700]}"
        if task['checkpoint']:
            text += f"\nLatest update: {task['checkpoint'][-1800:]}"
        if task['error']:
            text += f"\nError: {task['error'][-1000:]}"
    return text


def task_id_from(command, prefix):
    value = command[len(prefix):].strip()
    if value.isdigit() and int(value) > 0:
        return int(value)
    return None


def start_task(chat_id, message_id, prompt):
    try:
        acknowledge(chat_id, message_id)
    except Exception as error:
        print(f"telegram-tmux-control: reaction failed: {error}", file=sys.stderr, flush=True)
    try:
        task = TASKS.create(chat_id, prompt)
        TASK_WORKER.notify()
        reply(chat_id, f"Task T-{task['id']} queued. Use /task status {task['id']} to follow it.")
    except Exception as error:
        reply(chat_id, f"Unable to queue task: {error}")


def permitted(chat_id, state):
    return str(state.get("chat_id", "")) == str(chat_id)


def handle(message, state):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text")
    if chat_id is None or not text:
        return
    command = text.strip()
    if not state.get("chat_id"):
        if command.startswith("/pair ") and command[6:].strip() == CFG["PAIR_CODE"]:
            state["chat_id"] = chat_id
            reply(chat_id, "Paired. Send text for the direct Codex API. Use /tmux <text> for the terminal bridge. /help lists commands.")
        else:
            reply(chat_id, "This private bot needs pairing. Send: /pair <your pairing code>")
        return
    if not permitted(chat_id, state):
        # Do not reveal that a controller exists to unapproved chats.
        return
    if command in ("/start", "/help"):
        reply(chat_id, "Normal text: direct Codex request. /rc <prompt>: explicit request. /task <prompt>: durable long-running task. /tasks; /task status|pause|resume|cancel <id>. /tmux <text>, /screen, /status, /interrupt.")
    elif command == "/tasks":
        tasks = TASKS.recent(6)
        reply(chat_id, "No tasks yet." if not tasks else "\n".join(task_summary(task) for task in tasks))
    elif command == "/task":
        reply(chat_id, "Usage: /task <prompt>, /task status <id>, /task pause <id>, /task resume <id>, or /task cancel <id>")
    elif command.startswith("/task status "):
        task_id = task_id_from(command, "/task status ")
        reply(chat_id, task_summary(TASKS.get(task_id), detail=True) if task_id else "Usage: /task status <id>")
    elif command.startswith("/task pause "):
        task_id = task_id_from(command, "/task pause ")
        task, changed = TASKS.pause(task_id) if task_id else (None, False)
        reply(chat_id, f"Task T-{task_id} paused." if changed else "Only queued tasks can be paused.")
    elif command.startswith("/task resume "):
        task_id = task_id_from(command, "/task resume ")
        task, changed = TASKS.resume(task_id) if task_id else (None, False)
        if changed:
            TASK_WORKER.notify()
        reply(chat_id, f"Task T-{task_id} queued for resume." if changed else "Only paused, interrupted, or failed tasks can be resumed.")
    elif command.startswith("/task cancel "):
        task_id = task_id_from(command, "/task cancel ")
        task, changed = TASKS.cancel(task_id) if task_id else (None, False)
        if changed:
            TASK_WORKER.notify()
        reply(chat_id, f"Task T-{task_id} cancellation requested." if changed else "Task cannot be cancelled.")
    elif command.startswith("/task "):
        prompt = command[6:].strip()
        if prompt:
            start_task(chat_id, message["message_id"], prompt)
        else:
            reply(chat_id, "Usage: /task <prompt>")
    elif command == "/screen":
        reply(chat_id, screen())
    elif command == "/status":
        result = tmux("has-session", "-t", SESSION)
        reply(chat_id, f"tmux session '{SESSION}': " + ("available" if result.returncode == 0 else "unavailable"))
    elif command == "/interrupt":
        result = tmux("send-keys", "-t", SESSION, "C-c")
        if result.returncode == 0:
            try:
                acknowledge(chat_id, message["message_id"])
            except Exception as error:
                print(f"telegram-tmux-control: reaction failed: {error}", file=sys.stderr, flush=True)
        else:
            reply(chat_id, "Unable to reach tmux.")
    elif command == "/tmux":
        reply(chat_id, "Usage: /tmux <text>")
    elif command.startswith("/tmux "):
        if send_terminal(command[6:]):
            threading.Thread(target=delayed_screen, args=(chat_id,), daemon=True).start()
        else:
            reply(chat_id, "Unable to reach the tmux session.")
    elif command == "/rc":
        reply(chat_id, "Usage: /rc <prompt>")
    elif command.startswith("/rc "):
        prompt = command[4:].strip()
        if prompt:
            start_remote_control(chat_id, message["message_id"], prompt)
        else:
            reply(chat_id, "Usage: /rc <prompt>")
    elif command.startswith("/"):
        reply(chat_id, "Unknown command. Use /help.")
    else:
        start_remote_control(chat_id, message["message_id"], text)


def executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK) or bool(shutil.which(path))


def check_requirements():
    """Check the local setup without entering the Telegram polling loop."""
    problems = []

    try:
        mode = stat.S_IMODE(os.stat(CONFIG_PATH).st_mode)
        if mode & 0o077:
            problems.append(f"config file is mode {mode:03o}, expected 600: {CONFIG_PATH}")
        else:
            print(f"OK: protected config file: {CONFIG_PATH}")
    except OSError as error:
        problems.append(f"cannot inspect config file {CONFIG_PATH}: {error}")

    for label, path in (("Codex", CODEX_BIN), ("Node", NODE_BIN)):
        if executable(path):
            print(f"OK: {label}: {path}")
        else:
            problems.append(f"{label} executable not found: {path}")
    if shutil.which("tmux"):
        print("OK: tmux is on PATH")
    else:
        problems.append("tmux is not on PATH")
    if os.path.isdir(WORKSPACE):
        print(f"OK: workspace: {WORKSPACE}")
    else:
        problems.append(f"workspace is not a directory: {WORKSPACE}")

    result = tmux("has-session", "-t", SESSION)
    if result.returncode:
        print(f"WARN: tmux session '{SESSION}' is unavailable", file=sys.stderr)
    else:
        print(f"OK: tmux session '{SESSION}'")
    try:
        bot = api("getMe")
        print(f"OK: Telegram bot: @{bot.get('username', '(no username)')}")
    except Exception as error:
        problems.append(f"Telegram API preflight failed: {error}")

    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        return 1
    print("Setup check passed.")
    return 0


def main():
    global CFG, API_PREFIX, TASKS, TASK_WORKER
    try:
        CFG = config()
    except (OSError, RuntimeError) as error:
        print(f"telegram-tmux-control: configuration failed: {error}", file=sys.stderr)
        return 1
    API_PREFIX = f"/bot{CFG['BOT_TOKEN']}/"
    if len(sys.argv) == 2 and sys.argv[1] == "--check":
        return check_requirements()
    if len(sys.argv) > 1:
        print("Usage: telegram-tmux-control.py [--check]", file=sys.stderr)
        return 2
    try:
        TASKS = TaskStore(TASK_STATE_PATH)
        TASK_WORKER = TaskWorker(TASKS)
        TASK_WORKER.start()
    except (OSError, sqlite3.Error) as error:
        print(f"telegram-tmux-control: task store failed: {error}", file=sys.stderr)
        return 1
    state = read_state()
    try:
        # Establish the sender's TLS connection before the first user message,
        # making the immediate 🫡 acknowledgement a reused connection.
        api("getMe")
    except Exception as error:
        print(f"telegram-tmux-control: sender preflight failed: {error}", file=sys.stderr, flush=True)
    while True:
        try:
            updates = api("getUpdates", {"offset": state.get("offset", 0), "timeout": 30, "allowed_updates": json.dumps(["message"])})
            for update in updates:
                state["offset"] = update["update_id"] + 1
                handle(update.get("message", {}), state)
                write_state(state)
        except KeyboardInterrupt:
            return
        except Exception as error:
            print(f"telegram-tmux-control: {error}", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
