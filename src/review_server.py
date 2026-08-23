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
    /* Neutral scale, warmed slightly toward the brand blue's hue rather
       than a flat gray -- softer, closer to razorpay.com's own
       off-white surfaces (rgb(240,244,246), verified in its real HTML). */
    --bg: hsl(210, 45%, 97.5%);
    --panel: hsl(0, 0%, 100%);
    --border: hsl(210, 20%, 89%);
    --border-subtle: hsl(210, 35%, 95%);
    --ink: hsl(222, 25%, 14%);
    --muted: hsl(215, 12%, 44%);
    --faint: hsl(215, 10%, 63%);

    /* rgb(0, 153, 255) -- the single most-used color on razorpay.com's
       real homepage HTML (866 occurrences vs. white's 253), extracted
       directly from the fetched page, not approximated from a screenshot.
       This is the dominant accent on every page here, not a decoration
       confined to a sidebar. */
    --primary: hsl(204, 100%, 50%);
    --primary-strong: hsl(204, 100%, 38%);
    --primary-subtle: hsl(204, 100%, 92%);
    --primary-faint: hsl(204, 100%, 97%);
    --primary-glow: hsla(204, 100%, 50%, 0.22);
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
    /* 'Inter Tight' is a real, distinct heading face used on razorpay.com
       itself (21 occurrences in the fetched page), not a substitute
       guessed at -- freely embeddable via Google Fonts, unlike Blade's
       actual (licensed) heading face, TASA Orbiter. */
    --font-heading: 'Inter Tight', 'Inter', -apple-system, 'Segoe UI', sans-serif;
    --mono: 'Menlo', 'Cascadia Mono', Consolas, 'Roboto Mono', monospace;
    --shadow-low: 0px 2px 6px 0px hsla(220, 25%, 14%, 0.06);
    --shadow-mid: 0px 12px 24px -6px hsla(220, 25%, 14%, 0.10);
    --shadow-high: 0px 20px 40px -8px hsla(220, 25%, 14%, 0.14);
    --radius-xs: 4px;
    --radius-s: 10px;
    --radius-m: 14px;
    --radius-l: 20px;
    --radius-xl: 28px;
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
    --text-4xl: 48px;  --lh-4xl: 52px;
    --ls-tight: -0.013em; /* Blade letterSpacings.50, -1.3% */
    --ls-tighter: -0.025em;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior:smooth; }
  body {
    margin:0; background:var(--bg); color:var(--ink); font-family:var(--font);
    font-size:var(--text-sm); line-height:var(--lh-sm); -webkit-font-smoothing:antialiased;
    display:flex; min-height:100vh;
  }
  a { color:inherit; }
  ::selection { background:var(--primary-subtle); color:var(--primary-strong); }

  /* ---------------------------------------------------------- Sidebar --- */
  /* Light, not the heavy dark-navy admin-panel block this had before --
     razorpay.com's own real nav is white with the brand blue used only
     as an accent (active state, mark), not as a filled background. */
  aside.rail {
    width:264px; flex-shrink:0; background:var(--panel); color:var(--ink);
    padding:var(--sp-8) var(--sp-6); display:flex; flex-direction:column; gap:var(--sp-9);
    position:sticky; top:0; height:100vh; border-right:1px solid var(--border-subtle);
  }
  aside.rail .brand { display:flex; align-items:center; gap:var(--sp-4); }
  aside.rail .brand .mark {
    width:40px; height:40px; border-radius:var(--radius-m); background:var(--primary);
    display:flex; align-items:center; justify-content:center; font-weight:800; font-size:15px; flex-shrink:0;
    color:#fff; box-shadow:0 6px 16px -4px var(--primary-glow);
  }
  aside.rail .brand .name { font-weight:800; font-size:17px; letter-spacing:var(--ls-tight); line-height:1.25; font-family:var(--font-heading); color:var(--ink); }
  aside.rail nav { display:flex; flex-direction:column; gap:4px; }
  aside.rail nav a {
    display:flex; align-items:center; gap:var(--sp-4); padding:12px var(--sp-5);
    border-radius:var(--radius-pill); color:var(--muted); text-decoration:none; font-size:15px; font-weight:600;
    transition:background 0.14s, color 0.14s;
  }
  aside.rail nav a svg { width:18px; height:18px; flex-shrink:0; opacity:0.75; }
  aside.rail nav a:hover { background:var(--primary-faint); color:var(--primary-strong); }
  aside.rail nav a.active { background:var(--primary); color:#fff; box-shadow:0 8px 20px -6px var(--primary-glow); }
  aside.rail nav a.active svg { opacity:1; }

  /* ------------------------------------------------------------ Main ---- */
  main { flex:1; min-width:0; padding:var(--sp-10) var(--sp-11) 80px; max-width:2000px; }
  .page-head {
    background:radial-gradient(120% 180% at 0% 0%, var(--primary-faint) 0%, transparent 60%);
    margin:calc(var(--sp-10) * -1) calc(var(--sp-11) * -1) var(--sp-9);
    padding:var(--sp-10) var(--sp-11) var(--sp-8);
  }
  h1 {
    font-family:var(--font-heading); font-size:var(--text-4xl); line-height:var(--lh-4xl);
    font-weight:800; margin:0 0 var(--sp-3); letter-spacing:var(--ls-tighter);
  }
  h2 { font-family:var(--font-heading); font-size:var(--text-lg); line-height:var(--lh-lg); font-weight:700; margin:0 0 var(--sp-5); letter-spacing:-0.005em; }
  p.kicker { color:var(--primary-strong); font-weight:700; font-size:13px; text-transform:uppercase; letter-spacing:0.08em; margin:0 0 var(--sp-8); }

  /* --------------------------------------------------------- Overview --- */
  .overview { display:flex; gap:var(--sp-7); margin-bottom:var(--sp-9); flex-wrap:wrap; align-items:stretch; }
  .donut-card {
    background:var(--panel);
    border:1px solid var(--border-subtle); border-radius:var(--radius-xl);
    box-shadow:var(--shadow-mid); padding:var(--sp-9); display:flex; align-items:center; gap:var(--sp-8);
    min-width:340px; transition:box-shadow 0.22s, transform 0.22s;
  }
  .donut-card:hover { box-shadow:var(--shadow-high); transform:translateY(-3px); }
  .donut { width:144px; height:144px; border-radius:50%; flex-shrink:0; position:relative; }
  .donut .donut-label {
    position:absolute; inset:22px; background:var(--panel); border-radius:50%;
    display:flex; flex-direction:column; align-items:center; justify-content:center; box-shadow:inset 0 0 0 1px var(--border-subtle);
  }
  .donut .donut-label b { font-family:var(--font-heading); font-variant-numeric:tabular-nums; font-size:24px; line-height:1; color:var(--primary); font-weight:800; letter-spacing:var(--ls-tighter); }
  .donut .donut-label span { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em; margin-top:6px; font-weight:600; }
  .legend { display:flex; flex-direction:column; gap:11px; font-size:14px; }
  .legend .row { display:flex; align-items:center; gap:10px; }
  .legend .swatch { width:11px; height:11px; border-radius:4px; flex-shrink:0; }
  .legend .pct { font-family:var(--font-heading); font-variant-numeric:tabular-nums; color:var(--ink); margin-left:auto; padding-left:22px; font-weight:700; }

  .stats { display:flex; gap:var(--sp-6); flex-wrap:wrap; flex:1; }
  a.stat, a.category-card { text-decoration:none; color:inherit; cursor:pointer; }
  .stat {
    background:var(--panel); border:1px solid var(--border-subtle); border-radius:var(--radius-l);
    box-shadow:var(--shadow-low); padding:var(--sp-8); min-width:190px; flex:1;
    display:flex; flex-direction:column; gap:var(--sp-5); transition:box-shadow 0.22s, transform 0.22s, border-color 0.22s;
  }
  .stat:hover { box-shadow:var(--shadow-high); transform:translateY(-4px); border-color:var(--primary-subtle); }
  .stat b { display:block; font-family:var(--font-heading); font-variant-numeric:tabular-nums; font-size:44px; line-height:1.05; color:var(--ink); font-weight:800; letter-spacing:var(--ls-tighter); }
  .stat .stat-label { font-size:14px; color:var(--muted); font-weight:600; }
  .stat.tint-primary .icon-badge { background:var(--primary-subtle); color:var(--primary-strong); }
  .stat.tint-notice .icon-badge { background:var(--notice-bg); color:var(--notice); }
  .stat.tint-positive .icon-badge { background:var(--positive-bg); color:var(--positive); }
  .icon-badge {
    width:48px; height:48px; border-radius:var(--radius-m); display:flex; align-items:center; justify-content:center;
    background:var(--primary-subtle); color:var(--primary-strong); flex-shrink:0;
  }
  .icon-badge svg { width:24px; height:24px; }

  /* Clickable exception-category cards -- colored by what the category
     actually means (explained variance reads positive, a genuine gap
     reads negative), not decoration. */
  .category-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(240px, 1fr)); gap:var(--sp-6); }
  .category-card {
    display:flex; align-items:center; gap:var(--sp-6); padding:var(--sp-7); border-radius:var(--radius-l);
    border:1.5px solid transparent; transition:transform 0.18s, box-shadow 0.18s, border-color 0.18s;
    box-shadow:var(--shadow-low);
  }
  .category-card:hover { transform:translateY(-4px); box-shadow:var(--shadow-high); }
  .category-card-text { display:flex; flex-direction:column; min-width:0; }
  .category-card-text b { font-family:var(--font-heading); font-variant-numeric:tabular-nums; font-size:32px; font-weight:800; line-height:1.05; letter-spacing:var(--ls-tighter); }
  .category-card-text span { font-size:13.5px; font-weight:600; letter-spacing:0.01em; margin-top:2px; }
  .category-card.tone-positive { background:var(--positive-bg); border-color:hsla(150,100%,28%,0.18); }
  .category-card.tone-positive .icon-badge { background:hsla(150,100%,28%,0.15); color:var(--positive); }
  .category-card.tone-positive .category-card-text b { color:var(--positive); }
  .category-card.tone-notice { background:var(--notice-bg); border-color:hsla(25,100%,44%,0.18); }
  .category-card.tone-notice .icon-badge { background:hsla(25,100%,44%,0.15); color:var(--notice); }
  .category-card.tone-notice .category-card-text b { color:var(--notice); }
  .category-card.tone-negative { background:var(--negative-bg); border-color:hsla(4,85%,44%,0.18); }
  .category-card.tone-negative .icon-badge { background:hsla(4,85%,44%,0.15); color:var(--negative); }
  .category-card.tone-negative .category-card-text b { color:var(--negative); }
  .category-card.tone-information { background:var(--information-bg); border-color:hsla(200,100%,41%,0.18); }
  .category-card.tone-information .icon-badge { background:hsla(200,100%,41%,0.15); color:var(--information); }
  .category-card.tone-information .category-card-text b { color:var(--information); }

  /* Compact 3-bucket "how it resolved" bar -- deterministic vs AI vs unresolved */
  .stack-bar { display:flex; height:14px; border-radius:var(--radius-pill); overflow:hidden; width:100%; box-shadow:inset 0 0 0 1px var(--border); }
  .stack-bar .seg { height:100%; transition:width 0.5s ease-out; cursor:default; }
  .stack-legend { display:flex; gap:var(--sp-8); margin-top:var(--sp-6); flex-wrap:wrap; }
  .stack-legend .item { display:flex; align-items:center; gap:10px; }
  .stack-legend .swatch { width:11px; height:11px; border-radius:3px; flex-shrink:0; }
  .stack-legend b { font-family:var(--font-heading); font-variant-numeric:tabular-nums; font-size:22px; font-weight:800; color:var(--ink); }
  .stack-legend .item-label { font-size:12.5px; color:var(--muted); display:block; }

  /* ------------------------------------------------------------ Panels -- */
  .panel { background:var(--panel); border:1px solid var(--border-subtle); border-radius:var(--radius-l); box-shadow:var(--shadow-low); overflow:hidden; margin-bottom:var(--sp-8); }
  .panel .panel-head { padding:var(--sp-7) var(--sp-8); border-bottom:1px solid var(--border-subtle); display:flex; align-items:center; justify-content:space-between; gap:var(--sp-5); flex-wrap:wrap; }
  .panel-body { padding:var(--sp-8); }

  /* Auto table layout on purpose: short columns (order, status, category)
     size to their own content and never overflow by construction. Only
     the free-text reason column is explicitly capped (below), and the
     wrapper scrolls horizontally as the safety net if the total still
     doesn't fit -- never the page itself. */
  .table-scroll { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:var(--sp-6) var(--sp-6); border-bottom:1px solid var(--border-subtle); font-size:var(--text-sm); vertical-align:top; overflow-wrap:break-word; }
  tbody tr:last-child td { border-bottom:none; }
  tbody tr { transition:background 0.12s; }
  tbody tr:hover { background:var(--primary-faint); }
  th {
    background:var(--bg); font-size:12px; text-transform:uppercase; white-space:nowrap;
    letter-spacing:0.06em; color:var(--muted); border-bottom:1px solid var(--border); font-weight:700;
  }
  th.sortable { cursor:pointer; user-select:none; }
  th.sortable:hover { color:var(--primary-strong); }
  th.sortable .arrow { display:inline-block; margin-left:4px; opacity:0.35; font-size:10px; }
  th.sortable.sorted .arrow { opacity:1; color:var(--primary); }
  td.id-cell { overflow-wrap:anywhere; white-space:nowrap; }
  td.amount-cell { font-family:var(--mono); font-variant-numeric:tabular-nums; text-align:right; font-size:14px; font-weight:600; white-space:nowrap; }

  /* Order/settlement IDs as chips, not bare monospace text -- matches the
     pill/card treatment used everywhere else instead of looking like a
     leftover plain-text column. */
  .id-chip {
    display:inline-flex; align-items:center; font-family:var(--mono); font-variant-numeric:tabular-nums;
    font-size:12.5px; font-weight:700; padding:5px 11px; border-radius:var(--radius-s);
    white-space:nowrap; letter-spacing:0.01em;
  }
  .id-chip-order { background:var(--primary-faint); color:var(--primary-strong); border:1px solid hsla(204,100%,50%,0.18); }
  .id-chip-settlement { background:var(--bg); color:var(--muted); border:1px solid var(--border); }

  /* Reason/audit-trail cell -- the one column actually capped in width,
     since it's the only genuinely long free-text content. Everything
     else sizes naturally. */
  td.reason-cell { min-width:320px; max-width:460px; padding:var(--sp-5) var(--sp-6); }
  .audit-box {
    background:var(--bg); border:1px solid var(--border-subtle); border-radius:var(--radius-m);
    padding:var(--sp-5); font-size:14px; line-height:1.55; overflow-wrap:break-word;
  }
  .audit-box mark {
    background:var(--primary-subtle); color:var(--primary-strong); padding:1px 5px; border-radius:4px;
    font-weight:700; font-variant-numeric:tabular-nums;
  }
  .audit-box code.hl-quote {
    font-family:var(--mono); background:var(--panel); border:1px solid var(--border-subtle);
    padding:1px 6px; border-radius:4px; font-size:12.5px; color:var(--muted);
  }
  .audit-box strong.hl-point { color:var(--primary-strong); font-weight:800; }
  td.action-cell { min-width:190px; }
  td.action-cell form { display:block; margin-bottom:6px; }
  td.action-cell input[type=text] { width:100%; }

  .pill {
    display:inline-flex; align-items:center; gap:5px; font-size:12px; font-weight:700;
    padding:4px 12px; border-radius:var(--radius-pill); letter-spacing:0.01em; white-space:nowrap;
  }
  .pill.notice { background:var(--notice-bg); color:var(--notice); }
  .pill.information { background:var(--information-bg); color:var(--information); }
  .pill.negative { background:var(--negative-bg); color:var(--negative); }
  .pill.positive { background:var(--positive-bg); color:var(--positive); }
  .cat {
    font-family:var(--mono); font-size:12px; padding:4px 10px; border-radius:var(--radius-s);
    background:var(--bg); color:var(--muted); border:1px solid var(--border);
    display:inline-block; white-space:nowrap;
  }
  .cat-empty { color:var(--faint); }

  .narration { font-family:var(--mono); font-size:12.5px; color:var(--muted); margin-top:6px; overflow-wrap:anywhere; }
  .note-text { margin-top:9px; font-size:13px; color:var(--notice); background:var(--notice-bg); border-radius:var(--radius-s); padding:7px 11px; display:inline-block; }
  details { margin-top:7px; font-size:12.5px; }
  details summary { cursor:pointer; color:var(--primary); font-weight:600; list-style:none; }
  details summary::-webkit-details-marker { display:none; }
  details summary:before { content:"▸ "; font-size:10px; }
  details[open] summary:before { content:"▾ "; }
  details summary:hover { color:var(--primary-strong); }
  details > div { font-family:var(--mono); color:var(--muted); padding:4px 0 4px 16px; border-left:2px solid var(--primary-subtle); margin-top:5px; overflow-wrap:anywhere; }

  /* ------------------------------------------------------- Forms/buttons */
  form { margin:0 0 6px; display:inline-block; }
  form.filter-form { margin:0; display:flex; gap:var(--sp-4); flex-wrap:wrap; align-items:center; }
  button {
    font-family:var(--font); font-size:var(--text-xs); font-weight:700; padding:11px 20px;
    border-radius:var(--radius-pill); border:1px solid transparent; cursor:pointer; transition:filter 0.12s, box-shadow 0.12s, transform 0.08s;
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
    background:var(--panel); border:1px solid var(--border-subtle); border-radius:var(--radius-l); box-shadow:var(--shadow-low);
    padding:var(--sp-8); transition:box-shadow 0.22s, transform 0.22s; display:flex; flex-direction:column; gap:var(--sp-5);
  }
  .source-card:hover { box-shadow:var(--shadow-high); transform:translateY(-4px); }
  .source-card .value { font-size:36px; font-weight:800; font-family:var(--font-heading); font-variant-numeric:tabular-nums; color:var(--ink); letter-spacing:var(--ls-tighter); }
  .source-card .label { font-size:13px; color:var(--muted); font-weight:600; }

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

