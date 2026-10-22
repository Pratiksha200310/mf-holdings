"""Run one AMC for one month: fetch, enrich identifiers, validate, write CSV + report."""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from .base import AMC
from .identifiers import TickerMap, clean_isin, isin_is_valid, norm_name, strip_expiry
from .model import COLUMNS, LayoutError, Scheme

log = logging.getLogger(__name__)

NAV_LOW, NAV_HIGH = 97.0, 103.0
# Lines exempt from the identifier rule: cash, TREPS/repo, net receivables, deposits, margin.
CASH_TYPES = {"cash"}
# Physical gold/silver held by gold/silver ETFs: 'Gold', 'Physical Silver', 'GOLD .995 1KG BAR - Mumbai'.
PHYSICAL_RE = re.compile(r"^\s*(physical\s+)?(gold|silver)\b(?!.*\b(etf|fund|fof)\b)", re.I)


def enrich(schemes: list[Scheme], tickers: TickerMap) -> None:
    """Normalise identifiers and fill tickers from the exchange lists.

    - A value in the ISIN column that fails the ISIN check digit is not an ISIN. For derivatives it
      is usually a contract code (e.g. 'BIOCON250826'), which we resolve to the underlying.
    - Listed equity with no ISIN is matched to the exchange list by exact (normalised) name.
    - Derivatives get the underlying stock's ISIN and NSE symbol, or the index symbol for index futures.
    """
    local = {norm_name(h.instrument_name): clean_isin(h.isin)
             for s in schemes for h in s.holdings
             if h.asset_type == "equity" and isin_is_valid(clean_isin(h.isin))}
    for s in schemes:
        for h in s.holdings:
            h.instrument_name = re.sub(r"\s+", " ", h.instrument_name).strip()
            raw = re.sub(r"\s+", "", h.isin).upper()
            h.isin = clean_isin(raw)
            if h.asset_type == "derivative" and (not h.isin or not isin_is_valid(h.isin)):
                h.isin, h.ticker = tickers.underlying_of_derivative(h.instrument_name, raw, local)
            elif h.asset_type == "equity" and not isin_is_valid(h.isin):
                # Missing, or a placeholder such as HDFC's 'DUMMY0000123': try the exchange list.
                h.isin = tickers.isin_for_name(h.instrument_name) or h.isin
            if h.isin.startswith("DUMMY"):
                h.isin = ""  # a placeholder, not an identifier (HDFC, for not-yet-listed demerged shares)
            if h.isin and not h.ticker and h.asset_type != "cash":
                h.ticker = tickers.ticker_for(h.isin)
        _match_hedged_equity(s, tickers)


GENERIC = {"LTD", "LIMITED", "INDIA", "THE", "AND", "CO", "CORP", "CORPN", "COMPANY", "OF", "PREV",
           "EQUITY", "INDUSTRIES", "FINANCE", "FINANCIAL", "SERVICES", "BANK"}


def _tokens(name: str) -> set[str]:
    return {t for t in norm_name(strip_expiry(name)).split() if len(t) >= 3 and t not in GENERIC}


def _match_hedged_equity(scheme: Scheme, tickers: TickerMap) -> None:
    """Last resort for stock futures whose name matches nothing: the long equity line they hedge.

    In arbitrage/hedged books the short futures add up to exactly the number of shares held long,
    though the position may be split across two expiries. The names are often abbreviated past
    recognition ('PGC LTD FUT' for Power Grid, 'CROMPT GREA CON ELEC' for Crompton Greaves), so we
    group the unresolved futures by name without the expiry, sum their quantities, and accept an
    equity line with the same share count that also shares a distinctive word or the same industry.
    The match must be unique.
    """
    groups: dict[str, list] = {}
    for h in scheme.holdings:
        if h.asset_type == "derivative" and not h.isin and not h.ticker and h.quantity is not None:
            groups.setdefault(norm_name(strip_expiry(h.instrument_name)), []).append(h)
    for lines in groups.values():
        qty = abs(sum(h.quantity for h in lines))
        want, industry = _tokens(lines[0].instrument_name), lines[0].industry_or_rating.strip().lower()
        hits = {e.isin for e in scheme.holdings
                if e.asset_type == "equity" and e.isin and e.quantity is not None and abs(e.quantity) == qty
                and (want & _tokens(e.instrument_name)
                     or (industry and e.industry_or_rating.strip().lower() == industry))}
        if len(hits) == 1:
            isin = hits.pop()
            for h in lines:
                h.isin, h.ticker = isin, tickers.ticker_for(isin)


