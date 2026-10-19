"""Exercise the privileged merge step without GitHub credentials or network."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/dependabot-automerge.yml"
)


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_ci_completion_can_rearm_automerge_after_a_rerun():
    workflow = _workflow()
    # PyYAML's YAML 1.1 loader interprets the Actions "on" key as True.
    trigger = workflow[True]["workflow_run"]
    assert set(trigger["types"]) == {"requested", "completed"}
    assert "conclusion == 'success'" in workflow["jobs"]["merge"]["if"]


@pytest.mark.parametrize(
    "files,token,expected_token",
    [
        ("uv.lock", "", "actions-token"),
        ("uv.lock", "workflow-token", "actions-token"),
        (".github/workflows/publish.yml", "workflow-token", "workflow-token"),
        ("docs/api.md\n.github/workflows/ci.yml", "workflow-token", "workflow-token"),
        (".github/workflows/publish.yml", "", None),
    ],
)
def test_merge_uses_workflow_permission_only_when_needed(
    tmp_path, files, token, expected_token
):
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
if sys.argv[1] == "api":
    print(os.environ["TEST_CHANGED_FILES"])
elif sys.argv[1:3] == ["pr", "merge"]:
    pathlib.Path(os.environ["TEST_MERGE_RECORD"]).write_text(json.dumps({
        "token": os.environ["GH_TOKEN"], "args": sys.argv[1:]
    }))
else:
    raise SystemExit(2)
""")
    gh.chmod(0o755)
    record = tmp_path / "merge.json"
    step = _workflow()["jobs"]["merge"]["steps"][-1]
    assert step["if"] == "steps.verify.outputs.merge == 'true'"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "GH_TOKEN": "actions-token",
            "WORKFLOW_MERGE_TOKEN": token,
            "GITHUB_REPOSITORY": "anyhop/anyhop",
            "PR_NUMBER": "56",
            "HEAD_SHA": "a" * 40,
            "TEST_CHANGED_FILES": files,
            "TEST_MERGE_RECORD": str(record),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    if expected_token is None:
        assert result.returncode == 1
        assert "::error::" in result.stdout
        assert "DEPENDABOT_AUTOMERGE_TOKEN" in result.stdout
        assert not record.exists()
    else:
        assert result.returncode == 0, result.stderr
        merged = json.loads(record.read_text())
        assert merged["token"] == expected_token
        assert merged["args"] == [
            "pr",
            "merge",
            "56",
            "--repo",
            "anyhop/anyhop",
            "--auto",
            "--rebase",
            "--match-head-commit",
            "a" * 40,
        ]
    assert "actions-token" not in result.stdout + result.stderr
    assert "workflow-token" not in result.stdout + result.stderr
