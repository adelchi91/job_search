"""The admin's Claude Code account: read its status, and drive `claude auth login`.

The app never handles Anthropic OAuth or tokens itself. It runs the CLI's own login command
in a pseudo-terminal and relays two things to the admin's browser:
- the authorize URL the CLI hands to $BROWSER (localhost callback: works when the browser runs
  on this machine, i.e. local mode), captured by pointing $BROWSER at a tiny script;
- the manual authorize URL it prints (`code=true`): claude.ai then shows a code which the
  admin pastes back and we type into the CLI's prompt (server mode).
Credentials stay wherever the CLI keeps them (CLAUDE_CONFIG_DIR, or the macOS keychain).
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import subprocess
import tempfile
import termios
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

STATUS_TTL = 30.0
LOGIN_TIMEOUT = 600.0
LOG_LINES = 300
ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (e.g. OSC8 hyperlinks)
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[=>()][0-9A-Za-z]?"
)
AUTH_URL = re.compile(r"https://[^\s\"'<>]+/oauth/authorize\?[^\s\"'<>]+")
CODE_PROMPT = re.compile(r"paste code", re.I)
PLANS = {"max": "Max", "pro": "Pro", "team": "Team", "enterprise": "Enterprise"}


def binary() -> str | None:
    return shutil.which("claude")


def _cli(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess | None:
    exe = binary()
    if not exe:
        return None
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


@dataclass(frozen=True)
class ClaudeStatus:
    installed: bool
    logged_in: bool = False
    email: str = ""
    plan: str = ""
    org: str = ""
    method: str = ""
    expires_at: str = ""
    version: str = ""
    expired: bool = False

    @property
    def label(self) -> str:
        if not self.installed:
            return "Claude CLI not installed"
        if self.expired:
            return "Login expired — re-authenticate"
        if not self.logged_in:
            return "Not signed in"
        plan = f" ({self.plan})" if self.plan else ""
        return f"Signed in as {self.email or 'unknown account'}{plan}"

    def as_dict(self) -> dict:
        return {**asdict(self), "label": self.label}


def _plan(data: dict) -> str:
    raw = str(data.get("subscriptionType") or "").lower()
    return PLANS.get(raw, raw.capitalize())


def _past(stamp: str) -> bool:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) < datetime.now(UTC)
    except ValueError:
        return False


def _had_login(data: dict) -> bool:
    """A credentials file left behind while signed out means the login lapsed (Linux)."""
    folder = data.get("configDirectory") or ""
    return bool(folder) and (Path(folder) / ".credentials.json").exists()


def parse_status(data: dict, version: str = "", auth_failed: bool = False) -> ClaudeStatus:
    logged_in = bool(data.get("loggedIn"))
    expires = str(data.get("loginExpiresAt") or "")
    lapsed = (not logged_in and (_had_login(data) or (expires and _past(expires)))) or auth_failed
    return ClaudeStatus(
        installed=True,
        logged_in=logged_in and not auth_failed,
        email=str(data.get("email") or ""),
        plan=_plan(data),
        org=str(data.get("orgName") or ""),
        method=str(data.get("authMethod") or ""),
        expires_at=expires,
        version=version,
        expired=bool(lapsed),
    )


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: ClaudeStatus | None = None
        self.at = 0.0
        self.auth_failed = False


_cache = _Cache()


def cli_version() -> str:
    out = _cli(["--version"], timeout=10)
    return out.stdout.strip().split(" ")[0] if out and out.returncode == 0 else ""


def _read_status() -> ClaudeStatus:
    out = _cli(["auth", "status", "--json"])
    if out is None:
        return ClaudeStatus(installed=binary() is not None)
    try:
        data = json.loads(out.stdout or "{}")  # exit code is 1 when signed out
    except json.JSONDecodeError:
        data = {}
    return parse_status(data, cli_version(), _cache.auth_failed)


def status(force: bool = False) -> ClaudeStatus:
    """The CLI's account status, cached for STATUS_TTL seconds."""
    with _cache.lock:
        fresh = _cache.value and time.monotonic() - _cache.at < STATUS_TTL
        if fresh and not force:
            return _cache.value
    value = _read_status()
    with _cache.lock:
        _cache.value, _cache.at = value, time.monotonic()
    return value


def invalidate() -> None:
    with _cache.lock:
        _cache.value = None


def mark_auth_failed() -> None:
    """A `claude -p` call was refused: show "login expired" until the next sign-in."""
    with _cache.lock:
        _cache.auth_failed, _cache.value = True, None


def logout() -> bool:
    out = _cli(["auth", "logout"])
    invalidate()
    return bool(out and out.returncode == 0)


def clean(text: str) -> str:
    return re.sub(r"\r+\n", "\n", ANSI.sub("", text)).replace("\r", "\n")


BROWSER_SCRIPT = '#!/bin/sh\nprintf "%s\\n" "$1" >> "$(dirname "$0")/urls"\n'