def build_report(amc: AMC, month: date, schemes: list[Scheme], docs, expected: int | None) -> dict:
    per_scheme, outside, missing, bad_isin, warnings, physical = [], [], [], [], [], []
    for s in schemes:
        all_lines = round(sum(h.pct_of_nav or 0 for h in s.holdings), 4)
        ex_deriv = round(sum(h.pct_of_nav or 0 for h in s.holdings if h.asset_type != "derivative"), 4)
        # Total the scheme the way the AMC's own Grand Total does (see Scheme.nav_excludes_derivatives).
        total = ex_deriv if s.nav_excludes_derivatives else all_lines
        per_scheme.append({"scheme_name": s.name, "pct_of_nav_total": total,
                           "pct_of_nav_all_lines": all_lines, "pct_of_nav_excluding_derivatives": ex_deriv,
                           "derivatives_outside_nav_total": s.nav_excludes_derivatives,
                           "holdings": len(s.holdings), "source": s.source})
        warnings.extend(s.warnings)
        if not NAV_LOW <= total <= NAV_HIGH:
            outside.append({"scheme_name": s.name, "pct_of_nav_total": total})
        for h in s.holdings:
            if h.asset_type in CASH_TYPES:
                continue
            if PHYSICAL_RE.match(h.instrument_name):
                physical.append({"scheme_name": s.name, "instrument_name": h.instrument_name})
                continue
            if not h.isin and not h.ticker:
                missing.append({"scheme_name": s.name, "instrument_name": h.instrument_name,
                                "asset_type": h.asset_type})
            elif h.isin and not isin_is_valid(h.isin):
                bad_isin.append({"scheme_name": s.name, "instrument_name": h.instrument_name, "isin": h.isin})
    return {
        "amc": amc.key,
        "month": f"{month:%Y-%m}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [d.url for d in docs],
        "scheme_count": len(schemes),
        "expected_scheme_count": expected,
        "schemes": per_scheme,
        "schemes_outside_97_103": outside,
        "security_lines_without_identifier": missing,
        # Not required by the brief: identifiers printed in the file that fail the ISIN check digit.
        "isins_failing_check_digit": bad_isin,
        # Physical gold/silver held by commodity ETFs: not securities, so no ISIN exists.
        "physical_commodity_lines": physical,
        "holding_count": sum(len(s.holdings) for s in schemes),
        "source_file_warnings": warnings,
    }


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6f}".rstrip("0").rstrip(".") if v != int(v) or abs(v) < 1e15 else str(v)
    return str(v)


def write_outputs(out_dir: Path, amc: AMC, schemes: list[Scheme], report: dict) -> tuple[Path, Path]:
    """Write to temp files first, then rename, so a crash never leaves a half-written CSV."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path, rep_path = out_dir / f"{amc.key}.csv", out_dir / f"{amc.key}_report.json"
    tmp_csv, tmp_rep = csv_path.with_suffix(".csv.tmp"), rep_path.with_suffix(".json.tmp")
    with open(tmp_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for s in schemes:
            for h in s.holdings:
                w.writerow([amc.key, s.name, s.month_end.isoformat(), h.instrument_name, h.isin, h.ticker,
                            h.industry_or_rating, h.asset_type, _fmt(h.quantity),
                            _fmt(h.market_value_lakhs), _fmt(h.pct_of_nav)])
    tmp_rep.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_csv, csv_path)
    os.replace(tmp_rep, rep_path)
    return csv_path, rep_path


def run(amc: AMC, month: date, out_root: Path, tickers: TickerMap) -> dict:
    schemes, docs, expected = amc.fetch(month)
    if not schemes:
        raise LayoutError(f"{amc.key}: files found for {month:%Y-%m} but no schemes parsed")
    names = [s.name for s in schemes]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise LayoutError(f"{amc.key}: scheme(s) parsed twice: {sorted(dupes)}")
    if expected is not None and expected != len(schemes):
        raise LayoutError(f"{amc.key}: parsed {len(schemes)} schemes but the site lists {expected} for "
                          f"{month:%Y-%m}; refusing to write an incomplete month")
    enrich(schemes, tickers)
    report = build_report(amc, month, schemes, docs, expected)
    csv_path, rep_path = write_outputs(out_root / f"{month:%Y-%m}", amc, schemes, report)
    log.info("%s %s: %d schemes, %d holdings, %d outside 97-103, %d lines without identifier -> %s",
             amc.key, f"{month:%Y-%m}", len(schemes), report["holding_count"],
             len(report["schemes_outside_97_103"]), len(report["security_lines_without_identifier"]), csv_path)
    return report
