"""
Pins the invariants that make hybrid delegation safe to trust unread.

Each test corresponds to a failure found while verifying the design against the
TokyoEdu project (25 Apex files, 179 detected methods) and a live
qwen2.5-coder:7b worker. No test here contacts a model — the worker boundary is
exercised through `gate.py`, which is where the trust decision actually lives.
"""

from __future__ import annotations

import pathlib

import pytest

from rtk_sf.hybrid.complexity import extract_methods, find_method
from rtk_sf.hybrid.gate import check_method_replacement, reindent
from rtk_sf.hybrid.state import (
    STATUS_COMPLETED,
    CorrectionMemory,
    HandoverState,
)
from rtk_sf.hybrid.worker import strip_code_fences

SOURCE = """public with sharing class AccountService {
    private static final String PREFIX = 'ACC';

    public AccountService(String prefix) {
        this.prefix = prefix;
    }

    public Boolean validateBillingAddress(Account acc) {
        if (acc == null) { return false; }
        return acc.BillingPostalCode != null;
    }

    public void executeComplexFinancialRollback(Id accId, Decimal amount) {
        Savepoint sp = Database.setSavepoint();
        for (Invoice__c inv : [SELECT Id FROM Invoice__c WHERE Account__c = :accId]) {
            update inv;
        }
    }

    @AuraEnabled
    public static String remoteGetter(Id recId) {
        return String.valueOf(recId);
    }
}"""


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_simple_method_is_low_tier():
    unit = find_method(SOURCE, "validateBillingAddress")
    assert unit is not None
    assert unit.tier == "LOW"
    assert unit.blockers == []


def test_risk_signals_force_high_regardless_of_size():
    """A 5-line method with a Savepoint and DML in a loop is not 'simple'."""
    unit = find_method(SOURCE, "executeComplexFinancialRollback")
    assert unit.tier == "HIGH"
    assert "transaction-control" in unit.blockers
    assert "dml-in-loop" in unit.blockers


def test_aurenabled_is_a_flag_not_a_blocker():
    """Measured on TokyoEdu: treating @AuraEnabled as a hard blocker forced 60
    of 179 methods to Claude, which makes delegation pointless on any
    LWC-based project. It raises the score instead of vetoing the route."""
    unit = find_method(SOURCE, "remoteGetter")
    assert "remote-entry" in unit.flags
    assert unit.blockers == []


def test_constructor_is_not_delegatable():
    """`_METHOD_SIG` matches a constructor only by capturing an access modifier
    as the return type. Delegating it invites the worker to 'fix' the missing
    return type, silently turning a constructor into a method."""
    unit = find_method(SOURCE, "AccountService")
    assert unit is not None
    assert unit.is_constructor
    assert not unit.splice_safe
    assert unit.tier == "HIGH"


def test_risk_flags_do_not_leak_across_method_boundaries():
    """The annotation lookback must stop at the previous closing brace."""
    simple = find_method(SOURCE, "validateBillingAddress")
    assert "transaction-control" not in simple.blockers


def test_per_method_hashes_are_independent():
    """A file-level hash marks every method dirty when any one changes, which
    makes method-level handover meaningless."""
    units = {u.name: u.body_hash for u in extract_methods(SOURCE)}
    assert len(set(units.values())) == len(units)

    edited = SOURCE.replace("return acc.BillingPostalCode != null;", "return true;")
    after = {u.name: u.body_hash for u in extract_methods(edited)}
    changed = [k for k in units if units[k] != after[k]]
    assert changed == ["validateBillingAddress"]


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def test_identity_splice_is_byte_exact():
    """The invariant the whole design rests on."""
    for unit in extract_methods(SOURCE):
        if not unit.splice_safe:
            continue
        own = SOURCE[unit.sig_start : unit.body_close + 1]
        result, spliced = check_method_replacement(SOURCE, unit, own)
        assert spliced == SOURCE, unit.name


def test_gate_blocks_whole_class_rewrite():
    """The characteristic small-model failure: asked for one method, returns
    the class — and in doing so drops or mangles the others."""
    unit = find_method(SOURCE, "validateBillingAddress")
    result, spliced = check_method_replacement(SOURCE, unit, SOURCE)
    assert not result.ok
    assert spliced == SOURCE
    assert any("expected exactly 1" in e for e in result.errors)


def test_gate_blocks_truncated_output():
    unit = find_method(SOURCE, "validateBillingAddress")
    good = "public Boolean validateBillingAddress(Account acc) { return false; }"
    result, spliced = check_method_replacement(SOURCE, unit, good, truncated=True)
    assert not result.ok
    assert spliced == SOURCE


def test_gate_blocks_renamed_method():
    unit = find_method(SOURCE, "validateBillingAddress")
    result, _ = check_method_replacement(
        SOURCE, unit, "public Boolean validateSomethingElse(Account acc) { return false; }"
    )
    assert not result.ok
    assert any("expected 'validateBillingAddress'" in e for e in result.errors)


