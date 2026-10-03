"""
Pins the relevance defects in rtk_sf/search.py (issue #31, request B).

After #28 a Japanese multi-word query returned results instead of nothing, but
the wrong ones. For `セルフ登録 職員番号 生年月日 有資格者リスト` the top hits
were `Entry__c.BirthDate__c` and `Entry__c.StaffNumber__c` — two
leaf fields — while the LWC `selfRegistration` and the Apex
`SelfRegistrationController` / `SelfRegistrationService` that implement the
feature did not appear at all. `セルフ登録` on its own returned 0, though
`register` found all three.

Two causes, both fixed here:

1. Nothing connected a Japanese word to an English identifier, so
   「セルフ登録」 could not reach `SelfRegistrationController`.
2. Ranking considered only how many terms matched, so a field whose label
   happened to contain one query word outranked the component the query was
   about.
"""

from __future__ import annotations

import pytest

from rtk_sf.search import SearchEngine

SPECS = {
    "selfRegistration": """
component: selfRegistration
type: LightningComponentBundle
file: force-app/main/default/lwc/selfRegistration/selfRegistration.js
""",
    "SelfRegistrationController": """
component: SelfRegistrationController
type: ApexClass
file: force-app/main/default/classes/SelfRegistrationController.cls
methods:
  - submitApplication(Entry__c app)
""",
    "SelfRegistrationService": """
component: SelfRegistrationService
type: ApexClass
file: force-app/main/default/classes/SelfRegistrationService.cls
methods:
  - validate(Entry__c app)
""",
    "Entry__c.StaffNumber__c": """
component: Entry__c.StaffNumber__c
type: CustomField
file: force-app/main/default/objects/Entry__c/fields/StaffNumber__c.field-meta.xml
label: 職員番号
""",
    "Entry__c.BirthDate__c": """
component: Entry__c.BirthDate__c
type: CustomField
file: force-app/main/default/objects/Entry__c/fields/BirthDate__c.field-meta.xml
label: 生年月日
""",
    "Entry__c": """
component: Entry__c
type: CustomObject
file: force-app/main/default/objects/Entry__c
label: 申込
fields:
  - name: StaffNumber__c
    label: 職員番号
  - name: BirthDate__c
    label: 生年月日
""",
}

REPORTED_QUERY = "セルフ登録 職員番号 生年月日 有資格者リスト"


def _build(tmp_path, synonyms: str | None = None) -> SearchEngine:
    specs_dir = tmp_path / ".rtk-sf" / "specs"
    specs_dir.mkdir(parents=True)
    for name, body in SPECS.items():
        (specs_dir / f"{name}.yaml").write_text(body.lstrip(), encoding="utf-8")
    if synonyms is not None:
        (tmp_path / ".rtk-sf" / "synonyms.yaml").write_text(synonyms, encoding="utf-8")
    eng = SearchEngine(tmp_path)
    eng.sync_from_specs()
    return eng


@pytest.fixture()
def engine(tmp_path):
    eng = _build(tmp_path)
    yield eng
    eng.close()


def _names(results):
    return [r["name"] for r in results]


# ---------------------------------------------------------------------------
# The reported query
# ---------------------------------------------------------------------------


def test_reported_query_surfaces_the_implementing_components(engine):
    # The issue's limit=3 call returned two leaf fields and nothing else.
    names = _names(engine.search(REPORTED_QUERY, limit=3))
    assert {"selfRegistration", "SelfRegistrationController", "SelfRegistrationService"} & set(names)


def test_reported_query_does_not_lead_with_leaf_fields(engine):
    top = _names(engine.search(REPORTED_QUERY, limit=3))
    assert "Entry__c.BirthDate__c" not in top
    assert "Entry__c.StaffNumber__c" not in top


def test_every_implementing_component_outranks_the_leaf_fields(engine):
    ranks = {r["name"]: i for i, r in enumerate(engine.search(REPORTED_QUERY, limit=10))}
    worst_component = max(
        ranks["selfRegistration"],
        ranks["SelfRegistrationController"],
        ranks["SelfRegistrationService"],
    )
    best_field = min(
        ranks["Entry__c.StaffNumber__c"], ranks["Entry__c.BirthDate__c"]
    )
    assert worst_component < best_field


def test_the_object_holding_two_query_terms_still_ranks_first(engine):
    # Entry__c carries both 職員番号 and 生年月日: 2 of 4 terms beats 1.
    top = engine.search(REPORTED_QUERY, limit=1)[0]
    assert top["name"] == "Entry__c"
    assert top["matched_terms"] == 2


def test_single_japanese_word_reaches_an_english_identifier(engine):
    # Returned 0 results before: no indexed text contains "セルフ登録".
    assert "SelfRegistrationController" in _names(engine.search("セルフ登録", limit=5))


def test_english_word_still_reaches_the_same_components(engine):
    assert "SelfRegistrationController" in _names(engine.search("register", limit=5))


# ---------------------------------------------------------------------------
# Ranking rules
# ---------------------------------------------------------------------------


def test_name_matches_outrank_body_matches(engine):
    # Both the object and its field mention 職員番号; the field carries it as
    # its own name, the object only in a field list.
    results = engine.search("職員番号", limit=5)
    assert results[0]["name"] == "Entry__c.StaffNumber__c"
    assert results[0]["name_matches"] >= 1


def test_implementation_units_outrank_their_fields_on_a_tie(engine):
    results = engine.search("申込", limit=5)
    ranks = {r["name"]: i for i, r in enumerate(results)}
    assert ranks["Entry__c"] < min(
        ranks.get("Entry__c.StaffNumber__c", 99),
        ranks.get("Entry__c.BirthDate__c", 99),
    )


def test_more_matched_terms_still_wins(engine):
    results = engine.search("職員番号 生年月日 申込", limit=5)
    assert results[0]["matched_terms"] == 3
    assert results[0]["name"] == "Entry__c"


def test_direct_matches_are_reported_separately_from_synonyms(engine):
    direct = engine.search("職員番号", limit=1)[0]
    assert direct["direct_matches"] == 1

    via_synonym = engine.search("セルフ登録", limit=1)[0]
    assert via_synonym["matched_terms"] == 1
    assert via_synonym["direct_matches"] == 0


# ---------------------------------------------------------------------------
# Synonyms
# ---------------------------------------------------------------------------


def test_english_query_reaches_a_japanese_label(engine):
    assert "Entry__c.StaffNumber__c" in _names(engine.search("staff number", limit=5))


def test_project_synonyms_file_is_applied(tmp_path):
    eng = _build(tmp_path, synonyms="有資格者リスト: [SelfRegistrationService]\n")
    try:
        assert "SelfRegistrationService" in _names(eng.search("有資格者リスト", limit=5))
    finally:
        eng.close()


def test_project_synonyms_override_the_builtin_table(tmp_path):
    eng = _build(tmp_path, synonyms="セルフ登録: [Entry__c]\n")
    try:
        assert _names(eng.search("セルフ登録", limit=1)) == ["Entry__c"]
    finally:
        eng.close()


def test_malformed_synonyms_file_does_not_break_search(tmp_path):
    eng = _build(tmp_path, synonyms="this: [is\n  not: valid yaml\n")
    try:
        assert _names(eng.search("職員番号", limit=5))
    finally:
        eng.close()


def test_unmatched_query_still_returns_nothing(engine):
    assert engine.search("存在しない機能名", limit=5) == []
