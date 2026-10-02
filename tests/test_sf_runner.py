"""
Pins the two stacked deploy defects in rtk_sf/sf_runner.py:

1. _build_command emitted --source-dir and --metadata together, which the
   Salesforce CLI rejects at flag validation ("--metadata=... cannot also be
   provided when using --source-dir"), so the deploy never reached the org.
2. _summarize_deploy read the top-level integer exit code as `status` and
   called .title() on it, raising "'int' object has no attribute 'title'"
   and hiding the CLI's own error message.

Plus the two sf_command gaps from issue #28:

3. class_names was always emitted as --class-names, a flag `sf project deploy
   start` does not have — validate died on "Nonexistent flag: --class-names".
4. _summarize_describe read `result` off an error payload, which has none, and
   reported the empty dict as a success: "✅ Org: unknown ()".
"""

from __future__ import annotations

import pytest

from rtk_sf.sf_runner import (
    _build_command,
    _summarize_deploy,
    _summarize_describe,
    _summarize_retrieve,
    _summarize_run_test,
)

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


# ---------------------------------------------------------------------------
# _build_command — Apex test selection uses the flag each command actually has
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["deploy", "validate"])
def test_deploy_maps_class_names_to_tests_flag(action):
    cmd = _build_command(action, {"source_dir": "force-app", "class_names": ["A", "B"]})
    # --class-names does not exist on `sf project deploy start`.
    assert "--class-names" not in cmd
    assert cmd.count("--tests") == 2
    assert cmd[cmd.index("--tests") + 1] == "A"


@pytest.mark.parametrize("action", ["deploy", "validate"])
def test_deploy_implies_run_specified_tests(action):
    cmd = _build_command(action, {"class_names": "A"})
    assert cmd[cmd.index("--test-level") + 1] == "RunSpecifiedTests"


def test_explicit_test_level_is_not_overridden():
    cmd = _build_command(
        "deploy", {"class_names": "A", "test_level": "RunLocalTests"}
    )
    assert cmd.count("--test-level") == 1
    assert cmd[cmd.index("--test-level") + 1] == "RunLocalTests"


def test_comma_separated_class_names_become_separate_tests_flags():
    cmd = _build_command("validate", {"class_names": "A, B ,C"})
    assert [cmd[i + 1] for i, v in enumerate(cmd) if v == "--tests"] == ["A", "B", "C"]


def test_run_test_still_uses_class_names():
    cmd = _build_command("run_test", {"class_names": ["A", "B"]})
    assert "--tests" not in cmd
    assert cmd[cmd.index("--class-names") + 1] == "A,B"


def test_class_names_on_an_action_without_tests_is_rejected():
    with pytest.raises(ValueError) as exc:
        _build_command("retrieve", {"class_names": "A"})
    assert "class_names" in str(exc.value)


# ---------------------------------------------------------------------------
# _summarize_describe — an error payload is not a successful org
# ---------------------------------------------------------------------------

_NO_DEFAULT_ORG = {
    "name": "NoDefaultEnvError",
    "message": "No default environment found. Use -o or --target-org to specify an environment.",
    "status": 1,
}


def test_describe_surfaces_the_missing_org_error():
    out = _summarize_describe(_NO_DEFAULT_ORG, 0.4)
    assert out.startswith("❌")
    assert "No default environment found" in out
    assert "target_org" in out


def test_describe_never_reports_an_org_named_unknown():
    assert "unknown" not in _summarize_describe(_NO_DEFAULT_ORG, 0.4)


def test_describe_lists_the_org_fields():
    data = {
        "status": 0,
        "result": {
            "id": "00D000000000001EAA",
            "username": "user@example.com",
            "alias": "dev01",
            "instanceUrl": "https://example.my.salesforce.com",
            "connectedStatus": "Connected",
            "apiVersion": "62.0",
        },
    }
    out = _summarize_describe(data, 1.2)
    assert out.startswith("✅ Org: dev01")
    for expected in (
        "Username: user@example.com",
        "Org ID: 00D000000000001EAA",
        "Instance: https://example.my.salesforce.com",
        "Status: Connected",
        "API version: 62.0",
    ):
        assert expected in out


def test_describe_omits_fields_the_cli_did_not_return():
    data = {"status": 0, "result": {"username": "user@example.com"}}
    out = _summarize_describe(data, 1.0)
    assert "Username: user@example.com" in out
    assert "Alias:" not in out
    assert "Expires:" not in out


def test_describe_flags_an_unrecognised_payload():
    out = _summarize_describe({"status": 0, "result": {"accessToken": "x"}}, 1.0)
    assert out.startswith("⚠️")


# ---------------------------------------------------------------------------
# Every summarizer must consult the CLI-error shape
# ---------------------------------------------------------------------------


def test_retrieve_surfaces_cli_error():
    assert _summarize_retrieve(_NO_DEFAULT_ORG, 0.3).startswith("❌")


def test_run_test_surfaces_cli_error():
    assert _summarize_run_test(_NO_DEFAULT_ORG, 0.3).startswith("❌")


def test_deploy_surfaces_cli_error():
    assert _summarize_deploy(_NO_DEFAULT_ORG, 0.3).startswith("❌")


def test_retrieve_counts_files():
    data = {"status": 0, "result": {"fileCount": 7}}
    assert "7 file(s)" in _summarize_retrieve(data, 2.0)


def test_retrieve_falls_back_to_the_file_list():
    data = {"status": 0, "result": {"files": [{"filePath": "a"}, {"filePath": "b"}]}}
    assert "2 file(s)" in _summarize_retrieve(data, 2.0)


def test_describe_collapses_a_multi_line_field():
    # An unreachable org returns a whole REST error inside connectedStatus.
    data = {
        "status": 0,
        "result": {
            "alias": "coral",
            "connectedStatus": "HTTP response contains html content.\nCheck that the org exists.\n\nHTTP status code: 420.",
        },
    }
    out = _summarize_describe(data, 1.0)
    assert len(out.splitlines()) == 3  # headline + alias + status
    assert "Status: HTTP response contains html content. Check that" in out


def test_describe_truncates_an_overlong_field():
    data = {"status": 0, "result": {"alias": "a", "instanceUrl": "x" * 400}}
    out = _summarize_describe(data, 1.0)
    assert len(out.splitlines()) == 3
    assert out.rstrip().endswith("…")
