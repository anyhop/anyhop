"""Run the lint job's static gates inside pytest.

GitHub CI's test jobs only execute ``pytest``. Format, Ruff, mypy, shellcheck,
and shfmt are separate lint steps, so a green subset of pytest can still fail
CI. These tests run those gates. They do not replace the Docker or browser
jobs, which need a container build and Chromium.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(sys.executable).parent


def _tool(name: str) -> Path:
    path = BIN / name
    if path.is_file():
        return path
    pytest.fail(
        f"{name} is not installed next to {sys.executable}; run uv sync --all-extras"
    )


def _check(cmd: list[str]) -> None:
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout + result.stderr).strip()


def test_ruff_check_matches_ci():
    _check([str(_tool("ruff")), "check"])


def test_ruff_format_matches_ci():
    """The failure mode of an unformatted file: CI's ``ruff format --check``."""
    _check([str(_tool("ruff")), "format", "--check"])


def test_mypy_matches_ci():
    _check([str(_tool("mypy"))])


def test_shellcheck_matches_ci():
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.sh"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    scripts = [item.decode() for item in listed.stdout.split(b"\0") if item]
    assert scripts, "CI shellchecks every tracked .sh file"
    _check([str(_tool("shellcheck")), *scripts])


def test_shfmt_matches_ci():
    listed = subprocess.run(
        ["git", "ls-files", "-z", "*.sh"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    scripts = [item.decode() for item in listed.stdout.split(b"\0") if item]
    _check([str(_tool("shfmt")), "-d", *scripts])


def test_openapi_yaml_parses():
    """Folded scalars inside a flow mapping are invalid YAML.

    ``tests/test_openapi_contract.py`` also loads this file at import, which
    aborts collection. This test names that gate on its own.
    """
    spec = yaml.safe_load((ROOT / "docs" / "openapi.yaml").read_text())
    health = spec["paths"]["/health"]["get"]["responses"]["200"]
    ok = health["content"]["application/json"]["schema"]["properties"]["ok"]
    assert isinstance(ok["description"], str)
    assert ok["description"].strip()
