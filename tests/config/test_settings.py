from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.settings import EXAMPLE_FILE, load_settings


def test_example_config_loads_and_resolves_paths_against_its_folder():
    settings = load_settings(EXAMPLE_FILE)

    assert settings.server.host == "127.0.0.1"
    assert settings.paths.web_dist == (EXAMPLE_FILE.parent / "web" / "dist").resolve()
    assert settings.database.pg_data_dir == (EXAMPLE_FILE.parent.parent / "ludik-data" / "pgdata").resolve()


def test_missing_sections_fall_back_to_defaults(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text("[server]\nport = 9000\n", encoding="utf-8")

    settings = load_settings(config)

    assert settings.server.port == 9000
    assert settings.database.name == "ludik"
    assert settings.paths.spool_dir == (tmp_path / ".." / "ludik-data" / "spool").resolve()


def test_unknown_key_fails_instead_of_being_ignored(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text("[server]\nprot = 9000\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_settings(config)


def test_non_loopback_host_is_refused(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('[server]\nhost = "0.0.0.0"\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        load_settings(config)