# Semantic tone per exception category, matching what the category actually
# means (explained variance reads positive; a genuine unresolved gap reads
# negative) -- not just a rotating decoration.
CATEGORY_TONES = {
    "PARTIAL_PAYMENT": "positive",
    "ROUNDING": "positive",
    "TAX_DEDUCTION": "positive",
    "UTR_LEVEL_MISMATCH": "positive",
    "ON_HOLD_BY_RAZORPAY": "notice",
    "FUZZY_MATCH_NEEDS_REVIEW": "notice",
    "AFA_MANDATE_HOLD": "information",
    "DUPLICATE": "negative",
    "UNEXPLAINED": "negative",
}

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

CATEGORY_ICONS = {
    "PARTIAL_PAYMENT": ICON_ROWS, "ROUNDING": ICON_ROWS, "TAX_DEDUCTION": ICON_ROWS,
    "UTR_LEVEL_MISMATCH": ICON_BANK,
    "ON_HOLD_BY_RAZORPAY": ICON_ALERT, "FUZZY_MATCH_NEEDS_REVIEW": ICON_ALERT,
    "AFA_MANDATE_HOLD": ICON_KEY, "DUPLICATE": ICON_GATEWAY, "UNEXPLAINED": ICON_ALERT,
}


def render_status_pill(status: str) -> str:
    label, tone = STATUS_LABELS.get(status, (status, "information"))
    return f'<span class="pill {tone}">{escape(label)}</span>'


