"""
Pins the pasted-output corruption in rtk_sf/nlp_compactor.py (issue #28,
item 1: "rtk's hook rewrites CLI output — `sf … --json` goes through rtk's
formatting, so it cannot be read as JSON").

The UserPromptSubmit hook replaces the prompt the model sees, and the
compactor ran its prose rules over the whole thing:

  * `re.sub(r"[ \t]{2,}", " ")` flattened indentation inside fenced blocks,
    turning pasted YAML/JSON into something that no longer parses;
  * the English filler list deleted words *inside* quoted machine text —
    eslint's "'just' is assigned a value but never used" came out as
    "'' is assigned a value but never used".
"""

from __future__ import annotations

from rtk_sf.nlp_compactor import compact_prompt, is_code_heavy

ESLINT = """lint が通りません。直してください。
```
/Users/me/app/lwc/foo/foo.js
  12:5   error  'just' is assigned a value but never used  no-unused-vars
  40:11  warning  Unexpected console statement              no-console
```
"""

SF_JSON = """これを見てください
$ sf apex list log --json
{
  "status": 0,
  "result": [
    {"Id": "07L00", "Operation": "Api", "Status": "Success"}
  ]
}
"""

YAML_FENCE = """この設定を直してください
```yaml
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: python3 -m rtk_sf.hooks.bash_guard
```
"""


def _compact(text: str) -> str:
    return compact_prompt(text)[0]


# ---------------------------------------------------------------------------
# Machine text survives byte for byte
# ---------------------------------------------------------------------------


def test_fenced_block_is_preserved_exactly():
    fence = YAML_FENCE[YAML_FENCE.index("```") :].rstrip("\n")
    assert fence in _compact(YAML_FENCE)


def test_indentation_inside_a_fence_is_not_collapsed():
    out = _compact(YAML_FENCE)
    assert "  PreToolUse:" in out
    assert "          command: python3 -m rtk_sf.hooks.bash_guard" in out


def test_identifier_quoted_in_lint_output_is_not_deleted():
    out = _compact(ESLINT)
    assert "'just' is assigned a value but never used" in out


def test_eslint_column_alignment_is_preserved():
    assert "  12:5   error" in _compact(ESLINT)


def test_unfenced_cli_json_is_preserved():
    out = _compact(SF_JSON)
    assert '  "status": 0,' in out
    assert '    {"Id": "07L00", "Operation": "Api", "Status": "Success"}' in out


def test_inline_code_span_is_preserved():
    text = "`please check this` を参照してください。あとで確認してください。"
    assert "`please check this`" in _compact(text)


def test_shell_prompt_line_is_preserved():
    assert "$ sf apex list log --json" in _compact(SF_JSON)


def test_python_traceback_is_preserved():
    text = 'エラーです。直してください。\nTraceback (most recent call last):\n  File "a.py", line 3\n'
    out = _compact(text)
    assert 'File "a.py", line 3' in out


# ---------------------------------------------------------------------------
# Prose is still compacted
# ---------------------------------------------------------------------------


def test_japanese_politeness_is_still_stripped():
    out = _compact("すみません、Account__c の 一覧 を 取得 してください。よろしくお願いします。")
    assert "すみません" not in out
    assert "よろしくお願いします" not in out
    assert "Account__c" in out


def test_english_filler_is_still_stripped_in_prose():
    out = _compact("Could you please just deploy AccountService.cls? Thanks!")
    assert "please" not in out.lower()
    assert "AccountService.cls" in out


def test_prose_around_a_fence_is_still_compacted():
    out = _compact(YAML_FENCE)
    assert "してください" not in out.split("```")[0]


def test_compact_prompt_reports_real_lengths():
    text = "すみません、確認してください。"
    compacted, orig, new = compact_prompt(text)
    assert orig == len(text)
    assert new == len(compacted)


def test_no_placeholder_leaks_into_the_output():
    for text in (ESLINT, SF_JSON, YAML_FENCE):
        assert "\x00" not in _compact(text)


# ---------------------------------------------------------------------------
# is_code_heavy — the hook's skip switch
# ---------------------------------------------------------------------------


def test_pasted_output_is_code_heavy():
    for text in (ESLINT, SF_JSON, YAML_FENCE):
        assert is_code_heavy(text)


def test_plain_request_is_not_code_heavy():
    assert not is_code_heavy(
        "すみません、Account__c の 申込 一覧 を 取得 してください。よろしくお願いします。"
    )


def test_empty_prompt_is_not_code_heavy():
    assert not is_code_heavy("")
