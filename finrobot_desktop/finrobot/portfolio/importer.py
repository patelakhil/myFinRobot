"""CSV → purchase lots. Pure parsing, no I/O, no store.

Broker / spreadsheet exports agree on almost nothing, so :func:`parse_csv` is
deliberately forgiving about *shape* and strict about *values*:

* **Delimiter** is sniffed (``,`` ``;`` ``\\t`` ``|``), falling back to comma.
* **Headers** are matched case-insensitively after stripping spaces / ``_`` /
  ``-`` / ``.``: ``Symbol``, ``Ticker`` and ``Stock`` all mean ``ticker``;
  ``Qty``, ``Shares``, ``Quantity`` all mean ``units``; ``Price``, ``Cost``,
  ``Avg Cost``, ``Purchase Price`` all mean ``cost_per_unit``; ``Date``,
  ``Trade Date``, ``Acquired`` all mean ``purchase_date``. If no header row is
  detected the columns are taken positionally as
  ``ticker, units, cost_per_unit, purchase_date[, currency[, note]]``.
* If there is a ``total``/``cost basis`` column but no per-unit cost, the cost
  per unit is derived as ``total / units``. A bare ``Price`` / ``Cost`` column
  yields to a more specific one (``Average cost``) when both are present — in a
  holdings snapshot ``Price`` is the current quote, not the cost basis.
* **Holdings snapshots** (ticker, units and cost but no date column — what a
  broker's positions page gives you) import with today's date as the purchase
  date and a note saying so; the cost basis is exact, the holding period isn't.
* **Stacked paste**: copying a positions table out of a broker web page
  (Robinhood et al.) often yields one cell per line — a block of header lines,
  then each holding's cells in the same order. That layout is detected and
  regrouped into rows before the usual header matching runs.
* **Sells**: a ``side`` / ``action`` / ``type`` column (``buy``/``sell``,
  ``B``/``S``, ``bought``/``sold``…) marks the trade; without one, a negative
  unit count means a sell. For a sell, ``cost_per_unit`` is the sale price.
* **Numbers** tolerate currency symbols, thousands separators, and surrounding
  whitespace (``"$1,234.50"`` → 1234.5).
* **Dates** accept ISO (``2024-03-05``), slashed/dotted numeric forms with
  either day- or month-first order (an unambiguous first field > 12 decides;
  otherwise month-first wins — the US broker default), ``YYYYMMDD``, and the
  textual ``Mar 5, 2024`` / ``5 Mar 2024`` / ``March 5 2024`` forms.

Every row comes back as an :class:`ImportRow`; a row with ``errors`` is shown
to the user in the preview and skipped on commit — one bad line never blocks
the rest of the file.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Iterable
from datetime import date, datetime

from finrobot.engine.data.ticker import validate_ticker
from finrobot.portfolio.models import ImportPreview, ImportRow

logger = logging.getLogger(__name__)

# ── Header synonyms ───────────────────────────────────────────────────────────
# Keys are normalised: lower-case, with spaces / underscores / hyphens / dots
# removed, so "Cost per Unit", "cost_per_unit" and "COST-PER-UNIT" all collapse
# to "costperunit".

_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ticker": ("ticker", "symbol", "stock", "security", "code", "instrument", "tickersymbol"),
    "units": (
        "units",
        "unit",
        "quantity",
        "qty",
        "shares",
        "share",
        "numberofunits",
        "numberofshares",
        "noofunits",
        "noofshares",
        "count",
        "position",
        "holding",
        "holdings",
    ),
    "cost_per_unit": (
        "costperunit",
        "costunit",
        "costshare",
        "priceunit",
        "priceshare",
        "cost",
        "price",
        "unitcost",
        "unitprice",
        "purchaseprice",
        "buyprice",
        "avgcost",
        "averagecost",
        "avgprice",
        "averageprice",
        "costpershare",
        "priceperunit",
        "pricepershare",
        "costbasispershare",
        "costbasisperunit",
        "entryprice",
        "fillprice",
        "executionprice",
    ),
    "purchase_date": (
        "purchasedate",
        "date",
        "buydate",
        "tradedate",
        "acquired",
        "acquireddate",
        "dateacquired",
        "bought",
        "boughton",
        "purchased",
        "purchasedon",
        "opendate",
        "entrydate",
        "executiondate",
        "filldate",
    ),
    "total_cost": (
        "total",
        "totalcost",
        "costbasis",
        "amount",
        "amountpaid",
        "totalamount",
        "bookcost",
        "bookvalue",
        "netamount",
        "totalvalue",
    ),
    "currency": ("currency", "ccy", "cur", "curr"),
    "note": ("note", "notes", "memo", "comment", "comments", "description", "remarks"),
    "side": (
        "side",
        "action",
        "type",
        "transaction",
        "transactiontype",
        "tradetype",
        "buysell",
        "buy/sell",
        "direction",
        "activity",
        "order",
        "ordertype",
    ),
}

# Bare names that only mean "cost" when nothing more specific is present: a
# positions page has both "Price" (current quote) and "Average cost" (basis).
_WEAK_COST_HEADERS = frozenset({"price", "cost"})

# Positional fallback when the first line is not a header row.
_POSITIONAL = ("ticker", "units", "cost_per_unit", "purchase_date", "currency", "note", "side")

_SELL_WORDS = frozenset({"sell", "sold", "s", "sl", "sale", "short", "close", "disposal", "redeem"})
_BUY_WORDS = frozenset(
    {"buy", "bought", "b", "by", "purchase", "purchased", "long", "open", "acquire"}
)

_REQUIRED = ("ticker", "units", "purchase_date")

_SNAPSHOT_NOTE = "Imported from a holdings snapshot; purchase date unknown"

_DELIMITERS = ",;\t|"
_STACKED_MAX_COLUMNS = 16

_NUMBER_JUNK = re.compile(
    r"[\s$€£¥₹,'\u00a0]|USD|EUR|GBP|HKD|CNY|JPY|CAD|AUD|CHF|SGD|INR", re.IGNORECASE
)
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")

_TEXT_DATE_FORMATS = (
    "%b %d, %Y",  # Mar 5, 2024
    "%b %d %Y",  # Mar 5 2024
    "%B %d, %Y",  # March 5, 2024
    "%B %d %Y",  # March 5 2024
    "%d %b %Y",  # 5 Mar 2024
    "%d %B %Y",  # 5 March 2024
    "%d-%b-%Y",  # 5-Mar-2024
    "%d-%b-%y",  # 5-Mar-24
    "%b-%d-%Y",  # Mar-5-2024
    "%Y-%b-%d",  # 2024-Mar-05
)


def _norm_header(h: str) -> str:
    # "Cost (USD)" / "Price [$]" → the bare name; units in brackets are noise.
    # Everything that isn't a letter or digit is dropped too: separators, and the
    # sort arrows / zero-width characters a header copied off a web page carries.
    h = re.sub(r"[\(\[].*?[\)\]]", "", h)
    return "".join(ch for ch in h.lower() if ch.isalnum())


def _match_header(h: str) -> str | None:
    n = _norm_header(h)
    if not n:
        return None
    for field, names in _HEADER_SYNONYMS.items():
        if n in names:
            return field
    return None


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Sniffer gives up on single-column / tiny samples; count candidates.
        first = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {d: first.count(d) for d in ",;\t|"}
        best = max(counts, key=counts.get)  # type: ignore[arg-type]
        return best if counts[best] > 0 else ","


def parse_number(raw: str | None) -> float | None:
    """``"$1,234.50"`` → 1234.5; ``""`` → None. Raises ``ValueError`` on junk.

    European ``1.234,50`` is recognised when the only ``,`` sits after the last
    ``.`` and is followed by exactly 1–2 digits; otherwise ``,`` is a thousands
    separator.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or s in {"-", "—", "–", "n/a", "N/A", "NA", "null", "None"}:
        return None
    # Parenthesised negatives: (123.45) → -123.45 (accounting exports).
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    # European decimal comma: "1.234,50" or "1234,50" (comma then ≤2 digits at end).
    if re.fullmatch(r"[^,]*,\d{1,2}", s) and s.count(",") == 1:
        s = s.replace(".", "").replace(",", ".")
    s = _NUMBER_JUNK.sub("", s)
    if not s:
        return None
    try:
        v = float(s)
    except ValueError as exc:
        raise ValueError(f"not a number: {raw!r}") from exc
    return -v if neg else v