def render_shell(
    active: str, title: str, kicker: str = "", body_html: str = "", extra_script: str = "",
) -> str:
    """Wraps body_html in the page shell (sidebar nav, styles). `active`
    selects which nav item is highlighted -- one of overview/queue/records/sources."""
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

    kicker_html = f'<p class="kicker">{kicker}</p>' if kicker else ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} -- Settlement Reconciliation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Inter+Tight:wght@600;700;800&display=swap" rel="stylesheet">
<style>{PAGE_STYLE}</style>
</head>
<body>
  <aside class="rail">
    <div class="brand">
      <div class="mark">SR</div>
      <div class="name">Settlement<br>Reconciliation</div>
    </div>
    <nav>{nav_html}</nav>
  </aside>
  <main>
    <div class="page-head">
      <h1>{escape(title)}</h1>
      {kicker_html}
    </div>
    {body_html}
  </main>
  {extra_script}
</body>
</html>"""


# ---------------------------------------------------------------- Overview

def parse_last_report() -> dict | None:
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


def render_donut(all_rows: list[dict]) -> str:
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


def render_pass_bar(all_rows: list[dict]) -> str:
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


def readable_category(cat: str) -> str:
    """Category names are underscore-joined (FUZZY_MATCH_NEEDS_REVIEW) with
    no natural break point, so a narrow card wraps them mid-letter. A
    zero-width space after each underscore gives the browser a place to
    break that isn't inside a word."""
    return escape(cat).replace("_", "_​")


