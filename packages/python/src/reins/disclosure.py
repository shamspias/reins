"""Dual progressive disclosure (M3.1): capabilities AND schema, discovered on demand.

Never dump every capability schema or the whole data model into context
(invariant §2.12). Above a small threshold the model sees one constant-size
``search_capabilities`` tool instead of the full registry; matches are exposed
and become directly callable on the next turn. A :class:`SchemaIndex`, when
attached, is likewise reachable only through ``search_schema`` — the model
retrieves the few tables it needs, never the whole schema. Base context is
byte-identical whether 13 or 10,000 capabilities are registered.

Both search tools are harness-internal **reads**: deterministic, free at every
autonomy level, allowed under ``ask()``, and never gated — they only reveal
intent surfaces (:class:`reins.types.ToolSpec`), never policy metadata (§2.5).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from reins.errors import DisclosureError
from reins.registry import CapabilityRegistry
from reins.types import Capability, CapabilityResult, ToolCall, ToolSpec

DEFER_AFTER = 12  # above this many capabilities, defer them behind search (auto mode)

_MAX_CAPABILITY_MATCHES = 8  # bound every disclosure response (§2.13)
_MAX_TABLE_MATCHES = 5
_MAX_COLUMNS_SHOWN = 20
_MAX_QUERY_CHARS = 200

SEARCH_CAPABILITIES = "search_capabilities"
SEARCH_SCHEMA = "search_schema"

# Constant-size specs: deliberately no registry/table counts in the descriptions,
# so the base context is byte-identical at any scale (asserted by test).
_SEARCH_CAPABILITIES_SPEC = ToolSpec(
    name=SEARCH_CAPABILITIES,
    description=(
        "Search the available operations by keyword and load the matches. "
        "Call this first to find the operations you need; matches become "
        "directly callable."
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "keywords, e.g. 'refund order'"}},
        "required": ["query"],
        "additionalProperties": False,
    },
)

_SEARCH_SCHEMA_SPEC = ToolSpec(
    name=SEARCH_SCHEMA,
    description=(
        "Search the application's data model by keyword. Returns only the "
        "relevant tables with their fields."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "keywords, e.g. 'orders status'"}
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True, slots=True)
class TableInfo:
    """One table (or model) in the application's data model."""

    name: str
    description: str = ""
    columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SchemaIndex:
    """The searchable data model — populated by hand or by the ORM adapters (M4.x)."""

    tables: tuple[TableInfo, ...] = ()

    def search(self, query: str) -> list[TableInfo]:
        """The tables relevant to ``query``, best first, bounded."""
        query_tokens = _tokens(query)
        scored = sorted(
            (
                (score, table.name, table)
                for table in self.tables
                if (score := _score(query_tokens, table.name, table.description)) > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        return [table for _, _, table in scored[:_MAX_TABLE_MATCHES]]


@dataclass(slots=True)
class Disclosure:
    """Per-run disclosure state: what the model can currently see.

    ``defer=None`` (auto) defers capabilities behind search when more than
    ``DEFER_AFTER`` are registered; ``True``/``False`` force it. Discovered
    capability names accumulate in ``_exposed`` for the rest of the run — the
    exposed surface only ever grows with what the model actually needs.
    """

    registry: CapabilityRegistry
    schema: SchemaIndex | None = None
    defer: bool | None = None
    _exposed: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        reserved = (
            (SEARCH_CAPABILITIES, self.deferred),
            (SEARCH_SCHEMA, self.schema is not None),
        )
        for meta_name, active in reserved:
            if active and self.registry.get(meta_name) is not None:
                raise DisclosureError(
                    f"capability {meta_name!r} collides with the built-in discovery tool",
                    hint=f"rename your capability (e.g. {meta_name}_custom) — "
                    f"{meta_name!r} is reserved while disclosure is active",
                )

    @property
    def deferred(self) -> bool:
        if self.defer is not None:
            return self.defer
        return len(self.registry.list()) > DEFER_AFTER

    def tools(self) -> list[ToolSpec]:
        """The tool surface for the next model turn — tiny and constant when deferred."""
        if self.deferred:
            specs = [_SEARCH_CAPABILITIES_SPEC]
            specs += [
                cap.spec.tool_spec()
                for name in sorted(self._exposed)
                if (cap := self.registry.get(name)) is not None
            ]
        else:
            specs = [cap.tool_spec() for cap in self.registry.list()]
        if self.schema is not None:
            specs.append(_SEARCH_SCHEMA_SPEC)
        return specs

    def answer(self, call: ToolCall) -> CapabilityResult | None:
        """Handle a discovery call, or ``None`` if ``call`` is not one.

        Discovery is a harness-internal read: deterministic, never gated, and
        available under ``ask()``. Failures are errors-as-data with a ``→`` hint.
        """
        if call.name == SEARCH_CAPABILITIES and self.deferred:
            return self._answer(call, self._search_capabilities)
        if call.name == SEARCH_SCHEMA and self.schema is not None:
            return self._answer(call, self._search_schema)
        return None

    def _answer(
        self, call: ToolCall, handler: Callable[[str], CapabilityResult]
    ) -> CapabilityResult:
        query = call.arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return CapabilityResult(
                ok=False,
                error=f"{call.name}: 'query' must be a non-empty string"
                "\n  → pass keywords, e.g. query='refund order'",
            )
        return handler(query.strip()[:_MAX_QUERY_CHARS])  # bounded: model-controlled input

    def _search_capabilities(self, query: str) -> CapabilityResult:
        query_tokens = _tokens(query)
        scored = sorted(
            (
                (score, spec.name, spec)
                for spec in self.registry.list()
                if (score := _score(query_tokens, spec.name, spec.description)) > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        matches = [spec for _, _, spec in scored[:_MAX_CAPABILITY_MATCHES]]
        if not matches:
            return CapabilityResult(
                ok=False,
                error=f"no operations matched {query!r} "
                f"({len(self.registry.list())} are registered)"
                "\n  → try different keywords (verbs and nouns work best)",
            )
        self._exposed.update(spec.name for spec in matches)
        lines = [f"Found {len(matches)} operation(s); they are now directly callable:"]
        lines += [f"  {_render_capability(spec)}" for spec in matches]
        return CapabilityResult(ok=True, value="\n".join(lines))

    def _search_schema(self, query: str) -> CapabilityResult:
        assert self.schema is not None  # guarded by answer()
        matches = self.schema.search(query)
        if not matches:
            return CapabilityResult(
                ok=False,
                error=f"no tables matched {query!r} "
                f"({len(self.schema.tables)} tables in the data model)"
                "\n  → try different keywords (table or field names work best)",
            )
        lines = [f"Found {len(matches)} table(s):"]
        lines += [f"  {_render_table(table)}" for table in matches]
        return CapabilityResult(ok=True, value="\n".join(lines))


def _render_capability(spec: Capability) -> str:
    params = ", ".join(spec.input_schema.get("properties", {}))
    first_line = spec.description.splitlines()[0] if spec.description else ""
    return f"{spec.name}({params}) — {first_line}"


def _render_table(table: TableInfo) -> str:
    columns = ", ".join(table.columns[:_MAX_COLUMNS_SHOWN])
    if len(table.columns) > _MAX_COLUMNS_SHOWN:
        columns += ", …"
    described = f" — {table.description}" if table.description else ""
    fields = f" (fields: {columns})" if columns else ""
    return f"{table.name}{described}{fields}"


def _tokens(text: str) -> set[str]:
    """Lowercased word tokens; underscores and punctuation split words."""
    out, word = set(), []
    for ch in text.lower():
        if ch.isalnum():
            word.append(ch)
        elif word:
            out.add("".join(word))
            word = []
    if word:
        out.add("".join(word))
    return out


def _score(query_tokens: set[str], name: str, description: str) -> int:
    """Deterministic keyword relevance: name hits beat description hits."""
    name_tokens = _tokens(name)
    description_tokens = _tokens(description)
    score = 0
    for token in query_tokens:
        if token in name_tokens:
            score += 3
        elif any(token in name_token for name_token in name_tokens):
            score += 2  # substring of a name word ("refund" ~ "refunds")
        if token in description_tokens:
            score += 1
    return score
