"""macOS launchd job: run the pipeline every weekday at 08:00 local (Paris) time."""

from __future__ import annotations

import plistlib
import shutil
from pathlib import Path

LABEL = "io.jobhunter.daily"


def plist(project_dir: Path, uv: str, hour: int = 8, minute: int = 0) -> bytes:
    data = {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--project", str(project_dir), "job-hunter", "run"],
        "WorkingDirectory": str(project_dir),
        "StartCalendarInterval": [
            {"Weekday": day, "Hour": hour, "Minute": minute} for day in range(1, 6)
        ],
        "StandardOutPath": str(project_dir / "data" / "logs" / "daily.log"),
        "StandardErrorPath": str(project_dir / "data" / "logs" / "daily.log"),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:"
            + str(Path.home() / ".local" / "bin")
        },
    }
    return plistlib.dumps(data)


def install(project_dir: Path, agents_dir: Path | None = None) -> Path:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "data" / "logs").mkdir(parents=True, exist_ok=True)
    path = agents_dir / f"{LABEL}.plist"
    path.write_bytes(plist(project_dir.resolve(), uv))
    return path