def compute_cash_clarity(all_rows: list[dict]) -> dict:
    """Real Rs. amounts computed from this run's own persisted net_amount/
    category/status columns -- not a forecast, not a parallel calculation.
    Quantifies the 'this build sits upstream of Cashflow Forecaster' claim
    with a number instead of just an argument: every row that hit some
    exception/variance path is cash position a downstream forecaster would
    otherwise see as ambiguous; the portion this engine explained or
    matched is now trustworthy input, and the portion still open is
    disclosed honestly, not folded into the resolved figure."""
    at_risk = resolved = still_open = 0.0
    for r in all_rows:
        amt = r["net_amount"]
        if amt is None or not r["category"]:
            continue
        at_risk += amt
        if r["status"] == "EXCEPTION":
            still_open += amt
        else:
            resolved += amt
    return {
        "at_risk": round(at_risk, 2),
        "resolved": round(resolved, 2),
        "still_open": round(still_open, 2),
        "resolved_pct": round(100 * resolved / at_risk, 1) if at_risk else 0.0,
    }


def render_cash_clarity(all_rows: list[dict]) -> str:
    c = compute_cash_clarity(all_rows)
    if c["at_risk"] == 0:
        return ""
    return f"""
    <div class="panel">
      <div class="panel-head"><h2 style="margin:0">Cash-position clarity</h2></div>
      <div class="panel-body">
        <p style="margin:0 0 var(--sp-6);color:var(--muted);max-width:70ch">
          This isn't a forecast -- it's what this run's own numbers say about the input a
          forecaster like Razorpay's Cashflow Forecaster would actually receive.
        </p>
        <div class="stack-bar">
          <span class="seg" style="width:{c['resolved_pct']:.2f}%;background:{SWATCH_HEX['positive']}" title="Resolved: Rs.{c['resolved']:,.2f}"></span>
          <span class="seg" style="width:{100 - c['resolved_pct']:.2f}%;background:{SWATCH_HEX['negative']}" title="Still open: Rs.{c['still_open']:,.2f}"></span>
        </div>
        <div class="stack-legend">
          <div class="item">
            <span class="swatch" style="background:{SWATCH_HEX['positive']}"></span>
            <span><b>Rs.{c['resolved']:,.2f}</b><span class="item-label">resolved -- now trustworthy cash-position input ({c['resolved_pct']:.1f}%)</span></span>
          </div>
          <div class="item">
            <span class="swatch" style="background:{SWATCH_HEX['negative']}"></span>
            <span><b>Rs.{c['still_open']:,.2f}</b><span class="item-label">still open -- a forecaster would still be blind to this</span></span>
          </div>
        </div>
        <p style="margin:var(--sp-6) 0 0;color:var(--faint);font-size:13px">
          Of Rs.{c['at_risk']:,.2f} in settlement amounts that touched some exception or
          variance path this run, this engine explains where {c['resolved_pct']:.1f}% of it stands
          without guessing on the rest.
        </p>
      </div>
    </div>"""


