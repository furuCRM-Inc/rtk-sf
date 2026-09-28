"""
Ports the representative pure-function cases from flash-agent-stack's
jev-intent.test.ts and soqlCompiler.test.ts to pin the Python port
(rtk_sf/soql_compiler.py) against the same proven behavior.
"""

from __future__ import annotations

from rtk_sf.soql_compiler import (
    FIELD_SYNONYM_MAP,
    build_soql_filter,
    compile_query,
    extract_amount_op,
    extract_amount_value,
    extract_date_literal,
    extract_limit,
    extract_null_check_filter,
    extract_simple_update,
)


# ---------------------------------------------------------------------------
# extract_amount_value — compound Japanese numerals
# ---------------------------------------------------------------------------


def test_sums_compound_oku_plus_man_amount():
    assert extract_amount_value("1億5000万円の商談") == 150_000_000


def test_single_oku_amount():
    assert extract_amount_value("5億円以上の商談") == 500_000_000


def test_single_man_amount():
    assert extract_amount_value("500万円の商談") == 5_000_000


def test_single_senman_amount():
    assert extract_amount_value("3千万円の商談") == 30_000_000


def test_does_not_fold_unrelated_man_figures_far_apart():
    v = extract_amount_value("従業員500万人以上、収益とは無関係の会社概要のみ確認")
    assert v == 5_000_000


# ---------------------------------------------------------------------------
# extract_amount_value — English magnitude words
# ---------------------------------------------------------------------------


def test_parses_dollar_million():
    assert extract_amount_value("opportunities over $3 million") == 3_000_000


def test_parses_k_suffix():
    assert extract_amount_value("deals worth 50k or more") == 50_000


def test_parses_plain_large_number_with_currency_symbol():
    assert extract_amount_value("opportunities over $500,000") == 500_000


def test_parses_fullwidth_compound_amount():
    assert extract_amount_value("１００万円の商談") == 1_000_000


def test_parses_fullwidth_digit_and_comma_amount():
    assert extract_amount_value("１，０００，０００円の商談") == 1_000_000


def test_parses_plain_comma_separated_number():
    assert extract_amount_value("deals worth 100,000 or more") == 100_000


def test_parses_dollar_comma_number_combined_with_magnitude_word():
    assert extract_amount_value("opportunities over $1,500,000") == 1_500_000


def test_parses_comma_number_combined_with_man():
    assert extract_amount_value("5,000万円の商談") == 50_000_000


# ---------------------------------------------------------------------------
# extract_amount_op
# ---------------------------------------------------------------------------


def test_detects_exactly_as_equality():
    assert extract_amount_op("ちょうど500万円の商談") == "eq"
    assert extract_amount_op("exactly $500,000 deals") == "eq"


def test_detects_gte_lte():
    assert extract_amount_op("500万円以上") == "gte"
    assert extract_amount_op("500万円以下") == "lte"
    assert extract_amount_op("over 500000") == "gte"
    assert extract_amount_op("under 500000") == "lte"


# ---------------------------------------------------------------------------
# extract_limit
# ---------------------------------------------------------------------------


def test_all_every_keywords_return_max_page():
    assert extract_limit("show all 200 open cases") == 200
    assert extract_limit("show every case") == 200
    assert extract_limit("未解決ケースをすべて見せて") == 200
    assert extract_limit("全てのケースを見せて") == 200


def test_explicit_count_within_ceiling():
    assert extract_limit("5件見せて") == 5
    assert extract_limit("show me 30 records") == 30


def test_clamps_explicit_count_above_ceiling():
    assert extract_limit("show me 500 rows") == 100


def test_falls_back_to_default_of_20():
    assert extract_limit("show opportunities") == 20


# ---------------------------------------------------------------------------
# extract_date_literal
# ---------------------------------------------------------------------------


def test_resolves_named_periods_both_languages():
    assert extract_date_literal("this month") == "THIS_MONTH"
    assert extract_date_literal("今月") == "THIS_MONTH"
    assert extract_date_literal("来月") == "NEXT_MONTH"
    assert extract_date_literal("先月") == "LAST_MONTH"


def test_resolves_dynamic_n_day_windows():
    assert extract_date_literal("過去45日間") == "LAST_N_DAYS:45"
    assert extract_date_literal("last 45 days") == "LAST_N_DAYS:45"


def test_resolves_yesterday_tomorrow():
    assert extract_date_literal("yesterday") == "YESTERDAY"
    assert extract_date_literal("昨日") == "YESTERDAY"
    assert extract_date_literal("tomorrow") == "TOMORROW"
    assert extract_date_literal("明日") == "TOMORROW"


