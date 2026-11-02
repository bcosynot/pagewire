"""The console scripts the systemd units start must exist and be importable.

The NixOS module runs `pagewire-serve`, `pagewire-derive` and `pagewire-deliver` out of
the built package's `bin/`. Those names come from `[project.scripts]`; if one drifts —
renamed, mistyped, or its target moved — the failure is a unit that starts and dies on a
box nobody is watching. This checks the wiring here, where it is cheap.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

EXPECTED = {
    "pagewire-serve": "pagewire.app:serve",
    "pagewire-derive": "pagewire.sessions:main",
    "pagewire-deliver": "pagewire.delivery:main",
}


def test_the_scripts_the_module_names_are_declared() -> None:
    scripts = PYPROJECT["project"]["scripts"]
    for name, target in EXPECTED.items():
        assert scripts.get(name) == target, f"{name} should point at {target}"


def test_every_declared_script_resolves_to_a_callable() -> None:
    for name, target in PYPROJECT["project"]["scripts"].items():
        module_name, _, attribute = target.partition(":")
        assert module_name and attribute, f"{name} is not a mod:attr target: {target}"
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{target} is not callable"


def test_derive_cli_derives_from_a_database_and_reports_success(tmp_path, capsys) -> None:
    """A dry shape-check that the derive entry point actually runs end to end."""
    from pagewire.sessions import main

    db = tmp_path / "derived.db"
    assert main(["--db", str(db)]) == 0
    assert db.exists(), "the derive CLI should have created the database"
    assert "derived" in capsys.readouterr().out
