"""Shared pytest fixtures.

Owned centrally so that individual module test suites never have to define
overlapping fixtures. Everything here is deterministic and offline: no test in
this repository may reach the network or read the wall clock.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valkit.testing import (  # noqa: E402
    make_dataset,
    make_requirements,
    make_risks,
    make_run,
    make_sample_results,
    make_spec,
    make_tests,
)
from valkit.util import FrozenClock  # noqa: E402


@pytest.fixture
def clock() -> FrozenClock:
    """A clock starting at 2026-01-01T00:00:00Z, advancing one second per call."""
    return FrozenClock("2026-01-01T00:00:00Z", step=1.0)


@pytest.fixture
def spec():
    return make_spec()


@pytest.fixture
def dataset():
    return make_dataset(20)


@pytest.fixture
def run(spec):
    return make_run(spec=spec, n=180, failures=4)


@pytest.fixture
def requirements():
    return make_requirements()


@pytest.fixture
def risks():
    return make_risks()


@pytest.fixture
def test_cases():
    return make_tests()


@pytest.fixture
def sample_results():
    return make_sample_results(100, failures=5)


@pytest.fixture
def workdir(tmp_path):
    """An isolated working directory for stores that touch the filesystem."""
    target = tmp_path / "valkit-work"
    target.mkdir()
    return target
