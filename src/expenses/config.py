"""User configuration (see DESIGN.md, Configuration)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_ENV = "EXPENSES_CONFIG"
DB_NAME = "expenses.db"
_KEYS = ("backup_dir", "data_dir", "rules", "timezone")


class ConfigError(Exception):
    """Invalid or unreadable configuration."""


def default_config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return Path("~/.config/expenses/config.toml").expanduser()


@dataclass
class Config:
    backup_dir: Path = field(default_factory=lambda: Path("~/MyExpensesBackups").expanduser())
    data_dir: Path = field(default_factory=lambda: Path("~/.local/share/expenses").expanduser())
    rules: Path = field(default_factory=lambda: Path("~/.config/expenses/rules.yaml").expanduser())
    timezone: str = "Europe/Rome"

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME


def load_config(path: Path | None = None) -> Config:
    """Load the TOML config. A missing default file gives defaults; an explicit one must exist."""
    explicit = path is not None or CONFIG_ENV in os.environ
    path = Path(path).expanduser() if path is not None else default_config_path()
    cfg = Config()
    if not path.is_file():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return cfg
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid config file {path}: {e}") from None
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e.strerror}") from None
    unknown = sorted(set(data) - set(_KEYS))
    if unknown:
        raise ConfigError(f"unknown config key(s) in {path}: {', '.join(unknown)}")
    for key, value in data.items():
        if not isinstance(value, str) or not value:
            raise ConfigError(f"config key '{key}' must be a non-empty string")
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigError(f"unknown timezone: {value}") from None
        setattr(cfg, key, value if key == "timezone" else Path(value).expanduser())
    return cfg
