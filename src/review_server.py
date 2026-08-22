"""
Local review website for the reconciliation pipeline. Four real views, all
server-rendered from the live SQLite database and the actual data/ CSVs --
nothing on any page is a placeholder or a fixed sample.

    python review_server.py
    -> opens http://localhost:8000/ automatically

GET  /             -- Overview: resolution breakdown, category counts, the
                       last run's throughput/settlement source (parsed from
                       output/reconciliation_report.md if present).
GET  /queue        -- Review queue: every OPEN exception needing a human
                       decision, with a collapsible replay_log per row.
GET  /records      -- Every persisted row from the last run, not just open
                       exceptions -- instantly filterable client-side by
                       status and free-text search, sortable by column.
GET  /sources      -- Data source diagnostics: real row counts from
                       data/*.csv, whether the gateway-agnostic demo has
                       been run, and whether live Razorpay credentials are
                       configured (presence-checked only, values never read).
POST /resolve/<id> -- action=confirm|reject (terminal decision). Confirming
                       a FUZZY_MATCH_NEEDS_REVIEW row also writes a
                       narration_rules entry (see db.resolve_exception).
POST /note/<id>    -- attaches a clarification note without resolving the
                       row; it stays open and the note stays visible.

Stdlib-only (http.server + sqlite3): no framework, no build step.

Every design token below -- color, type size/line-height/letter-spacing,
spacing, radius, shadow -- is copied verbatim from Razorpay's open-source
Blade design system (github.com/razorpay/blade), checked directly against
packages/blade/src/tokens/global/{colors,typography,spacing,border}.ts and
tokens/theme/bladeTheme.ts, not approximated. Heading typeface falls back
to Blade's own documented fallback stack (Arial) since Blade's actual
heading face, TASA Orbiter, is a licensed commercial font not available to
embed here. This is a private, local dev tool, not an official Razorpay
product.
"""
import csv
import json
import os
import re
import threading
import webbrowser
from collections import Counter, defaultdict
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
from config import load_dotenv

load_dotenv()

PORT = 8000
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

