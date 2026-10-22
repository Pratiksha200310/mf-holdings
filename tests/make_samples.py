"""Rebuild the small sample workbooks in tests/samples/ from the download cache.

Each sample keeps a few whole sheets (one or two schemes) of a real AMC file, chosen because they
contain that AMC's layout traps. Values only; trailing empty rows dropped.

    python -m holdings 2026-09          # fills .cache/
    python tests/make_samples.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holdings.amcs import REGISTRY  # noqa: E402
from holdings.excel import read_workbook, text  # noqa: E402
from holdings.fetch import Fetcher  # noqa: E402

SAMPLES = ROOT / "tests" / "samples"
SEP = date(2026, 9, 1)

# sample file -> (amc, month, document label contains, sheets to keep)
PLAN = {
    "quant_2026-09.xlsx": ("quant", SEP, "September 2026", ["qMSE", "qMES"]),
    "sundaram_2026-09.xlsx": ("sundaram", SEP, "Fixed Income", ["Index", "SUNONF", "SFRSTP"]),
    "hdfc_2026-09.xlsx": ("hdfc", SEP, "Aggressive Hybrid", None),
    "mahindra_2026-09.xlsx": ("mahindra", SEP, "September", ["MMF12", "MMF15"]),
    "mahindra_2026-07.xlsx": ("mahindra", date(2026, 7, 1), "July", ["MMF12"]),
    "jioblackrock_overnight_2026-09.xlsx": ("jioblackrock", SEP, "Overnight", None),
    "jioblackrock_arbitrage_2026-09.xlsx": ("jioblackrock", SEP, "Arbitrage", None),
    "bandhan_bal_adv_2026-09.xlsx": ("bandhan", SEP, "Balanced Advantage", None),
    "bandhan_gold_etf_2026-09.xlsx": ("bandhan", SEP, "Gold ETF 30", None),
    "bandhan_floating_2026-08.xlsx": ("bandhan", date(2026, 8, 1), "Floating Interest", None),
}


def trim(rows: list[list]) -> list[list]:
    while rows and not any(text(c) for c in rows[-1]):
        rows = rows[:-1]
    return rows


def main() -> None:
    fetcher = Fetcher(ROOT / ".cache", offline=True)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    for out, (key, month, label, keep) in PLAN.items():
        amc = REGISTRY[key](fetcher)
        doc = next(d for d in amc.discover(month) if label in d.label)
        sheets = read_workbook(fetcher.download(doc.url))
        names = keep or list(sheets)
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name in names:
            rows = sheets[name]
            if name == "Index" and keep:  # list only the sheets we kept
                rows = [rows[0]] + [r for r in rows[1:] if len(r) > 1 and text(r[1]) in keep]
            ws = wb.create_sheet(name[:31])
            for r in trim(rows):
                ws.append(list(r))
        wb.save(SAMPLES / out)
        print(out, names)


if __name__ == "__main__":
    main()