def parse_date(raw: str | None) -> date | None:
    """Best-effort date parse (see module docstring for accepted forms).
    Returns None for an empty cell; raises ``ValueError`` when unparseable."""
    if raw is None:
        return None
    s = raw.strip().strip('"').strip("'")
    if not s:
        return None
    # A timestamp ("2024-03-05 15:04:05", "2024-03-05T15:04:05Z") → date part.
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T\s]", s)
    if m:
        s = m.group(1)
    # ISO / ISO-ish: YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _mk_date(y, mo, d, raw)
    # Compact YYYYMMDD
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _mk_date(y, mo, d, raw)
    # Slashed / dotted / dashed numeric with the year LAST: A/B/YYYY or A/B/YY.
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", s)
    if m:
        a, b, y = (int(g) for g in m.groups())
        if y < 100:
            y += 2000 if y < 70 else 1900
        # Unambiguous when one field exceeds 12; otherwise month-first.
        if a > 12 and b <= 12:
            d, mo = a, b
        elif b > 12 and a <= 12:
            mo, d = a, b
        else:
            mo, d = a, b
        return _mk_date(y, mo, d, raw)
    for fmt in _TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date: {raw!r}")


def _mk_date(y: int, mo: int, d: int, raw: str) -> date:
    try:
        return date(y, mo, d)
    except ValueError as exc:
        raise ValueError(f"invalid calendar date: {raw!r}") from exc