def render_overview() -> str:
    all_rows = db.get_all_exceptions()
    open_rows = db.get_open_exceptions()
    donut_html = render_donut(all_rows)

    by_category = defaultdict(int)
    for r in all_rows:
        if r["category"]:
            by_category[r["category"]] += 1

    category_cards = "".join(
        f"""<a class="category-card tone-{CATEGORY_TONES.get(cat, 'information')}" href="/records?q={cat}">
              <div class="icon-badge">{CATEGORY_ICONS.get(cat, ICON_ALERT)}</div>
              <div class="category-card-text">
                <b>{count}</b>
                <span>{readable_category(cat)}</span>
              </div>
            </a>"""
        for cat, count in sorted(by_category.items(), key=lambda kv: -kv[1])
    ) or '<div class="empty-state">No categorized exceptions.</div>'

    report_info = parse_last_report()
    throughput_stat = ""
    if report_info and report_info.get("rows_per_sec"):
        throughput_stat = f"""
        <a class="stat tint-notice" href="/sources">
          <div class="icon-badge">{ICON_ALERT}</div>
          <b>{report_info["rows_per_sec"]}/sec</b>
          <span class="stat-label">last run throughput</span>
        </a>"""

    body = f"""
    <div class="overview">
      {donut_html}
      <div class="stats">
        <a class="stat tint-primary" href="/records">
          <div class="icon-badge">{ICON_ROWS}</div>
          <b>{len(all_rows)}</b>
          <span class="stat-label">rows in last batch</span>
        </a>
        <a class="stat tint-notice" href="/queue">
          <div class="icon-badge">{ICON_ALERT}</div>
          <b>{len(open_rows)}</b>
          <span class="stat-label">need a decision</span>
        </a>
        {throughput_stat}
      </div>
    </div>
    {render_pass_bar(all_rows)}
    <div class="panel">
      <div class="panel-head"><h2 style="margin:0">Exceptions by category</h2></div>
      <div class="panel-body"><div class="category-grid">{category_cards}</div></div>
    </div>
    {render_cash_clarity(all_rows)}"""
    return render_shell("overview", "Overview", "", body)


