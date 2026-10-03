"""
Pins the index report's skip accounting.

Running `rtk-sf index` on a project in another language printed
`Indexed: 0, Skipped: N (unchanged)` and nothing else. That is wrong twice: the
files had never been indexed, so they were not "unchanged", and the files the
project is actually made of were not counted at all. The output read as a
malfunction rather than as "there is no Salesforce metadata here".
"""

from __future__ import annotations

from rtk_sf.indexer import SalesforceIndexer

APEX = """public with sharing class Thing {
    public Boolean ok() { return true; }
}
"""


def _apex_project(root):
    classes = root / "force-app" / "main" / "default" / "classes"
    classes.mkdir(parents=True)
    (classes / "Thing.cls").write_text(APEX, encoding="utf-8")
    return root


def test_unsupported_file_types_are_counted_not_ignored(tmp_path):
    """On a Dart/Flutter tree the only honest signal is that files were seen."""
    (tmp_path / "lib").mkdir()
    for i in range(3):
        (tmp_path / "lib" / f"w{i}.dart").write_text("class W {}", encoding="utf-8")
    (tmp_path / "pubspec.yaml").write_text("name: demo", encoding="utf-8")

    counters = SalesforceIndexer(tmp_path).index_project()

    assert counters["indexed"] == 0
    assert counters["unsupported"] == 4
    assert counters["unsupported_suffixes"][".dart"] == 3
    assert counters["unsupported_suffixes"][".yaml"] == 1


def test_non_metadata_xml_is_unrecognized_not_unchanged(tmp_path):
    """A manifest that was never indexed must not be reported as unchanged."""
    android = tmp_path / "android"
    android.mkdir()
    (android / "AndroidManifest.xml").write_text(
        "<manifest><application/></manifest>", encoding="utf-8"
    )

    counters = SalesforceIndexer(tmp_path).index_project()

    assert counters["indexed"] == 0
    assert counters["unrecognized"] == 1
    assert counters["unchanged"] == 0


def test_unchanged_is_only_used_on_a_second_run(tmp_path):
    _apex_project(tmp_path)
    indexer = SalesforceIndexer(tmp_path)

    first = indexer.index_project()
    assert first["indexed"] == 1
    assert first["unchanged"] == 0

    second = SalesforceIndexer(tmp_path).index_project()
    assert second["indexed"] == 0
    assert second["unchanged"] == 1
    assert second["unrecognized"] == 0


def test_skipped_stays_the_sum_for_existing_callers(tmp_path):
    """`skipped` is still consumed by cmd_index and cmd_setup."""
    (tmp_path / "a.xml").write_text("<nope/>", encoding="utf-8")
    _apex_project(tmp_path)

    counters = SalesforceIndexer(tmp_path).index_project(tmp_path)

    assert counters["skipped"] == counters["unchanged"] + counters["unrecognized"]


def test_search_root_reports_the_directory_actually_scanned(tmp_path):
    """index_project falls back to the project root when force-app is absent;
    the CLI used to print the force-app path it had not used."""
    (tmp_path / "x.dart").write_text("class X {}", encoding="utf-8")

    counters = SalesforceIndexer(tmp_path).index_project()

    assert counters["search_root"] == str(tmp_path)

    _apex_project(tmp_path)
    counters = SalesforceIndexer(tmp_path).index_project()
    assert counters["search_root"] == str(tmp_path / "force-app")
