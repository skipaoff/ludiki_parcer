"""
VFP: Validated, immutable bootstrap settings of the terminal loaded from config.toml.
Changes when: a component needs a new non-secret knob at startup.
Anti-goal:
1. Secrets in settings — keys and passwords come only from the keystore.
2. Silently accepting typos — unknown keys fail loudly instead of falling back to defaults.
3. Trading parameters edited from the interface — those are stored with history in the database (PLAN.md, section 13).
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = REPO_ROOT / "config.toml"
EXAMPLE_FILE = REPO_ROOT / "config.example.toml"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSettings(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    open_browser: bool = True
    dev_ui_port: int = Field(default=5173, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value not in LOOPBACK_HOSTS:
            raise ValueError("the terminal listens on loopback only (127.0.0.1, localhost or ::1)")
        return value


class PathSettings(_Section):
    data_dir: Path = Path("../ludik-data")
    spool_dir: Path = Path("../ludik-data/spool")
    logs_dir: Path = Path("../ludik-data/logs")
    web_dist: Path = Path("web/dist")


class DatabaseSettings(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = "ludik"
    user: str = "ludik"
    autostart: bool = True
    pg_bin_dir: Path = Path("../ludik-data/pgsql/bin")
    pg_data_dir: Path = Path("../ludik-data/pgdata")
    connect_timeout_s: float = Field(default=5, gt=0)


class StorageSettings(_Section):
    flush_interval_ms: int = Field(default=300, ge=50, le=5000)
    max_batch_rows: int = Field(default=5000, ge=1)
    reconnect_interval_s: float = Field(default=5, gt=0)


class UiSettings(_Section):
    snapshot_hz: float = Field(default=5, gt=0, le=20)
    heartbeat_interval_s: float = Field(default=5, gt=0)
    heartbeat_timeout_s: float = Field(default=15, gt=0)
    journal_buffer: int = Field(default=500, ge=10)


class BinanceSettings(_Section):
    enabled: bool = True
    demo: bool = False


class MexcSettings(_Section):
    enabled: bool = True


class ExchangesSettings(_Section):
    probe_interval_s: float = Field(default=10, ge=2)
    request_timeout_s: float = Field(default=10, gt=0)
    clock_warning_ms: int = Field(default=1000, ge=100)
    binance: BinanceSettings = BinanceSettings()
    mexc: MexcSettings = MexcSettings()


class InstrumentsSettings(_Section):
    refresh_interval_s: float = Field(default=3600, ge=60)
    max_price_gap_pct: Decimal = Field(default=Decimal("20"), gt=0)
    max_index_gap_pct: Decimal = Field(default=Decimal("1"), gt=0)


class LoggingSettings(_Section):
    level: str = "INFO"


class Settings(_Section):
    server: ServerSettings = ServerSettings()
    paths: PathSettings = PathSettings()
    database: DatabaseSettings = DatabaseSettings()
    storage: StorageSettings = StorageSettings()
    ui: UiSettings = UiSettings()
    exchanges: ExchangesSettings = ExchangesSettings()
    instruments: InstrumentsSettings = InstrumentsSettings()
    logging: LoggingSettings = LoggingSettings()


def _resolve(path: Path, base: Path) -> Path:
    return path if path.is_absolute() else (base / path).resolve()


def load_settings(path: Path | None = None) -> Settings:
    """
    Read settings from path, else config.toml, else config.example.toml.
    Relative paths are resolved against the directory of the file that was read.
    """
    source = path or (CONFIG_FILE if CONFIG_FILE.exists() else EXAMPLE_FILE)
    with source.open("rb") as handle:
        raw = tomllib.load(handle)
    settings = Settings.model_validate(raw)
    base = source.resolve().parent
    return settings.model_copy(
        update={
            "paths": settings.paths.model_copy(
                update={name: _resolve(value, base) for name, value in settings.paths}
            ),
            "database": settings.database.model_copy(
                update={
                    "pg_bin_dir": _resolve(settings.database.pg_bin_dir, base),
                    "pg_data_dir": _resolve(settings.database.pg_data_dir, base),
                }
            ),
        }
    )
