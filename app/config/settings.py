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


class GateSettings(_Section):
    enabled: bool = True


class AsterSettings(_Section):
    enabled: bool = True


class BingxSettings(_Section):
    enabled: bool = True


class BybitSettings(_Section):
    enabled: bool = True


class BitgetSettings(_Section):
    enabled: bool = True


class KucoinSettings(_Section):
    enabled: bool = True


class HyperliquidSettings(_Section):
    enabled: bool = True


class VariationalSettings(_Section):
    """Variational Omni publishes market statistics only; there is no trading API yet, so it is read-only."""

    enabled: bool = True
    poll_ms: int = Field(default=2000, ge=1000)  # public limit: 10 requests per 10 seconds per IP
    # The public statistics are served from a cache: quotes were 41-105 s old, median about a minute (14.09.2026).
    # Comparing minute-old prices with live books produces fake gaps, so they stay "stale" until fresher data exists.
    max_quote_age_ms: int = Field(default=30000, ge=1000)


# The order fixes which exchange is leg "a" of a pair key, so new exchanges are only ever appended before variational.
EXCHANGE_ORDER = ("binance", "mexc", "gate", "aster", "bingx", "bybit", "bitget", "kucoin", "hyperliquid", "variational")
READ_ONLY_EXCHANGES = frozenset({"variational"})


class ExchangesSettings(_Section):
    probe_interval_s: float = Field(default=10, ge=2)
    request_timeout_s: float = Field(default=10, gt=0)
    clock_warning_ms: int = Field(default=1000, ge=100)
    binance: BinanceSettings = BinanceSettings()
    mexc: MexcSettings = MexcSettings()
    gate: GateSettings = GateSettings()
    aster: AsterSettings = AsterSettings()
    bingx: BingxSettings = BingxSettings()
    bybit: BybitSettings = BybitSettings()
    bitget: BitgetSettings = BitgetSettings()
    kucoin: KucoinSettings = KucoinSettings()
    hyperliquid: HyperliquidSettings = HyperliquidSettings()
    variational: VariationalSettings = VariationalSettings()

    def enabled_names(self) -> list[str]:
        return [name for name in EXCHANGE_ORDER if getattr(self, name).enabled]


class InstrumentsSettings(_Section):
    refresh_interval_s: float = Field(default=3600, ge=60)
    max_price_gap_pct: Decimal = Field(default=Decimal("20"), gt=0)
    max_index_gap_pct: Decimal = Field(default=Decimal("1"), gt=0)


class FeedSettings(_Section):
    size_usd: Decimal = Field(default=Decimal("100"), gt=0)
    min_roi_pct: Decimal = Decimal("0.50")
    candidate_margin_pct: Decimal = Field(default=Decimal("0.50"), ge=0)
    book_limit: int = Field(default=60, ge=2, le=200)
    tracking_limit: int = Field(default=20, ge=0, le=200)
    radar_rows: int = Field(default=20, ge=0, le=100)  # coins in the radar, each with up to RADAR_PAIRS_PER_TOKEN pairs
    funding_horizon_h: Decimal = Field(default=Decimal("8"), ge=1, le=168)  # funding counted into the expected result
    fresh_ms: int = Field(default=1000, ge=100)
    quiet_book_max_ms: int = Field(default=10000, ge=1000)
    resubscribe_after_ms: int = Field(default=5000, ge=1000)
    tick_ms: int = Field(default=200, ge=50)
    enter_after_ms: int = Field(default=45000, ge=0)
    exit_hysteresis_pct: Decimal = Field(default=Decimal("0.10"), ge=0)
    exit_after_ms: int = Field(default=2000, ge=0)
    default_taker_fee_binance_pct: Decimal = Field(default=Decimal("0.05"), ge=0)
    default_taker_fee_mexc_pct: Decimal = Field(default=Decimal("0.05"), ge=0)
    default_taker_fee_gate_pct: Decimal = Field(default=Decimal("0.075"), ge=0)
    default_taker_fee_aster_pct: Decimal = Field(default=Decimal("0.035"), ge=0)
    default_taker_fee_bingx_pct: Decimal = Field(default=Decimal("0.05"), ge=0)
    default_taker_fee_bybit_pct: Decimal = Field(default=Decimal("0.055"), ge=0)
    default_taker_fee_bitget_pct: Decimal = Field(default=Decimal("0.06"), ge=0)
    default_taker_fee_kucoin_pct: Decimal = Field(default=Decimal("0.06"), ge=0)
    default_taker_fee_hyperliquid_pct: Decimal = Field(default=Decimal("0.045"), ge=0)
    default_taker_fee_variational_pct: Decimal = Field(default=Decimal("0"), ge=0)
    mexc_ticker_poll_ms: int = Field(default=1000, ge=500)
    gate_ticker_poll_ms: int = Field(default=1000, ge=500)
    bingx_ticker_poll_ms: int = Field(default=1000, ge=500)
    bingx_premium_poll_ms: int = Field(default=5000, ge=1000)
    bybit_ticker_poll_ms: int = Field(default=1000, ge=500)
    bitget_ticker_poll_ms: int = Field(default=1000, ge=500)
    kucoin_ticker_poll_ms: int = Field(default=1000, ge=500)
    hyperliquid_poll_ms: int = Field(default=2000, ge=1000)  # weight 20 of 1,200 a minute per IP

    def default_taker_fee_pct(self, exchange: str) -> Decimal:
        return getattr(self, f"default_taker_fee_{exchange}_pct")


class PortfolioSettings(_Section):
    positions_poll_s: float = Field(default=5, ge=1)
    balances_poll_s: float = Field(default=60, ge=10)
    funding_poll_s: float = Field(default=300, ge=60)
    liquidation_warning_pct: Decimal = Field(default=Decimal("10"), gt=0)


class TradingSettings(_Section):
    enabled: bool = False
    leverage_binance: int = Field(default=3, ge=1, le=50)
    leverage_mexc: int = Field(default=3, ge=1, le=50)
    leverage_gate: int = Field(default=3, ge=1, le=50)
    leverage_aster: int = Field(default=3, ge=1, le=50)
    leverage_bingx: int = Field(default=3, ge=1, le=50)
    leverage_bybit: int = Field(default=3, ge=1, le=50)
    leverage_bitget: int = Field(default=3, ge=1, le=50)
    leverage_kucoin: int = Field(default=3, ge=1, le=50)
    leverage_hyperliquid: int = Field(default=3, ge=1, le=50)

    def leverage(self, exchange: str) -> int:
        return getattr(self, f"leverage_{exchange}", 1)
    isolated: bool = True
    entry_min_roi_pct: Decimal = Decimal("0.50")
    max_open_pairs: int = Field(default=3, ge=1, le=50)
    max_total_usd: Decimal = Field(default=Decimal("5000"), gt=0)
    one_pair_per_token: bool = True
    margin_buffer_pct: Decimal = Field(default=Decimal("20"), ge=0)
    status_query_attempts: int = Field(default=10, ge=1)
    status_query_interval_ms: int = Field(default=1000, ge=100)
    close_attempts: int = Field(default=3, ge=1)
    close_retry_pause_ms: int = Field(default=300, ge=0)
    maintenance_block_s: int = Field(default=60, ge=1)
    warmup_retry_s: int = Field(default=60, ge=5)


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
    feed: FeedSettings = FeedSettings()
    portfolio: PortfolioSettings = PortfolioSettings()
    trading: TradingSettings = TradingSettings()
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
