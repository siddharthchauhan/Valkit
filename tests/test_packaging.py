"""Tests for what actually ships.

These exist because of a class of bug that a checkout cannot reveal. Data files
— the document templates, the audit schema, the console's assets — are found on
disk by path, so they work whether or not `pyproject.toml` declares them. The
omission surfaces only in an installed wheel, which is to say in the container,
at which point an installed ValKit cannot open an audit trail and the console
serves nothing.

So the assertion is against the filesystem rather than against the manifest: a
new data file that nobody remembered to declare fails here.
"""

from __future__ import annotations

import glob
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("valkit", "api")


def _patterns() -> dict[str, list[str]]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    return config["tool"]["setuptools"]["package-data"]


def _data_files(package: str) -> set[str]:
    """Every non-Python file inside a package, as a POSIX path relative to it."""
    root = ROOT / package
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.suffix not in {".py", ".pyc"}
        and "__pycache__" not in path.parts
    }


def _declared(package: str) -> set[str]:
    """What the declared patterns actually match on disk.

    Resolved with ``glob`` against the package directory rather than with
    ``PurePath.match``, because that is what setuptools does — and the two
    differ exactly where it matters: ``PurePath.match`` does not understand
    ``**``, so a check written with it would fail on patterns that work.
    """
    root = ROOT / package
    matched: set[str] = set()
    for pattern in _patterns().get(package, []):
        matched.update(
            path
            for path in glob.glob(pattern, root_dir=str(root), recursive=True)
            if (root / path).is_file()
        )
    return {Path(path).as_posix() for path in matched}


class TestPackageData:
    @pytest.mark.parametrize("package", PACKAGES)
    def test_every_data_file_is_declared(self, package):
        undeclared = sorted(_data_files(package) - _declared(package))
        assert undeclared == [], (
            f"{package}: these files are on disk but match no package-data pattern, "
            f"so they will be missing from an installed wheel: "
            f"{', '.join(undeclared)}"
        )

    @pytest.mark.parametrize("package", PACKAGES)
    def test_no_pattern_is_dead(self, package):
        """A pattern matching nothing is a rename nobody followed through."""
        for pattern in _patterns().get(package, []):
            assert glob.glob(
                pattern, root_dir=str(ROOT / package), recursive=True
            ), f"{package}: pattern {pattern!r} matches nothing"

    def test_the_audit_schema_is_declared(self):
        """Named on its own because its absence disables the audit trail."""
        from valkit.audit import store

        assert store._SCHEMA_PATH.is_file()
        assert "audit/schema.sql" in _declared("valkit")

    def test_the_console_assets_are_declared(self):
        declared = _declared("api")
        for name in ("index.html", "app.js", "styles.css"):
            assert f"static/{name}" in declared, name

    def test_every_template_is_declared(self):
        from valkit.docgen.generator import TEMPLATE_DIR

        declared = _declared("valkit")
        templates = sorted(TEMPLATE_DIR.glob("*.j2"))
        assert templates
        for template in templates:
            assert template.relative_to(ROOT / "valkit").as_posix() in declared


class TestPackages:
    def test_both_packages_are_included(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            config = tomllib.load(handle)
        include = config["tool"]["setuptools"]["packages"]["find"]["include"]
        assert "valkit*" in include
        assert "api*" in include

    def test_the_deployment_entry_points_import(self):
        """The names infra/terraform runs: `uvicorn api.main:app` and
        `python -m valkit.worker`."""
        import importlib

        assert importlib.import_module("valkit.worker").main
        pytest.importorskip("fastapi", reason="the API extra is not installed")
        assert importlib.import_module("api.main").app

    def test_the_console_is_where_the_application_looks_for_it(self):
        pytest.importorskip("fastapi", reason="the API extra is not installed")
        from api.main import STATIC_DIR

        assert STATIC_DIR.is_dir()
        assert (STATIC_DIR / "index.html").is_file()
