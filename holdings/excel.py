"""Small helpers for reading messy AMC spreadsheets."""
from __future__ import annotations

import re
from pathlib import Path


def read_workbook(path: Path) -> dict[str, list[list]]:
    """Return {sheet name: rows}, each row a list of cell values. Handles .xlsx and legacy .xls."""
    path = Path(path)
    with open(path, "rb") as fh:
        magic = fh.read(8)
    if magic.startswith(b"PK"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
        finally:
            wb.close()
    if magic.startswith(b"\xd0\xcf\x11\xe0"):
        import os

        import xlrd

        with open(os.devnull, "w") as quiet:  # xlrd prints warnings about formulas it ignores
            book = xlrd.open_workbook(path, logfile=quiet)
        return {sh.name: [sh.row_values(i) for i in range(sh.nrows)] for sh in book.sheets()}
    raise ValueError(f"{path.name}: not an Excel file (starts with {magic!r})")


def text(v) -> str:
    """Cell value as trimmed single-spaced text ('' for empty)."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\s+", " ", str(v)).strip()


_NUM_JUNK = re.compile(r"[,\s₹%]|Rs\.?", re.I)


def num(v) -> float | None:
    """Parse a numeric cell. '', '-', 'NIL', 'N.A.' -> None. '(1.5)' -> -1.5."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _NUM_JUNK.sub("", str(v))
    if s in ("", "-", "--") or s.upper() in ("NIL", "NA", "N.A.", "N.A"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        f = float(s)
    except ValueError:
        return None
    return -f if neg else f


def find_header(rows: list[list], required: list[str], max_scan: int = 40) -> tuple[int, dict[str, int]]:
    """Find the header row: the first row whose cells contain every `required` keyword.

    Returns (row index, {lower-cased header text: column index}). Raises ValueError if not found.
    """
    for i, row in enumerate(rows[:max_scan]):
        cells = {text(c).lower(): j for j, c in enumerate(row) if text(c)}
        joined = " | ".join(cells)
        if all(k in joined for k in required):
            return i, cells
    raise ValueError(f"no header row containing {required}")


def col(header: dict[str, int], *keywords: str) -> int | None:
    """Index of the first header cell containing any of the keywords."""
    for k in keywords:
        for name, j in header.items():
            if k in name:
                return j
    return None


def lakhs_factor(header_text: str) -> float:
    """Multiplier that converts a market-value column to lakhs, from its header text."""
    h = header_text.lower()
    if "lakh" in h or "lac" in h:
        return 1.0
    if "crore" in h or re.search(r"\bcr\b", h):
        return 100.0
    if "rs" in h or "rupee" in h or "inr" in h or "₹" in h:
        return 1 / 100_000
    raise ValueError(f"cannot tell the unit of market value column {header_text!r}")