# ------------------------------------------------------------- Review queue

def render_log_entry(entry: dict | str) -> str:
    if isinstance(entry, dict):
        confidence = f" confidence={entry['confidence']:.2f}" if entry.get("confidence") is not None else ""
        return (f"[pass {escape(str(entry.get('pass', '?')))}] {escape(entry.get('action', ''))}{confidence} "
                f"-- {escape(entry.get('detail', ''))}")
    return escape(str(entry))  # legacy plain-string entries from before structured logging


def summarize_replay(replay_log: list) -> str:
    """A clean match has no `reason` set -- reconcile.py only writes one for
    variance/exception cases -- but the replay_log already has the real
    stage-by-stage explanation computed for every row. This builds one
    readable line from those same structured entries instead of leaving a
    clean match with nothing to show but a dash."""
    details = [entry.get("detail", "") for entry in replay_log if isinstance(entry, dict) and entry.get("detail")]
    if not details:
        return ""
    return "; ".join(d[0].upper() + d[1:] for d in details if d)


_REASON_NUMBER_RE = re.compile(r"(Rs\.\d[\d,]*(?:\.\d+)?|\b\d{1,3}(?:\.\d+)?%)")
_REASON_QUOTE_RE = re.compile(r"(&#x27;.*?&#x27;)")


