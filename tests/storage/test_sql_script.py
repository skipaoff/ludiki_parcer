from app.config.settings import REPO_ROOT
from app.storage.sql_script import split_statements


def test_semicolons_inside_literals_comments_and_dollar_quotes_do_not_split():
    script = """
    -- leading comment; with semicolon
    CREATE TABLE a (note TEXT DEFAULT 'x;y', "odd;name" INT);
    /* block; comment */
    INSERT INTO a (note) VALUES ('it''s; fine');
    DO $body$ BEGIN PERFORM 1; END $body$;
    SELECT $$a;b$$;
    """

    statements = split_statements(script)

    assert len(statements) == 4
    assert statements[0].endswith('"odd;name" INT)')
    assert statements[1].endswith("VALUES ('it''s; fine')")
    assert statements[2] == "DO $body$ BEGIN PERFORM 1; END $body$"
    assert statements[3] == "SELECT $$a;b$$"


def test_trailing_comment_only_chunks_are_dropped():
    assert split_statements("SELECT 1;\n-- end of file\n") == ["SELECT 1"]
    assert split_statements("  \n ; ; ") == []


def test_repository_migrations_split_into_expected_statement_counts():
    schema = split_statements((REPO_ROOT / "db" / "001_schema.sql").read_text(encoding="utf-8"))
    timescale = split_statements((REPO_ROOT / "db" / "002_timescale.sql").read_text(encoding="utf-8"))

    assert schema[0].endswith("BEGIN") and schema[-1] == "COMMIT"
    assert any("WITH (timescaledb.continuous)" in statement for statement in timescale)
    assert all(not statement.rstrip().endswith(";") for statement in schema + timescale)