PAGE_STYLE = """
  :root {
    /* Blade neutral scale (blueGrayLight) -- surfaces, borders, text */
    --bg: hsl(220, 25%, 97%);
    --panel: hsl(0, 0%, 100%);
    --border: hsl(204, 8%, 88%);
    --border-subtle: hsl(220, 20%, 94%);
    --ink: hsl(200, 10%, 18%);
    --muted: hsl(204, 9%, 42%);
    --faint: hsl(203, 8%, 62%);

    /* Blade azure -- Razorpay's primary blue. This is the dominant accent
       on every page here, not a decoration confined to the sidebar. */
    --primary: hsl(218, 89%, 51%);
    --primary-strong: hsl(218, 87%, 43%);
    --primary-subtle: hsl(218, 100%, 92%);
    --primary-faint: hsl(217, 100%, 98%);
    --deep: hsl(218, 90%, 20%);
    --deep-raised: hsl(218, 80%, 26%);

    /* Blade feedback semantics: emerald/crimson/cider/sapphire */
    --positive: hsl(150, 100%, 28%);
    --positive-bg: hsl(150, 39%, 93%);
    --negative: hsl(4, 85%, 44%);
    --negative-bg: hsl(5, 75%, 97%);
    --notice: hsl(25, 100%, 44%);
    --notice-bg: hsl(23, 100%, 97%);
    --information: hsl(200, 100%, 41%);
    --information-bg: hsl(198, 85%, 95%);

    --font: 'Inter', -apple-system, 'Segoe UI', Arial, sans-serif;
    --font-heading: Arial, -apple-system, 'Segoe UI', sans-serif;
    --mono: 'Menlo', 'Cascadia Mono', Consolas, 'Roboto Mono', monospace;
    --shadow-low: 0px 2px 4px 0px hsla(200, 10%, 18%, 0.06);
    --shadow-mid: 0px 16px 12px 0px hsla(200, 10%, 18%, 0.06);
    --shadow-high: 0px 8px 24px -4px hsla(200, 10%, 18%, 0.10);
    --radius-xs: 4px;
    --radius-s: 8px;
    --radius-m: 12px;
    --radius-l: 16px;
    --radius-xl: 20px;
    --radius-pill: 9999px;

    /* Blade's real spacing scale (packages/blade/.../global/spacing.ts) */
    --sp-1: 2px; --sp-2: 4px; --sp-3: 8px; --sp-4: 12px; --sp-5: 16px;
    --sp-6: 20px; --sp-7: 24px; --sp-8: 32px; --sp-9: 40px; --sp-10: 48px; --sp-11: 56px;

    /* Blade's real type scale, size/line-height paired exactly
       (packages/blade/.../global/typography.ts, onDesktop) */
    --text-2xs: 12px;  --lh-2xs: 17px;
    --text-xs:  14px;  --lh-xs:  20px;
    --text-sm:  16px;  --lh-sm:  24px;
    --text-md:  18px;  --lh-md:  24px;
    --text-lg:  20px;  --lh-lg:  26px;
    --text-xl:  24px;  --lh-xl:  32px;
    --text-2xl: 32px;  --lh-2xl: 38px;
    --text-3xl: 40px;  --lh-3xl: 46px;
    --ls-tight: -0.013em; /* Blade letterSpacings.50, -1.3% */
  }
  * { box-sizing: border-box; }
  html { scroll-behavior:smooth; }
  body {
    margin:0; background:var(--bg); color:var(--ink); font-family:var(--font);
    font-size:var(--text-xs); line-height:var(--lh-xs); -webkit-font-smoothing:antialiased;
    display:flex; min-height:100vh;
  }
  a { color:inherit; }
  ::selection { background:var(--primary-subtle); color:var(--deep); }

  /* ---------------------------------------------------------- Sidebar --- */
  aside.rail {
    width:252px; flex-shrink:0; background:var(--deep); color:hsl(0,0%,100%);
    padding:var(--sp-8) var(--sp-6); display:flex; flex-direction:column; gap:var(--sp-9);
    position:sticky; top:0; height:100vh;
  }
  aside.rail .brand { display:flex; align-items:center; gap:var(--sp-4); }
  aside.rail .brand .mark {
    width:38px; height:38px; border-radius:var(--radius-m); background:var(--primary);
    display:flex; align-items:center; justify-content:center; font-weight:700; font-size:15px; flex-shrink:0;
    box-shadow:0 0 0 4px hsla(218,89%,51%,0.18);
  }
  aside.rail .brand .name { font-weight:700; font-size:16px; letter-spacing:var(--ls-tight); line-height:1.3; font-family:var(--font-heading); }
  aside.rail nav { display:flex; flex-direction:column; gap:3px; }
  aside.rail nav a {
    display:flex; align-items:center; gap:var(--sp-4); padding:11px var(--sp-5);
    border-radius:var(--radius-s); color:hsla(0,0%,100%,0.65); text-decoration:none; font-size:14px; font-weight:500;
    transition:background 0.14s, color 0.14s;
  }
  aside.rail nav a svg { width:17px; height:17px; flex-shrink:0; opacity:0.85; }
  aside.rail nav a:hover { background:hsla(0,0%,100%,0.08); color:#fff; }
  aside.rail nav a.active { background:var(--primary); color:#fff; box-shadow:var(--shadow-mid); }
  aside.rail nav a.active svg { opacity:1; }
  aside.rail .env-pill {
    margin-top:auto; display:inline-flex; align-items:center; font-size:12px; font-weight:600; letter-spacing:0.03em; text-transform:uppercase;
    background:hsla(0,0%,100%,0.10); padding:8px 14px; border-radius:var(--radius-pill); color:hsla(0,0%,100%,0.85); width:fit-content;
  }

  /* ------------------------------------------------------------ Main ---- */
  main { flex:1; min-width:0; padding:var(--sp-10) var(--sp-10) 72px; max-width:1360px; }
  h1 {
    font-family:var(--font-heading); font-size:var(--text-2xl); line-height:var(--lh-2xl);
    font-weight:700; margin:0 0 var(--sp-2); letter-spacing:var(--ls-tight);
  }
  h2 { font-family:var(--font-heading); font-size:var(--text-md); line-height:var(--lh-md); font-weight:600; margin:0 0 var(--sp-5); letter-spacing:-0.005em; }
  p.kicker { color:var(--primary-strong); font-weight:600; font-size:13px; text-transform:uppercase; letter-spacing:0.05em; margin:0 0 var(--sp-8); }

  /* --------------------------------------------------------- Overview --- */
  .overview { display:flex; gap:var(--sp-7); margin-bottom:var(--sp-8); flex-wrap:wrap; align-items:stretch; }
  .donut-card {
    background:linear-gradient(160deg, var(--primary-faint) 0%, var(--panel) 55%);
    border:1px solid var(--border); border-radius:var(--radius-l);
    box-shadow:var(--shadow-low); padding:var(--sp-8); display:flex; align-items:center; gap:var(--sp-8);
    min-width:320px; transition:box-shadow 0.18s, transform 0.18s;
  }
  .donut-card:hover { box-shadow:var(--shadow-high); transform:translateY(-2px); }
  .donut { width:132px; height:132px; border-radius:50%; flex-shrink:0; position:relative; }
  .donut .donut-label {
    position:absolute; inset:20px; background:var(--panel); border-radius:50%;
    display:flex; flex-direction:column; align-items:center; justify-content:center; box-shadow:inset 0 0 0 1px var(--border-subtle);
  }
  .donut .donut-label b { font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:30px; line-height:1; color:var(--primary-strong); font-weight:700; }
  .donut .donut-label span { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; margin-top:5px; }
  .legend { display:flex; flex-direction:column; gap:10px; font-size:13px; }
  .legend .row { display:flex; align-items:center; gap:9px; }
  .legend .swatch { width:10px; height:10px; border-radius:3px; flex-shrink:0; }
  .legend .pct { font-family:var(--mono); font-variant-numeric:tabular-nums; color:var(--muted); margin-left:auto; padding-left:20px; font-weight:600; }

  .stats { display:flex; gap:var(--sp-6); flex-wrap:wrap; flex:1; }
  .stat {
    background:var(--panel); border:1px solid var(--border); border-radius:var(--radius-l);
    box-shadow:var(--shadow-low); padding:var(--sp-7); min-width:180px; flex:1;
    display:flex; flex-direction:column; gap:var(--sp-5); transition:box-shadow 0.18s, transform 0.18s;
  }
  .stat:hover { box-shadow:var(--shadow-high); transform:translateY(-2px); }
  .stat b { display:block; font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:var(--text-2xl); line-height:var(--lh-2xl); color:var(--ink); font-weight:700; }
  .stat .stat-label { font-size:13px; color:var(--muted); font-weight:600; }
  .stat.tint-primary .icon-badge { background:var(--primary-subtle); color:var(--primary-strong); }
  .stat.tint-notice .icon-badge { background:var(--notice-bg); color:var(--notice); }
  .stat.tint-positive .icon-badge { background:var(--positive-bg); color:var(--positive); }
  .icon-badge {
    width:40px; height:40px; border-radius:var(--radius-m); display:flex; align-items:center; justify-content:center;
    background:var(--primary-subtle); color:var(--primary-strong); flex-shrink:0;
  }
  .icon-badge svg { width:20px; height:20px; }

  /* Compact 3-bucket "how it resolved" bar -- deterministic vs AI vs unresolved */
  .stack-bar { display:flex; height:14px; border-radius:var(--radius-pill); overflow:hidden; width:100%; box-shadow:inset 0 0 0 1px var(--border); }
  .stack-bar .seg { height:100%; transition:width 0.5s ease-out; cursor:default; }
  .stack-legend { display:flex; gap:var(--sp-8); margin-top:var(--sp-6); flex-wrap:wrap; }
  .stack-legend .item { display:flex; align-items:center; gap:10px; }
  .stack-legend .swatch { width:11px; height:11px; border-radius:3px; flex-shrink:0; }
  .stack-legend b { font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:22px; font-weight:700; color:var(--ink); }
  .stack-legend .item-label { font-size:12.5px; color:var(--muted); display:block; }

  /* ------------------------------------------------------------ Panels -- */
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:var(--radius-l); box-shadow:var(--shadow-low); overflow:hidden; margin-bottom:var(--sp-7); }
  .panel .panel-head { padding:var(--sp-6) var(--sp-7); border-bottom:1px solid var(--border-subtle); display:flex; align-items:center; justify-content:space-between; gap:var(--sp-5); flex-wrap:wrap; }
  .panel-body { padding:var(--sp-7); }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:var(--sp-6) var(--sp-6); border-bottom:1px solid var(--border-subtle); font-size:var(--text-xs); vertical-align:top; }
  tbody tr:last-child td { border-bottom:none; }
  tbody tr { transition:background 0.12s; }
  tbody tr:hover { background:var(--primary-faint); }
  th {
    background:var(--bg); font-size:12px; text-transform:uppercase;
    letter-spacing:0.06em; color:var(--muted); border-bottom:1px solid var(--border); font-weight:700;
  }
  th.sortable { cursor:pointer; user-select:none; }
  th.sortable:hover { color:var(--primary-strong); }
  th.sortable .arrow { display:inline-block; margin-left:4px; opacity:0.35; font-size:10px; }
  th.sortable.sorted .arrow { opacity:1; color:var(--primary); }
  td.id-cell { font-family:var(--mono); font-variant-numeric:tabular-nums; color:var(--ink); font-size:13px; }
  td.amount-cell { font-family:var(--mono); font-variant-numeric:tabular-nums; text-align:right; font-size:13px; font-weight:600; }

  .pill {
    display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:700;
    padding:4px 12px; border-radius:var(--radius-pill); letter-spacing:0.01em; white-space:nowrap;
  }
  .pill.notice { background:var(--notice-bg); color:var(--notice); }
  .pill.information { background:var(--information-bg); color:var(--information); }
  .pill.negative { background:var(--negative-bg); color:var(--negative); }
  .pill.positive { background:var(--positive-bg); color:var(--positive); }
  .cat { font-family:var(--mono); font-size:11px; padding:3px 9px; border-radius:var(--radius-s); background:var(--bg); color:var(--muted); border:1px solid var(--border); }

  .narration { font-family:var(--mono); font-size:12.5px; color:var(--muted); margin-top:6px; }
  .note-text { margin-top:9px; font-size:13px; color:var(--notice); background:var(--notice-bg); border-radius:var(--radius-s); padding:7px 11px; display:inline-block; }
  details { margin-top:7px; font-size:12.5px; }
  details summary { cursor:pointer; color:var(--primary); font-weight:600; list-style:none; }
  details summary::-webkit-details-marker { display:none; }
  details summary:before { content:"▸ "; font-size:10px; }
  details[open] summary:before { content:"▾ "; }
  details summary:hover { color:var(--primary-strong); }
  details > div { font-family:var(--mono); color:var(--muted); padding:4px 0 4px 16px; border-left:2px solid var(--primary-subtle); margin-top:5px; }

  /* ------------------------------------------------------- Forms/buttons */
  form { margin:0 0 6px; display:inline-block; }
  form.filter-form { margin:0; display:flex; gap:var(--sp-4); flex-wrap:wrap; align-items:center; }
  button {
    font-family:var(--font); font-size:var(--text-xs); font-weight:600; padding:10px 16px;
    border-radius:var(--radius-s); border:1px solid transparent; cursor:pointer; transition:filter 0.12s, box-shadow 0.12s, transform 0.08s;
    display:inline-flex; align-items:center; gap:7px;
  }
  button svg { width:14px; height:14px; flex-shrink:0; }
  button:hover { filter:brightness(0.96); }
  button:active { transform:scale(0.98); }
  button:focus-visible { outline:2px solid var(--primary); outline-offset:2px; }
  button.approve { background:var(--positive); color:#fff; }
  button.approve:hover { box-shadow:0 0 0 4px var(--positive-bg); }
  button.reject { background:var(--panel); color:var(--negative); border-color:var(--negative-bg); }
  button.reject:hover { background:var(--negative-bg); }
  button.pending { background:var(--panel); color:var(--notice); border:1px solid var(--border); }
  button.pending:hover { background:var(--notice-bg); }
  button.ghost { background:var(--panel); color:var(--muted); border:1px solid var(--border); }
  button.ghost:hover { background:var(--bg); }
  button.primary { background:var(--primary); color:#fff; }
  button.primary:hover { box-shadow:0 0 0 4px var(--primary-subtle); }
  input[type=text], select {
    font-family:var(--font); font-size:var(--text-xs); padding:10px 14px; border:1px solid var(--border);
    border-radius:var(--radius-s); background:var(--panel); color:var(--ink); transition:border-color 0.12s, box-shadow 0.12s;
  }
  input[type=text] { width:280px; }
  input[type=text]:focus-visible, select:focus-visible, input[type=text]:focus, select:focus {
    outline:none; border-color:var(--primary); box-shadow:0 0 0 3px var(--primary-subtle);
  }

  .empty-state { padding:64px; text-align:center; color:var(--muted); font-size:var(--text-sm); }

  .source-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:var(--sp-6); }
  .source-card {
    background:var(--panel); border:1px solid var(--border); border-radius:var(--radius-l); box-shadow:var(--shadow-low);
    padding:var(--sp-7); transition:box-shadow 0.18s, transform 0.18s; display:flex; flex-direction:column; gap:var(--sp-5);
  }
  .source-card:hover { box-shadow:var(--shadow-high); transform:translateY(-2px); }
  .source-card .value { font-size:var(--text-2xl); font-weight:700; font-family:var(--mono); font-variant-numeric:tabular-nums; color:var(--ink); }
  .source-card .label { font-size:13px; color:var(--muted); font-weight:600; }
  .status-chip {
    display:flex; align-items:center; gap:var(--sp-4); background:var(--panel); border:1px solid var(--border);
    border-radius:var(--radius-l); box-shadow:var(--shadow-low); padding:var(--sp-6) var(--sp-7);
  }
  .status-chip .text b { display:block; font-size:14px; font-weight:700; }
  .status-chip .text span { font-size:12.5px; color:var(--muted); }
  .dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:8px; flex-shrink:0; }
  .dot.on { background:var(--positive); box-shadow:0 0 0 4px var(--positive-bg); } .dot.off { background:var(--faint); }

  @media (prefers-reduced-motion: reduce) {
    * { transition:none !important; }
  }
"""

