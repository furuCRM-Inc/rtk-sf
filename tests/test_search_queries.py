"""
Pins the CJK query defects in rtk_sf/search.py (issue #28, item 5).

The FTS5 index uses the trigram tokenizer, and the query layer used to hand
the user's string to MATCH unchanged. Two consequences made Japanese search
look broken while an English word found the same component:

1. Bare terms are AND-ed by FTS5, so "セルフ登録 職員番号" returned 0 results
   as soon as the two words lived in sibling components.
2. A term below 3 characters matches nothing at all under the trigram
   tokenizer — and 2-character words ("職員", "番号") are everywhere in
   Japanese metadata labels.
"""

from __future__ import annotations

import pytest

from rtk_sf.search import SearchEngine

SPECS = {
    # Holds both words of the "セルフ登録 職員番号" query.
    "SelfRegisterStaffController": """
component: SelfRegisterStaffController
type: ApexClass
file: force-app/main/default/classes/SelfRegisterStaffController.cls
notes: セルフ登録画面で職員番号を検証する。
""",
    # Holds only "セルフ登録" — the other query word lives on Staff__c.
    "SelfRegisterController": """
component: SelfRegisterController
type: ApexClass
file: force-app/main/default/classes/SelfRegisterController.cls
notes: セルフ登録画面のコントローラ。
""",
    "Staff__c": """
component: Staff__c
type: CustomObject
file: force-app/main/default/objects/Staff__c
label: 職員
fields:
  - name: StaffNumber__c
    label: 職員番号
  - name: BirthDate__c
    label: 生年月日
""",
    # Holds only "有資格者リスト" — "生年月日" lives on Staff__c.
    "QualifiedListController": """
component: QualifiedListController
type: ApexClass
file: force-app/main/default/classes/QualifiedListController.cls
notes: 有資格者リストを生成する。
""",
    "AccountService": """
component: AccountService
type: ApexClass
file: force-app/main/default/classes/AccountService.cls
notes: Account business logic.
""",
}


@pytest.fixture()
def engine(tmp_path):
    specs_dir = tmp_path / ".rtk-sf" / "specs"
    specs_dir.mkdir(parents=True)
    for name, body in SPECS.items():
        (specs_dir / f"{name}.yaml").write_text(body.lstrip(), encoding="utf-8")

    eng = SearchEngine(tmp_path)
    assert eng.sync_from_specs() == len(SPECS)
    yield eng
    eng.close()


def _names(results):
    return [r["name"] for r in results]


# ---------------------------------------------------------------------------
# Multi-word Japanese queries
# ---------------------------------------------------------------------------


def test_japanese_multi_word_query_prefers_the_component_holding_every_term(engine):
    results = engine.search("セルフ登録 職員番号", limit=5)
    assert _names(results)[0] == "SelfRegisterStaffController"
    assert results[0]["matched_terms"] == 2


def test_japanese_multi_word_query_spanning_components_still_returns_hits(engine):
    # "生年月日" is only on Staff__c, "有資格者リスト" only on
    # QualifiedListController. FTS5's implicit AND found neither.
    names = _names(engine.search("生年月日 有資格者リスト", limit=5))
    assert "Staff__c" in names
    assert "QualifiedListController" in names


def test_partial_matches_rank_below_full_matches(engine):
    results = engine.search("セルフ登録 職員番号", limit=5)
    assert _names(results)[0] == "SelfRegisterStaffController"
    # SelfRegisterController and Staff__c hold one term each.
    assert all(r["matched_terms"] == 1 for r in results[1:])


def test_results_are_ranked_by_how_many_terms_matched(engine):
    results = engine.search("職員 番号 生年月日", limit=5)
    counts = [r["matched_terms"] for r in results]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 3


def test_query_of_only_short_terms_is_not_wiped_out(engine):
    # Both terms are 2 characters — unrepresentable in a trigram index, so
    # the FTS path matched nothing and the whole query returned 0 results.
    assert "Staff__c" in _names(engine.search("職員 番号", limit=5))


# ---------------------------------------------------------------------------
# Terms the trigram index cannot represent
# ---------------------------------------------------------------------------


def test_two_character_japanese_term_is_found(engine):
    # Shorter than a trigram — only the LIKE path can reach it.
    assert "Staff__c" in _names(engine.search("職員", limit=5))


def test_assert_fixture_covers_the_reported_queries(engine):
    # Guards the fixture itself: the point of these specs is that no single
    # component contains every word of the reported queries.
    assert len(engine.search("生年月日 有資格者リスト", limit=5)) >= 2


def test_single_character_term_is_found(engine):
    assert _names(engine.search("職", limit=5))


# ---------------------------------------------------------------------------
# Normalization and syntax
# ---------------------------------------------------------------------------


def test_full_width_ascii_query_matches_half_width_index(engine):
    assert "AccountService" in _names(engine.search("ＡｃｃｏｕｎｔＳｅｒｖｉｃｅ", limit=5))


def test_japanese_punctuation_between_terms_is_not_searched(engine):
    results = engine.search("セルフ登録、職員番号。", limit=5)
    assert _names(results)[0] == "SelfRegisterStaffController"


def test_english_query_still_works(engine):
    assert "SelfRegisterController" in _names(engine.search("selfregistercontroller", limit=5))


def test_fts_prefix_syntax_is_passed_through(engine):
    assert "Staff__c" in _names(engine.search("staff*", limit=5))


def test_unmatched_query_returns_nothing(engine):
    assert engine.search("存在しないコンポーネント名", limit=5) == []


def test_empty_query_returns_nothing(engine):
    assert engine.search("   ", limit=5) == []


def test_limit_is_respected(engine):
    assert len(engine.search("職員 番号 生年月日 account", limit=2)) == 2
