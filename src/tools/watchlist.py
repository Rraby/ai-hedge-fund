#!/usr/bin/env python3
"""
Watchlist 10-Q analyzer: fetches SEC filings via EdgarTools, compares QoQ
metrics, then generates a structured AI brief via Claude.

Usage:
    poetry run python src/tools/watchlist.py --ticker NVDA
    poetry run python src/tools/watchlist.py --all
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows terminals (avoids cp1252 UnicodeEncodeError)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

WATCHLIST = ["NVDA", "ASML", "PLTR", "ARM", "SMCI", "CEVA", "BBAI", "AMBA", "SOUN", "VRT"]
REPORTS_DIR = Path(__file__).parent / "reports"
MODEL = "claude-sonnet-4-6"

# Columns that are metadata, not financial data periods
_META = {
    "concept", "label", "standard_concept", "level", "abstract",
    "dimension", "is_breakdown", "dimension_axis", "dimension_member",
    "dimension_member_label", "dimension_label", "balance", "weight",
    "preferred_sign", "parent_concept", "parent_abstract_concept",
}

_COMPANY_NAMES = {
    "NVDA": "NVIDIA Corporation",     "ASML": "ASML Holding N.V.",
    "PLTR": "Palantir Technologies",  "ARM":  "Arm Holdings plc",
    "SMCI": "Super Micro Computer",   "CEVA": "CEVA, Inc.",
    "BBAI": "BigBear.ai Holdings",    "AMBA": "Ambarella, Inc.",
    "SOUN": "SoundHound AI",          "VRT":  "Vertiv Holdings",
}

# ── EDGAR setup ───────────────────────────────────────────────────────────────

def setup_edgar():
    """Configure edgartools SSL + identity; returns the edgar module."""
    import edgar
    edgar.configure_http(use_system_certs=True)

    identity = os.getenv("EDGAR_IDENTITY")
    if not identity:
        print("SEC EDGAR requires a name + email for rate-limiting identification.")
        print("Example: 'Jane Smith jane@example.com'")
        identity = input("EDGAR identity: ").strip()
        if not identity:
            sys.exit("Error: EDGAR identity is required.")
        os.environ["EDGAR_IDENTITY"] = identity

    edgar.set_identity(identity)
    return edgar

# ── Financial extraction ──────────────────────────────────────────────────────

def _data_cols(df):
    """Return ordered list of non-metadata columns (financial data periods)."""
    return [c for c in df.columns if c not in _META]


def _stmt_value(df, standard_concept: str, col: str):
    """
    Pull a single value from a Statement DataFrame by standard_concept.
    Skips abstract header rows and dimension breakdown rows.
    """
    mask = (
        (df["standard_concept"] == standard_concept)
        & (~df["abstract"].fillna(False))
        & (~df["is_breakdown"].fillna(False))
    )
    rows = df[mask]
    if rows.empty or col not in rows.columns:
        return None
    vals = rows[col].dropna()
    return float(vals.iloc[0]) if not vals.empty else None


def extract_metrics(filing) -> dict | None:
    """Extract all required financial metrics from a single 10-Q / 6-K filing."""
    import io, sys as _sys

    try:
        q = filing.obj()
        if q is None:
            return None
        fin = q.financials
        if fin is None:
            return None

        # Suppress verbose "Failed to resolve" warnings from edgartools internals
        old_stderr, _sys.stderr = _sys.stderr, io.StringIO()
        try:
            base = fin.get_financial_metrics()
        finally:
            _sys.stderr = old_stderr

        revenue    = base.get("revenue")
        op_income  = base.get("operating_income")
        net_income = base.get("net_income")
        op_cf      = base.get("operating_cash_flow")
        capex      = base.get("capital_expenditures")
        fcf        = base.get("free_cash_flow")
        shares_dil = base.get("shares_outstanding_diluted")

        eps_diluted = (net_income / shares_dil) if (net_income and shares_dil) else None

        # Gross profit from income statement DataFrame
        gross_profit = None
        try:
            inc_df  = fin.income_statement().to_dataframe()
            inc_col = (_data_cols(inc_df) or [None])[0]
            if inc_col:
                gross_profit = _stmt_value(inc_df, "GrossProfit", inc_col)
        except Exception:
            pass

        gross_margin = (gross_profit / revenue) if (gross_profit and revenue) else None
        op_margin    = (op_income / revenue) if (op_income and revenue) else None

        # Cash and debt from balance sheet DataFrame
        cash = total_debt = None
        try:
            bs_df  = fin.balance_sheet().to_dataframe()
            bs_col = (_data_cols(bs_df) or [None])[0]
            if bs_col:
                # Cash = cash + marketable debt securities (exclude equity securities)
                cash_mask = (
                    (bs_df["standard_concept"] == "CashAndMarketableSecurities")
                    & (~bs_df["label"].str.contains("equity", case=False, na=False))
                    & (~bs_df["is_breakdown"].fillna(False))
                )
                cash_vals = bs_df[cash_mask][bs_col].dropna()
                cash = float(cash_vals.sum()) if not cash_vals.empty else None

                # Total debt = short-term + long-term
                debt_mask = (
                    bs_df["standard_concept"].isin(["ShortTermDebt", "LongTermDebt"])
                    & (~bs_df["is_breakdown"].fillna(False))
                )
                debt_vals = bs_df[debt_mask][bs_col].dropna()
                total_debt = float(debt_vals.sum()) if not debt_vals.empty else None
        except Exception:
            pass

        return {
            "filing_date":      str(filing.filing_date),
            "period_of_report": str(filing.period_of_report),
            "revenue":          revenue,
            "gross_profit":     gross_profit,
            "gross_margin":     gross_margin,
            "operating_income": op_income,
            "operating_margin": op_margin,
            "net_income":       net_income,
            "eps_diluted":      eps_diluted,
            "operating_cf":     op_cf,
            "capex":            capex,
            "fcf":              fcf,
            "cash":             cash,
            "total_debt":       total_debt,
        }

    except Exception as e:
        print(f"  Warning: extraction failed: {e}")
        return None

# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_money(v) -> str:
    if v is None:
        return "N/A"
    abs_v = abs(v)
    if abs_v >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs_v >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"


def _fmt_pct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "N/A"


def _fmt_eps(v) -> str:
    return f"${v:.3f}" if v is not None else "N/A"


def _qoq(new, old) -> str:
    """Return a '(+X.X% QoQ)' annotation, or empty string."""
    if new is None or old is None or old == 0:
        return ""
    chg = (new - old) / abs(old)
    sign = "+" if chg >= 0 else ""
    return f" ({sign}{chg*100:.1f}% QoQ)"

# ── Claude prompt ─────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a senior equity analyst. Given quarterly 10-Q financial data, provide:

1. **Top 3 Changes vs Prior Quarter** — the most significant metric movements with specific numbers
2. **Bull Case** — 3 data-supported reasons to own the stock
3. **Bear Case / Risks** — 3 specific risks or concerns backed by the data
4. **Verdict** — one line: "Buy / Hold / Sell — [High/Medium/Low] conviction — [one sentence rationale]"

Be direct and quantitative. No hedging language or generic statements."""