INTERACTIVITY_SCRIPT = """
<script>
(function () {
  var searchInput = document.getElementById("live-search");
  var statusSelect = document.getElementById("live-status");
  var rows = Array.prototype.slice.call(document.querySelectorAll("tbody tr[data-search]"));
  var countEl = document.getElementById("filter-count");

  function applyFilter() {
    var q = (searchInput ? searchInput.value : "").toLowerCase();
    var status = statusSelect ? statusSelect.value : "";
    var visible = 0;
    rows.forEach(function (row) {
      var matchesText = !q || row.getAttribute("data-search").indexOf(q) !== -1;
      var matchesStatus = !status || row.getAttribute("data-status") === status;
      var show = matchesText && matchesStatus;
      row.style.display = show ? "" : "none";
      if (show) visible++;
    });
    if (countEl) countEl.textContent = visible + " of " + rows.length + " rows";
  }

  if (searchInput) searchInput.addEventListener("input", applyFilter);
  if (statusSelect) statusSelect.addEventListener("change", applyFilter);

  var sortState = { key: null, dir: 1 };
  document.querySelectorAll("th.sortable").forEach(function (th, colIndex) {
    th.addEventListener("click", function () {
      var table = th.closest("table");
      var tbody = table.querySelector("tbody");
      var key = th.getAttribute("data-sort-key");
      sortState.dir = sortState.key === key ? -sortState.dir : 1;
      sortState.key = key;
      document.querySelectorAll("th.sortable").forEach(function (h) { h.classList.remove("sorted"); });
      th.classList.add("sorted");
      th.querySelector(".arrow").textContent = sortState.dir === 1 ? "\\u2191" : "\\u2193";

      var bodyRows = Array.prototype.slice.call(tbody.querySelectorAll("tr[data-search]"));
      bodyRows.sort(function (a, b) {
        var av = a.children[colIndex].getAttribute("data-value") || a.children[colIndex].textContent.trim();
        var bv = b.children[colIndex].getAttribute("data-value") || b.children[colIndex].textContent.trim();
        var an = parseFloat(av), bn = parseFloat(bv);
        var cmp = (!isNaN(an) && !isNaN(bn)) ? (an - bn) : av.localeCompare(bv);
        return cmp * sortState.dir;
      });
      bodyRows.forEach(function (row) { tbody.appendChild(row); });
    });
  });

  applyFilter();
})();
</script>
"""