def highlight_reason(escaped_reason: str) -> str:
    """Every reason line above already follows the same shape: a fact,
    then ' -- ', then the actual takeaway a reviewer needs. Rather than
    handing over that whole sentence as one flat paragraph, surface the
    numbers (what changed), the quoted narration (the raw evidence), and
    the takeaway clause (why it matters) so it can be scanned instead of
    read end to end. Runs strictly on already-escaped text -- every
    substitution wraps a span of that same text, never inserts anything
    unescaped, so this stays safe even though narration text ultimately
    comes from an uploaded CSV."""
    text = _REASON_NUMBER_RE.sub(r"<mark>\1</mark>", escaped_reason)
    text = _REASON_QUOTE_RE.sub(r'<code class="hl-quote">\1</code>', text)
    if " -- " in text:
        head, _, tail = text.rpartition(" -- ")
        text = f'{head} -- <strong class="hl-point">{tail}</strong>'
    return text


def render_row(r: dict, show_actions: bool = True) -> str:
    replay_log = json.loads(r["replay_log"] or "[]")
    replay_html = "".join(f"<div>{render_log_entry(s)}</div>" for s in replay_log) or "<div>(no stages recorded)</div>"
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
        resolution_tone = {"CONFIRMED": "positive", "REJECTED": "negative"}.get(r["resolution_status"], "information")
        actions_html = f'<span class="pill {resolution_tone}">{escape(r["resolution_status"])}</span>'

    category_html = f'<span class="cat">{escape(r["category"])}</span>' if r["category"] else '<span class="cat-empty">&mdash;</span>'

    order_html = (f'<span class="id-chip id-chip-order">{escape(r["order_id"])}</span>'
                  if r["order_id"] else '<span class="cat-empty">&mdash;</span>')
    settlement_html = (f'<span class="id-chip id-chip-settlement">{escape(r["settlement_id"])}</span>'
                        if r["settlement_id"] else '<span class="cat-empty">&mdash;</span>')

    reason_text = r["reason"] or summarize_replay(replay_log) or "No stages recorded for this row."

    return f"""
    <tr data-search="{search_blob}" data-status="{escape(r["status"])}">
      <td class="id-cell" data-value="{escape(r["order_id"] or "")}">{order_html}</td>
      <td class="id-cell" data-value="{escape(r["settlement_id"] or "")}">{settlement_html}</td>
      <td class="amount-cell" data-value="{r["net_amount"] if r["net_amount"] is not None else ""}">{net}</td>
      <td data-value="{escape(r["status"])}">{render_status_pill(r["status"])}</td>
      <td data-value="{escape(r["category"] or "")}">{category_html}</td>
      <td class="reason-cell">
        <div class="audit-box">
          {highlight_reason(escape(reason_text))}
          {narration_html}
          <details><summary>Replay log</summary>{replay_html}</details>
          {note_html}
        </div>
      </td>
      <td class="action-cell">{actions_html}</td>
    </tr>"""