def _build_prompt(ticker: str, curr: dict, prior: dict) -> str:
    def row(label, c_val, p_val, fmt_fn):
        chg = _qoq(c_val, p_val)
        return f"  {label}: {fmt_fn(c_val)}{chg}  |  Prior: {fmt_fn(p_val)}"

    lines = [
        f"TICKER: {ticker}",
        f"Current Quarter: {curr['period_of_report']}  (Filed: {curr['filing_date']})",
        f"Prior Quarter:   {prior['period_of_report']}  (Filed: {prior['filing_date']})",
        "",
        "INCOME STATEMENT:",
        row("Revenue",          curr["revenue"],          prior["revenue"],          _fmt_money),
        row("Gross Profit",     curr["gross_profit"],     prior["gross_profit"],     _fmt_money),
        row("Gross Margin",     curr["gross_margin"],     prior["gross_margin"],     _fmt_pct),
        row("Operating Income", curr["operating_income"], prior["operating_income"], _fmt_money),
        row("Operating Margin", curr["operating_margin"], prior["operating_margin"], _fmt_pct),
        row("Net Income",       curr["net_income"],       prior["net_income"],       _fmt_money),
        row("EPS (diluted)",    curr["eps_diluted"],      prior["eps_diluted"],      _fmt_eps),
        "",
        "CASH FLOW:",
        row("Operating CF",     curr["operating_cf"],     prior["operating_cf"],     _fmt_money),
        row("CapEx",            curr["capex"],            prior["capex"],            _fmt_money),
        row("Free Cash Flow",   curr["fcf"],              prior["fcf"],              _fmt_money),
        "",
        "BALANCE SHEET:",
        row("Cash + Mkt Sec",   curr["cash"],             prior["cash"],             _fmt_money),
        row("Total Debt",       curr["total_debt"],       prior["total_debt"],       _fmt_money),
    ]
    return "\n".join(lines)


def _call_claude(ticker: str, curr: dict, prior: dict, client: anthropic.Anthropic) -> str:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(ticker, curr, prior)}],
    )
    return resp.content[0].text

# ── Output builders ───────────────────────────────────────────────────────────

_BAR  = "─" * 58
_DBAR = "═" * 60


def _terminal_report(ticker: str, curr: dict, prior: dict, analysis: str) -> str:
    def line(label, val, fmt_fn, old=None):
        qoq = _qoq(val, old) if old is not None else ""
        return f"  {label:<22} {fmt_fn(val)}{qoq}"

    sections = [
        f"\n{_DBAR}",
        f"  {ticker}  |  Period: {curr['period_of_report']}  |  Filed: {curr['filing_date']}",
        _DBAR,
        "",
        "  KEY METRICS (vs Prior Quarter)",
        f"  {_BAR}",
        line("Revenue",          curr["revenue"],          _fmt_money, prior["revenue"]),
        line("Gross Margin",     curr["gross_margin"],     _fmt_pct,   prior["gross_margin"]),
        line("Operating Margin", curr["operating_margin"], _fmt_pct,   prior["operating_margin"]),
        line("EPS (diluted)",    curr["eps_diluted"],      _fmt_eps,   prior["eps_diluted"]),
        line("Free Cash Flow",   curr["fcf"],              _fmt_money, prior["fcf"]),
        line("Cash & Mkt Sec",   curr["cash"],             _fmt_money),
        line("Total Debt",       curr["total_debt"],       _fmt_money),
        "",
        "  AI ANALYSIS",
        f"  {_BAR}",
    ]
    for ln in analysis.split("\n"):
        sections.append(f"  {ln}")
    sections.append(f"\n{_DBAR}\n")
    return "\n".join(sections)


