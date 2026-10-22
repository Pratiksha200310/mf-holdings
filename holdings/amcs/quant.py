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

import json
import re
from datetime import date
from pathlib import Path

from ..base import AMC, Document
from ..excel import read_workbook, text
from ..model import LayoutError, NotPublishedError, Scheme
from ..statement import Layout, month_names, parse_date, parse_statement

BASE = "https://www.quantmutual.com"
COMBINED_API = BASE + "/statutorydisclosures.aspx/displaydisclouser"
PER_SCHEME_API = BASE + "/statutorydisclosures.aspx/displaydisfundname"

LINK_RE = re.compile(r"""<a[^>]+href=['"]([^'"]+)['"][^>]*>(.*?)</a>""", re.I | re.S)
MAJOR_SECTIONS = ("EQUITY", "DERIVATIVES", "DEBT", "MONEY MARKET", "FIXED DEPOSITS", "OTHERS")


def _major(label: str) -> str:
    up = label.upper()
    return next((m for m in MAJOR_SECTIONS if up.startswith(m)), "")


def classify(major: str, sub: str, name: str, industry: str = "") -> str:
    """Map Quant's section headings to the brief's asset_type."""
    major, s, n = _major(major), sub.lower(), name.upper()
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


LAYOUT = Layout(header_keys=["isin", "instrument", "% to nav"], is_major=lambda t: bool(_major(t)),
                classify=classify)


def parse_sheet(rows: list[list], sheet: str, month: date) -> Scheme:
    # Title block: "quant Mutual Fund" / scheme name / description / "... AS ON 30 Sep 2026".
    titles = [text(c) for r in rows[:12] for c in r if text(c)]
    as_on = next((t for t in titles if "AS ON" in t.upper()), None)
    if len(titles) < 2 or not titles[1].lower().startswith("quant") or as_on is None:
        raise LayoutError(f"sheet {sheet}: unexpected title block {titles[:4]}")
    month_end = parse_date(as_on)
    if (month_end.year, month_end.month) != (month.year, month.month):
        raise LayoutError(f"sheet {sheet}: dated {month_end}, expected {month:%Y-%m}")
    return parse_statement(rows, LAYOUT, sheet=sheet, scheme_name=titles[1], month_end=month_end)


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
        names = month_names(month.month)
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
        names = "|".join(sorted(month_names(month.month), key=len, reverse=True))
        pat = re.compile(rf"(?<![a-z])({names})(?![a-z])[\s_\-]*{month.year}", re.I)
        return sum(1 for href, _ in links if pat.search(href.rsplit("/", 1)[-1]))

    def parse(self, path: Path, month: date) -> list[Scheme]:
        sheets = read_workbook(path)
        return [parse_sheet(rows, name, month) for name, rows in sheets.items()]