NAV_ICONS = {
    "overview": '<svg viewBox="0 0 16 16" fill="none"><rect x="2" y="2" width="5" height="5" rx="1" stroke="currentColor" stroke-width="1.4"/><rect x="9" y="2" width="5" height="8" rx="1" stroke="currentColor" stroke-width="1.4"/><rect x="2" y="9" width="5" height="5" rx="1" stroke="currentColor" stroke-width="1.4"/><rect x="9" y="12" width="5" height="2" rx="1" stroke="currentColor" stroke-width="1.4"/></svg>',
    "queue": '<svg viewBox="0 0 16 16" fill="none"><path d="M3 5h10M3 8h10M3 11h6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
    "records": '<svg viewBox="0 0 16 16" fill="none"><rect x="2" y="2.5" width="12" height="11" rx="1.5" stroke="currentColor" stroke-width="1.4"/><path d="M2 6h12" stroke="currentColor" stroke-width="1.4"/></svg>',
    "sources": '<svg viewBox="0 0 16 16" fill="none"><ellipse cx="8" cy="4" rx="5.5" ry="2" stroke="currentColor" stroke-width="1.4"/><path d="M2.5 4v8c0 1.1 2.46 2 5.5 2s5.5-.9 5.5-2V4" stroke="currentColor" stroke-width="1.4"/><path d="M2.5 8c0 1.1 2.46 2 5.5 2s5.5-.9 5.5-2" stroke="currentColor" stroke-width="1.4"/></svg>',
}
ICON_CHECK = '<svg viewBox="0 0 16 16" fill="none"><path d="M3 8.5L6.5 12L13 4.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
ICON_CROSS = '<svg viewBox="0 0 16 16" fill="none"><path d="M4 4L12 12M12 4L4 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
ICON_NOTE = '<svg viewBox="0 0 16 16" fill="none"><path d="M11.5 2.5L13.5 4.5L5 13L2.5 13.5L3 11L11.5 2.5Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>'

