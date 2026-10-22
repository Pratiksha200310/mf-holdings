"""Rebuild out/index.html from whatever reports exist on disk. Plain HTML, no scripts, works offline."""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .amcs import REGISTRY


def build(out_root: Path) -> Path:
    months = sorted(p.name for p in out_root.iterdir() if p.is_dir() and len(p.name) == 7 and p.name[4] == "-")
    rows = []
    for key, cls in REGISTRY.items():
        cells = [f"<th>{html.escape(cls.name)}</th>"]
        for m in months:
            rep_path = out_root / m / f"{key}_report.json"
            csv_path = out_root / m / f"{key}.csv"
            if not rep_path.exists():
                cells.append('<td class="none">not extracted</td>')
                continue
            rep = json.loads(rep_path.read_text(encoding="utf-8"))
            bad = len(rep["schemes_outside_97_103"])
            noid = len(rep["security_lines_without_identifier"])
            cls_name = "ok" if bad == 0 and noid == 0 else "warn"
            cells.append(
                f'<td class="{cls_name}"><b>extracted</b><br>'
                f'{rep["scheme_count"]} schemes · {rep["holding_count"]} lines<br>'
                f'{bad} outside 97–103%<br>{noid} lines without ISIN/ticker<br>'
                f'<a href="{m}/{csv_path.name}">CSV</a> · <a href="{m}/{rep_path.name}">report</a></td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")

    head = "".join(f"<th>{m}</th>" for m in months)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>MF holdings extraction status</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#222}}
table{{border-collapse:collapse}} th,td{{border:1px solid #ccc;padding:.6rem .8rem;vertical-align:top;font-size:14px}}
thead th{{background:#f3f3f3}} tbody th{{text-align:left}}
td.ok{{background:#e8f6ea}} td.warn{{background:#fff4dc}} td.none{{background:#f6f6f6;color:#888}}
</style></head><body>
<h1>Mutual fund holdings: extraction status</h1>
<p>Green: every scheme totals 97–103% of NAV and every security line has an identifier. Amber: see the report.</p>
<table><thead><tr><th>AMC</th>{head}</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<p><small>Generated {now}</small></p>
</body></html>
"""
    path = out_root / "index.html"
    path.write_text(page, encoding="utf-8")
    return path