def _markdown_report(ticker: str, curr: dict, prior: dict, analysis: str) -> str:
    def md_row(label, c_val, p_val, fmt_fn):
        qoq = _qoq(c_val, p_val).strip("() ").replace("QoQ", "").strip()
        return f"| {label} | {fmt_fn(c_val)} | {fmt_fn(p_val)} | {qoq} |"

    rows = [
        md_row("Revenue",          curr["revenue"],          prior["revenue"],          _fmt_money),
        md_row("Gross Profit",     curr["gross_profit"],     prior["gross_profit"],     _fmt_money),
        md_row("Gross Margin",     curr["gross_margin"],     prior["gross_margin"],     _fmt_pct),
        md_row("Operating Income", curr["operating_income"], prior["operating_income"], _fmt_money),
        md_row("Operating Margin", curr["operating_margin"], prior["operating_margin"], _fmt_pct),
        md_row("Net Income",       curr["net_income"],       prior["net_income"],       _fmt_money),
        md_row("EPS (diluted)",    curr["eps_diluted"],      prior["eps_diluted"],      _fmt_eps),
        md_row("Operating CF",     curr["operating_cf"],     prior["operating_cf"],     _fmt_money),
        md_row("CapEx",            curr["capex"],            prior["capex"],            _fmt_money),
        md_row("Free Cash Flow",   curr["fcf"],              prior["fcf"],              _fmt_money),
        md_row("Cash & Mkt Sec",   curr["cash"],             prior["cash"],             _fmt_money),
        md_row("Total Debt",       curr["total_debt"],       prior["total_debt"],       _fmt_money),
    ]

    lines = [
        f"# {ticker} — 10-Q Analysis: {curr['period_of_report']}",
        f"*Filed: {curr['filing_date']} | Prior Quarter: {prior['period_of_report']}*  ",
        f"*Generated: {datetime.today().strftime('%Y-%m-%d')} | Model: {MODEL}*",
        "",
        "## Key Metrics",
        "",
        "| Metric | Current Quarter | Prior Quarter | QoQ Change |",
        "|--------|----------------|---------------|------------|",
        *rows,
        "",
        "## AI Analysis",
        "",
        analysis,
        "",
        "---",
        f"*Source: SEC EDGAR via edgartools | Analysis: Claude ({MODEL})*",
    ]
    return "\n".join(lines)

# ── HTML report ───────────────────────────────────────────────────────────────

def _parse_analysis(text: str) -> dict:
    """
    Parse Claude's markdown output into structured components for HTML rendering.
    Handles both markdown-table and numbered-list section formats.
    """
    import re

    result = {
        "changes":      [],
        "bull":         [],
        "bear":         [],
        "verdict_full": "",
        "signal":       "hold",
        "conviction":   "medium",
    }

    def md_inline(s: str) -> str:
        s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'\*([^*]+?)\*',  r'<em>\1</em>', s)
        return s

    def extract_numbered(block: str, max_n: int = 3) -> list:
        items = []
        for m in re.finditer(r'^\d+\.\s+(.+?)(?=^\d+\.|\Z)', block, re.MULTILINE | re.DOTALL):
            item = re.split(r'\n\s*\n', m.group(1).strip())[0]
            item = item.replace('\n', ' ').strip()
            if item:
                items.append(md_inline(item))
            if len(items) >= max_n:
                break
        return items

    def extract_changes(block: str, max_n: int = 3) -> list:
        items = []
        for line in block.split('\n'):
            s = line.strip()
            if s.startswith('|') and not re.match(r'^\|[\s\-:|]+\|', s):
                cells = [c.strip() for c in s.strip('|').split('|') if c.strip()]
                if cells and cells[0].lower() not in ('metric', 'measure', 'item'):
                    label  = cells[0]
                    change = cells[-1] if len(cells) > 2 else (cells[1] if len(cells) > 1 else "")
                    items.append(md_inline(f'<strong>{label}</strong> — {change}'))
                    if len(items) >= max_n:
                        break
        if not items:
            items = extract_numbered(block, max_n)
        if not items:
            for m in re.finditer(r'^[-•]\s+(.+?)$', block, re.MULTILINE):
                items.append(md_inline(m.group(1).strip()))
                if len(items) >= max_n:
                    break
        return items

    for part in re.split(r'\n(?=#{2,3}\s)', text):
        hm = re.match(r'^#{2,3}\s+(?:\d+\.\s+)?(.+)', part)
        if not hm:
            continue
        heading = re.sub(r'[^\x00-\x7F]+', '', hm.group(1)).strip().lower()
        body    = part[hm.end():].strip()

        if 'top 3 changes' in heading or ('changes' in heading and 'prior' in heading):
            result["changes"] = extract_changes(body)
        elif 'bull' in heading:
            result["bull"] = extract_numbered(body)
        elif 'bear' in heading or 'risk' in heading:
            result["bear"] = extract_numbered(body)
        elif 'verdict' in heading:
            result["verdict_full"] = body
            result["signal"] = (
                "buy"  if re.search(r'\bBuy\b',  body) else
                "sell" if re.search(r'\bSell\b', body) else "hold"
            )
            result["conviction"] = (
                "high" if re.search(r'[Hh]igh\s+[Cc]onviction', body) else
                "low"  if re.search(r'[Ll]ow\s+[Cc]onviction',  body) else "medium"
            )

    return result


