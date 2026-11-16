"""A user's editable YAML files and personal documents, with validation and safe paths.

Every path is resolved inside the user's data dir: a name from the browser can never
reach a sibling user's folder. YAML is checked by loading it the same way the daily run
does before it replaces the file, and the previous version is kept under .history/.
"""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import yaml

from job_hunter.config import Company, Profile, Settings
from job_hunter.users import DOC_FOLDERS

YAML_FILES = ("config.yaml", "companies.yaml", "master_cv.yaml")
HISTORY_KEEP = 10
MAX_DOC_BYTES = 15 * 1024 * 1024
MAX_ZIP_BYTES = 200 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ ()-]+")
SKIP_EXPORT = ("dry-run",)


class FileError(ValueError):
    """A user-facing problem with a file: shown on the page, never a 500."""


# --- YAML -------------------------------------------------------------------
def _check_config(data: dict) -> None:
    cfg = dict(data)
    Settings(**cfg, profile=Profile(**(cfg.pop("profile", None) or {})))


def _check_companies(data: dict) -> None:
    items = data.get("companies")
    if not isinstance(items, list):
        raise FileError("companies.yaml needs a top-level 'companies:' list")
    for item in items:
        Company(**item)


def _check_master(data: dict) -> None:
    if not data.get("name"):
        raise FileError("master_cv.yaml needs a 'name'")
    if not isinstance(data.get("experience", []), list):
        raise FileError("'experience' must be a list")


CHECKS = {
    "config.yaml": _check_config,
    "companies.yaml": _check_companies,
    "master_cv.yaml": _check_master,
}


def validate_yaml(name: str, text: str) -> dict:
    """Parse and check one editable file. Raises FileError with a readable message."""
    if name not in CHECKS:
        raise FileError(f"{name} is not an editable file")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FileError(f"YAML syntax error: {exc}") from exc
    if not isinstance(data, dict):
        raise FileError("the file must be a YAML mapping (key: value)")
    try:
        CHECKS[name](data)
    except TypeError as exc:  # unknown or missing keys in a dataclass
        raise FileError(f"unexpected content: {exc}") from exc
    return data


def read_yaml(data_dir: Path, name: str) -> str:
    if name not in YAML_FILES:
        raise FileError(f"{name} is not an editable file")
    path = data_dir / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _history_dir(data_dir: Path, name: str) -> Path:
    return data_dir / ".history" / name


def history(data_dir: Path, name: str) -> list[Path]:
    folder = _history_dir(data_dir, name)
    return sorted(folder.glob("*.yaml"), reverse=True) if folder.exists() else []


def save_yaml(data_dir: Path, name: str, text: str) -> None:
    validate_yaml(name, text)
    path = data_dir / name
    if path.exists():
        folder = _history_dir(data_dir, name)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copy(path, folder / f"{stamp}.yaml")
        for old in history(data_dir, name)[HISTORY_KEEP:]:
            old.unlink()
    path.write_text(text.replace("\r\n", "\n"), encoding="utf-8")


def undo_yaml(data_dir: Path, name: str) -> bool:
    """Put back the most recent saved version. False when there is none."""
    versions = history(data_dir, name)
    if not versions:
        return False
    shutil.move(versions[0], data_dir / name)
    return True


# --- documents --------------------------------------------------------------
def safe_name(filename: str) -> str:
    base = Path((filename or "").replace("\\", "/")).name
    cleaned = SAFE_NAME.sub("_", base).strip(" .")
    if not cleaned:
        raise FileError("that file name is not usable")
    return cleaned[:120]


def doc_path(data_dir: Path, folder: str, filename: str) -> Path:
    if folder not in DOC_FOLDERS:
        raise FileError(f"unknown folder {folder!r}")
    root = (data_dir / "files" / folder).resolve()
    path = (root / safe_name(filename)).resolve()
    if path.parent != root:
        raise FileError("invalid path")
    return path


def save_doc(data_dir: Path, folder: str, filename: str, content: bytes) -> Path:
    if len(content) > MAX_DOC_BYTES:
        raise FileError("files are limited to 15 MB")
    path = doc_path(data_dir, folder, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def list_docs(data_dir: Path) -> dict[str, list[dict]]:
    out = {}
    for folder in DOC_FOLDERS:
        root = data_dir / "files" / folder
        files = sorted(p for p in root.glob("*") if p.is_file()) if root.exists() else []
        out[folder] = [{"name": p.name, "size": p.stat().st_size} for p in files]
    return out


# --- export / import ----------------------------------------------------------
def export_zip(data_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(data_dir.rglob("*")):
            rel = path.relative_to(data_dir)
            if path.is_file() and rel.parts[0] not in SKIP_EXPORT:
                zf.write(path, rel.as_posix())
    return buf.getvalue()


def _members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if sum(i.file_size for i in infos) > MAX_ZIP_BYTES:
        raise FileError("the archive is larger than 200 MB once unpacked")
    for info in infos:
        name = info.filename
        is_link = (info.external_attr >> 28) == 0xA
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or is_link:
            raise FileError(f"unsafe path in archive: {name}")
    return infos


def _common_prefix(infos: list[zipfile.ZipInfo]) -> str:
    """Accept both a zip of the folder's contents and a zip of the folder itself."""
    tops = {Path(i.filename).parts[0] for i in infos}
    names = {Path(i.filename).name for i in infos}
    if len(tops) == 1 and not (tops & names):
        return tops.pop() + "/"
    return ""


def import_zip(data_dir: Path, content: bytes) -> int:
    """Unpack an exported data folder over the user's dir. Returns the number of files."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise FileError("that is not a zip file") from exc
    with zf:
        infos = _members(zf)
        prefix = _common_prefix(infos)
        root = data_dir.resolve()
        for info in infos:
            rel = info.filename[len(prefix) :]
            target = (root / rel).resolve()
            if root not in target.parents:
                raise FileError(f"unsafe path in archive: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
    return len(infos)


def resolve_in(data_dir: Path, stored: str) -> Path | None:
    """Map a stored absolute path (maybe from another machine) to a file inside data_dir.

    Paths recorded before a move keep their 'applications/NNNNN/...' tail, which is
    re-rooted here. Anything that would land outside the user's dir is refused.
    """
    if not stored:
        return None
    root = data_dir.resolve()
    path = Path(stored)
    candidates = [path if path.is_absolute() else root / path]
    parts = path.parts
    if "applications" in parts:
        candidates.append(root.joinpath(*parts[parts.index("applications") :]))
    for cand in candidates:
        real = cand.resolve()
        if root in real.parents and real.exists():
            return real
    return None
