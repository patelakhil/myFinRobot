"""CSV importer (portfolio/importer.py) — header matching, value parsing,
per-row error attribution, and export round-trip."""

from __future__ import annotations

from datetime import date

import pytest

from finrobot.portfolio.importer import lots_to_csv, parse_csv, parse_date, parse_number

TODAY = date(2026, 1, 1)


# ── parse_number ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("150", 150.0),
        ("150.25", 150.25),
        ("$1,234.50", 1234.5),
        ("€1.234,50", 1234.5),  # European decimal comma
        ("1 234,50", 1234.5),
        ("(12.5)", -12.5),  # accounting negative
        (" 42 ", 42.0),
        ("1,000", 1000.0),  # thousands, not decimal (3 digits after comma)
        ("1234.5 USD", 1234.5),
        ("", None),
        ("-", None),
        ("N/A", None),
        (None, None),
    ],
)
def test_parse_number(raw, expected) -> None:
    assert parse_number(raw) == expected


def test_parse_number_rejects_junk() -> None:
    with pytest.raises(ValueError):
        parse_number("ten")


# ── parse_date ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-03-05", date(2024, 3, 5)),
        ("2024/3/5", date(2024, 3, 5)),
        ("2024.03.05", date(2024, 3, 5)),
        ("20240305", date(2024, 3, 5)),
        ("2024-03-05T15:04:05Z", date(2024, 3, 5)),
        ("2024-03-05 15:04", date(2024, 3, 5)),
        ("03/05/2024", date(2024, 3, 5)),  # ambiguous → month-first
        ("25/03/2024", date(2024, 3, 25)),  # first field > 12 → day-first
        ("03/25/2024", date(2024, 3, 25)),
        ("03/05/24", date(2024, 3, 5)),
        ("5-Mar-2024", date(2024, 3, 5)),
        ("Mar 5, 2024", date(2024, 3, 5)),
        ("March 5 2024", date(2024, 3, 5)),
        ("5 March 2024", date(2024, 3, 5)),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(raw, expected) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["yesterday", "2024-13-01", "32/01/2024", "2024"])
