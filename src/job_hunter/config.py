"""Load the search profile (config.yaml) and the company watchlist (companies.yaml)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

PACKAGE_DIR = Path(__file__).parent
DEFAULTS_DIR = PACKAGE_DIR / "defaults"


@dataclass
class Company:
    name: str
    ats: str = ""  # greenhouse | lever | ashby | workable | "" (board search only)
    token: str = ""
    region: str = ""  # "eu" selects the EU Greenhouse host
    enabled: bool = True


@dataclass
class Profile:
    name: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    github: str = ""
    answers: dict = field(default_factory=dict)  # notice period, salary, work authorisation...


@dataclass
class Settings:
    roles: list[str]
    role_keywords: list[str]
    exclude_keywords: list[str]
    locations: list[str]
    remote_regions: list[str]
    board_queries: list[str]
    fit_threshold: int = 60
    max_open_per_company: int = 2
    max_tailored_per_run: int = 15
    score_model: str = "claude-haiku-5-5"
    tailor_model: str = "claude-sonnet-5-5"
    jobspy_sites: list[str] = field(default_factory=lambda: ["linkedin", "indeed", "glassdoor"])
    profile: Profile = field(default_factory=Profile)
    companies: list[Company] = field(default_factory=list)
    data_dir: Path = Path("data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.sqlite"

    @property
    def master_cv_path(self) -> Path:
        return self.data_dir / "master_cv.yaml"


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_companies(path: Path) -> list[Company]:
    raw = _read_yaml(path).get("companies", [])
    return [Company(**item) for item in raw]


def load_settings(data_dir: Path | None = None) -> Settings:
    """Read data/config.yaml and data/companies.yaml, falling back to the bundled defaults."""
    data_dir = Path(data_dir or "data")
    cfg_path = data_dir / "config.yaml"
    comp_path = data_dir / "companies.yaml"
    cfg = _read_yaml(cfg_path if cfg_path.exists() else DEFAULTS_DIR / "config.yaml")
    companies = load_companies(comp_path if comp_path.exists() else DEFAULTS_DIR / "companies.yaml")
    profile = Profile(**cfg.pop("profile", {}))
    return Settings(**cfg, profile=profile, companies=companies, data_dir=data_dir)
