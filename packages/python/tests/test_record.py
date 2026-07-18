"""Tests for record / replay (M8.4): capture a run's model responses and re-run it
offline, deterministically."""

import json
from pathlib import Path

import pytest

from reins import Agent, RecordingModel, ReplayModel, capability
from reins.errors import ModelError
from reins.model import FakeModel, ModelParams


@capability
def get_order(order_id: int) -> dict[str, int]:
    "Get an order by its id."
    return {"id": order_id, "total": 42}


SCRIPT = [
    {"call": "get_order", "args": {"order_id": 8842}},
    {"final": "Order 8842 total is 42."},
]


def test_record_then_replay_reproduces_a_full_run(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"

    recorded = Agent(
        model=RecordingModel(FakeModel.from_script(SCRIPT), path),
        capabilities=[get_order],
    )
    live = recorded.ask("what is order 8842's total?")

    # Re-run offline from the recording — no model key, no network, same answer.
    replayed = Agent(model=ReplayModel(path), capabilities=[get_order])
    offline = replayed.ask("what is order 8842's total?")

    assert offline.output.text == live.output.text == "Order 8842 total is 42."


def test_recording_writes_one_json_line_per_response(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    Agent(
        model=RecordingModel(FakeModel.from_script(SCRIPT), path),
        capabilities=[get_order],
    ).ask("total?")

    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["finish_reason"] == "tool_calls"
    assert json.loads(lines[1])["finish_reason"] == "stop"


def test_replay_exhausted_is_a_hinted_error(tmp_path: Path) -> None:
    path = tmp_path / "one.jsonl"
    RecordingModel(FakeModel.from_script([{"final": "hi"}]), path).complete([], [], ModelParams())

    replay = ReplayModel(path)
    assert replay.complete([], [], ModelParams()).message.text == "hi"
    with pytest.raises(ModelError, match="replay exhausted"):
        replay.complete([], [], ModelParams())


def test_recording_truncates_a_previous_file(tmp_path: Path) -> None:
    path = tmp_path / "old.jsonl"
    path.write_text("stale contents from a previous run\n")
    RecordingModel(FakeModel.from_script([{"final": "x"}]), path)  # __post_init__ truncates
    assert path.read_text() == ""
