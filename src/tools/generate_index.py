"""
Generate a dark-theme index.html for all HTML reports in src/tools/reports/.

Extracts ticker, company name, signal, and date from each report file,
then writes a sortable index table to reports/index.html.
"""

import re
from pathlib import Path


REPORTS_DIR = Path(__file__).parent / "reports"

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Watchlist &middot; Reports</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#0a0d14;--surface:#0f1520;--card:#111827;
  --border:#1e2d42;--text:#e2e8f0;--muted:#64748b;--dim:#2d3748;
  --blue:#3b82f6;--num:#7dd3fc;
  --green:#22c55e;--amber:#f59e0b;--red:#ef4444;
  --green-bg:#052e16;--amber-bg:#1c1000;--red-bg:#1c0303;
  --green-bdr:#166534;--amber-bdr:#92400e;--red-bdr:#7f1d1d;
}}
body{{background:var(--bg);color:var(--text);
     font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}}
.container{{max-width:960px;margin:0 auto;padding:48px 24px 80px}}
h1{{font-family:'Playfair Display',serif;font-size:2rem;font-weight:700;
   letter-spacing:-.5px;margin-bottom:6px}}
.subtitle{{color:var(--muted);font-size:13px;margin-bottom:36px}}
table{{width:100%;border-collapse:collapse}}
thead th{{font-size:9px;font-weight:600;letter-spacing:1.8px;text-transform:uppercase;
         color:var(--muted);text-align:left;padding:0 12px 10px;cursor:pointer;
         user-select:none;white-space:nowrap}}
thead th:hover{{color:var(--text)}}
thead th .sort-icon{{margin-left:4px;opacity:.4}}
thead th.active .sort-icon{{opacity:1}}
tbody tr{{border-top:1px solid var(--border);transition:background .15s}}
tbody tr:hover{{background:var(--card)}}
tbody td{{padding:14px 12px;vertical-align:middle}}
.ticker{{font-family:'IBM Plex Mono',monospace;font-weight:500;
         color:var(--num);font-size:13px}}
.company{{color:var(--text)}}
.date{{font-family:'IBM Plex Mono',monospace;color:var(--muted);font-size:12px}}
a{{text-decoration:none;color:inherit}}
a:hover .company{{color:var(--blue);text-decoration:underline}}
.pill{{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:500;
       padding:4px 12px;border-radius:4px;border:1px solid;
       letter-spacing:2px;white-space:nowrap;display:inline-block}}
.pill.buy {{color:var(--green);background:var(--green-bg);border-color:var(--green-bdr)}}
.pill.hold{{color:var(--amber);background:var(--amber-bg);border-color:var(--amber-bdr)}}
.pill.sell{{color:var(--red);  background:var(--red-bg);  border-color:var(--red-bdr)}}
@media(max-width:600px){{
  .company{{display:block;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
}}
</style>
</head>
<body>
<div class="container">
  <h1>AI Watchlist</h1>
  <p class="subtitle">{count} report{plural} &middot; click a row to open</p>
  <table id="tbl">
    <thead>
      <tr>
        <th data-col="ticker">Ticker<span class="sort-icon">↕</span></th>
        <th data-col="company">Company<span class="sort-icon">↕</span></th>
        <th data-col="signal">Signal<span class="sort-icon">↕</span></th>
        <th data-col="date">Date<span class="sort-icon">↕</span></th>
      </tr>
    </thead>
    <tbody>
{rows}
    </tbody>
  </table>
</div>
<script>
(function(){{
  const tbl = document.getElementById('tbl');
  const tbody = tbl.querySelector('tbody');
  const headers = tbl.querySelectorAll('thead th');
  let sortCol = 'date', sortAsc = false;

  const signalOrder = {{buy:0, hold:1, sell:2}};

  function rowValue(tr, col){{
    return tr.dataset[col] || '';
  }}

  function sort(col){{
    if(sortCol === col){{ sortAsc = !sortAsc; }}
    else{{ sortCol = col; sortAsc = col !== 'date'; }}

    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => {{
      let av = rowValue(a, col), bv = rowValue(b, col);
      if(col === 'signal'){{
        av = signalOrder[av] ?? 9;
        bv = signalOrder[bv] ?? 9;
        const cmp = av - bv;
        return sortAsc ? cmp : -cmp;
      }}
      const cmp = av.localeCompare(bv, undefined, {{numeric: true}});
      return sortAsc ? cmp : -cmp;
    }});
    rows.forEach(r => tbody.appendChild(r));

    headers.forEach(h => {{
      h.classList.toggle('active', h.dataset.col === col);
      const icon = h.querySelector('.sort-icon');
      if(h.dataset.col === col) icon.textContent = sortAsc ? '↑' : '↓';
      else icon.textContent = '↕';
    }});
  }}

  headers.forEach(h => h.addEventListener('click', () => sort(h.dataset.col)));
  sort('date');
}})();
</script>
</body>
</html>
"""

ROW_TEMPLATE = (
    '      <tr data-ticker="{ticker}" data-company="{company_lower}" '
    'data-signal="{signal}" data-date="{date}">'
    '<td><a href="{filename}"><span class="ticker">{ticker}</span></a></td>'
    '<td><a href="{filename}"><span class="company">{company}</span></a></td>'
    '<td><span class="pill {signal}">{signal_label}</span></td>'
    '<td><a href="{filename}"><span class="date">{date}</span></a></td></tr>'
)


def parse_report(path: Path) -> dict | None:
    m = re.match(r"^([A-Z0-9]+)_(\d{8})$", path.stem)
    if not m:
        return None

    ticker = m.group(1)
    raw_date = m.group(2)
    date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"

    content = path.read_text(encoding="utf-8", errors="replace")

    company_match = re.search(
        r'class="company-name playfair"[^>]*>\s*([^<]+?)\s*<', content
    )
    company = company_match.group(1).strip() if company_match else ticker

    signal_match = re.search(r'class="signal-pill\s+(buy|hold|sell)"', content)
    signal = signal_match.group(1) if signal_match else "hold"

    return {
        "ticker": ticker,
        "company": company,
        "signal": signal,
        "date": date,
        "filename": path.name,
    }


def build_row(r: dict) -> str:
    return ROW_TEMPLATE.format(
        ticker=r["ticker"],
        company=r["company"],
        company_lower=r["company"].lower(),
        signal=r["signal"],
        signal_label=r["signal"].upper(),
        date=r["date"],
        filename=r["filename"],
    )


def main() -> None:
    reports = []
    for html_file in sorted(REPORTS_DIR.glob("*.html")):
        if html_file.name == "index.html":
            continue
        data = parse_report(html_file)
        if data:
            reports.append(data)

    reports.sort(key=lambda r: (r["date"], r["ticker"]), reverse=True)

    rows = "\n".join(build_row(r) for r in reports)
    count = len(reports)
    plural = "" if count == 1 else "s"

    index_html = HTML_TEMPLATE.format(count=count, plural=plural, rows=rows)

    out = REPORTS_DIR / "index.html"
    out.write_text(index_html, encoding="utf-8")
    print(f"Written: {out}  ({count} report{plural})")


if __name__ == "__main__":
    main()