def generate_html_report(data: dict, ticker: str, output_path: Path) -> None:
    """
    Generate a self-contained dark-theme HTML financial report.

    data keys: ticker, curr, prior, analysis, generated, model, form_used
    curr / prior are dicts from extract_metrics().
    """
    import re, json

    curr      = data.get("curr",      {})
    prior     = data.get("prior",     {})
    analysis  = data.get("analysis",  "")
    generated = data.get("generated", datetime.today().strftime("%Y-%m-%d"))
    model     = data.get("model",     MODEL)

    parsed     = _parse_analysis(analysis)
    signal     = parsed["signal"]      # "buy" | "hold" | "sell"
    conviction = parsed["conviction"]  # "high" | "medium" | "low"

    company_name = _COMPANY_NAMES.get(ticker, ticker)
    curr_period  = curr.get("period_of_report", "N/A")
    prior_period = prior.get("period_of_report", "N/A")
    curr_filed   = curr.get("filing_date", "N/A")

    # ── Value helpers ─────────────────────────────────────────────────────────
    def cv(key): return curr.get(key)
    def pv(key): return prior.get(key)

    def qoq_pct(key):
        c, p = cv(key), pv(key)
        if c is None or p is None or p == 0:
            return None
        return (c - p) / abs(p) * 100

    def arrow_html(key, higher_is_good=True):
        pct = qoq_pct(key)
        if pct is None:
            return '<span class="arrow neutral">&#x2192;</span><span class="chg neutral"> N/A</span>'
        up   = pct > 0
        good = up if higher_is_good else not up
        cls  = "up" if good else "dn"
        sym  = "&#x2191;" if up else "&#x2193;"
        sign = "+" if pct > 0 else ""
        return (f'<span class="arrow {cls}">{sym}</span>'
                f'<span class="chg {cls}"> {sign}{pct:.1f}%</span>')

    # ── Metrics grid ──────────────────────────────────────────────────────────
    metric_defs = [
        ("REVENUE",        _fmt_money(cv("revenue")),        arrow_html("revenue")),
        ("EPS DILUTED",    _fmt_eps(cv("eps_diluted")),       arrow_html("eps_diluted")),
        ("GROSS MARGIN",   _fmt_pct(cv("gross_margin")),      arrow_html("gross_margin")),
        ("OP. MARGIN",     _fmt_pct(cv("operating_margin")),  arrow_html("operating_margin")),
        ("FREE CASH FLOW", _fmt_money(cv("fcf")),             arrow_html("fcf")),
        ("CASH + MKT SEC", _fmt_money(cv("cash")),            arrow_html("cash")),
        ("TOTAL DEBT",     _fmt_money(cv("total_debt")),      arrow_html("total_debt", higher_is_good=False)),
    ]

    metrics_html = "".join(
        f'<div class="metric-card">'
        f'<div class="metric-label">{lbl}</div>'
        f'<div class="metric-value mono">{val}</div>'
        f'<div class="metric-arrow">{arr}</div>'
        f'</div>'
        for lbl, val, arr in metric_defs
    )

    # ── Analysis section lists ─────────────────────────────────────────────────
    def ul_items(items, cls):
        if not items:
            return f'<li class="{cls} empty">No data extracted.</li>'
        return "".join(f'<li class="{cls}">{item}</li>' for item in items)

    changes_html = ul_items(parsed["changes"], "change-item")
    bull_html    = ul_items(parsed["bull"],    "bull-item")
    bear_html    = ul_items(parsed["bear"],    "bear-item")

    # ── Verdict ───────────────────────────────────────────────────────────────
    SIG = {
        "buy":  {"label": "BUY",  "color": "#22c55e", "bg": "#052e16", "border": "#166534"},
        "hold": {"label": "HOLD", "color": "#f59e0b", "bg": "#1c1000", "border": "#92400e"},
        "sell": {"label": "SELL", "color": "#ef4444", "bg": "#1c0303", "border": "#7f1d1d"},
    }
    sc = SIG[signal]
    conviction_label = conviction.title() + " Conviction"

    verdict_html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>',
                          parsed.get("verdict_full", "N/A")).replace('\n', '<br>')

    # ── Key quote: first bold phrase from verdict ─────────────────────────────
    qm = re.search(r'\*\*(.+?)\*\*', parsed.get("verdict_full", ""))
    key_quote = qm.group(1) if qm else "See full verdict below."

    # ── Watch next quarter: lead sentence of first bear point ─────────────────
    watch_text = "N/A — see bear case above."
    if parsed["bear"]:
        plain = re.sub(r'<[^>]+>', '', parsed["bear"][0])
        watch_text = re.split(r'(?<=[.!?])\s', plain)[0][:280]

    # ── Guidance: keyword search in full analysis text ────────────────────────
    gm = re.search(
        r'(?:guidance|next quarter|expects?|full[- ]year|FY\s?\d{4})[^.\n]+[.\n]',
        analysis, re.IGNORECASE
    )
    guidance_html = (
        f'<em>"{re.sub(r"[*#`]", "", gm.group(0)).strip()}"</em>'
        if gm else
        '<span class="muted">No explicit guidance extracted from this filing analysis.</span>'
    )

    # ── Print-only metrics table ──────────────────────────────────────────────
    def print_row(label, cval, pval, fmt_fn):
        if cval is not None and pval is not None and pval != 0:
            p   = (cval - pval) / abs(pval) * 100
            chg = f"{'+' if p > 0 else ''}{p:.1f}%"
        else:
            chg = "N/A"
        return (f'<tr><td>{label}</td>'
                f'<td>{fmt_fn(cval)}</td>'
                f'<td>{fmt_fn(pval)}</td>'
                f'<td>{chg}</td></tr>')

    print_rows = "".join([
        print_row("Revenue",        cv("revenue"),          pv("revenue"),          _fmt_money),
        print_row("Gross Margin",   cv("gross_margin"),     pv("gross_margin"),     _fmt_pct),
        print_row("Op. Margin",     cv("operating_margin"), pv("operating_margin"), _fmt_pct),
        print_row("EPS Diluted",    cv("eps_diluted"),      pv("eps_diluted"),      _fmt_eps),
        print_row("Free Cash Flow", cv("fcf"),              pv("fcf"),              _fmt_money),
        print_row("Cash + Mkt Sec", cv("cash"),             pv("cash"),             _fmt_money),
        print_row("Total Debt",     cv("total_debt"),       pv("total_debt"),       _fmt_money),
    ])

    # ── Chart.js data payload ─────────────────────────────────────────────────
    def sf(key, src, scale=1.0):
        val = src.get(key)
        return round(val / scale, 3) if val else 0

    chart_json = json.dumps({
        "signal": signal,
        "revenue": {
            "prior_label": prior_period,
            "curr_label":  curr_period,
            "prior":       sf("revenue", prior, 1e9),
            "curr":        sf("revenue", curr,  1e9),
        },
        "margins": {
            "gross_prior": round((pv("gross_margin")     or 0) * 100, 1),
            "gross_curr":  round((cv("gross_margin")     or 0) * 100, 1),
            "op_prior":    round((pv("operating_margin") or 0) * 100, 1),
            "op_curr":     round((cv("operating_margin") or 0) * 100, 1),
        },
    })

    # ── CSS — regular string so literal { } braces need no escaping ──────────
    CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0d14;--surface:#0f1520;--card:#111827;
  --border:#1e2d42;--text:#e2e8f0;--muted:#64748b;--dim:#2d3748;
  --blue:#3b82f6;--num:#7dd3fc;
  --green:#22c55e;--amber:#f59e0b;--red:#ef4444;
  --green-bg:#052e16;--amber-bg:#1c1000;--red-bg:#1c0303;
  --green-bdr:#166534;--amber-bdr:#92400e;--red-bdr:#7f1d1d;
}
body{background:var(--bg);color:var(--text);
     font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}