def test_gate_blocks_governor_limit_violation():
    unit = find_method(SOURCE, "validateBillingAddress")
    bad = (
        "public Boolean validateBillingAddress(Account acc) {\n"
        "    for (Account a : [SELECT Id FROM Account]) { update a; }\n"
        "    return true;\n"
        "}"
    )
    result, _ = check_method_replacement(SOURCE, unit, bad)
    assert not result.ok


def test_brace_counting_is_string_and_comment_aware():
    """`code.count("{")` miscounts any brace inside a literal or comment, and
    would reject this valid body."""
    unit = find_method(SOURCE, "validateBillingAddress")
    ok_body = (
        "public Boolean validateBillingAddress(Account acc) {\n"
        "    String s = 'a { b';  // trailing } in a comment\n"
        "    return s != null;\n"
        "}"
    )
    result, spliced = check_method_replacement(SOURCE, unit, ok_body)
    assert result.ok, result.errors
    assert spliced != SOURCE


def test_gate_detects_collateral_damage():
    """A replacement that also edits a sibling method must be refused even
    when the result is syntactically valid."""
    unit = find_method(SOURCE, "validateBillingAddress")
    sneaky = (
        "public Boolean validateBillingAddress(Account acc) {\n"
        "    return false;\n"
        "}\n\n"
        "    public Boolean injected(Account acc) {\n"
        "        return true;\n"
        "    }"
    )
    result, spliced = check_method_replacement(SOURCE, unit, sneaky)
    assert not result.ok
    assert spliced == SOURCE


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_reindent_preserves_relative_structure():
    """A column-0 completion keeps its internal nesting and gains the target
    indent; flattening it produces valid Apex and an unreviewable diff."""
    out = reindent("void f() {\n    if (x) {\n        y();\n    }\n}", "    ")
    assert out.splitlines() == [
        "    void f() {",
        "        if (x) {",
        "            y();",
        "        }",
        "    }",
    ]


def test_reindent_handles_already_indented_completion():
    out = reindent("    void g() {\n        return;\n    }", "    ")
    assert out.splitlines() == ["    void g() {", "        return;", "    }"]


def test_reindent_leaves_blank_lines_empty():
    assert "   \n" not in reindent("void f() {\n\n    y();\n}", "    ")


def test_strip_code_fences_removes_markdown_and_prose():
    """Measured: the model emits a fence even when told not to."""
    raw = "Here you go:\n```apex\npublic void x() {\n    return;\n}\n```\nHope this helps!"
    assert strip_code_fences(raw) == "public void x() {\n    return;\n}"


def test_strip_code_fences_is_a_noop_for_bare_code():
    assert strip_code_fences("public void y() { }") == "public void y() { }"


# ---------------------------------------------------------------------------
# State and correction memory
# ---------------------------------------------------------------------------

def test_state_survives_a_spec_regeneration(tmp_path):
    """Handover state must not live in `.rtk-sf/specs/`: the indexer rewrites
    those files wholesale, and `watcher.py` reindexes on every save — so a
    worker's own write would erase the record of it."""
    rtk = tmp_path / ".rtk-sf"
    (rtk / "specs").mkdir(parents=True)
    spec = rtk / "specs" / "AccountService.yaml"
    spec.write_text("component: AccountService\n", encoding="utf-8")

    st = HandoverState(rtk, "AccountService")
    st.record("validateBillingAddress", STATUS_COMPLETED, body_hash="abc123")
    st.save()

    spec.write_text("component: AccountService\nregenerated: true\n", encoding="utf-8")

    assert HandoverState(rtk, "AccountService").get("validateBillingAddress") is not None


def test_state_round_trips_japanese(tmp_path):
    st = HandoverState(tmp_path / ".rtk-sf", "AccountService")
    st.record("f", STATUS_COMPLETED, summary="郵便番号の検証を追加")
    st.save()
    assert "郵便番号の検証を追加" in st.path.read_text(encoding="utf-8")


def test_state_tolerates_a_corrupt_file(tmp_path):
    """A handover log is an optimization; a parse failure must degrade to 'no
    history', never crash the tool."""
    rtk = tmp_path / ".rtk-sf"
    (rtk / "handover").mkdir(parents=True)
    (rtk / "handover" / "AccountService.yaml").write_text("{ this: is: not: yaml", encoding="utf-8")
    assert HandoverState(rtk, "AccountService").revision == 0


def test_delta_returns_only_unseen_records(tmp_path):
    st = HandoverState(tmp_path / ".rtk-sf", "AccountService")
    st.record("a", STATUS_COMPLETED)
    st.record("b", STATUS_COMPLETED)
    delta = st.render_delta(since=1)
    assert "b" in delta and "] a" not in delta


def test_is_current_detects_a_stale_record(tmp_path):
    st = HandoverState(tmp_path / ".rtk-sf", "AccountService")
    st.record("f", STATUS_COMPLETED, body_hash="hash-v1")
    assert st.is_current("f", "hash-v1")
    assert not st.is_current("f", "hash-v2")