class LoginSession:
    """One `claude auth login` run in a pty. Thread-safe; read it through snapshot()."""

    def __init__(self, exe: str, timeout: float = LOGIN_TIMEOUT, args: list[str] | None = None):
        self.exe, self.timeout = exe, timeout
        self.args = args if args is not None else ["auth", "login", "--claudeai"]
        self.state = "starting"
        self.lines: deque[str] = deque(maxlen=LOG_LINES)
        self.partial = ""
        self._held = ""  # a trailing "\r" waits for the next chunk (it may start with "\n")
        self.manual_url = ""
        self.prompted = False
        self.started = time.monotonic()
        self._secret = ""
        self._lock = threading.Lock()
        self._tmp = tempfile.TemporaryDirectory(prefix="jh-claude-login-")
        self._fd = -1
        self.proc: subprocess.Popen | None = None

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> LoginSession:
        browser = Path(self._tmp.name) / "browser"
        browser.write_text(BROWSER_SCRIPT)
        browser.chmod(0o700)
        env = {**os.environ, "BROWSER": str(browser), "NO_COLOR": "1", "TERM": "dumb"}
        master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO  # never echo the pasted code back into the log
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        self.proc = subprocess.Popen(
            [self.exe, *self.args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        self._fd = master
        threading.Thread(target=self._pump, name="claude-login", daemon=True).start()
        return self

    @property
    def running(self) -> bool:
        return self.state in ("starting", "waiting", "verifying")

    def _pump(self) -> None:
        import select

        deadline = self.started + self.timeout
        while self.proc and self.proc.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self._fd], [], [], 0.25)
            if ready and not self._read_once():
                break
        while self._read_once(block=False):
            pass
        self._finish(timed_out=time.monotonic() >= deadline)

    def _read_once(self, block: bool = True) -> bool:
        if not block:
            import select

            if not select.select([self._fd], [], [], 0)[0]:
                return False
        try:
            data = os.read(self._fd, 4096)
        except OSError:
            return False
        if data:
            self.feed(data.decode("utf-8", "replace"))
        return bool(data)

    def _finish(self, timed_out: bool) -> None:
        proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()
        code = proc.wait(timeout=5) if proc else 1
        with self._lock:
            if self.state != "cancelled":
                self.state = "timed_out" if timed_out else ("succeeded" if code == 0 else "failed")
        if self.state == "succeeded":
            with _cache.lock:
                _cache.auth_failed = False
        invalidate()
        os.close(self._fd)
        self._tmp.cleanup()

    def cancel(self) -> None:
        with self._lock:
            if not self.running:
                return
            self.state = "cancelled"
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    # -- output ------------------------------------------------------------------------
    def _mask(self, text: str) -> str:
        return text.replace(self._secret, "[code]") if self._secret else text

    def feed(self, chunk: str) -> None:
        with self._lock:
            raw, self._held = self._held + chunk, ""
            if raw.endswith("\r"):
                raw, self._held = raw[:-1], "\r"
            *done, self.partial = (self.partial + clean(raw)).split("\n")
            for line in done:
                self.lines.append(self._mask(line))
            for text in [*done, self.partial]:
                self._scan(text)

    def _scan(self, text: str) -> None:
        url = AUTH_URL.search(text)
        if url and not self.manual_url:
            self.manual_url = url.group(0)
        if CODE_PROMPT.search(text):
            self.prompted = True
        if (self.manual_url or self.prompted) and self.state == "starting":
            self.state = "waiting"

    def local_url(self) -> str:
        """The localhost-callback URL the CLI tried to open in a browser, if any."""
        try:
            urls = (Path(self._tmp.name) / "urls").read_text().split()
        except OSError:
            return ""
        return next((u for u in urls if AUTH_URL.fullmatch(u)), "")

    def submit_code(self, code: str) -> None:
        code = code.strip()
        if not code or any(c in code for c in "\r\n\x1b") or len(code) > 512:
            raise ValueError("paste the code exactly as claude.ai shows it")
        with self._lock:
            if self.state not in ("starting", "waiting"):
                raise ValueError("no sign-in is waiting for a code")
            self._secret, self.state = code, "verifying"
            if self.partial:
                self.lines.append(self.partial)
                self.partial = ""
            self.lines.append("[code submitted]")
        os.write(self._fd, code.encode() + b"\r")

    def snapshot(self) -> dict:
        with self._lock:
            log = [*self.lines, self._mask(self.partial)] if self.partial else list(self.lines)
            return {
                "state": self.state,
                "running": self.running,
                "manual_url": self.manual_url,
                "local_url": self.local_url() if self.running else "",
                "prompted": self.prompted,
                "log": log,
            }


class _Login:
    lock = threading.Lock()
    current: LoginSession | None = None


def current() -> LoginSession | None:
    return _Login.current


def start_login(timeout: float = LOGIN_TIMEOUT) -> LoginSession:
    """Start `claude auth login`, or return the run already in progress (one per process)."""
    exe = binary()
    if not exe:
        raise RuntimeError("the claude CLI is not installed on this server")
    with _Login.lock:
        if _Login.current and _Login.current.running:
            return _Login.current
        _Login.current = LoginSession(exe, timeout).start()
        return _Login.current
