"""Tests for the ``reins`` CLI (M4.3): reflect a real database, inspect it, and
answer read-only questions — all without hand-written models or a live LLM."""

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, insert

from reins import console as cli
from reins.errors import ReinsError
from reins.model import FakeModel


@pytest.fixture
def db_url(tmp_path: Path) -> Iterator[str]:
    path = tmp_path / "shop.db"
    url = f"sqlite:///{path}"
    metadata = MetaData()
    customers = Table(
        "customers",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String, unique=True),
        Column("name", String),
    )
    engine = create_engine(url)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(customers),
            [{"email": "a@x.com", "name": "Ada"}, {"email": "b@x.com", "name": "Ben"}],
        )
    engine.dispose()
    yield url


def _collector() -> tuple[list[str], object]:
    lines: list[str] = []
    return lines, lines.append


# --- inspect (no model / API key required) ----------------------------------------


def test_inspect_lists_read_capabilities(db_url: str) -> None:
    lines, out = _collector()
    assert cli.cmd_inspect(db_url, out=out) == 0
    text = "\n".join(lines)
    assert "find_customers" in text
    assert "count_customers" in text
    assert "read" in text
    assert "create_customers" not in text  # writes hidden without --write


def test_inspect_write_shows_gated_capabilities(db_url: str) -> None:
    lines, out = _collector()
    assert cli.cmd_inspect(db_url, can_write=True, out=out) == 0
    text = "\n".join(lines)
    assert "create_customers" in text
    assert "delete_customers" in text
    assert "destructive" in text


def test_inspect_reports_the_table_count(db_url: str) -> None:
    lines, out = _collector()
    cli.cmd_inspect(db_url, out=out)
    assert any("1 table" in line for line in lines)


def test_reflected_unique_column_becomes_the_natural_key(db_url: str) -> None:
    # Reflection records `email UNIQUE` as a table constraint/index (not
    # column.unique), so the lookup key must still resolve to email, not the id.
    lines, out = _collector()
    cli.cmd_inspect(db_url, out=out)
    text = "\n".join(lines)
    assert "get_customers_by_email" in text
    assert "get_customers_by_id" not in text


# --- ask / chat (read-only, model injected) ---------------------------------------


def test_ask_answers_a_read_question(db_url: str) -> None:
    lines, out = _collector()
    model = FakeModel.from_script(
        [{"call": "count_customers", "args": {}}, {"final": "There are 2 customers."}]
    )
    assert cli.cmd_ask(db_url, "how many customers?", model=model, out=out) == 0
    assert lines == ["There are 2 customers."]


def test_chat_loops_until_exit(db_url: str) -> None:
    lines, out = _collector()
    prompts = iter(["how many customers?", "exit"])
    model = FakeModel.from_script(
        [{"call": "count_customers", "args": {}}, {"final": "2 customers."}]
    )
    rc = cli.cmd_chat(db_url, model=model, input_fn=lambda _prompt: next(prompts), out=out)
    assert rc == 0
    assert any("2 customers." in line for line in lines)


def test_chat_stops_on_eof(db_url: str) -> None:
    _lines, out = _collector()

    def eof(_prompt: str) -> str:
        raise EOFError

    model = FakeModel.from_script([{"final": "unused"}])
    assert cli.cmd_chat(db_url, model=model, input_fn=eof, out=out) == 0


# --- reflection errors -------------------------------------------------------------


def test_reflect_empty_database_is_hinted(tmp_path: Path) -> None:
    from reins.orm.sqlalchemy import reflect_database

    empty = f"sqlite:///{tmp_path / 'empty.db'}"
    create_engine(empty).connect().close()  # create an empty file db
    with pytest.raises(ReinsError, match="→"):
        reflect_database(empty)


# --- argument parsing / main() -----------------------------------------------------


def test_main_inspect_prints_capabilities(db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["inspect", db_url]) == 0
    out = capsys.readouterr().out
    assert "find_customers" in out


def test_main_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "reins" in capsys.readouterr().out


def test_main_no_command_shows_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()


def test_main_reports_reins_errors_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = f"sqlite:///{tmp_path / 'empty.db'}"
    create_engine(empty).connect().close()
    assert cli.main(["inspect", empty]) == 1
    err = capsys.readouterr().err
    assert "reins:" in err and "→" in err