def test_resolves_ototoi_to_two_day_window():
    assert extract_date_literal("一昨日") == "LAST_N_DAYS:2"


def test_resolves_sakiototoi_to_three_day_window():
    assert extract_date_literal("一昨々日") == "LAST_N_DAYS:3"


# ---------------------------------------------------------------------------
# build_soql_filter — CreatedDate vs CloseDate heuristic
# ---------------------------------------------------------------------------


def test_new_deals_this_month_en_maps_to_created_date():
    result = build_soql_filter("show new deals this month", "Opportunity", set())
    cond = next((c for c in result["conditions"] if c["field"] in ("CreatedDate", "CloseDate")), None)
    assert cond is not None and cond["field"] == "CreatedDate"


def test_shinki_shodan_ja_maps_to_created_date():
    result = build_soql_filter("今月の新規商談", "Opportunity", set())
    cond = next((c for c in result["conditions"] if c["field"] in ("CreatedDate", "CloseDate")), None)
    assert cond is not None and cond["field"] == "CreatedDate"


def test_opportunities_closing_this_month_maps_to_close_date():
    result = build_soql_filter("opportunities closing this month", "Opportunity", set())
    cond = next((c for c in result["conditions"] if c["field"] in ("CreatedDate", "CloseDate")), None)
    assert cond is not None and cond["field"] == "CloseDate"


def test_deals_added_this_month_maps_to_created_date():
    result = build_soql_filter("deals added this month", "Opportunity", set())
    cond = next((c for c in result["conditions"] if c["field"] in ("CreatedDate", "CloseDate")), None)
    assert cond is not None and cond["field"] == "CreatedDate"


# ---------------------------------------------------------------------------
# build_soql_filter — Case status negation fix
# ---------------------------------------------------------------------------


def test_not_closed_case_resolves_is_closed_false():
    result = build_soql_filter("クローズしていないケース", "Case", set())
    cond = next((c for c in result["conditions"] if c["field"] == "IsClosed"), None)
    assert cond is not None and cond["value"] is False


def test_closed_case_control_resolves_is_closed_true():
    result = build_soql_filter("クローズ済みのケース", "Case", set())
    cond = next((c for c in result["conditions"] if c["field"] == "IsClosed"), None)
    assert cond is not None and cond["value"] is True


# ---------------------------------------------------------------------------
# build_soql_filter — Lead IsConverted
# ---------------------------------------------------------------------------


def test_converted_leads_resolves_true():
    result = build_soql_filter("converted leads", "Lead", set())
    cond = next((c for c in result["conditions"] if c["field"] == "IsConverted"), None)
    assert cond is not None and cond["value"] is True


def test_mihenkan_lead_resolves_false():
    result = build_soql_filter("未変換のリード", "Lead", set())
    cond = next((c for c in result["conditions"] if c["field"] == "IsConverted"), None)
    assert cond is not None and cond["value"] is False


# ---------------------------------------------------------------------------
# build_soql_filter — English parent-account context
# ---------------------------------------------------------------------------


def test_acme_corp_apostrophe_s_opportunities_resolves_account_name_like():
    result = build_soql_filter("Acme Corp's opportunities", "Opportunity", set())
    cond = next((c for c in result["conditions"] if c["field"] == "Account.Name"), None)
    assert cond is not None and cond["value"] == "%Acme Corp%"


def test_opportunities_for_acme_corp_resolves_account_name_like():
    result = build_soql_filter("opportunities for Acme Corp", "Opportunity", set())
    cond = next((c for c in result["conditions"] if c["field"] == "Account.Name"), None)
    assert cond is not None and cond["value"] == "%Acme Corp%"


def test_escapes_percent_and_underscore_in_account_name():
    result = build_soql_filter("100%_Growth Inc's opportunities", "Opportunity", set())
    cond = next((c for c in result["conditions"] if c["field"] == "Account.Name"), None)
    assert cond is not None and cond["value"] == "%100\\%\\_Growth Inc%"


# ---------------------------------------------------------------------------
# extract_simple_update
# ---------------------------------------------------------------------------


def test_change_the_amount_to_5000000_no_record_name():
    result = extract_simple_update("change the amount to 5000000", None, set())
    assert result is not None
    assert result["fields"] == {"Amount": 5_000_000}
    assert result["search_name"] is None


def test_set_stage_to_closed_won():
    result = extract_simple_update("set stage to Closed Won", None, set())
    assert result is not None
    assert result["fields"] == {"StageName": "Closed Won"}


