"""
Pins the duplicated-body defect in rtk_sf/skeleton.py (issue #28, item 6).

`_METHOD_SIG` also matches control flow — `else if (cond) {` parses as
return type "else", name "if", params "cond". Such a match sits *inside* a
method body, so the emit loop rewound its cursor and printed the enclosing
body a second time, interleaved with `/* Logic Hidden */` placeholders. The
reported symptom was "the same method body appears twice alongside collapsed
fragments".
"""

from __future__ import annotations

from rtk_sf.skeleton import build_skeleton

SOURCE = """public with sharing class StaffService {
    private static final String PREFIX = 'STF';

    public StaffService() {
        this.cache = new Map<Id, Staff__c>();
    }

    public void saveRecord(Staff__c s) {
        if (s == null) {
            return;
        } else if (String.isBlank(s.StaffNumber__c)) {
            throw new IllegalArgumentException('職員番号は必須です');
        }
        insert s;
    }

    public Integer calculateAge(Date birthDate) {
        Integer years = birthDate.monthsBetween(Date.today()) / 12;
        if (years < 0) {
            years = 0;
        } else if (years > 150) {
            years = 150;
        }
        return years;
    }

    private class Row {
        public String label;
        public String valueOf(String key) {
            return key + PREFIX;
        }
    }
}
"""


def test_focused_body_appears_exactly_once():
    out = build_skeleton(SOURCE, focus_methods=["saveRecord"])
    assert out.count("insert s;") == 1
    assert out.count("throw new IllegalArgumentException") == 1


def test_collapsed_body_leaves_no_leaked_fragments():
    out = build_skeleton(SOURCE, focus_methods=["saveRecord"])
    # Nothing from calculateAge's body may survive.
    assert "years = 150;" not in out
    assert "Integer years" not in out
    assert "monthsBetween" not in out


def test_one_placeholder_per_collapsed_method():
    out = build_skeleton(SOURCE, focus_methods=["saveRecord"])
    # calculateAge and Row.valueOf — the constructor and saveRecord stay whole.
    assert out.count("/* Logic Hidden */") == 2


def test_control_flow_is_not_reported_as_a_method():
    out = build_skeleton(SOURCE, focus_methods=[])
    # An `else if` must never acquire its own collapsed block.
    assert "else if" not in out


def test_constructor_and_fields_are_preserved():
    out = build_skeleton(SOURCE, focus_methods=[])
    assert "private static final String PREFIX = 'STF';" in out
    assert "this.cache = new Map<Id, Staff__c>();" in out


def test_signatures_all_survive():
    out = build_skeleton(SOURCE, focus_methods=[])
    for sig in (
        "public void saveRecord(Staff__c s)",
        "public Integer calculateAge(Date birthDate)",
        "public String valueOf(String key)",
        "private class Row",
    ):
        assert sig in out


def test_skeleton_is_shorter_than_the_source():
    out = build_skeleton(SOURCE, focus_methods=["saveRecord"])
    assert len(out) < len(SOURCE)


def test_source_without_methods_is_returned_unchanged():
    src = "public class Empty {\n    private Integer x = 1;\n}\n"
    assert build_skeleton(src) == src
