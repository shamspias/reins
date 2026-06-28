"""M0.1 smoke tests: the package imports and exposes a non-empty version string.

These exist so the gate (`make check`) runs a real, passing suite on the empty
scaffold rather than `pytest` reporting "no tests collected" (exit code 5).
Feature tests arrive with their milestones from M1.1 onward.
"""

import reins


def test_package_imports() -> None:
    assert reins.__name__ == "reins"


def test_version_is_nonempty_str() -> None:
    assert isinstance(reins.__version__, str)
    assert reins.__version__
