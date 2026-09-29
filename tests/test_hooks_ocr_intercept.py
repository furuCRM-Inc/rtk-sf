"""
Pins the PreToolUse[Read] OCR hook's contract.

Two rules, both learned from the hook failing in practice:

1. **Never write to stderr.** Claude Code reports stderr output as a hook error
   and fails the Read, so a noisy OCR engine turns "read this image" into a hard
   failure. PaddleOCR and torch emit model-loading notices on stderr — partly
   from native code — which is why the OCR call runs with fds 1 and 2 redirected.
2. **Block with JSON, not exit 2.** On exit 2 Claude Code takes the blocking
   message from stderr, so text written to stdout is discarded and the user sees
   "hook error: No stderr output". The documented structured form is exit 0 with
   a `permissionDecision` of "deny" on stdout.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HOOK = [sys.executable, "-m", "rtk_sf.hooks.ocr_intercept"]

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _stub_ocr(tmp_path, body: str) -> str:
    """
    Replace only `rtk_sf.vision_ocr`, leaving the real package importable.

    Shadowing the whole package cannot work — the hook itself lives in
    `rtk_sf.hooks`. `sitecustomize` runs before the hook module, so the stub is
    already in sys.modules when `from rtk_sf.vision_ocr import …` resolves.
    """
    site_dir = tmp_path / "sitedir"
    site_dir.mkdir(exist_ok=True)
    (site_dir / "sitecustomize.py").write_text(
        "import sys, types\n"
        "mod = types.ModuleType('rtk_sf.vision_ocr')\n"
        f"{body}\n"
        "mod.extract_image_text = extract_image_text\n"
        "sys.modules['rtk_sf.vision_ocr'] = mod\n",
        encoding="utf-8",
    )
    return str(site_dir)


def _run(payload, extra_path=None):
    env = dict(os.environ)
    if extra_path:
        env["PYTHONPATH"] = f"{extra_path}:{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        HOOK,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )


def _image(tmp_path, name="shot.png"):
    path = tmp_path / name
    path.write_bytes(PNG_HEADER)
    return path


# ---------------------------------------------------------------------------
# Pass-through cases
# ---------------------------------------------------------------------------


def test_non_image_reads_pass_through_untouched():
    result = _run({"tool_name": "Read", "tool_input": {"file_path": "/tmp/notes.md"}})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_missing_path_is_ignored():
    result = _run({"tool_name": "Read", "tool_input": {}})
    assert result.returncode == 0
    assert result.stderr == ""


def test_malformed_payload_does_not_break_the_read():
    result = subprocess.run(HOOK, input="not json", capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    assert result.stderr == ""


# ---------------------------------------------------------------------------
# The regression: a noisy OCR stack must not fail the Read
# ---------------------------------------------------------------------------


# The stub's noise has to happen when the hook calls it: `sitecustomize` runs at
# interpreter startup, long before the hook's silenced section, so module-level
# writes here would test the harness rather than the hook. Import-time noise from
# a real engine is covered by test_the_real_environment_never_leaks_stderr.
NOISY_FAILING_ENGINE = (
    "import os, sys\n"
    "def extract_image_text(path, preprocess=True):\n"
    "    sys.stderr.write('UserWarning: torch.quantize_per_tensor is deprecated\\n')\n"
    "    os.write(2, b\"Creating model: ('PP-LCNet_x1_0_doc_ori', None, None)\\n\")\n"
    "    sys.stdout.write('progress: 50%\\n')\n"
    "    raise RuntimeError('engine could not be instantiated')\n"
)


def test_a_noisy_ocr_engine_cannot_fail_the_read(tmp_path):
    """
    Observed in the wild as `PreToolUse:Read hook error: Creating model: …`,
    which blocked reading the image at all. The engine's chatter — including
    writes straight to fd 2 from native code — must stay out of the protocol.
    """
    site = _stub_ocr(tmp_path, NOISY_FAILING_ENGINE)
    image = _image(tmp_path)

    result = _run({"tool_name": "Read", "tool_input": {"file_path": str(image)}}, extra_path=site)

    assert result.returncode == 0, "a failing OCR engine must fall back to the native Read"
    assert result.stderr == "", f"the hook leaked stderr: {result.stderr!r}"
    assert "progress" not in result.stdout, "engine stdout must not reach the protocol channel"


def test_successful_ocr_denies_the_read_with_the_text_as_the_reason(tmp_path):
    site = _stub_ocr(
        tmp_path,
        "import os\n"
        "def extract_image_text(path, preprocess=True):\n"
        "    os.write(2, b'loading model\\n')\n"
        "    return 'INVOICE 2026-09-29 total 1200'\n",
    )
    image = _image(tmp_path, "invoice.png")

    result = _run({"tool_name": "Read", "tool_input": {"file_path": str(image)}}, extra_path=site)

    assert result.returncode == 0, "the structured form blocks with exit 0, not exit 2"
    assert result.stderr == ""

    payload = json.loads(result.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "PreToolUse"
    assert payload["permissionDecision"] == "deny"
    reason = payload["permissionDecisionReason"]
    assert "INVOICE 2026-09-29 total 1200" in reason, "the OCR text is what Claude receives"
    assert "loading model" not in reason, "silenced output must not leak into the reason"


def test_empty_ocr_result_falls_back_rather_than_returning_nothing(tmp_path):
    site = _stub_ocr(
        tmp_path,
        "def extract_image_text(path, preprocess=True):\n    return ''\n",
    )
    image = _image(tmp_path, "blank.png")

    result = _run({"tool_name": "Read", "tool_input": {"file_path": str(image)}}, extra_path=site)

    # An empty transcription is not a useful answer; the image itself is.
    assert result.returncode == 0
    assert result.stderr == ""


def test_the_real_environment_never_leaks_stderr(tmp_path):
    """
    Runs against whatever OCR stack is actually installed. Either outcome is
    acceptable — text (exit 2) or fallback (exit 0) — but stderr must be empty
    either way, because that is what decides whether the Read survives.
    """
    image = _image(tmp_path, "real.png")
    result = _run({"tool_name": "Read", "tool_input": {"file_path": str(image)}})

    assert result.returncode == 0, "allow and deny both exit 0 in the structured form"
    assert result.stderr == "", f"the installed OCR stack leaked stderr: {result.stderr[:200]!r}"
    if result.stdout:
        payload = json.loads(result.stdout)["hookSpecificOutput"]
        assert payload["permissionDecision"] == "deny"