.container{max-width:1200px;margin:0 auto;padding:36px 24px 72px}
.playfair{font-family:'Playfair Display',serif}
.mono{font-family:'IBM Plex Mono',monospace}
.muted{color:var(--muted)}

/* Header */
.header{display:flex;align-items:flex-start;justify-content:space-between;
        margin-bottom:32px;padding-bottom:24px;border-bottom:1px solid var(--border);
        gap:16px;flex-wrap:wrap}
.company-name{font-size:2.2rem;font-weight:700;letter-spacing:-.5px;
              color:var(--text);margin-bottom:6px}
.header-meta{color:var(--muted);font-size:12px;display:flex;
             gap:8px;align-items:center;flex-wrap:wrap}
.header-meta .ticker{color:var(--num);font-family:'IBM Plex Mono',monospace;
                     font-weight:500;font-size:13px}
.header-meta .sep{color:var(--dim)}
.signal-pill{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:500;
             padding:7px 18px;border-radius:4px;border:1px solid;
             letter-spacing:3px;white-space:nowrap;margin-top:2px}
.signal-pill.buy {color:var(--green);background:var(--green-bg);border-color:var(--green-bdr)}
.signal-pill.hold{color:var(--amber);background:var(--amber-bg);border-color:var(--amber-bdr)}
.signal-pill.sell{color:var(--red);  background:var(--red-bg);  border-color:var(--red-bdr)}

/* Metrics grid */
.metrics-grid{display:grid;grid-template-columns:repeat(7,1fr);
              gap:10px;margin-bottom:20px}
@media(max-width:960px){.metrics-grid{grid-template-columns:repeat(4,1fr)}}
@media(max-width:560px){.metrics-grid{grid-template-columns:repeat(2,1fr)}}
.metric-card{background:var(--card);border:1px solid var(--border);
             border-radius:6px;padding:14px 12px}
