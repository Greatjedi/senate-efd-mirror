"""Offline tests for scrape.py's pure parsing functions, run against the
real captured fixtures in tests/fixtures/ (no network calls -- see
scrape.py's module docstring for why senate.gov can't be hit from here
or from CI in a test context)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _load_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------
# parse_detail_html
# --------------------------------------------------------------------------

def test_goog_brkb_exact_values():
    html = _load_fixture("ptr_goog_brkb.html")
    rows = scrape.parse_detail_html(html)
    assert len(rows) == 2

    by_ticker = {r["ticker"]: r for r in rows}
    assert set(by_ticker) == {"GOOG", "BRK.B"}

    goog = by_ticker["GOOG"]
    assert goog["txdate_iso"] == "2026-06-23"
    assert goog["owner"] == "Self"
    assert goog["asset_description"] == "Alphabet Inc. - Class C Capital Stock"
    assert goog["asset_type"] == "Stock"
    assert goog["type_raw"] == "Purchase"
    assert goog["type_mapped"] == "P"
    assert goog["amount"] == "$1,001 - $15,000"

    brkb = by_ticker["BRK.B"]
    assert brkb["txdate_iso"] == "2026-06-23"
    assert brkb["owner"] == "Self"
    assert brkb["asset_description"] == "Berkshire Hathaway Inc. New Common Stock"
    assert brkb["asset_type"] == "Stock"
    assert brkb["type_raw"] == "Purchase"
    assert brkb["type_mapped"] == "P"
    assert brkb["amount"] == "$15,001 - $50,000"


def test_aapl_nvda_six_rows_and_type_mapping():
    html = _load_fixture("ptr_aapl_nvda.html")
    rows = scrape.parse_detail_html(html)
    assert len(rows) == 6

    tickers = [r["ticker"] for r in rows]
    assert tickers == ["AAPL", "AAPL", "CCI", "NVDA", "COHR", "MU"]

    by_ticker_owner = {(r["ticker"], r["owner"]): r for r in rows}

    aapl_self = by_ticker_owner[("AAPL", "Self")]
    assert aapl_self["type_raw"] == "Sale (Partial)"
    assert aapl_self["type_mapped"] == "S (partial)"

    aapl_spouse = by_ticker_owner[("AAPL", "Spouse")]
    assert aapl_spouse["type_raw"] == "Sale (Partial)"
    assert aapl_spouse["type_mapped"] == "S (partial)"

    cci = by_ticker_owner[("CCI", "Self")]
    assert cci["type_raw"] == "Sale (Full)"
    assert cci["type_mapped"] == "S"

    nvda = by_ticker_owner[("NVDA", "Self")]
    assert nvda["type_raw"] == "Sale (Partial)"
    assert nvda["type_mapped"] == "S (partial)"

    cohr = by_ticker_owner[("COHR", "Self")]
    assert cohr["type_raw"] == "Purchase"
    assert cohr["type_mapped"] == "P"

    mu = by_ticker_owner[("MU", "Self")]
    assert mu["type_raw"] == "Purchase"
    assert mu["type_mapped"] == "P"


def test_no_ticker_yields_empty_string_ticker():
    html = _load_fixture("ptr_no_ticker.html")
    rows = scrape.parse_detail_html(html)
    assert len(rows) == 2
    for r in rows:
        assert r["ticker"] == ""
        # honesty rule: original portal string preserved for auditability
        assert r["type_raw"] == "Sale (Full)"
        assert r["type_mapped"] == "S"


def test_multi_fixture_row_count():
    html = _load_fixture("ptr_multi.html")
    rows = scrape.parse_detail_html(html)
    assert len(rows) == 11


# --------------------------------------------------------------------------
# parse_search_rows
# --------------------------------------------------------------------------

def test_parse_search_rows_ptr_vs_paper_and_date_conversion():
    search_json = json.loads(_load_fixture("search_ptr.json"))
    rows = scrape.parse_search_rows(search_json)

    assert len(rows) == len(search_json["data"])

    kinds = {r["kind"] for r in rows}
    assert kinds == {"ptr", "paper"}

    paper_rows = [r for r in rows if r["kind"] == "paper"]
    assert len(paper_rows) == 2  # the two Blumenthal /paper/ links in the fixture

    ptr_rows = [r for r in rows if r["kind"] == "ptr"]
    assert len(ptr_rows) == len(rows) - 2

    # Spot check the first row: Bernie Moreno, filed 07/24/2026, electronic.
    first = rows[0]
    assert first["senator"] == "Bernie Moreno"
    assert first["kind"] == "ptr"
    assert first["report_date_iso"] == "2026-07-24"
    assert first["uuid"] == "bccf83ce-dd72-4ab6-8564-b3bbb1d2ee55"
    assert first["path"] == "/search/view/ptr/bccf83ce-dd72-4ab6-8564-b3bbb1d2ee55/"

    # Spot check a paper (image-only) row: RICHARD BLUMENTHAL, filed 07/17/2026.
    paper = paper_rows[0]
    assert paper["senator"] == "RICHARD BLUMENTHAL"
    assert paper["report_date_iso"] == "2026-07-17"


# --------------------------------------------------------------------------
# date conversion + type map, directly
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("06/23/2026", "2026-06-23"),
        ("12/31/2025", "2025-12-31"),
        ("1/5/2026", "2026-01-05"),
        ("", ""),
        (None, ""),
        ("not-a-date", "not-a-date"),
    ],
)
def test_to_iso_date(raw, expected):
    assert scrape._to_iso_date(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Purchase", "P"),
        ("Sale (Full)", "S"),
        ("Sale (Partial)", "S (partial)"),
        ("Exchange", "E"),
        ("Something Weird", "Something Weird"),  # honesty rule: passthrough
    ],
)
def test_type_map(raw, expected):
    assert scrape._map_type(raw) == expected


# --------------------------------------------------------------------------
# build_filings (assembly)
# --------------------------------------------------------------------------

def test_build_filings_assembles_full_contract():
    search_json = json.loads(_load_fixture("search_ptr.json"))
    search_rows = scrape.parse_search_rows(search_json)
    moreno_row = next(
        r for r in search_rows
        if r["uuid"] == "bccf83ce-dd72-4ab6-8564-b3bbb1d2ee55"
    )

    detail_html_by_path = {moreno_row["path"]: _load_fixture("ptr_goog_brkb.html")}
    filings = scrape.build_filings([moreno_row], detail_html_by_path)

    assert len(filings) == 2
    for f in filings:
        assert set(f.keys()) == {
            "senator", "ticker", "type", "amount", "transaction_date",
            "report_date", "asset_description", "ptr_link", "type_raw",
        }
        assert f["senator"] == "Bernie Moreno"
        assert f["report_date"] == "2026-07-24"
        assert f["ptr_link"] == (
            "https://efdsearch.senate.gov/search/view/ptr/"
            "bccf83ce-dd72-4ab6-8564-b3bbb1d2ee55/"
        )

    tickers = {f["ticker"] for f in filings}
    assert tickers == {"GOOG", "BRK.B"}