def test_correction_window_is_bounded(tmp_path):
    """Unbounded bad-code examples would evict the task itself: a 300-LOC class
    is ~4,200 tokens per failure, against a worker window of 4,096."""
    st = HandoverState(tmp_path / ".rtk-sf", "AccountService")
    mem = CorrectionMemory(st)
    for i in range(5):
        mem.add("f", f"critique number {i}", bad_body="public void f() { }")
    block = mem.render_for_worker("f")
    assert block.count("Failed attempt") == 2
    assert "critique number 4" in block


def test_resolve_promotes_lessons_and_drops_bad_code(tmp_path):
    """On success the critique survives as a code-free project rule; the wrong
    code does not, because a pile of near-identical bad examples biases a small
    model toward that shape."""
    st = HandoverState(tmp_path / ".rtk-sf", "AccountService")
    mem = CorrectionMemory(st)
    mem.add("f", "Use String.isBlank for String params.", bad_body="public void f() { bad(); }")
    mem.resolve("f")

    other = mem.render_for_worker("someOtherMethod")
    assert "String.isBlank" in other
    assert "bad()" not in other


def test_lessons_are_deduplicated(tmp_path):
    st = HandoverState(tmp_path / ".rtk-sf", "AccountService")
    mem = CorrectionMemory(st)
    mem.lessons.add("Always null-check the parameter.")
    assert mem.lessons.add("always   NULL-check the parameter.") is False
    assert len(mem.lessons.top()) == 1


# ---------------------------------------------------------------------------
# Corpus-level invariant
# ---------------------------------------------------------------------------

_CORPUS = pathlib.Path("/Users/hoangkagawa/TokyoEdu/force-app")


@pytest.mark.skipif(not _CORPUS.exists(), reason="TokyoEdu corpus not present")
def test_identity_splice_is_byte_exact_across_real_corpus():
    """The regression that matters: run every real method through the splice
    path and require the file back unchanged. This is what caught both the
    lost-indentation bug and the constructor false positive."""
    checked = 0
    for path in sorted(_CORPUS.rglob("*.cls")):
        src = path.read_text(encoding="utf-8", errors="replace")
        for unit in extract_methods(src):
            if not unit.splice_safe:
                continue
            own = src[unit.sig_start : unit.body_close + 1]
            result, spliced = check_method_replacement(src, unit, own)
            assert result.ok, f"{path.name}:{unit.name} {result.errors}"
            assert spliced == src, f"{path.name}:{unit.name}"
            checked += 1
    assert checked > 100, f"corpus too small to be meaningful ({checked})"


# ---------------------------------------------------------------------------
# No-worker path
# ---------------------------------------------------------------------------

def test_detection_is_cached_so_an_offline_host_is_probed_once():
    """The MCP server builds a fresh Orchestrator per tool call. Without a
    cache, an unreachable worker host costs the full probe timeout on every
    hybrid_* call — measured at 3.01 s against an unroutable address."""
    import time

    from rtk_sf.hybrid.worker import clear_detection_cache, detect_worker

    clear_detection_cache()
    url = "http://127.0.0.1:59998"  # refused, so the first probe is cheap too

    first = detect_worker(url, timeout=0.5)
    assert not first.available

    start = time.monotonic()
    second = detect_worker(url, timeout=0.5)
    elapsed = time.monotonic() - start

    assert second is first, "second probe should be served from cache"
    assert elapsed < 0.05, f"cached probe took {elapsed:.3f}s"


def test_delegate_writes_nothing_without_a_worker(tmp_path):
    """The no-worker path must refuse cleanly, not partially apply."""
    from rtk_sf.hybrid.orchestrator import Orchestrator
    from rtk_sf.hybrid.worker import clear_detection_cache

    classes = tmp_path / "force-app" / "classes"
    classes.mkdir(parents=True)
    target = classes / "AccountService.cls"
    target.write_text(SOURCE, encoding="utf-8")

    clear_detection_cache()
    orch = Orchestrator(tmp_path, base_url="http://127.0.0.1:59997")
    out = orch.delegate(
        "AccountService", [{"method": "validateBillingAddress", "instruction": "Add a guard."}]
    )

    assert "No local worker available" in out
    assert target.read_text(encoding="utf-8") == SOURCE
    assert not (classes / "AccountService.cls.rtk-bak").exists()


def test_plan_and_state_still_work_without_a_worker(tmp_path):
    """Routing and handover reads are local computation — they must not depend
    on a worker being reachable."""
    from rtk_sf.hybrid.orchestrator import Orchestrator
    from rtk_sf.hybrid.worker import clear_detection_cache

    classes = tmp_path / "force-app" / "classes"
    classes.mkdir(parents=True)
    (classes / "AccountService.cls").write_text(SOURCE, encoding="utf-8")

    clear_detection_cache()
    orch = Orchestrator(tmp_path, base_url="http://127.0.0.1:59996")

    plan = orch.plan("AccountService")
    assert "worker: none" in plan
    assert "validateBillingAddress" in plan

    assert "critique recorded" in orch.critique(
        "AccountService", "validateBillingAddress", "Use String.isBlank for String params."
    )
    assert "validateBillingAddress" in orch.state("AccountService", since=0)