def test_change_acme_corps_amount_resolves_record_name_and_field():
    result = extract_simple_update("change Acme Corp's amount to 5000000", None, set())
    assert result is not None
    assert result["search_name"] == "Acme Corp"
    assert result["fields"] == {"Amount": 5_000_000}


def test_japanese_update_pattern_control():
    result = extract_simple_update("A社の金額を1000万に変更", None, set())
    assert result is not None
    assert result["search_name"] == "A社"
    assert result["fields"] == {"Amount": 10_000_000}


def test_field_synonym_map_english_entries():
    assert FIELD_SYNONYM_MAP["amount"] == "Amount"
    assert FIELD_SYNONYM_MAP["stage"] == "StageName"
    assert FIELD_SYNONYM_MAP["owner"] == "OwnerId"


# ---------------------------------------------------------------------------
# extract_null_check_filter
# ---------------------------------------------------------------------------


def test_no_phone_number_resolves_phone_is_null():
    cond = extract_null_check_filter("leads with no phone number", set())
    assert cond == {"field": "Phone", "op": "is_null"}


def test_accounts_without_a_website_resolves_website_is_null():
    cond = extract_null_check_filter("accounts without a website", set())
    assert cond == {"field": "Website", "op": "is_null"}


def test_missing_email_resolves_email_is_null():
    cond = extract_null_check_filter("contacts missing email", set())
    assert cond == {"field": "Email", "op": "is_null"}


def test_denwa_bango_ga_misettei_resolves_phone_is_null():
    cond = extract_null_check_filter("電話番号が未設定の取引先", set())
    assert cond == {"field": "Phone", "op": "is_null"}


def test_returns_none_when_no_recognizable_field_mentioned():
    assert extract_null_check_filter("show me all accounts", set()) is None


def test_respects_valid_fields_when_schema_provided():
    assert extract_null_check_filter("no phone number", {"Website"}) is None
    assert extract_null_check_filter("no phone number", {"Phone"}) == {"field": "Phone", "op": "is_null"}


def test_wires_through_build_soql_filter_end_to_end():
    result = build_soql_filter("leads with no phone number", "Lead", set())
    cond = next((c for c in result["conditions"] if c["field"] == "Phone"), None)
    assert cond == {"field": "Phone", "op": "is_null"}


# ---------------------------------------------------------------------------
# compile_query — SOQL injection safety (single-quote escaping)
# ---------------------------------------------------------------------------


def _base_input(**overrides):
    return {"intent": "SOQL_SEARCH", "sobject": "Account", "conditions": [], **overrides}


def test_escapes_single_quote_in_like_value():
    result = compile_query(_base_input(conditions=[{"field": "Name", "op": "like", "value": "%O'Reilly%"}]))
    assert "LIKE '%O\\'Reilly%'" in result["query"]
    assert "LIKE '%O'Reilly%'" not in result["query"]


def test_escapes_single_quote_in_eq_value():
    result = compile_query(_base_input(conditions=[{"field": "Name", "op": "eq", "value": "D'Angelo Corp"}]))
    assert "= 'D\\'Angelo Corp'" in result["query"]


def test_escapes_single_quotes_inside_in_clause():
    result = compile_query(_base_input(conditions=[{"field": "Name", "op": "in", "value": ["O'Reilly", "D'Angelo"]}]))
    assert "'O\\'Reilly'" in result["query"]
    assert "'D\\'Angelo'" in result["query"]


# ---------------------------------------------------------------------------
# compile_query — LIMIT bounds safety
# ---------------------------------------------------------------------------


def test_caps_huge_limit_at_max_limit():
    result = compile_query(_base_input(limit=100_000))
    assert "LIMIT 2000" in result["query"]


def test_negative_limit_defaults_to_default_limit():
    result = compile_query(_base_input(limit=-5))
    assert "LIMIT 20" in result["query"]


def test_zero_limit_defaults_to_default_limit():
    result = compile_query(_base_input(limit=0))
    assert "LIMIT 20" in result["query"]


def test_normal_limit_passes_through_unchanged():
    result = compile_query(_base_input(limit=50))
    assert "LIMIT 50" in result["query"]


# ---------------------------------------------------------------------------
# compile_query — null/blank condition serialization
# ---------------------------------------------------------------------------


def test_is_null_serializes_without_quotes():
    result = compile_query(_base_input(conditions=[{"field": "Phone", "op": "is_null"}]))
    assert "Phone = null" in result["query"]


def test_not_null_serializes_without_quotes():
    result = compile_query(_base_input(conditions=[{"field": "Website", "op": "not_null"}]))
    assert "Website != null" in result["query"]
