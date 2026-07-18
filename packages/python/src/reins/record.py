"""Record / replay (M8.4): capture a run's model responses to a file and re-run it
offline, deterministically — no API key, no cost, no network.

Wrap any :class:`~reins.model.Model` in :class:`RecordingModel` to append each
response to a JSON-lines file as it is produced; later, :class:`ReplayModel` feeds
those responses back in order (like :class:`~reins.model.FakeModel`, but loaded from
disk). Same recording + same harness ⇒ an identical run — the determinism boundary
(CLAUDE.md §3) made persistent. Handy for debugging a real run, or turning one live
run into a fast, deterministic regression fixture.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from reins.codec import from_dict, to_dict
from reins.errors import ModelError
from reins.model import Model, ModelParams
from reins.types import Message, ModelResponse, ToolSpec


@dataclass(slots=True)
class RecordingModel:
    """Wraps a :class:`~reins.model.Model`, appending each response it returns to
    ``path`` (one JSON object per line) as the run proceeds — so even a crashed run
    leaves a usable partial recording. Any existing file at ``path`` is truncated."""

    inner: Model
    path: str | Path

    def __post_init__(self) -> None:
        Path(self.path).write_text("", encoding="utf-8")

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        params: ModelParams,
    ) -> ModelResponse:
        response = self.inner.complete(messages, tools, params)
        with Path(self.path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(to_dict(response)) + "\n")
        return response


@dataclass(slots=True)
class ReplayModel:
    """Replays the responses recorded by :class:`RecordingModel`, in order — a
    :class:`~reins.model.Model` with no network, no key, and no stochasticity."""

    path: str | Path
    _responses: list[ModelResponse] = field(init=False, default_factory=list)
    _index: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        text = Path(self.path).read_text(encoding="utf-8")
        self._responses = [
            from_dict(ModelResponse, json.loads(line)) for line in text.splitlines() if line.strip()
        ]

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        params: ModelParams,
    ) -> ModelResponse:
        if self._index >= len(self._responses):
            raise ModelError(
                f"replay exhausted after {len(self._responses)} recorded response(s)",
                hint="the harness asked for more turns than were recorded — re-record "
                "against the live model, or check the run is deterministic",
            )
        response = self._responses[self._index]
        self._index += 1
        return response