STATUS_LABELS = {
    "MATCHED": ("Clean match", "positive"),
    "MATCHED_WITH_VARIANCE": ("Explained variance", "positive"),
    "MATCHED_EXACT_REFERENCE": ("Exact reference", "information"),
    "MATCHED_LEARNED_PATTERN": ("Learned pattern", "information"),
    "MATCHED_AI_ASSISTED": ("AI-assisted", "information"),
    "MATCHED_LOW_CONFIDENCE": ("Needs review", "notice"),
    "EXCEPTION": ("Exception", "negative"),
}
SWATCH_HEX = {
    "positive": "hsl(150, 100%, 28%)", "information": "hsl(200, 100%, 41%)",
    "notice": "hsl(25, 100%, 44%)", "negative": "hsl(4, 85%, 44%)",
}
STATUS_ORDER = ["MATCHED", "MATCHED_WITH_VARIANCE", "MATCHED_EXACT_REFERENCE",
                "MATCHED_LEARNED_PATTERN", "MATCHED_AI_ASSISTED", "MATCHED_LOW_CONFIDENCE", "EXCEPTION"]

# Collapses the 7 granular statuses into the one comparison that actually
# matters for the pitch -- how much of the batch a deterministic pass
# closed versus how much needed the LLM arbiter versus what's genuinely
# unresolved. The donut above already shows the full 7-way breakdown, so
# this stays a 3-bucket summary instead of repeating it.
PASS_BUCKETS = [
    ("Deterministic", ["MATCHED", "MATCHED_WITH_VARIANCE", "MATCHED_EXACT_REFERENCE", "MATCHED_LEARNED_PATTERN"], "positive"),
    ("AI-assisted", ["MATCHED_AI_ASSISTED", "MATCHED_LOW_CONFIDENCE"], "information"),
    ("Unresolved", ["EXCEPTION"], "negative"),
]

ICON_ROWS = '<svg viewBox="0 0 20 20" fill="none"><rect x="3" y="3" width="14" height="14" rx="2.5" stroke="currentColor" stroke-width="1.5"/><path d="M3 8h14M8 8v9" stroke="currentColor" stroke-width="1.5"/></svg>'
ICON_ALERT = '<svg viewBox="0 0 20 20" fill="none"><path d="M10 3l8 14H2L10 3z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M10 8.5v3.2M10 14.3h.01" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>'
ICON_BANK = '<svg viewBox="0 0 20 20" fill="none"><path d="M10 2.5L18 7H2L10 2.5z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M3.5 7v8M7 7v8M13 7v8M16.5 7v8" stroke="currentColor" stroke-width="1.5"/><path d="M2 17.5h16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'
ICON_LEDGER = '<svg viewBox="0 0 20 20" fill="none"><rect x="3.5" y="2.5" width="13" height="15" rx="1.5" stroke="currentColor" stroke-width="1.5"/><path d="M7 7h6M7 10.5h6M7 14h3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>'
ICON_GATEWAY = '<svg viewBox="0 0 20 20" fill="none"><circle cx="7" cy="7" r="4" stroke="currentColor" stroke-width="1.5"/><circle cx="13" cy="13" r="4" stroke="currentColor" stroke-width="1.5"/></svg>'
ICON_KEY = '<svg viewBox="0 0 20 20" fill="none"><circle cx="6.5" cy="13.5" r="3.5" stroke="currentColor" stroke-width="1.5"/><path d="M9 11l7-7M13 4l2 2M16 5v3h-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
ICON_DB = '<svg viewBox="0 0 20 20" fill="none"><ellipse cx="10" cy="5" rx="6.5" ry="2.5" stroke="currentColor" stroke-width="1.5"/><path d="M3.5 5v10c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5V5" stroke="currentColor" stroke-width="1.5"/><path d="M3.5 10c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5" stroke="currentColor" stroke-width="1.5"/></svg>'


