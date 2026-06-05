"""Application settings, loaded from environment + ``.env`` via pydantic-settings.

Conventions:

* ``NFL_LEAGUE_ID`` and ``NFL_COOKIE`` are *required* — missing either
  raises a single readable error pointing the user at ``.env.example``.
* ``NFL_COOKIE`` is wrapped in ``SecretStr`` so it never accidentally
  leaks via ``repr`` / log formatting / FastAPI debug pages.
* Paths are resolved relative to the project root if given as relative
  strings, so ``./data/fantasy.db`` always means the same file no matter
  what the current working directory is when commands are invoked.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SettingsError(RuntimeError):
    """Raised when ``.env``/environment is misconfigured. Carries an
    actionable message that the CLI surfaces to the user verbatim."""


class Settings(BaseSettings):
    """All application configuration. One source of truth."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Required: league identity ---
    nfl_league_id: str
    nfl_cookie: SecretStr
    league_start_year: int

    # --- Database ---
    database_url: str = "sqlite:///./data/fantasy.db"

    # --- API server ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_dir: Path = Path("./data/logs")

    # --- Rate limiting ---
    nfl_com_delay_seconds: float = 2.0
    sleeper_requests_per_min: int = 120

    # --- Caches ---
    nflverse_cache_dir: Path = Path("./data/nflverse_cache")

    # --- Media (content-addressed avatar store) ---
    # Raw avatar bytes land here (gitignored); the ``assets`` table holds the
    # metadata. The read API streams bytes from this root via /assets/{id}.
    assets_dir: Path = Path("./data/assets")

    # --- Tuning ---
    scoring_verify_tolerance: float = 0.1
    save_raw_html: bool = False

    # --- Scope ---
    # Positions this league can actually roster. nflverse returns the entire
    # NFL player universe (every IDP, lineman, long-snapper, etc.); we only
    # ingest player metadata for these. Team defense ("DEF") is synthesized
    # separately, not emitted by nflverse load_players, but is kept here so
    # the set is the single source of truth for "rosterable in this league".
    # Override via RELEVANT_POSITIONS as a comma-separated string.
    relevant_positions: str = "QB,RB,WR,TE,K,DEF"

    # ---- Validators ----

    @field_validator("nfl_league_id")
    @classmethod
    def _league_id_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("NFL_LEAGUE_ID is empty")
        return v.strip()

    @field_validator("nfl_cookie")
    @classmethod
    def _cookie_not_blank(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().strip():
            raise ValueError("NFL_COOKIE is empty")
        return v

    @field_validator("league_start_year")
    @classmethod
    def _reasonable_start_year(cls, v: int) -> int:
        if v < 1999 or v > 2100:
            raise ValueError(f"LEAGUE_START_YEAR={v} is outside the supported range (1999..2100)")
        return v

    @field_validator("log_dir", "nflverse_cache_dir", "assets_dir")
    @classmethod
    def _resolve_path(cls, v: Path) -> Path:
        if not v.is_absolute():
            return (PROJECT_ROOT / v).resolve()
        return v

    @property
    def relevant_positions_set(self) -> frozenset[str]:
        """``relevant_positions`` parsed into an upper-cased frozenset."""
        return frozenset(p.strip().upper() for p in self.relevant_positions.split(",") if p.strip())


def load_settings() -> Settings:
    """Load settings, converting pydantic's verbose error into a single
    actionable message that calls out the missing required vars first."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        lines = ["Configuration error — check your .env (see .env.example):"]
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            msg = err["msg"]
            lines.append(f"  - {loc.upper()}: {msg}")
        raise SettingsError("\n".join(lines)) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor; the CLI / FastAPI both pull from here."""
    return load_settings()