def _looks_like_header(cells: list[str]) -> bool:
    """A header row has at least one recognised column name and no cell that
    parses as a number (a ticker-only data row like ``AAPL,10,150,2024-01-02``
    has numbers; ``Symbol,Qty,Price,Date`` has none)."""
    matched = sum(1 for c in cells if _match_header(c) is not None)
    if matched == 0:
        return False
    for c in cells:
        try:
            if parse_number(c) is not None:
                return False
        except ValueError:
            pass
    return True


def _clean_cells(row: Iterable[str]) -> list[str]:
    return [(c or "").strip() for c in row]


def _is_number(cell: str) -> bool:
    try:
        return parse_number(cell) is not None
    except ValueError:
        return False


def _unstack(lines: list[str]) -> list[list[str]] | None:
    """Regroup a one-cell-per-line paste into rows, or None if ``lines`` isn't
    one.

    The header block is the shortest prefix of N non-numeric lines that names a
    ticker, a unit count and a cost, such that the remaining lines split evenly
    into N-cell records whose ticker cell is a valid ticker and whose units cell
    is a number. Requiring every record to line up keeps an ordinary single-
    column file from being misread as stacked.

    Lines with no letter or digit are dropped first: the gain/loss arrow icon
    beside a return figure pastes as its own line (``▲``, or an invisible
    zero-width character) and would otherwise knock every record out of step.
    """
    cells = [c for c in (ln.strip() for ln in lines) if any(ch.isalnum() for ch in c)]
    for n in range(3, min(_STACKED_MAX_COLUMNS, len(cells) - 1) + 1):
        header = cells[:n]
        if _is_number(header[-1]):
            break  # data has started; a longer header can only be worse
        if any(d in h for h in header for d in _DELIMITERS):
            return None  # a delimited header row, not a stacked one
        fields = [_match_header(h) for h in header]
        if not {"ticker", "units"} <= set(fields) or not (
            "cost_per_unit" in fields or "total_cost" in fields
        ):
            continue
        body = cells[n:]
        if not body or len(body) % n:
            continue
        rows = [body[i : i + n] for i in range(0, len(body), n)]
        t, u = fields.index("ticker"), fields.index("units")
        if all(_valid_ticker(r[t]) and _is_number(r[u]) for r in rows):
            return [header, *rows]
    return None


def _infer_ticker_column(rows: list[list[str]], *, used: set[int]) -> int | None:
    width = max((len(r) for r in rows), default=0)
    for idx in range(width):
        if idx in used:
            continue
        values = [r[idx] if idx < len(r) else "" for r in rows]
        if all(v and v == v.upper() and _valid_ticker(v) for v in values):
            return idx
    return None