def render_status_pill(status):
    label, tone = STATUS_LABELS.get(status, (status, "information"))
    return f'<span class="pill {tone}">{escape(label)}</span>'


def render_shell(active, title, kicker="", body_html="", extra_script=""):
    nav_items = [
        ("overview", "/", "Overview"),
        ("queue", "/queue", "Review queue"),
        ("records", "/records", "All records"),
        ("sources", "/sources", "Data sources"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="{"active" if key == active else ""}">{NAV_ICONS[key]}{label}</a>'
        for key, href, label in nav_items
    )

    live_configured = bool(os.environ.get("RAZORPAY_KEY_ID", "").strip())
    env_label = "Live" if live_configured else "Synthetic"

    kicker_html = f'<p class="kicker">{kicker}</p>' if kicker else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} -- Settlement Reconciliation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{PAGE_STYLE}</style>
</head>
<body>
  <aside class="rail">
    <div class="brand">
      <div class="mark">SR</div>
      <div class="name">Settlement<br>Reconciliation</div>
    </div>
    <nav>{nav_html}</nav>
    <div class="env-pill"><span class="dot {'on' if live_configured else 'off'}"></span>{escape(env_label)}</div>
  </aside>
  <main>
    <h1>{escape(title)}</h1>
    {kicker_html}
    {body_html}
  </main>
  {extra_script}
