"""quant Mutual Fund (quantmutual.com).

Discovery: the statutory-disclosures page has no file links in its HTML. Its JavaScript calls
ASP.NET page methods that return an HTML fragment of links inside JSON ({"d": "<ul>..."}):

  POST /statutorydisclosures.aspx/displaydisclouser   {"id": "<year>", "cat": "MONTHLY PORTFOLIO"}
      -> one combined workbook per month, link text "September 2026"
  POST /statutorydisclosures.aspx/displaydisfundname  {"id": "<year>", "cat": "MONTHLY PORTFOLIO - FUND - WISE"}
      -> one workbook per scheme, every link labelled "quant Active Fund"

We parse the combined workbook (its link label names the month reliably; the per-scheme file
names are inconsistent: 30_Sep_2026, 30-Sep-2026, Apr_2026, 29_May_2026). The per-scheme list
is used only to count schemes, as an independent completeness check.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date
from pathlib import Path

from ..base import AMC, Document
from ..excel import col, find_header, lakhs_factor, num, read_workbook, text
from ..model import Holding, LayoutError, NotPublishedError, Scheme

BASE = "https://www.quantmutual.com"
COMBINED_API = BASE + "/statutorydisclosures.aspx/displaydisclouser"
PER_SCHEME_API = BASE + "/statutorydisclosures.aspx/displaydisfundname"

LINK_RE = re.compile(r"""<a[^>]+href=['"]([^'"]+)['"][^>]*>(.*?)</a>""", re.I | re.S)
DATE_RE = re.compile(r"(\d{1,2})[\s\-_/]*([A-Za-z]{3,9})[\s\-_/,]*(\d{4})")
MAJOR_SECTIONS = ("EQUITY", "DERIVATIVES", "DEBT", "MONEY MARKET", "FIXED DEPOSITS", "OTHERS")


def _month_names(m: int) -> set[str]:
    full, abbr = calendar.month_name[m].lower(), calendar.month_abbr[m].lower()
    names = {full, abbr}
    if m == 9:
        names.add("sept")
    return names


def parse_month_end(s: str) -> date:
    """'MONTHLY PORTFOLIO STATEMENT AS ON 30 Sep 2026' -> date(2026, 9, 30)."""
    m = DATE_RE.search(s)
    if not m:
        raise LayoutError(f"no date in {s!r}")
    day, mon, year = m.groups()
    for i in range(1, 13):
        if mon.lower() in _month_names(i):
            return date(int(year), i, int(day))
    raise LayoutError(f"unknown month {mon!r} in {s!r}")


def classify(major: str, sub: str, name: str) -> str:
    """Map Quant's section headings to the brief's asset_type."""
    s, n = sub.lower(), name.upper()
    if "repo" in s or "receivable" in s or "TREPS" in n or "NET CURRENT ASSETS" in n:
        return "cash"
    if major == "FIXED DEPOSITS":
        return "cash"
    if any(k in s for k in ("reit", "infrastructure investment trust", "mutual fund unit",
                            "alternative investment", "physical commod")):
        return "other"
    if major == "EQUITY":
        return "equity"
    if major == "DERIVATIVES":
        return "derivative"
    if major in ("DEBT", "MONEY MARKET"):
        return "debt"
    raise LayoutError(f"cannot classify {name!r} under section {major!r} / {sub!r}")


def parse_sheet(rows: list[list], sheet: str, month: date) -> Scheme:
    try:
        h, header = find_header(rows, ["isin", "instrument", "% to nav"])
    except ValueError as e:
        raise LayoutError(f"sheet {sheet}: {e}") from None

    titles = [text(c) for r in rows[:h] for c in r if text(c)]
    as_on = next((t for t in titles if "AS ON" in t.upper()), None)
    if len(titles) < 2 or not titles[1].lower().startswith("quant") or as_on is None:
        raise LayoutError(f"sheet {sheet}: unexpected title block {titles[:4]}")
    scheme_name = titles[1]
    month_end = parse_month_end(as_on)
    if (month_end.year, month_end.month) != (month.year, month.month):
        raise LayoutError(f"sheet {sheet}: dated {month_end}, expected {month:%Y-%m}")

    c_sr = header.get("sr", header.get("sr.", header.get("sr. no.", 0)))
    c_isin, c_name = col(header, "isin"), col(header, "instrument")
    c_rating, c_ind = col(header, "rating"), col(header, "industry")
    c_qty, c_mv, c_pct = col(header, "quantity"), col(header, "market value"), col(header, "% to nav")
    if None in (c_isin, c_name, c_qty, c_mv, c_pct):
        raise LayoutError(f"sheet {sheet}: missing columns in header {list(header)}")
    mv_header = next(k for k, j in header.items() if j == c_mv)
    factor = lakhs_factor(mv_header)

    holdings: list[Holding] = []
    warnings: list[str] = []
    major = sub = ""
    grand_total = None
    section_rows: list[Holding] = []   # holdings since the last heading, checked against "Sub Total"
    prev_row: tuple | None = None
    prev_subtotal: tuple | None = None
    for row in rows[h + 1:]:
        row = list(row) + [None] * (max(header.values()) + 1 - len(row))
        label = text(row[c_name])
        if label.lower() == "grand total":
            grand_total = (num(row[c_mv]), num(row[c_pct]))
            break  # everything below is notes, hedging disclosure, NAV history and risk-o-meters
        if num(row[c_sr]) is None:
            # Not a holding: a section heading, a (Sub) Total line or a blank row.
            if not label:
                continue
            if label.lower() == "sub total":
                st = (num(row[c_mv]), num(row[c_pct]))
                if st == prev_subtotal and not section_rows:
                    continue  # the same Sub Total line printed twice (seen in Equity Savings, Sep 2026)
                prev_subtotal = st
                _check_subtotal(st, section_rows, factor, f"{sheet} / {sub or major}", warnings)
                section_rows = []
                continue
            if label.lower() == "total":
                continue
            up = label.upper()
            hit = next((m for m in MAJOR_SECTIONS if up.startswith(m)), None)
            if hit:
                major, sub = hit, ""
            else:
                sub = label
            section_rows = []
            continue

        # Identical consecutive lines (same ISIN, name, quantity, value and %) are a copy-paste
        # error in the source, not two positions. Keep one and say so in the report.
        key = (text(row[c_isin]), label, num(row[c_qty]), num(row[c_mv]), num(row[c_pct]))
        if key == prev_row:
            warnings.append(f"{sheet}: dropped duplicated line {label!r} (identical to the line above)")
            continue
        prev_row = key

        asset_type = classify(major, sub, label)
        rating = text(row[c_rating]) if c_rating is not None else ""
        industry = text(row[c_ind]) if c_ind is not None else ""
        rating = "" if rating.upper() in ("N.A.", "NA", "-") else rating
        industry = "" if industry.upper() in ("N.A.", "NA", "-") else industry
        mv = num(row[c_mv])
        h_ = Holding(
            instrument_name=label,
            isin=text(row[c_isin]),
            industry_or_rating=(industry or rating) if asset_type in ("equity", "derivative") else (rating or industry),
            asset_type=asset_type,
            quantity=num(row[c_qty]),
            market_value_lakhs=None if mv is None else mv * factor,
            pct_of_nav=num(row[c_pct]),
        )
        holdings.append(h_)
        section_rows.append(h_)

    if grand_total is None:
        raise LayoutError(f"sheet {sheet}: no 'Grand Total' row; refusing to guess where holdings end")
    if not holdings:
        raise LayoutError(f"sheet {sheet}: no holdings found")

    # Cross-check against the file's own Grand Total: catches skipped or double-counted rows.
    # Quant is not consistent about whether short futures count towards the Grand Total
    # (Multi Asset includes them, Equity Savings does not), so accept either, and remember which.
    gt_mv = grand_total[0] or 0.0
    all_mv = sum(x.market_value_lakhs or 0 for x in holdings) / factor
    ex_deriv = sum(x.market_value_lakhs or 0 for x in holdings if x.asset_type != "derivative") / factor
    tol = max(1.0, 0.001 * abs(gt_mv))
    if abs(all_mv - gt_mv) <= tol:
        excludes = False
    elif abs(ex_deriv - gt_mv) <= tol:
        excludes = True
    else:
        raise LayoutError(f"sheet {sheet}: holdings add up to {all_mv:.2f} lakhs ({ex_deriv:.2f} without "
                          f"derivatives) but the file's Grand Total is {gt_mv:.2f}")
    return Scheme(name=scheme_name, month_end=month_end, holdings=holdings, source=sheet,
                  nav_excludes_derivatives=excludes, warnings=warnings)


def _check_subtotal(st: tuple, rows: list[Holding], factor: float, where: str, warnings: list[str]) -> None:
    """Compare a section's lines with its printed Sub Total.

    A mismatch is a warning, not an error: Quant sometimes prints a futures Sub Total with some or
    all of the lines missing (Arbitrage Fund July 2026, Equity Savings July/August 2026). We cannot
    invent those positions, so we report them. The Grand Total check in parse_sheet is the hard gate.
    """
    mv, pct = st
    if mv is None:
        if rows:
            warnings.append(f"{where}: {len(rows)} lines but an empty Sub Total")
        return
    ours = sum(x.market_value_lakhs or 0 for x in rows) / factor
    if abs(ours - mv) > max(0.5, 0.001 * abs(mv)):
        warnings.append(f"{where}: {len(rows)} lines add up to {ours:.2f} lakhs but the printed Sub Total "
                        f"is {mv:.2f} lakhs ({pct}% of NAV); the missing lines are not in the AMC's file")


def _links(payload: bytes) -> list[tuple[str, str]]:
    html = json.loads(payload)["d"]
    return [(href, re.sub(r"<[^>]+>|\s+", " ", label).strip()) for href, label in LINK_RE.findall(html)]


class Quant(AMC):
    key = "quant"
    name = "quant Mutual Fund"
    homepage = BASE

    def _list(self, api: str, year: int, cat: str) -> list[tuple[str, str]]:
        raw = self.fetcher.post_json(api, {"id": str(year), "cat": cat}, cache=False)
        try:
            return _links(raw)
        except (ValueError, KeyError) as e:
            raise LayoutError(f"quant: {api} no longer returns the expected JSON ({e})") from None

    def discover(self, month: date) -> list[Document]:
        links = self._list(COMBINED_API, month.year, "MONTHLY PORTFOLIO")
        if not links:
            raise LayoutError("quant: monthly portfolio listing came back empty")
        names = _month_names(month.month)
        hits = []
        for href, label in links:
            parts = label.lower().split()
            if len(parts) == 2 and parts[0] in names and parts[1] == str(month.year):
                hits.append(Document(url=BASE + href if href.startswith("/") else href, label=label))
        if not hits:
            seen = ", ".join(label for _, label in links)
            raise NotPublishedError(f"quant: no monthly portfolio for {month:%B %Y} (site lists: {seen})")
        if len(hits) > 1:
            raise LayoutError(f"quant: {len(hits)} files labelled {month:%B %Y}: {[d.url for d in hits]}")
        return hits

    def expected_scheme_count(self, month: date, docs: list[Document]) -> int | None:
        links = self._list(PER_SCHEME_API, month.year, "MONTHLY PORTFOLIO - FUND - WISE")
        names = "|".join(sorted(_month_names(month.month), key=len, reverse=True))
        pat = re.compile(rf"(?<![a-z])({names})(?![a-z])[\s_\-]*{month.year}", re.I)
        return sum(1 for href, _ in links if pat.search(href.rsplit("/", 1)[-1]))

    def parse(self, path: Path, month: date) -> list[Scheme]:
        sheets = read_workbook(path)
        return [parse_sheet(rows, name, month) for name, rows in sheets.items()]
