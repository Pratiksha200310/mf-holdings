"""A parser for the SEBI monthly portfolio statement layout most AMCs use.

The sheet looks like:

    <title rows: AMC, scheme name, "... as on 30 Sep 2026">
    SR | ISIN | Name of the instrument | Rating | Industry | Quantity | Market value | % of NAV
         EQUITY & EQUITY RELATED              <- major section heading
         (a) Listed / awaiting listing        <- sub-section heading
    1  | INE...| Reliance Industries Ltd | ...  <- holding
         Sub Total                            <- checked against the lines above it
         Total
    ...
         Grand Total                          <- end of holdings; checked against all lines
    <notes, NAV history, risk-o-meters: ignored>

Each AMC module supplies what differs: which headings are major sections, how a
(section, sub-section, name) maps to an asset type, and how to find the scheme name and date.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

from .excel import col, find_header, lakhs_factor, num, text
from .model import Holding, LayoutError, Scheme

DATE_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-_/.,]*([A-Za-z]{3,9})[\s\-_/.,]*(\d{4})")
ISIN_LIKE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
NA = {"", "N.A.", "NA", "N.A", "-", "--", "NIL"}


def month_names(m: int) -> set[str]:
    names = {calendar.month_name[m].lower(), calendar.month_abbr[m].lower()}
    if m == 9:
        names.add("sept")
    return names


def parse_date(s: str) -> date:
    """'... AS ON 30 Sep 2026' / '... ended 30 September 2026' -> date(2026, 9, 30)."""
    for m in DATE_RE.finditer(s):
        day, mon, year = m.groups()
        for i in range(1, 13):
            if mon.lower() in month_names(i):
                return date(int(year), i, int(day))
    raise LayoutError(f"no date in {s!r}")


@dataclass
class Layout:
    """What an AMC module tells the parser about its sheets."""
    header_keys: list[str]                       # keywords that identify the header row
    is_major: Callable[[str], bool]              # is this heading a top-level section?
    classify: Callable[[str, str, str, str], str]  # (major, sub, name, industry/rating) -> asset_type
    name_keys: tuple[str, ...] = ("name of the instrument", "instrument", "name of instrument")
    isin_keys: tuple[str, ...] = ("isin",)
    qty_keys: tuple[str, ...] = ("quantity", "qty")
    mv_keys: tuple[str, ...] = ("market value", "mkt value", "market/fair value", "value")
    pct_keys: tuple[str, ...] = ("% to nav", "% of net asset", "% to net asset", "% of nav", "% to aum")
    rating_keys: tuple[str, ...] = ("rating",)
    industry_keys: tuple[str, ...] = ("industry",)
    serial_keys: tuple[str, ...] = ("sr", "sl", "s.no", "sr.", "sl no", "sr no")


def parse_statement(rows: list[list], layout: Layout, *, sheet: str, scheme_name: str,
                    month_end: date) -> Scheme:
    try:
        h, header = find_header(rows, layout.header_keys)
    except ValueError as e:
        raise LayoutError(f"sheet {sheet}: {e}") from None

    c_name, c_isin = col(header, *layout.name_keys), col(header, *layout.isin_keys)
    c_qty, c_mv, c_pct = col(header, *layout.qty_keys), col(header, *layout.mv_keys), col(header, *layout.pct_keys)
    c_rating, c_ind = col(header, *layout.rating_keys), col(header, *layout.industry_keys)
    c_sr = next((j for name, j in header.items() if name.rstrip(".") in layout.serial_keys
                 or name.startswith(("sr", "sl", "s.no"))), None)
    if None in (c_name, c_mv, c_pct):
        raise LayoutError(f"sheet {sheet}: missing columns in header {list(header)}")
    mv_header = next(k for k, j in header.items() if j == c_mv)
    factor = lakhs_factor(mv_header)
    width = max(header.values()) + 1

    holdings: list[Holding] = []
    warnings: list[str] = []
    major = sub = ""
    grand_total = None
    section_rows: list[Holding] = []
    prev_key: tuple | None = None
    prev_subtotal: tuple | None = None

    def cell(row, j):
        return row[j] if j is not None else None

    for row in rows[h + 1:]:
        row = list(row) + [None] * (width - len(row))
        name = text(row[c_name])
        isin_cell = text(cell(row, c_isin))
        looks_isin = ISIN_LIKE.match(isin_cell.replace(" ", "")) is not None
        # Headings and total lines sit in the name column for most AMCs, but in the ISIN column
        # (or the first column) for others, e.g. HDFC.
        label = name or (isin_cell if not looks_isin else "") or next((text(c) for c in row if text(c)), "")
        low = label.lower()
        if low.startswith("grand total"):
            grand_total = (num(row[c_mv]), num(row[c_pct]))
            break
        if not label:
            continue
        if low.startswith("sub total") or low.startswith("subtotal"):
            st = (num(row[c_mv]), num(row[c_pct]))
            if not (st == prev_subtotal and not section_rows):  # same Sub Total printed twice
                prev_subtotal = st
                _check_subtotal(st, section_rows, factor, f"{sheet} / {sub or major}", warnings)
            section_rows = []
            continue
        if low == "total" or low.startswith("total "):
            continue

        serial = num(cell(row, c_sr))
        mv = num(row[c_mv])
        is_holding = bool(name) and (serial is not None or looks_isin or mv is not None)
        if not is_holding:
            if layout.is_major(label):
                major, sub = label, ""
            else:
                sub = label
            section_rows = []
            continue
        label, isin = name, isin_cell

        key = (isin, label, num(cell(row, c_qty)), mv, num(row[c_pct]))
        if key == prev_key:
            warnings.append(f"{sheet}: dropped duplicated line {label!r} (identical to the line above)")
            continue
        prev_key = key

        rating = text(cell(row, c_rating))
        industry = text(cell(row, c_ind)) if c_ind != c_rating else ""
        rating = "" if rating.upper() in NA else rating
        industry = "" if industry.upper() in NA else industry
        asset_type = layout.classify(major, sub, label, industry or rating)
        if asset_type in ("equity", "derivative"):
            ind_or_rating = industry or rating
        else:
            ind_or_rating = rating or industry
        hold = Holding(
            instrument_name=label,
            isin=isin,
            industry_or_rating=ind_or_rating,
            asset_type=asset_type,
            quantity=num(cell(row, c_qty)),
            market_value_lakhs=None if mv is None else mv * factor,
            pct_of_nav=num(row[c_pct]),
        )
        holdings.append(hold)
        section_rows.append(hold)

    if grand_total is None:
        raise LayoutError(f"sheet {sheet}: no 'Grand Total' row; refusing to guess where holdings end")
    if not holdings:
        raise LayoutError(f"sheet {sheet}: no holdings found")

    # % of NAV: some AMCs print 4.25, others 0.0425. The Grand Total line tells us which.
    gt_mv, gt_pct = grand_total[0] or 0.0, grand_total[1]
    if gt_pct is not None and abs(gt_pct - 1) < 0.02:
        for x in holdings:
            if x.pct_of_nav is not None:
                x.pct_of_nav *= 100
    elif gt_pct is not None and abs(gt_pct - 100) > 2:
        raise LayoutError(f"sheet {sheet}: Grand Total % of NAV is {gt_pct}; expected 100 or 1")

    # Cross-check against the file's own Grand Total: catches skipped or double-counted rows.
    # Whether short derivatives count towards it differs between AMCs (and even between Quant
    # schemes), so accept either convention and remember which one the file used.
    all_mv = sum(x.market_value_lakhs or 0 for x in holdings) / factor
    ex_deriv = sum(x.market_value_lakhs or 0 for x in holdings if x.asset_type != "derivative") / factor
    tol = max(1.0, 0.001 * abs(gt_mv))
    if abs(all_mv - gt_mv) <= tol:
        excludes = False
    elif abs(ex_deriv - gt_mv) <= tol:
        excludes = True
    else:
        raise LayoutError(f"sheet {sheet}: holdings add up to {all_mv:.2f} ({ex_deriv:.2f} without "
                          f"derivatives) but the file's Grand Total is {gt_mv:.2f}")
    return Scheme(name=scheme_name, month_end=month_end, holdings=holdings, source=sheet,
                  nav_excludes_derivatives=excludes, warnings=warnings)


def _check_subtotal(st: tuple, rows: list[Holding], factor: float, where: str, warnings: list[str]) -> None:
    """Compare a section's lines with its printed Sub Total.

    A mismatch is a warning, not an error: some files print a Sub Total with some or all of the
    lines missing (quant Arbitrage Fund futures, July 2026). We cannot invent those positions, so
    we report them. The Grand Total check is the hard gate.
    """
    mv, pct = st
    if mv is None:
        if rows:
            warnings.append(f"{where}: {len(rows)} lines but an empty Sub Total")
        return
    ours = sum(x.market_value_lakhs or 0 for x in rows) / factor
    if abs(ours - mv) > max(0.5, 0.001 * abs(mv)):
        warnings.append(f"{where}: {len(rows)} lines add up to {ours:.2f} lakhs but the printed Sub Total "
                        f"is {mv:.2f} lakhs ({pct} of NAV); the missing lines are not in the AMC's file")