.metric-label{font-size:9px;font-weight:600;letter-spacing:1.8px;
              text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.metric-value{font-size:1.1rem;font-weight:500;color:var(--num);
              margin-bottom:4px;white-space:nowrap;overflow:hidden;
              text-overflow:ellipsis}
.metric-arrow{font-size:12px}
.arrow.up,.chg.up{color:var(--green)}
.arrow.dn,.chg.dn{color:var(--red)}
.arrow.neutral,.chg.neutral{color:var(--muted)}

/* Charts row */
.charts-row{display:grid;grid-template-columns:1fr 1fr 260px;
            gap:14px;margin-bottom:20px}
@media(max-width:900px){.charts-row{grid-template-columns:1fr 1fr}}
@media(max-width:900px){.signal-card{grid-column:1/-1}}
.chart-card{background:var(--card);border:1px solid var(--border);
            border-radius:6px;padding:20px}
.chart-title{font-size:9px;font-weight:600;letter-spacing:1.8px;
             text-transform:uppercase;color:var(--muted);margin-bottom:14px}
.signal-card{display:flex;flex-direction:column;align-items:center;
             justify-content:center;padding:20px 16px 16px}
.gauge-wrap{width:180px;height:95px;position:relative}
.gauge-label{font-family:'IBM Plex Mono',monospace;font-size:1.8rem;
             font-weight:600;letter-spacing:4px;margin-top:6px}
.gauge-label.buy {color:var(--green)}
.gauge-label.hold{color:var(--amber)}
.gauge-label.sell{color:var(--red)}
.gauge-sub{font-size:10px;color:var(--muted);letter-spacing:1px;
           text-transform:uppercase;margin-top:4px}

/* Sections */
.section{background:var(--card);border:1px solid var(--border);
         border-radius:6px;padding:22px 24px;margin-bottom:14px}
.section-title{font-size:9px;font-weight:600;letter-spacing:2px;
               text-transform:uppercase;color:var(--muted);
               margin-bottom:14px;padding-bottom:10px;
               border-bottom:1px solid var(--border)}
.section-title.green{color:var(--green)}
.section-title.red  {color:var(--red)}

/* Lists */
.changes-list,.bull-list,.bear-list{list-style:none;display:flex;
                                     flex-direction:column;gap:9px}
.change-item{padding:10px 14px;background:var(--surface);
             border-left:3px solid var(--blue);border-radius:0 4px 4px 0;
             font-size:13px;line-height:1.5}
.bull-item{padding:10px 14px;background:var(--surface);
           border-left:3px solid var(--green);border-radius:0 4px 4px 0;
           font-size:13px;line-height:1.5}
.bear-item{padding:10px 14px;background:var(--surface);
           border-left:3px solid var(--red);border-radius:0 4px 4px 0;
           font-size:13px;line-height:1.5}
.empty{color:var(--muted);font-style:italic}

/* Two-column */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
@media(max-width:680px){.two-col{grid-template-columns:1fr}}

/* Guidance */
.guidance-box{background:var(--card);border:1px solid var(--border);
              border-radius:6px;padding:18px 22px;margin-bottom:14px;
              font-size:13px;line-height:1.6}

/* Quote */
.quote-box{background:var(--surface);border-left:4px solid var(--blue);
           border-radius:0 6px 6px 0;padding:18px 22px;margin-bottom:14px}
.quote-box blockquote{font-style:italic;font-size:14px;color:var(--text);
                       margin-bottom:8px;line-height:1.7}
.quote-source{font-size:11px;color:var(--muted);
              font-family:'IBM Plex Mono',monospace}

/* Watch box */
.watch-box{background:var(--amber-bg);border:1px solid var(--amber-bdr);
           border-radius:6px;padding:14px 20px;margin-bottom:14px}
.watch-label{font-family:'IBM Plex Mono',monospace;font-size:9px;font-weight:600;
             letter-spacing:2px;color:var(--amber);margin-bottom:7px}
.watch-box p{font-size:13px;color:var(--text);line-height:1.5}

/* Verdict */
.verdict-box{border-radius:6px;padding:22px 24px;margin-bottom:24px;border:1px solid}
.verdict-label{font-size:9px;font-weight:600;letter-spacing:2px;
               text-transform:uppercase;margin-bottom:10px}
.verdict-box p{font-size:14px;line-height:1.75}

/* Footer */
.footer{font-size:11px;color:var(--dim);text-align:center;
        padding-top:20px;border-top:1px solid var(--border);
        font-family:'IBM Plex Mono',monospace;letter-spacing:.5px}

/* Print-only (hidden on screen) */
.print-only{display:none}

/* Print styles */
@media print{
  body{background:#fff!important;color:#000!important;font-size:11pt}
  .container{max-width:100%;padding:0}
  .company-name{color:#000!important;font-size:18pt}
  .header{border-bottom:1pt solid #000}
  .header-meta{color:#333!important}
  .signal-pill{border:1pt solid #000!important;color:#000!important;background:transparent!important}
  .metric-card{background:#f5f5f5!important;border:.5pt solid #ccc!important}
  .metric-value{color:#000!important}
  .metric-label{color:#555!important}
  .charts-row{display:none!important}
  .section,.guidance-box,.watch-box,.verdict-box,.two-col>div,.quote-box{
    background:transparent!important;border:.5pt solid #ccc!important}
  .section-title{color:#000!important;border-bottom:.5pt solid #ccc!important}
  .section-title.green{color:#166534!important}
  .section-title.red  {color:#991b1b!important}
  .change-item,.bull-item,.bear-item{background:#f9f9f9!important;color:#000!important}
  .change-item{border-left:3pt solid #2563eb!important}
  .bull-item  {border-left:3pt solid #166534!important}
  .bear-item  {border-left:3pt solid #991b1b!important}
  .quote-box{border-left:3pt solid #2563eb!important;background:#f0f4ff!important}
  .quote-box blockquote{color:#000!important}
  .watch-box{background:#fffbeb!important;border-color:#d97706!important}
  .watch-label{color:#92400e!important}
  .verdict-box{border-color:#999!important}
  .footer{color:#666!important;border-top:.5pt solid #ccc!important}
  .arrow.up,.chg.up{color:#166534!important}
  .arrow.dn,.chg.dn{color:#991b1b!important}
  .print-only{display:block!important;margin:16pt 0;page-break-inside:avoid}
  .print-only table{width:100%;border-collapse:collapse;font-size:10pt}
  .print-only th,.print-only td{border:.5pt solid #ccc;padding:4pt 7pt;text-align:left}
  .print-only th{background:#f0f0f0;font-weight:bold}
  .print-only caption{font-size:11pt;font-weight:bold;margin-bottom:6pt;text-align:left}
}
"""

    # ── JS — regular string; uses CHART_DATA constant injected by the f-string ─
    JS = """
document.addEventListener('DOMContentLoaded', function () {
  var D = CHART_DATA;

  var TICK = {
    color: '#64748b',
    font: { family: "'IBM Plex Mono', monospace", size: 11 }
  };
  var GRID    = { color: '#1e2d42' };
  var TOOLTIP = {
    backgroundColor: '#141d2b', titleColor: '#7dd3fc',
    bodyColor: '#e2e8f0', borderColor: '#1e2d42', borderWidth: 1
  };

  /* Revenue bar chart */
  var revEl = document.getElementById('revenueChart');
  if (revEl) {
    new Chart(revEl, {
      type: 'bar',
      data: {
        labels: [D.revenue.prior_label, D.revenue.curr_label],
        datasets: [{
          label: 'Revenue ($B)',
          data: [D.revenue.prior, D.revenue.curr],
          backgroundColor: ['rgba(59,130,246,0.30)', 'rgba(59,130,246,0.80)'],
          borderColor:     ['rgba(59,130,246,0.60)', 'rgba(59,130,246,1.00)'],
          borderWidth: 1, borderRadius: 4
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: true,
        plugins: {
          legend: { display: false },
          tooltip: Object.assign({}, TOOLTIP, {
            callbacks: { label: function(c){ return ' $' + c.parsed.y.toFixed(2) + 'B'; } }
          })
        },
        scales: {
          x: { grid: GRID, ticks: TICK },
          y: { grid: GRID, ticks: Object.assign({}, TICK, {
            callback: function(v){ return '$' + v.toFixed(1) + 'B'; }
          })}
        }
      }
    });
  }

  /* Margin grouped bar chart */
  var mrgEl = document.getElementById('marginsChart');
  if (mrgEl) {
    new Chart(mrgEl, {
      type: 'bar',
      data: {
        labels: ['Gross Margin', 'Op. Margin'],
        datasets: [
          {
            label: 'Prior Q',
            data: [D.margins.gross_prior, D.margins.op_prior],
            backgroundColor: 'rgba(99,102,241,0.35)',
            borderColor:     'rgba(99,102,241,0.80)',
            borderWidth: 1, borderRadius: 4
          },
          {
            label: 'Current Q',
            data: [D.margins.gross_curr, D.margins.op_curr],
            backgroundColor: 'rgba(34,197,94,0.50)',
            borderColor:     'rgba(34,197,94,0.90)',
            borderWidth: 1, borderRadius: 4
          }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: true,
        plugins: {
          legend: {
            labels: {
              color: '#64748b',
              font: { family: "'IBM Plex Mono', monospace", size: 11 },
              boxWidth: 10
            }
          },
          tooltip: Object.assign({}, TOOLTIP, {
            callbacks: {
              label: function(c){
                return ' ' + c.dataset.label + ': ' + c.parsed.y.toFixed(1) + '%';
              }
            }
          })
        },
        scales: {
          x: { grid: GRID, ticks: TICK },
          y: { grid: GRID, ticks: Object.assign({}, TICK, {
            callback: function(v){ return v + '%'; }
          })}
        }
      }
    });
  }

  /* Signal gauge — half-donut, active segment highlighted */
  var gaugeEl = document.getElementById('gaugeChart');
  if (gaugeEl) {
    var DIM    = '#1a2535';
    var COLORS = { buy: '#22c55e', hold: '#f59e0b', sell: '#ef4444' };
    var sig    = D.signal;
    new Chart(gaugeEl, {
      type: 'doughnut',
      data: {
        datasets: [{
          data: [1, 1, 1],
          backgroundColor: [
            sig === 'sell' ? COLORS.sell : DIM,
            sig === 'hold' ? COLORS.hold : DIM,
            sig === 'buy'  ? COLORS.buy  : DIM
          ],
          borderWidth: 0
        }]
      },
      options: {
        rotation: -90, circumference: 180, cutout: '66%',
        plugins: {
          legend:  { display: false },
          tooltip: { enabled: false }
        },
        animation: { duration: 700, easing: 'easeInOutQuart' }
      }
    });
  }
});
"""

    # ── Assemble HTML (f-string — only Python vars here, no raw CSS/JS braces) ─
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{ticker} &middot; {curr_period} &middot; AI Watchlist</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>{CSS}</style>
</head>
<body>
<div class="container">

  <!-- ① Header -->
  <div class="header">
    <div>
      <div class="company-name playfair">{company_name}</div>
      <div class="header-meta">
        <span class="ticker">{ticker}</span>
        <span class="sep">&middot;</span>
        <span>Period&nbsp;{curr_period}</span>
        <span class="sep">&middot;</span>
        <span>Filed&nbsp;{curr_filed}</span>
        <span class="sep">&middot;</span>
        <span>Prior&nbsp;{prior_period}</span>
      </div>
    </div>
    <div class="signal-pill {signal}">{sc['label']}</div>
  </div>

  <!-- ② Key Metrics Grid -->
  <div class="metrics-grid">
{metrics_html}
  </div>

  <!-- ③ Charts Row -->
  <div class="charts-row">
    <div class="chart-card">
      <div class="chart-title">Revenue ($B) &mdash; QoQ</div>
      <canvas id="revenueChart" height="160"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-title">Margins (%) &mdash; QoQ</div>
      <canvas id="marginsChart" height="160"></canvas>
    </div>
    <div class="chart-card signal-card">
      <div class="chart-title">Signal</div>
      <div class="gauge-wrap">
        <canvas id="gaugeChart"></canvas>
      </div>
      <div class="gauge-label {signal}">{sc['label']}</div>
      <div class="gauge-sub">{conviction_label}</div>
    </div>
  </div>

  <!-- ④ What Changed -->
  <div class="section">
    <div class="section-title">What Changed</div>
    <ul class="changes-list">{changes_html}</ul>
  </div>

  <!-- ⑤ Bull / Bear -->
  <div class="two-col">
    <div class="section">
      <div class="section-title green">Bull Case</div>
      <ul class="bull-list">{bull_html}</ul>
    </div>
    <div class="section">
      <div class="section-title red">Bear Case / Risks</div>
      <ul class="bear-list">{bear_html}</ul>
    </div>
  </div>

  <!-- ⑥ Guidance -->
  <div class="guidance-box">
    <div class="section-title">Guidance &amp; Forward Commentary</div>
    <p>{guidance_html}</p>
  </div>

  <!-- ⑦ Key Quote -->
  <div class="quote-box">
    <blockquote>&#8220;{key_quote}&#8221;</blockquote>
    <div class="quote-source">&mdash; {model} analysis &middot; {generated}</div>
  </div>

  <!-- ⑧ Watch Next Quarter -->
  <div class="watch-box">
    <div class="watch-label">&#x26A0; WATCH NEXT QUARTER</div>
    <p>{watch_text}</p>
  </div>

  <!-- ⑨ Verdict -->
  <div class="verdict-box" style="background:{sc['bg']};border-color:{sc['border']}">
    <div class="verdict-label" style="color:{sc['color']}">Verdict</div>
    <p>{verdict_html}</p>
  </div>

  <!-- ⑩ Footer -->
  <div class="footer">
    Generated by AI Watchlist &nbsp;&middot;&nbsp; Source: SEC EDGAR
    &nbsp;&middot;&nbsp; {generated}
  </div>

  <!-- Print-only metrics summary (hidden on screen, visible when printed) -->
  <div class="print-only">
    <table>
      <caption>Key Metrics &mdash; {ticker} &nbsp; {curr_period} vs {prior_period}</caption>
      <thead>
        <tr><th>Metric</th><th>Current Q</th><th>Prior Q</th><th>QoQ</th></tr>
      </thead>
      <tbody>{print_rows}</tbody>
    </table>
  </div>

</div>
<script>
var CHART_DATA = {chart_json};
{JS}
</script>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"  HTML report saved -> {output_path}")

# ── Orchestration ─────────────────────────────────────────────────────────────

def _earnings_6k_filings(company, limit: int = 20) -> list:
    """
    Return 6-K filings that contain actual quarterly financial statements.
    Foreign private issuers (ARM, ASML) file quarterly earnings as 6-K.
    We identify earnings filings by scanning for ones with revenue data,
    keeping only one per calendar quarter to avoid duplicates.
    """
    import io, sys

    all_6k = company.get_filings(form="6-K")
    earnings = []
    seen_periods = set()

    for f in all_6k:
        if len(earnings) >= limit:
            break
        period = str(f.period_of_report or "")
        if not period or period in seen_periods:
            continue
        obj = f.obj()
        fin = getattr(obj, "financials", None)
        if fin is None:
            continue
        # Suppress the verbose "Failed to resolve" warnings from edgartools
        old_stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            metrics = fin.get_financial_metrics()
        except Exception:
            metrics = {}
        finally:
            sys.stderr = old_stderr
        if metrics.get("revenue"):
            seen_periods.add(period)
            earnings.append(f)

    return earnings


def analyze_ticker(ticker: str, edgar, client: anthropic.Anthropic):
    print(f"\n[{ticker}] Fetching 10-Q filings from SEC EDGAR...")
    form_used = "10-Q"
    try:
        company = edgar.Company(ticker)
        filings  = company.get_filings(form="10-Q")
    except edgar.CompanyNotFoundError:
        print(f"  x Company '{ticker}' not found on EDGAR.")
        return
    except Exception as e:
        print(f"  x Could not fetch filings: {e}")
        return

    # Foreign private issuers (e.g. ARM, ASML) file 6-K instead of 10-Q
    if len(filings) < 2:
        print(f"  No 10-Q filings found. Trying 6-K (foreign private issuer)...")
        filings  = _earnings_6k_filings(company)
        form_used = "6-K"

    if len(filings) < 2:
        print(f"  x Need at least 2 earnings filings; found {len(filings)}.")
        return

    print(f"  Found {len(filings)} {form_used} earnings filings. Extracting current + prior quarter...")
    curr  = extract_metrics(filings[0])
    prior = extract_metrics(filings[1])

    if curr is None or prior is None:
        print(f"  x Financial extraction failed for {ticker}.")
        return

    print(f"  Current: {curr['period_of_report']}  |  Prior: {prior['period_of_report']}")
    print(f"  Calling Claude ({MODEL})...")

    analysis = _call_claude(ticker, curr, prior, client)

    print(_terminal_report(ticker, curr, prior, analysis))

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.today().strftime("%Y%m%d")
    out_path = REPORTS_DIR / f"{ticker}_{date_str}.md"
    out_path.write_text(_markdown_report(ticker, curr, prior, analysis), encoding="utf-8")
    print(f"  Report saved -> {out_path}")

    report_data = {
        "ticker":    ticker,
        "curr":      curr,
        "prior":     prior,
        "analysis":  analysis,
        "generated": date_str,
        "model":     MODEL,
        "form_used": form_used,
    }
    generate_html_report(report_data, ticker, out_path.with_suffix(".html"))


def main():
    parser = argparse.ArgumentParser(
        description="Watchlist 10-Q analyzer (EdgarTools + Claude)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python watchlist.py --ticker NVDA\n"
            "  python watchlist.py --all\n\n"
            "Set EDGAR_IDENTITY in .env to skip the identity prompt:\n"
            "  EDGAR_IDENTITY=Your Name your@email.com"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", metavar="TICKER", help="Single ticker symbol")
    group.add_argument("--all",    action="store_true", help="Run all 10 watchlist tickers")
    args = parser.parse_args()

    edgar  = setup_edgar()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("Error: ANTHROPIC_API_KEY not found in environment or .env")

    # Use system certificate store so the Anthropic API call works on
    # corporate networks that perform SSL inspection (same fix as edgartools).
    import ssl, httpx, truststore
    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client = anthropic.Anthropic(
        api_key=api_key,
        http_client=httpx.Client(verify=ssl_ctx),
    )

    tickers = WATCHLIST if args.all else [args.ticker.upper()]
    for ticker in tickers:
        analyze_ticker(ticker, edgar, client)


if __name__ == "__main__":
    main()
