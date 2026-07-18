#!/usr/bin/env python3
"""Private Telegram controller for tmux and the local Codex app-server API."""
import http.client
import json
import os
import select
import shutil
import stat
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
        self.lock = threading.Lock()
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

    def run(self, prompt):
        with self.lock:
            self._start()
            result = self._request("turn/start", {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt}]})
            turn_id = result["turn"]["id"]
            deadline = time.monotonic() + 600
            answer = ""
            while True:
                message = self._receive(max(0.1, deadline - time.monotonic()))
                if message.get("method") == "item/agentMessage/delta":
                    answer += self._text_from(message.get("params", {}))
                elif message.get("method") == "item/completed":
                    item = message.get("params", {}).get("item", {})
                    if item.get("type") == "agentMessage" and not answer:
                        answer = self._text_from(item)
                elif message.get("method") == "turn/completed":
                    turn = message.get("params", {}).get("turn", {})
                    if turn.get("id") == turn_id:
                        if turn.get("status") != "completed":
                            raise RuntimeError(f"Codex turn ended with status: {turn.get('status', 'unknown')}")
                        return answer.strip() or "Codex completed without a text response."


CODEX_RPC = CodexAppServer()


class TelegramApi:
    """Small keep-alive Telegram client; one instance per request lane."""

    def __init__(self):
        self.connection = None
        self.lock = threading.Lock()

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


def run_remote_control(chat_id, prompt):
    """Run a Telegram request in the independent direct app-server thread."""
    try:
        answer = CODEX_RPC.run(prompt)
        reply(chat_id, answer)
    except Exception as error:
        print(f"telegram-tmux-control: app-server request failed: {error}", file=sys.stderr, flush=True)
        reply(chat_id, f"Codex API request failed: {error}")


def send_terminal(text):
    typed = tmux("send-keys", "-t", SESSION, "-l", text)
    entered = tmux("send-keys", "-t", SESSION, "Enter")
    return typed.returncode == 0 and entered.returncode == 0


def reply(chat_id, text):
    # Plain text avoids Telegram markup interpretation of terminal output.
    api("sendMessage", {"chat_id": chat_id, "text": text[:4096]})


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
        reply(chat_id, "Normal text: direct Codex app-server API. /rc <prompt>: explicit API prompt. /tmux <text>: terminal input, then last 30 lines after 20 seconds. /screen, /status, /interrupt.")
    elif command == "/screen":
        reply(chat_id, screen())
    elif command == "/status":
        result = tmux("has-session", "-t", SESSION)
        reply(chat_id, f"tmux session '{SESSION}': " + ("available" if result.returncode == 0 else "unavailable"))
    elif command == "/interrupt":
        result = tmux("send-keys", "-t", SESSION, "C-c")
        reply(chat_id, "Sent Ctrl-C." if result.returncode == 0 else "Unable to reach tmux.")
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
            reply(chat_id, "🫡")
            threading.Thread(target=run_remote_control, args=(chat_id, prompt), daemon=True).start()
        else:
            reply(chat_id, "Usage: /rc <prompt>")
    elif command.startswith("/"):
        reply(chat_id, "Unknown command. Use /help.")
    else:
        reply(chat_id, "🫡")
        threading.Thread(target=run_remote_control, args=(chat_id, text), daemon=True).start()


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
    global CFG, API_PREFIX
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