def render_queue() -> str:
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
      <div class="table-scroll">
      <table>
        <thead><tr><th>Order</th><th>Settlement</th><th>Net (Rs.)</th><th>Status</th><th>Category</th><th>Reason / audit trail</th><th>Action</th></tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
      </div>
    </div>"""
    return render_shell("queue", "Review queue", "", body)


# -------------------------------------------------------------- All records

def render_records(initial_query: str = "") -> str:
    """Renders every persisted row once; filtering and sorting happen
    instantly client-side via INTERACTIVITY_SCRIPT, not a server round trip.
    initial_query pre-fills the search box -- how a category card on
    Overview deep-links here already filtered."""
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
          <input type="text" id="live-search" placeholder="Search order, settlement, narration..." value="{escape(initial_query)}">
          <select id="live-status">
            <option value="">All types</option>
            {status_options}
          </select>
          <span id="filter-count" style="color:var(--muted);font-size:13px;margin-left:auto">{len(all_rows)} of {len(all_rows)} rows</span>
        </form>
      </div>
    </div>
    <div class="panel">
      <div class="table-scroll">
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
      </div>
    </div>"""
    return render_shell("records", "All records", "", body, extra_script=INTERACTIVITY_SCRIPT)


# ------------------------------------------------------------- Data sources

def count_csv_rows(path: Path) -> int | None:
    if not path.exists():
        return None
    with open(path, newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def render_sources() -> str:
    settlement_count = count_csv_rows(DATA_DIR / "settlement_report.csv")
    bank_count = count_csv_rows(DATA_DIR / "bank_statement.csv")
    ledger_count = count_csv_rows(DATA_DIR / "internal_ledger.csv")
    gateway_b_count = count_csv_rows(DATA_DIR / "gateway_b_export.csv")

    def card(icon, label, value):
        display = "--" if value is None else str(value)
        return f"""
        <div class="source-card">
          <div class="icon-badge">{icon}</div>
          <div class="value">{display}</div>
          <div class="label">{label}</div>
        </div>"""

    body = f"""
    <div class="source-grid">
      {card(ICON_ROWS, "Settlements", settlement_count)}
      {card(ICON_BANK, "Bank rows", bank_count)}
      {card(ICON_LEDGER, "Ledger rows", ledger_count)}
      {card(ICON_GATEWAY, "Gateway B rows", gateway_b_count)}
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
            initial_query = parse_qs(parsed.query).get("q", [""])[0]
            self._respond_html(render_records(initial_query=initial_query))
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


def main() -> None:
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
