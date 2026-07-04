"""Lock the flagship example (M4.5): `make demo` runs end-to-end and shows the
whole story — generated capabilities, a read answer, identity-scoped RLS, a gated
write with a visible approval prompt, and the audit trail."""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

# packages/python/tests/ -> repo root is parents[3].
REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_demo() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "examples.fastapi_sqlalchemy.demo"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_demo_runs_the_whole_story() -> None:
    out = _run_demo()
    # 1. auto-exposed capabilities (reads + gated writes), no hand-written CRUD
    assert "find_orders" in out
    assert "update_order" in out
    assert "find_my_orders" in out  # the composed, hand-written scoped capability
    # 2. a read answered through the agent
    assert "orders currently open" in out
    # 3. identity-scoped RLS: each user sees only their own orders
    assert "ada@shop.test sees: ['ORD-1', 'ORD-2']" in out
    assert "ben@shop.test sees: ['ORD-3']" in out
    # 4. a write pauses at a real approval prompt, then executes
    assert "⚠ Reins wants to run a WRITE: update_order" in out
    assert "ORD-2 status = 'cancelled'" in out
    # 5. the audit trail records the gated write
    assert "approved" in out
    assert "executed" in out
