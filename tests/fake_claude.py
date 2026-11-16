"""Install a fake `claude` CLI on PATH for tests, so they never touch a real Claude login.

The fake reads FAKE_CLAUDE_* env vars to decide what to print. Its login mimics the real
2.1.x flow: call $BROWSER with a localhost-callback URL, print the manual URL inside an OSC8
hyperlink, show "Paste code here if prompted >" and accept only the code "good-code".
"""

from __future__ import annotations

import sys
from pathlib import Path

LOCAL = "https://claude.com/cai/oauth/authorize?code=true&redirect_uri=http%3A%2F%2Flocalhost%3A5555%2Fcallback&state=s"
MANUAL = "https://claude.com/cai/oauth/authorize?code=true&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=s"

SCRIPT = r"""
import json, os, subprocess, sys, time
args = sys.argv[1:]
env = os.environ
if args == ["--version"]:
    print("9.9.9 (Claude Code)")
elif args[:2] == ["auth", "status"]:
    data = json.loads(env.get("FAKE_CLAUDE_STATUS") or '{"loggedIn": false}')
    print(json.dumps(data))
    sys.exit(0 if data.get("loggedIn") else 1)
elif args[:2] == ["auth", "logout"]:
    print("Successfully logged out")
elif args[:2] == ["auth", "login"]:
    if env.get("FAKE_CLAUDE_HANG"):
        time.sleep(30)
    subprocess.run([env["BROWSER"], LOCAL])
    sys.stdout.write("Opening browser to sign in…\r\n")
    link = "\x1b]8;;" + MANUAL + "\x07" + MANUAL + "\x1b]8;;\x07"
    sys.stdout.write("If the browser didn't open, visit: " + link + "\r\n")
    sys.stdout.write("\x1b[?25hPaste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "good-code":
        print("Login successful.")
    else:
        print("Login failed: That code wasn't accepted (" + code + ")")
        sys.exit(1)
"""


def install(folder: Path, monkeypatch) -> Path:
    """Write the fake as `claude` in `folder` and put it first on PATH."""
    exe = folder / "claude"
    head = f"#!{sys.executable}\nLOCAL = {LOCAL!r}\nMANUAL = {MANUAL!r}\n"
    exe.write_text(head + SCRIPT)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}:/usr/bin:/bin")
    monkeypatch.delenv("FAKE_CLAUDE_STATUS", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_HANG", raising=False)
    return exe
