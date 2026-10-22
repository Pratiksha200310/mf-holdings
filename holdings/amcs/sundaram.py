"""Sundaram Mutual Fund (sundarammutual.com).

Discovery: Statutory Disclosures -> "Monthly/Fortnightly Portfolios/Ad hoc Portfolios"
(/monthly-fortnightly-adhoc-portfolios). The page HTML has no file links. Choosing
"Monthly Portfolio" calls an AjaxPro method:

  POST /ajax/Modules_Disclosure_Monthly_Fortnightly_Adhoc_Portfolios,App_Web_<hash>.ashx
       ?_method=GetCategory&_session=no      body: Catid=Monthly

`App_Web_<hash>` is the compiled-assembly name, which changes whenever the site is redeployed,
so we never hard-code it: we read the current proxy script URL from the page's <script> tags.
The reply is a JavaScript string of HTML: one accordion per financial year, one tab per month,
and in each tab two workbooks ("Equity & Fund of Funds - Sep 2026", "Fixed Income - Sep 2026").

Each workbook has an "Index" sheet (acronym -> scheme name) and one sheet per scheme. The Index
sheets give an independent scheme count for the completeness check.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from ..base import AMC, Document
from ..excel import read_workbook, text
from ..model import LayoutError, NotPublishedError, Scheme
from ..statement import Layout, month_names, parse_date, parse_statement

BASE = "https://www.sundarammutual.com"
PAGE = BASE + "/monthly-fortnightly-adhoc-portfolios"
AJAX_SRC_RE = re.compile(r'src="(/ajax/Modules_Disclosure_Monthly_Fortnightly_Adhoc_Portfolios,[^"]+\.ashx)"')


def classify(major: str, sub: str, name: str, industry: str = "") -> str:
    """Sundaram headings: 'A) Equity & Equity Related', 'B) Debt Instruments', 'C) Money Market
    Instruments', 'D) Mutual Fund Units', 'E) Others', with '(a) ...' sub-sections."""
    m, s, n = major.lower(), sub.lower(), name.lower()
    if "net current assets" in n or "margin money" in n or "treps" in s or "reverserepo" in s \
            or "deposits with" in s or n.startswith("treps"):
        return "cash"
    if "derivative" in s or "interest rate swap" in n or "interest rate swap" in s \
            or "future" in industry.lower() or "option" in industry.lower():
        return "derivative"
    if "share application money" in s:
        return "other"
    if m.startswith("a)"):
        if "non-convertible preference" in s:
            return "debt"
        return "equity"
    if m.startswith(("b)", "c)")):
        return "debt"
    if m.startswith(("d)", "e)")):
        return "other"
    raise LayoutError(f"sundaram: cannot classify {name!r} under {major!r} / {sub!r}")


LAYOUT = Layout(
    header_keys=["isin", "instrument", "% of net asset"],
    is_major=lambda t: re.match(r"^[A-Z]\)\s", t) is not None,
    classify=classify,
)


def parse_listing(js_string: str) -> list[tuple[str, str, str, str]]:
    """AjaxPro reply -> [(financial year, month tab, link label, href)]."""
    s = js_string.strip()
    if not (s.startswith("'") and s.endswith("'")):
        raise LayoutError(f"sundaram: unexpected listing reply {s[:80]!r}")
    html = s[1:-1].replace("\\'", "'").replace('\\"', '"')
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for acc in soup.select(".accordion-item"):
        fy = acc.select_one(".accordion-header").get_text(strip=True)
        for btn in acc.select("button.nav-link"):
            pane = acc.find(id=btn.get("data-bs-target", "").lstrip("#"))
            if pane is None:
                continue
            for a in pane.find_all("a", href=True):
                out.append((fy, btn.get_text(strip=True), a.get_text(" ", strip=True), a["href"]))
    return out


class Sundaram(AMC):
    key = "sundaram"
    name = "Sundaram Mutual Fund"
    homepage = BASE

    def discover(self, month: date) -> list[Document]:
        page = self.fetcher.get(PAGE, cache=False).decode("utf-8", errors="replace")
        m = AJAX_SRC_RE.search(page)
        if not m:
            raise LayoutError("sundaram: portfolio page no longer loads the AjaxPro proxy script")
        api = BASE + m.group(1) + "?_method=GetCategory&_session=no"
        raw = self.fetcher.request("POST", api, data="Catid=Monthly", cache=False).decode("utf-8", "replace")
        links = parse_listing(raw)
        if not links:
            raise LayoutError("sundaram: monthly portfolio listing came back empty")

        names = month_names(month.month)
        label_re = re.compile(rf"-\s*({'|'.join(names)})\w*\s+{month.year}\s*$", re.I)
        docs = [Document(url=BASE + href if href.startswith("/") else href, label=label)
                for _, _, label, href in links if label_re.search(label)]
        if not docs:
            raise NotPublishedError(f"sundaram: no monthly portfolio for {month:%B %Y} "
                                    f"(latest listed: {links[0][2]!r})")
        return docs

    def parse(self, path: Path, month: date) -> list[Scheme]:
        sheets = read_workbook(path)
        index = sheets.get("Index")
        if index is None:
            raise LayoutError(f"sundaram: {path.name} has no Index sheet")
        # Index: S.NO. | ACRONYM | SCHEME NAME
        listed = {text(r[1]): text(r[2]) for r in index[1:] if len(r) > 2 and text(r[1])}
        missing = set(listed) - set(sheets)
        if missing:
            raise LayoutError(f"sundaram: Index lists sheets that are not in the workbook: {sorted(missing)}")
        self._expected = getattr(self, "_expected", 0) + len(listed)

        schemes = []
        for acronym in listed:
            rows = sheets[acronym]
            scheme_name = text(rows[1][0]) if len(rows) > 1 else ""
            when = next((text(c) for r in rows[:6] for c in r if "month ended" in text(c).lower()), "")
            if not scheme_name or not when:
                raise LayoutError(f"sundaram: sheet {acronym}: unexpected title block")
            month_end = parse_date(when)
            if (month_end.year, month_end.month) != (month.year, month.month):
                raise LayoutError(f"sundaram: sheet {acronym}: dated {month_end}, expected {month:%Y-%m}")
            schemes.append(parse_statement(rows, LAYOUT, sheet=acronym, scheme_name=scheme_name,
                                           month_end=month_end))
        extra = set(sheets) - set(listed) - {"Index"}
        if extra:
            raise LayoutError(f"sundaram: sheets not in the Index: {sorted(extra)}")
        return schemes

    def fetch(self, month: date):
        self._expected = 0
        schemes, docs, _ = super().fetch(month)
        return schemes, docs, self._expected