def test_parse_date_rejects_junk(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_date(raw)


# ── parse_csv ────────────────────────────────────────────────────────────────


def test_header_synonyms_and_per_row_errors() -> None:
    text = (
        "Symbol,Qty,Purchase Price ($),Trade Date,Notes\n"
        'AAPL,10,"$150.25",2024-01-15,first buy\n'
        "msft,5,300,01/20/2024,\n"
        "BRK.B,2,410.5,2024-02-01,\n"
        "bad ticker!,3,10,2024-01-01,\n"
        "TSLA,-4,200,2024-01-01,\n"
        "NVDA,1,100,2099-01-01,future\n"
        "AMZN,3,,2024-01-01,no cost\n"
        "GOOG,2,100,not a date,\n"
    )
    p = parse_csv(text, today=TODAY)
    assert p.columns_detected == {
        "ticker": "Symbol",
        "units": "Qty",
        "cost_per_unit": "Purchase Price ($)",
        "purchase_date": "Trade Date",
        "note": "Notes",
    }
    assert p.delimiter == ","
    assert p.valid_count == 4  # TSLA's -4 units is a sell, not an error
    assert p.error_count == 4
    assert p.imported is None

    by_line = {r.line_no: r for r in p.rows}
    assert by_line[2].ticker == "AAPL"
    assert by_line[2].units == 10
    assert by_line[2].cost_per_unit == 150.25
    assert by_line[2].purchase_date == date(2024, 1, 15)
    assert by_line[2].note == "first buy"
    assert by_line[3].ticker == "MSFT"  # upper-cased
    assert by_line[3].purchase_date == date(2024, 1, 20)
    assert by_line[3].note is None
    assert by_line[4].ticker == "BRK-B"  # share-class canonicalised
    assert "Invalid ticker" in by_line[5].errors[0]
    assert by_line[6].ok and by_line[6].side == "sell" and by_line[6].units == 4
    assert "in the future" in by_line[7].errors[0]
    assert "cost_per_unit is empty" in by_line[8].errors[0]
    assert "purchase_date" in by_line[9].errors[0]


def test_headerless_positional_semicolon_and_european_numbers() -> None:
    text = "AAPL;10;150;2024-01-15\nGOOG;2;2.800,50;15.02.2024;EUR;eu format\n"
    p = parse_csv(text, today=TODAY)
    assert p.delimiter == ";"
    assert p.columns_detected["ticker"] == "column 1"
    assert p.valid_count == 2
    a, g = p.rows
    assert a.currency == "USD"  # default
    assert g.cost_per_unit == 2800.5
    assert g.purchase_date == date(2024, 2, 15)
    assert g.currency == "EUR"
    assert g.note == "eu format"


def test_total_cost_derives_cost_per_unit() -> None:
    p = parse_csv("ticker,shares,total cost,date\nAAPL,10,1500,2024-01-15\n", today=TODAY)
    assert p.rows[0].ok
    assert p.rows[0].cost_per_unit == 150.0


def test_tab_delimited_and_bom() -> None:
    p = parse_csv("\ufeffticker\tunits\tcost\tdate\nAAPL\t1\t2\t2024-01-01\n", today=TODAY)
    assert p.delimiter == "\t"
    assert p.valid_count == 1


def test_missing_required_columns_is_one_structural_error() -> None:
    p = parse_csv("name,value\nfoo,1\nbar,2\n", today=TODAY)
    assert p.valid_count == 0
    assert p.error_count == 1
    assert len(p.rows) == 1
    assert "Could not find required column(s)" in p.rows[0].errors[0]
    assert "cost_per_unit" in p.rows[0].errors[0]
    assert "purchase_date" not in p.rows[0].errors[0].split(".")[0]  # optional now


def test_missing_ticker_header_names_what_was_found() -> None:
    p = parse_csv("Name,Shares,Average cost\nBraze,3,5\n", today=TODAY)
    (r,) = p.rows
    assert r.errors[0].startswith("Could not find required column(s): ticker.")
    assert "Found: Name, Shares, Average cost." in r.errors[0]


def test_blank_lines_and_empty_input() -> None:
    assert parse_csv("", today=TODAY).rows == []
    assert parse_csv("\n\n  \n", today=TODAY).rows == []
    p = parse_csv("ticker,units,cost,date\n\nAAPL,1,2,2024-01-01\n\n", today=TODAY)
    assert p.valid_count == 1


def test_default_currency_and_bad_currency_code() -> None:
    text = "ticker,units,cost,date,currency\nAAPL,1,2,2024-01-01,\nMSFT,1,2,2024-01-01,dollars\n"
    p = parse_csv(text, default_currency="cad", today=TODAY)
    assert p.rows[0].currency == "CAD"
    assert p.rows[0].ok
    assert not p.rows[1].ok
    assert "currency" in p.rows[1].errors[0]


def test_export_round_trips_through_import() -> None:
    lots = [
        {
            "ticker": "AAPL",
            "units": 10.0,
            "cost_per_unit": 150.25,
            "purchase_date": date(2024, 1, 15),
            "currency": "USD",
            "note": 'has, comma and "quotes"',
        },
        {"ticker": "MSFT", "units": 2.5, "cost_per_unit": 300.0, "purchase_date": date(2024, 2, 1)},
    ]
    text = lots_to_csv(lots)
    assert text.splitlines()[0] == "ticker,side,units,cost_per_unit,purchase_date,currency,note"
    p = parse_csv(text, today=TODAY)
    assert p.error_count == 0
    a, m = p.rows
    assert (a.ticker, a.units, a.cost_per_unit, a.purchase_date, a.note) == (
        "AAPL",
        10.0,
        150.25,
        date(2024, 1, 15),
        'has, comma and "quotes"',
    )
    assert (m.ticker, m.units, m.cost_per_unit, m.currency) == ("MSFT", 2.5, 300.0, "USD")


def test_side_column_and_negative_units() -> None:
    text = (
        "Symbol,Action,Qty,Price,Date\n"
        "AAPL,Buy,10,150,2024-01-01\n"
        "AAPL,SOLD,4,200,2024-06-01\n"
        "MSFT,,-2,300,2024-02-02\n"
        "TSLA,S,1,250,2024-02-02\n"
        "NVDA,hold,1,1,2024-01-01\n"
        "AMD,buy,0,1,2024-01-01\n"
    )
    p = parse_csv(text, today=TODAY)
    by = {r.line_no: r for r in p.rows}
    assert (by[2].side, by[2].units) == ("buy", 10)
    assert (by[3].side, by[3].units) == ("sell", 4)
    assert (by[4].side, by[4].units) == ("sell", 2)  # negative units, no side column
    assert (by[5].side, by[5].units) == ("sell", 1)
    assert "side: expected buy/sell" in by[6].errors[0]
    assert "non-zero" in by[7].errors[0]


def test_export_round_trips_sells() -> None:
    lots = [
        {
            "ticker": "AAPL",
            "side": "buy",
            "units": 10.0,
            "cost_per_unit": 100.0,
            "purchase_date": date(2024, 1, 1),
        },
        {
            "ticker": "AAPL",
            "side": "sell",
            "units": 4.0,
            "cost_per_unit": 150.0,
            "purchase_date": date(2024, 2, 1),
        },
    ]
    p = parse_csv(lots_to_csv(lots), today=TODAY)
    assert p.error_count == 0
    assert [(r.side, r.units) for r in p.rows] == [("buy", 10.0), ("sell", 4.0)]


# ── Holdings snapshots / stacked paste ───────────────────────────────────────

# A positions table copied from a broker web page (Robinhood): one cell per
# line, with a blank line where the total-return arrow icon was.
STACKED_PASTE = """Name
Symbol
Shares
Price
Average cost
Total return
Equity
SPDR Gold Trust
GLD
7
$371.62
$177.70

$1,357.44
$2,601.34
Robinhood Markets
HOOD
16.348
$90.16
$11.77

$1,281.58
$1,473.95
Everpure
P
0.548
$133.13
$58.38

$40.97
$72.97
"""


def test_stacked_paste_regroups_into_rows() -> None:
    p = parse_csv(STACKED_PASTE, today=TODAY)
    assert p.delimiter == "\n"
    assert p.columns_detected == {
        "ticker": "Symbol",
        "units": "Shares",
        "cost_per_unit": "Average cost",  # not "Price", the current quote
        "purchase_date": "today (no date column)",
    }
    assert p.error_count == 0
    assert [(r.ticker, r.units, r.cost_per_unit) for r in p.rows] == [
        ("GLD", 7.0, 177.7),
        ("HOOD", 16.348, 11.77),
        ("P", 0.548, 58.38),
    ]
    assert all(r.purchase_date == TODAY for r in p.rows)
    assert all("snapshot" in (r.note or "") for r in p.rows)


@pytest.mark.parametrize("icon", ["\u25b2", "\u25bc", "\u200b", "\ufeff"])
def test_stacked_paste_ignores_icon_only_lines(icon: str) -> None:
    p = parse_csv(STACKED_PASTE.replace("\n\n", f"\n{icon}\n"), today=TODAY)
    assert p.error_count == 0
    assert [r.ticker for r in p.rows] == ["GLD", "HOOD", "P"]


def test_stacked_paste_with_a_ragged_record_is_not_regrouped() -> None:
    # A dropped cell misaligns every later record; better to fail loudly than
    # to import shifted columns.
    ragged = STACKED_PASTE.replace("$11.77\n", "")
    p = parse_csv(ragged, today=TODAY)
    assert p.valid_count == 0


def test_snapshot_csv_without_dates_uses_today() -> None:
    text = "Symbol,Shares,Price,Average cost\nAAPL,10,230,150\n"
    p = parse_csv(text, today=TODAY)
    assert p.columns_detected["cost_per_unit"] == "Average cost"
    (r,) = p.rows
    assert r.ok and r.cost_per_unit == 150 and r.purchase_date == TODAY


def test_single_column_file_is_not_stacked() -> None:
    p = parse_csv("ticker\nAAPL\nMSFT\nGOOG\n", today=TODAY)
    assert p.delimiter != "\n"
    assert p.valid_count == 0


def test_tab_paste_infers_unlabelled_ticker_column_and_cleans_headers() -> None:
    text = (
        "Name\t\tShares\tPrice \u25bc\tAverage cost\u200b\tEquity\n"
        "SPDR Gold Trust\tGLD\t7\t$371.62\t$177.70\t$2,601.34\n"
        "Braze\tBRZE\t40.659\t$45.85\t$31.17\t$1,864.21\n"
    )
    p = parse_csv(text, today=TODAY)
    assert p.error_count == 0
    assert p.columns_detected["ticker"] == "column 2"
    assert [(r.ticker, r.cost_per_unit) for r in p.rows] == [("GLD", 177.7), ("BRZE", 31.17)]
