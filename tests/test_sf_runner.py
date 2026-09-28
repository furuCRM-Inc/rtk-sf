"""
Pins the two stacked deploy defects in rtk_sf/sf_runner.py:

1. _build_command emitted --source-dir and --metadata together, which the
   Salesforce CLI rejects at flag validation ("--metadata=... cannot also be
   provided when using --source-dir"), so the deploy never reached the org.
2. _summarize_deploy read the top-level integer exit code as `status` and
   called .title() on it, raising "'int' object has no attribute 'title'"
   and hiding the CLI's own error message.
"""

from __future__ import annotations

import pytest

from rtk_sf.sf_runner import _build_command, _summarize_deploy


# ---------------------------------------------------------------------------
# _build_command — source_dir / metadata are mutually exclusive for deploy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["deploy", "validate"])
def test_deploy_rejects_source_dir_with_metadata(action):
    with pytest.raises(ValueError) as exc:
        _build_command(action, {"source_dir": "force-app", "metadata": "ApexClass:Foo"})
    assert "not both" in str(exc.value)


def test_deploy_with_source_dir_only():
    cmd = _build_command("deploy", {"source_dir": "force-app", "target_org": "dev01"})
    assert "--source-dir" in cmd
    assert "--metadata" not in cmd


def test_deploy_with_metadata_list_only():
    cmd = _build_command("deploy", {"metadata": ["ApexClass:Foo", "ApexClass:Bar"]})
    assert cmd.count("--metadata") == 2
    assert "--source-dir" not in cmd


def test_retrieve_still_accepts_both_flags():
    # sf project retrieve start allows the combination — do not constrain it.
    cmd = _build_command("retrieve", {"source_dir": "force-app", "metadata": "ApexClass:Foo"})
    assert "--source-dir" in cmd
    assert "--metadata" in cmd


# ---------------------------------------------------------------------------
# _summarize_deploy — top-level CLI error shape
# ---------------------------------------------------------------------------


def test_summarize_surfaces_top_level_cli_error():
    # Shape returned by the CLI when it rejects the command itself: exit code
    # as `status`, no nested `result`.
    data = {
        "status": 2,
        "name": "Error",
        "message": "--metadata=ApexClass:Foo cannot also be provided when using --source-dir",
        "exitCode": 2,
    }
    out = _summarize_deploy(data, 0.4)
    assert out.startswith("❌")
    assert "cannot also be provided" in out


def test_summarize_falls_back_to_name_without_message():
    out = _summarize_deploy({"status": 1, "name": "NoOrgFound"}, 0.1)
    assert "NoOrgFound" in out


def test_summarize_coerces_non_string_status():
    # Defensive: a numeric status must not raise AttributeError.
    data = {"status": 0, "result": {"numberComponentsDeployed": 3}}
    assert "3 component(s)" in _summarize_deploy(data, 1.0)


def test_summarize_success():
    data = {
        "status": 0,
        "result": {
            "status": "Succeeded",
            "numberComponentsDeployed": 42,
            "numberComponentErrors": 0,
            "username": "dev01@example.com",
        },
    }
    out = _summarize_deploy(data, 8.3)
    assert out.startswith("✅ Succeeded: 42 component(s)")
    assert "dev01@example.com" in out
    assert "8.3s" in out


def test_summarize_component_failures():
    data = {
        "status": 0,
        "result": {
            "status": "Failed",
            "numberComponentErrors": 1,
            "details": {
                "componentFailures": [
                    {"componentType": "ApexClass", "fullName": "Foo", "problem": "boom"}
                ]
            },
        },
    }
    out = _summarize_deploy(data, 2.0)
    assert "Deploy failed" in out
    assert "ApexClass Foo: boom" in out
