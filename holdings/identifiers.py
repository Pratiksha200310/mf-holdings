"""ISIN checks and ISIN -> exchange ticker mapping from the exchanges' public lists.

NSE: the equity, SME and ETF security lists on nsearchives.nseindia.com (plain CSV).
BSE: the daily equity bhavcopy CSV, which carries ISIN and the numeric BSE scrip code.
     (BSE's scrip-list API sits behind bot protection; the bhavcopy file does not.)
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, timedelta

from .fetch import Fetcher

log = logging.getLogger(__name__)

NSE_LISTS = [
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv",
    "https://nsearchives.nseindia.com/content/equities/eq_etfseclist.csv",
]
BSE_BHAVCOPY = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"

# Index futures/options: normalised name as AMCs print it -> NSE F&O symbol.
INDEX_SYMBOLS = {
    "NSE NIFTY": "NIFTY", "NIFTY": "NIFTY", "NIFTY 50": "NIFTY", "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY", "BANK NIFTY": "BANKNIFTY",
    "MIDCAPNIFTY": "MIDCPNIFTY", "MIDCPNIFTY": "MIDCPNIFTY", "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "FINNIFTY": "FINNIFTY", "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "NIFTYNXT50": "NIFTYNXT50", "NIFTY NEXT 50": "NIFTYNXT50",
}

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def isin_is_valid(isin: str) -> bool:
    """Format plus ISO 6166 check digit (Luhn over the letters-as-numbers expansion)."""
    if not ISIN_RE.match(isin or ""):
        return False
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total = 0
    # Double every second digit starting from the rightmost one.
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return (10 - total % 10) % 10 == int(isin[-1])


def clean_isin(raw) -> str:
    s = re.sub(r"\s+", "", str(raw or "")).upper()
    return s if ISIN_RE.match(s) else ""


EXPIRY_RE = re.compile(
    r"(\s*[-(]?\s*(\d{1,2}[/\-. ])?(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)[A-Z]*[/\-. ]\d{2,4}\)?"
    r"|\s*[-(]?\s*\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\)?"
    r"|\s+(FUT|FUTURE|FUTURES|STOCK FUTURE|INDEX FUTURE|OPT|CE|PE))\s*$", re.I)


def strip_expiry(name: str) -> str:
    """'Cipla Ltd OCT-2026' / 'Infosys Limited 25/08/2026' -> the underlying's name.

    Also drops former-name notes ('REC Ltd (Prev : Rural Electrification ...)') and
    '- Equity' style suffixes some AMCs add to derivative lines.
    """
    prev = None
    while prev != name:
        prev, name = name, EXPIRY_RE.sub("", name).strip()
    name = re.sub(r"\s*\(\s*(pre|prev|previously|formerly|earlier)\b.*$", "", name, flags=re.I)
    name = re.sub(r"\s*-\s*(equity|equ)\s*$", "", name, flags=re.I)
    return name.strip()


def norm_name(name: str) -> str:
    return _norm_name(name)


def _norm_name(name: str) -> str:
    s = name.upper().replace("&", " AND ").replace("'", "")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\b(LIMITED|LTD|THE|CO|COMPANY|CORPORATION|CORP|INDIA)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class TickerMap:
    def __init__(self) -> None:
        self.nse_by_isin: dict[str, str] = {}
        self.bse_by_isin: dict[str, str] = {}
        self.isin_by_name: dict[str, str] = {}
        self.isin_by_symbol: dict[str, str] = {}

    @classmethod
    def load(cls, fetcher: Fetcher) -> "TickerMap":
        tm = cls()
        for url in NSE_LISTS:
            text = fetcher.get(url, cache=False).decode("utf-8-sig", errors="replace")
            tm._add_nse(text)
        tm._add_bse(fetcher)
        log.info("ticker map: %d NSE, %d BSE ISINs", len(tm.nse_by_isin), len(tm.bse_by_isin))
        return tm

    def _add_nse(self, text: str) -> None:
        for row in csv.DictReader(io.StringIO(text)):
            row = {k.strip().upper().replace("_", " "): (v or "").strip() for k, v in row.items() if k}
            isin = row.get("ISIN NUMBER") or row.get("ISINNUMBER") or ""
            symbol = row.get("SYMBOL", "")
            if ISIN_RE.match(isin) and symbol:
                self.nse_by_isin.setdefault(isin, symbol)
                self.isin_by_symbol.setdefault(symbol, isin)
                name = row.get("NAME OF COMPANY", "")
                if name:
                    self.isin_by_name.setdefault(_norm_name(name), isin)

    def _add_bse(self, fetcher: Fetcher) -> None:
        # The bhavcopy only exists for trading days; walk back to the most recent one.
        day = date.today()
        for _ in range(10):
            url = BSE_BHAVCOPY.format(d=day)
            day -= timedelta(days=1)
            try:
                text = fetcher.get(url, headers={"Referer": "https://www.bseindia.com/"})
            except Exception:
                continue
            if not text.startswith(b"TradDt"):
                # BSE answers 200 with its home page for days that have no file yet.
                fetcher.forget(url)
                continue
            for row in csv.DictReader(io.StringIO(text.decode("utf-8-sig", errors="replace"))):
                isin, code = row.get("ISIN", ""), row.get("FinInstrmId", "")
                if ISIN_RE.match(isin) and code:
                    self.bse_by_isin.setdefault(isin, code)
                    self.isin_by_name.setdefault(_norm_name(row.get("FinInstrmNm", "")), isin)
            return
        log.warning("no BSE bhavcopy found in the last 10 days; BSE codes will be missing")

    def ticker_for(self, isin: str) -> str:
        """NSE symbol if listed on NSE, else the BSE scrip code, else ''."""
        return self.nse_by_isin.get(isin) or self.bse_by_isin.get(isin, "")

    def underlying_of_derivative(self, name: str, code: str, local: dict[str, str] | None = None) -> tuple[str, str]:
        """(isin, ticker) of a stock/index future's underlying, from the file's contract code or name.

        Quant prints either the underlying's ISIN, or a contract code like 'BIOCON250826'
        (NSE symbol + expiry DDMMYY), or only a name like "Divi's Laboratories Limited 25/08/2026".
        Sundaram prints names like "Cipla Ltd OCT-2026". `local` maps normalised names of equity
        lines in the same file to their ISINs: an arbitrage short is nearly always against a stock
        the scheme holds, and the AMC spells both the same way.
        Index futures have no ISIN; their ticker is the NSE index symbol (NIFTY, BANKNIFTY, ...).
        """
        m = re.match(r"^([A-Z0-9&\-]+?)(\d{6})$", code or "")
        if m:
            sym = m.group(1)
            if sym in self.isin_by_symbol:
                return self.isin_by_symbol[sym], sym
            if sym in INDEX_SYMBOLS.values():
                return "", sym
        base = strip_expiry(name)
        idx = INDEX_SYMBOLS.get(_norm_name(base))
        if idx:
            return "", idx
        isin = (local or {}).get(_norm_name(base)) or self.isin_for_name(base)
        return isin, self.ticker_for(isin) if isin else ""

    def isin_for_name(self, name: str) -> str:
        """Exact match on a normalised company name. No fuzzy matching: a wrong ISIN is worse than none."""
        return self.isin_by_name.get(_norm_name(name), "")
