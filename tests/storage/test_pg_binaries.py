"""The same config.toml has to find PostgreSQL on Windows and on a Mac."""

from pathlib import Path

from app.storage.pg_binaries import find_pg_ctl

BUNDLE = Path("../ludik-data/pgsql/bin")


def lookup(present: set[str], platform: str, on_path: str | None = None):
    # Path renders with backslashes on Windows, so the fake filesystem is asked in posix form.
    return find_pg_ctl(
        BUNDLE,
        platform=platform,
        exists=lambda path: path.as_posix() in present,
        which=lambda name: on_path,
    )


def test_the_bundled_windows_binaries_win_over_everything_else():
    found = lookup({(BUNDLE / "pg_ctl.exe").as_posix(), "/opt/homebrew/opt/postgresql@18/bin/pg_ctl"}, "win32", on_path="C:/pg/pg_ctl.exe")

    assert found == BUNDLE / "pg_ctl.exe"


def test_path_is_used_when_the_configured_directory_has_nothing():
    assert lookup(set(), "darwin", on_path="/usr/local/bin/pg_ctl") == Path("/usr/local/bin/pg_ctl")


def test_homebrew_keeps_postgresql_out_of_path_so_its_prefixes_are_checked_by_name():
    found = lookup({"/opt/homebrew/opt/postgresql@18/bin/pg_ctl"}, "darwin")

    assert found.as_posix() == "/opt/homebrew/opt/postgresql@18/bin/pg_ctl"


def test_intel_homebrew_prefix_is_checked_too():
    assert lookup({"/usr/local/opt/postgresql@18/bin/pg_ctl"}, "darwin").as_posix() == "/usr/local/opt/postgresql@18/bin/pg_ctl"


def test_a_machine_without_postgresql_gets_no_path_and_the_cluster_is_left_alone():
    assert lookup(set(), "darwin") is None
    assert lookup({"/opt/homebrew/opt/postgresql@18/bin/pg_ctl"}, "linux") is None