def _valid_ticker(raw: str) -> bool:
    try:
        validate_ticker(raw)
    except ValueError:
        return False
    return True


def parse_csv(
    text: str,
    *,
    default_currency: str = "USD",
    today: date | None = None,
) -> ImportPreview:
    """Parse CSV text into an :class:`ImportPreview` (never raises on bad
    rows — every problem is attached to its row)."""
    today = today or date.today()
    text = text.lstrip("\ufeff")
    # Drop fully blank lines up front so the sniffer/header detection sees data.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ImportPreview(rows=[], valid_count=0, error_count=0)

    stacked = _unstack(lines)
    if stacked is not None:
        delimiter = "\n"
        all_rows = stacked
    else:
        delimiter = _sniff_delimiter("\n".join(lines[:20]))
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
        all_rows = [_clean_cells(r) for r in reader]
        all_rows = [r for r in all_rows if any(r)]
    if not all_rows:
        return ImportPreview(rows=[], valid_count=0, error_count=0, delimiter=delimiter)

    # Column map: canonical field → column index.
    col: dict[str, int] = {}
    detected: dict[str, str] = {}
    start = 0
    first = all_rows[0]
    if _looks_like_header(first):
        start = 1
        for idx, h in enumerate(first):
            field = _match_header(h)
            # First match wins — a sheet with both "Price" and "Avg Cost" maps
            # the leftmost; the user sees which header was chosen in
            # ``columns_detected`` and can rename.
            if field is None:
                continue
            weak = field == "cost_per_unit" and _norm_header(h) in _WEAK_COST_HEADERS
            prev = detected.get(field)
            displaces_weak = (
                field == "cost_per_unit"
                and prev is not None
                and _norm_header(prev) in _WEAK_COST_HEADERS
                and not weak
            )
            if field not in col or displaces_weak:
                col[field] = idx
                detected[field] = h
    else:
        # Headerless: map by position up to the WIDEST row, not just the first —
        # a file whose first line omits the optional currency/note columns must
        # not blind later lines that carry them.
        width = max(len(r) for r in all_rows)
        for idx, field in enumerate(_POSITIONAL):
            if idx < width:
                col[field] = idx
                detected[field] = f"column {idx + 1}"

    # No ticker header (a broker table headed "Name" with the symbol in an
    # unlabelled column): take the first unmapped column whose every cell is an
    # upper-case ticker.
    if start and "ticker" not in col:
        idx = _infer_ticker_column(all_rows[start:], used=set(col.values()))
        if idx is not None:
            col["ticker"] = idx
            detected["ticker"] = first[idx] or f"column {idx + 1}"

    # A positions snapshot has everything but the date: take today's.
    snapshot = (
        "purchase_date" not in col
        and {"ticker", "units"} <= col.keys()
        and ("cost_per_unit" in col or "total_cost" in col)
    )
    if snapshot:
        detected["purchase_date"] = "today (no date column)"

    rows: list[ImportRow] = []
    missing = [f for f in _REQUIRED if f not in col and f != "purchase_date"]
    if missing or ("cost_per_unit" not in col and "total_cost" not in col):
        need = list(missing)
        if "cost_per_unit" not in col and "total_cost" not in col:
            need.append("cost_per_unit (or total_cost)")
        logger.info("import: required columns missing %s; header cells %r", need, first)
        # Surface a single structural error on line 1 so the preview explains
        # itself instead of listing N identical per-row failures.
        rows.append(
            ImportRow(
                line_no=1,
                errors=[
                    "Could not find required column(s): "
                    + ", ".join(need)
                    + ". Expected headers like: ticker, units, cost_per_unit[, purchase_date]."
                    + (f" Found: {', '.join(c for c in first if c)}." if start else "")
                ],
            )
        )
        return ImportPreview(
            rows=rows,
            valid_count=0,
            error_count=1,
            columns_detected=detected,
            delimiter=delimiter,
        )

    def cell(r: list[str], field: str) -> str | None:
        i = col.get(field)
        if i is None or i >= len(r):
            return None
        return r[i]

    for offset, r in enumerate(all_rows[start:], start=start):
        line_no = offset + 1
        row = ImportRow(line_no=line_no, currency=default_currency.upper())

        raw_ticker = cell(r, "ticker")
        if not raw_ticker:
            row.errors.append("ticker is empty")
        else:
            try:
                row.ticker = validate_ticker(raw_ticker)
            except ValueError as exc:
                row.errors.append(str(exc))

        # Side: an explicit column wins; otherwise a NEGATIVE unit count means a
        # sell (the common spreadsheet convention), positive means a buy.
        raw_side = (cell(r, "side") or "").strip().lower()
        side_from_col: str | None = None
        if raw_side:
            if raw_side in _SELL_WORDS or raw_side.startswith(("sell", "sold")):
                side_from_col = "sell"
            elif raw_side in _BUY_WORDS or raw_side.startswith(("buy", "bought")):
                side_from_col = "buy"
            else:
                row.errors.append(f"side: expected buy/sell (got {raw_side!r})")

        try:
            units = parse_number(cell(r, "units"))
        except ValueError as exc:
            units = None
            row.errors.append(f"units: {exc}")
        if units is None:
            if not any("units" in e for e in row.errors):
                row.errors.append("units is empty")
        elif units == 0:
            row.errors.append("units must be non-zero")
        else:
            if side_from_col is not None:
                row.side = side_from_col  # type: ignore[assignment]
            elif units < 0:
                row.side = "sell"
            row.units = abs(units)

        cost: float | None = None
        try:
            cost = parse_number(cell(r, "cost_per_unit"))
        except ValueError as exc:
            row.errors.append(f"cost_per_unit: {exc}")
        if cost is None and "total_cost" in col and row.units:
            try:
                total = parse_number(cell(r, "total_cost"))
            except ValueError as exc:
                total = None
                row.errors.append(f"total_cost: {exc}")
            if total is not None:
                cost = abs(total) / row.units
        if cost is None:
            if not any("cost_per_unit" in e or "total_cost" in e for e in row.errors):
                row.errors.append("cost_per_unit is empty")
        elif cost < 0:
            row.errors.append(f"cost_per_unit must be >= 0 (got {cost:g})")
        else:
            row.cost_per_unit = cost

        try:
            pdate = today if snapshot else parse_date(cell(r, "purchase_date"))
        except ValueError as exc:
            pdate = None
            row.errors.append(f"purchase_date: {exc}")
        if pdate is None:
            if not any("purchase_date" in e for e in row.errors):
                row.errors.append("purchase_date is empty")
        elif pdate > today:
            row.errors.append(f"purchase_date {pdate.isoformat()} is in the future")
        else:
            row.purchase_date = pdate

        ccy = (cell(r, "currency") or "").strip().upper()
        if ccy:
            if _CURRENCY_CODE.match(ccy):
                row.currency = ccy
            else:
                row.errors.append(f"currency must be a 3-letter ISO code (got {ccy!r})")

        note = cell(r, "note")
        row.note = note.strip() or None if note else None
        if snapshot and row.note is None:
            row.note = _SNAPSHOT_NOTE

        rows.append(row)

    valid = sum(1 for r in rows if r.ok)
    return ImportPreview(
        rows=rows,
        valid_count=valid,
        error_count=len(rows) - valid,
        columns_detected=detected,
        delimiter=delimiter,
    )


# ── Export ───────────────────────────────────────────────────────────────────

EXPORT_HEADER = ("ticker", "side", "units", "cost_per_unit", "purchase_date", "currency", "note")


def lots_to_csv(lots: Iterable[dict[str, object]]) -> str:
    """Serialise lots to the canonical header — the same shape :func:`parse_csv`
    reads back, so export → import round-trips losslessly."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(EXPORT_HEADER)
    for lot in lots:
        pdate = lot.get("purchase_date")
        w.writerow(
            [
                lot.get("ticker", ""),
                lot.get("side") or "buy",
                _fmt_num(lot.get("units")),
                _fmt_num(lot.get("cost_per_unit")),
                pdate.isoformat() if isinstance(pdate, date) else (pdate or ""),
                lot.get("currency", "USD"),
                lot.get("note") or "",
            ]
        )
    return buf.getvalue()


def _fmt_num(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v) if isinstance(v, float) else str(v)