</body>
</html>"""


# ---------------------------------------------------------------- Overview

def parse_last_report():
    """Reads output/reconciliation_report.md for the settlement source and
    throughput of the most recent report.py run, if one exists. Returns
    None if no report has been generated yet -- never fabricates a value."""
    path = OUTPUT_DIR / "reconciliation_report.md"
    if not path.exists():
        return None
    text = path.read_text()
    info = {}
    for line in text.splitlines():
        if line.startswith("**Settlement data source:**"):
            info["source"] = line.split(":**", 1)[1].split("(")[0].strip()
        elif line.startswith("**Throughput:**"):
            match = re.search(r"\(([\d.]+) rows/sec\)", line)
            if match:
                info["rows_per_sec"] = match.group(1)
    return info or None


def render_donut(all_rows):
    total = len(all_rows)
    if total == 0:
        return "<div class='panel'><div class='empty-state'>No batch yet -- run <code>run_all.py</code></div></div>"

    counts = Counter(r["status"] for r in all_rows)
    stops, cursor, legend_rows = [], 0.0, []
    for status in STATUS_ORDER:
        c = counts.get(status, 0)
        if not c:
            continue
        pct = 100 * c / total
        label, tone = STATUS_LABELS[status]
        color = SWATCH_HEX[tone]
        stops.append(f"{color} {cursor:.2f}% {cursor + pct:.2f}%")
        cursor += pct
        legend_rows.append(
            f'<div class="row" title="{escape(label)}"><span class="swatch" style="background:{color}"></span>'
            f'{escape(label)}<span class="pct">{pct:.1f}%</span></div>'
        )

    gradient = ", ".join(stops)
    resolved_pct = round(100 * sum(c for s, c in counts.items() if s != "EXCEPTION") / total, 1)

    return f"""
    <div class="donut-card">
      <div class="donut" style="background:conic-gradient({gradient})">
        <div class="donut-label"><b>{resolved_pct}%</b><span>resolved</span></div>
      </div>
      <div class="legend">{"".join(legend_rows)}</div>
    </div>"""


def render_pass_bar(all_rows):
    """Collapses the 7-way status breakdown into 3 buckets -- deterministic,
    AI-assisted, unresolved -- the comparison the pitch actually rests on.
    The donut above already covers the granular view, so this earns its
    place by showing something the donut doesn't: how rarely the arbiter
    is needed at all."""
    total = len(all_rows)
    if total == 0:
        return ""
    counts = Counter(r["status"] for r in all_rows)

    segs, items = [], []
    for label, statuses, tone in PASS_BUCKETS:
        c = sum(counts.get(s, 0) for s in statuses)
        pct = 100 * c / total if total else 0
        color = SWATCH_HEX[tone]
        if c:
            segs.append(f'<span class="seg" style="width:{pct:.2f}%;background:{color}" title="{escape(label)}: {pct:.1f}%"></span>')
        items.append(f"""
          <div class="item">
            <span class="swatch" style="background:{color}"></span>
            <span><b>{pct:.0f}%</b><span class="item-label">{escape(label)}</span></span>
          </div>""")

    return f"""
    <div class="panel"><div class="panel-body">
      <h2>How it resolved</h2>
      <div class="stack-bar">{"".join(segs)}</div>
      <div class="stack-legend">{"".join(items)}</div>
    </div></div>"""


def render_overview():
    all_rows = db.get_all_exceptions()
    open_rows = db.get_open_exceptions()
    donut_html = render_donut(all_rows)

    by_category = defaultdict(int)
    for r in all_rows:
        if r["category"]:
            by_category[r["category"]] += 1
    category_rows = "".join(
        f'<tr><td class="id-cell">{escape(cat)}</td><td class="amount-cell">{count}</td></tr>'
        for cat, count in sorted(by_category.items(), key=lambda kv: -kv[1])
    ) or '<tr><td colspan="2" class="empty-state">No categorized exceptions.</td></tr>'

    report_info = parse_last_report()
    throughput_stat = ""
    if report_info and report_info.get("rows_per_sec"):
        throughput_stat = f"""
        <div class="stat tint-notice">
          <div class="icon-badge">{ICON_ALERT}</div>
          <b>{report_info["rows_per_sec"]}/sec</b>
          <span class="stat-label">last run throughput</span>
        </div>"""

    body = f"""
    <div class="overview">
      {donut_html}
      <div class="stats">
        <div class="stat tint-primary">
          <div class="icon-badge">{ICON_ROWS}</div>
          <b>{len(all_rows)}</b>
          <span class="stat-label">rows in last batch</span>
        </div>
        <div class="stat tint-notice">
          <div class="icon-badge">{ICON_ALERT}</div>
          <b>{len(open_rows)}</b>
          <span class="stat-label">need a decision</span>
        </div>
        {throughput_stat}
      </div>
    </div>
    {render_pass_bar(all_rows)}
    <div class="panel">
      <div class="panel-head"><h2 style="margin:0">Exceptions by category</h2></div>
      <table><thead><tr><th>Category</th><th>Count</th></tr></thead><tbody>{category_rows}</tbody></table>
    </div>"""
    return render_shell("overview", "Overview", "", body)


# ------------------------------------------------------------- Review queue

def render_row(r, show_actions=True):
    replay_log = json.loads(r["replay_log"] or "[]")
    replay_html = "".join(f"<div>{escape(s)}</div>" for s in replay_log) or "<div>(no stages recorded)</div>"
    note_html = (f'<div class="note-text">Note: "{escape(r["resolution_note"])}"</div>'
                 if r["resolution_note"] else "")
    narration_html = (f'<div class="narration">narration: "{escape(r["narration"])}"</div>'
                       if r["narration"] else "")
    net = "" if r["net_amount"] is None else f'{r["net_amount"]:,.2f}'
    search_blob = escape(" ".join(str(v) for v in [
        r["order_id"], r["settlement_id"], r["category"], r["narration"], r["reason"],
    ] if v).lower())

    actions_html = ""
    if show_actions:
        actions_html = f"""
        <form method="POST" action="/resolve/{r['id']}">
          <input type="hidden" name="action" value="confirm">
          <button class="approve" type="submit">{ICON_CHECK} Confirm match</button>
        </form>
        <form method="POST" action="/resolve/{r['id']}">
          <input type="hidden" name="action" value="reject">
          <button class="reject" type="submit">{ICON_CROSS} Reject</button>
        </form>
        <form method="POST" action="/note/{r['id']}">
          <input type="text" name="note" placeholder="clarification note" required>
          <button class="pending" type="submit">{ICON_NOTE} Save note</button>
        </form>"""
    else:
        actions_html = f'<span class="cat">{escape(r["resolution_status"])}</span>'

    return f"""
    <tr data-search="{search_blob}" data-status="{escape(r["status"])}">
      <td class="id-cell" data-value="{escape(r["order_id"] or "")}">{escape(r["order_id"] or "(none)")}</td>
      <td class="id-cell" data-value="{escape(r["settlement_id"] or "")}">{escape(r["settlement_id"] or "(none)")}</td>
      <td class="amount-cell" data-value="{r["net_amount"] if r["net_amount"] is not None else ""}">{net}</td>
      <td data-value="{escape(r["status"])}">{render_status_pill(r["status"])}</td>
      <td data-value="{escape(r["category"] or "")}"><span class="cat">{escape(r["category"] or "")}</span></td>
      <td>{escape(r["reason"] or "")}
          {narration_html}
          <details><summary>Replay log</summary>{replay_html}</details>
          {note_html}
      </td>
      <td>{actions_html}</td>
    </tr>"""


def render_queue():
    open_rows = db.get_open_exceptions()
    all_rows = db.get_all_exceptions()
    resolved_count = sum(1 for r in all_rows if r["resolution_status"] != "OPEN")
    body_rows = "".join(render_row(r) for r in open_rows) or (
        '<tr><td colspan="7" class="empty-state">Queue is clear -- nothing needs a decision.</td></tr>'
    )
    body = f"""
    <div class="overview">
      <div class="stats">
        <div class="stat tint-notice">
          <div class="icon-badge">{ICON_ALERT}</div>
          <b>{len(open_rows)}</b>
          <span class="stat-label">need a decision</span>
        </div>
        <div class="stat tint-positive">
          <div class="icon-badge">{ICON_ROWS}</div>
          <b>{resolved_count}</b>
          <span class="stat-label">resolved this session</span>
        </div>
      </div>
    </div>
    <div class="panel">
      <table>
        <thead><tr><th>Order</th><th>Settlement</th><th>Net (Rs.)</th><th>Status</th><th>Category</th><th>Reason / audit trail</th><th>Action</th></tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>"""
    return render_shell("queue", "Review queue", "", body)


# -------------------------------------------------------------- All records

def render_records():
    """Renders every persisted row once; filtering and sorting happen
    instantly client-side via INTERACTIVITY_SCRIPT, not a server round trip."""
    all_rows = db.get_all_exceptions()
    status_options = "".join(
        f'<option value="{s}">{STATUS_LABELS.get(s, (s, ""))[0]}</option>' for s in STATUS_ORDER
    )
    body_rows = "".join(render_row(r, show_actions=False) for r in all_rows) or (
        '<tr><td colspan="7" class="empty-state">No records yet -- run <code>python run_all.py</code> first.</td></tr>'
    )
    body = f"""
    <div class="panel">
      <div class="panel-body">
        <form class="filter-form" onsubmit="return false">
          <input type="text" id="live-search" placeholder="Search order, settlement, narration...">
          <select id="live-status">
            <option value="">All statuses</option>
            {status_options}
          </select>
          <span id="filter-count" style="color:var(--muted);font-size:13px;margin-left:auto">{len(all_rows)} of {len(all_rows)} rows</span>
        </form>
      </div>
    </div>
    <div class="panel">
      <table>
        <thead><tr>
          <th class="sortable" data-sort-key="order"><span>Order</span><span class="arrow">&#8597;</span></th>
          <th class="sortable" data-sort-key="settlement"><span>Settlement</span><span class="arrow">&#8597;</span></th>
          <th class="sortable" data-sort-key="net"><span>Net (Rs.)</span><span class="arrow">&#8597;</span></th>
          <th class="sortable" data-sort-key="status"><span>Status</span><span class="arrow">&#8597;</span></th>
          <th class="sortable" data-sort-key="category"><span>Category</span><span class="arrow">&#8597;</span></th>
          <th>Reason / audit trail</th>
          <th>Resolution</th>
        </tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>"""
    return render_shell("records", "All records", "", body, extra_script=INTERACTIVITY_SCRIPT)


# ------------------------------------------------------------- Data sources

def count_csv_rows(path):
    if not path.exists():
        return None
    with open(path, newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def render_sources():
    settlement_count = count_csv_rows(DATA_DIR / "settlement_report.csv")
    bank_count = count_csv_rows(DATA_DIR / "bank_statement.csv")
    ledger_count = count_csv_rows(DATA_DIR / "internal_ledger.csv")
    gateway_b_count = count_csv_rows(DATA_DIR / "gateway_b_export.csv")
    db_exists = (DATA_DIR / "reconcile.db").exists()

    live_configured = bool(os.environ.get("RAZORPAY_KEY_ID", "").strip())

    def card(icon, label, value):
        display = "--" if value is None else str(value)
        return f"""
        <div class="source-card">
          <div class="icon-badge">{icon}</div>
          <div class="value">{display}</div>
          <div class="label">{label}</div>
        </div>"""

    def chip(is_on, title, on_text, off_text):
        return f"""
        <div class="status-chip">
          <span class="dot {'on' if is_on else 'off'}"></span>
          <div class="text"><b>{title}</b><span>{on_text if is_on else off_text}</span></div>
        </div>"""

    body = f"""
    <div class="source-grid">
      {card(ICON_ROWS, "Settlements", settlement_count)}
      {card(ICON_BANK, "Bank rows", bank_count)}
      {card(ICON_LEDGER, "Ledger rows", ledger_count)}
      {card(ICON_GATEWAY, "Gateway B rows", gateway_b_count)}
    </div>
    <div class="source-grid" style="margin-top:var(--sp-6)">
      {chip(live_configured, "Live connection", "Configured", "Synthetic only")}
      {chip(db_exists, "Persistence", "Ready", "Not created yet")}
    </div>"""
    return render_shell("sources", "Data sources", "", body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._respond_html(render_overview())
        elif path == "/queue":
            self._respond_html(render_queue())
        elif path == "/records":
            self._respond_html(render_records())
        elif path == "/sources":
            self._respond_html(render_sources())
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        parts = parsed.path.strip("/").split("/")

        try:
            if len(parts) == 2 and parts[0] == "resolve":
                action = fields.get("action", [""])[0]
                db.resolve_exception(int(parts[1]), action)
            elif len(parts) == 2 and parts[0] == "note":
                note = fields.get("note", [""])[0]
                db.add_note(int(parts[1]), note)
            else:
                self.send_error(404)
                return
        except (ValueError, KeyError) as e:
            self.send_error(400, str(e))
            return

        self.send_response(303)
        self.send_header("Location", "/queue")
        self.end_headers()

    def _respond_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"Serving the review site at {url}  (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
