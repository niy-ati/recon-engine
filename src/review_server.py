"""
Live human-review server. Confirming a match in the browser writes
straight into narration_rules -- no CSV export, no separate script.

    python review_server.py
    -> opens http://localhost:8000/ automatically

GET  /             -- lists every open exception needing a decision, with
                       a collapsible replay_log per row.
POST /resolve/<id> -- action=confirm|reject (terminal decision). Confirming
                       a FUZZY_MATCH_NEEDS_REVIEW row also writes a
                       narration_rules entry (see db.resolve_exception).
POST /note/<id>    -- attaches a clarification note without resolving the
                       row; it stays open and the note stays visible.

Stdlib-only (http.server + sqlite3): no framework, no build step.

Colors, type, elevation, radius, and spacing tokens are copied from
Razorpay's open-source Blade design system (github.com/razorpay/blade).
This is a private, local dev tool, not an official Razorpay product.
"""
import json
import threading
import webbrowser
from collections import Counter
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import db

PORT = 8000

PAGE_STYLE = """
  :root {
    /* Blade neutral scale (blueGrayLight) -- surfaces, borders, text */
    --bg: hsl(0, 0%, 97%);
    --panel: hsl(0, 0%, 100%);
    --border: hsl(204, 8%, 88%);
    --border-subtle: hsl(0, 0%, 97%);
    --ink: hsl(200, 10%, 18%);
    --muted: hsl(204, 9%, 42%);
    --faint: hsl(203, 8%, 62%);

    /* Blade azure -- "Razorpay Blue" primary + deep Prussian-blue-like surface */
    --primary: hsl(218, 89%, 51%);
    --primary-strong: hsl(218, 87%, 43%);
    --primary-subtle: hsl(218, 100%, 92%);
    --deep: hsl(218, 90%, 20%);
    --deep-raised: hsl(218, 80%, 26%);

    /* Blade feedback semantics -- positive/negative/notice/information,
       used exactly as Blade categorizes them, not reassigned */
    --positive: hsl(150, 100%, 28%);
    --positive-bg: hsl(150, 39%, 93%);
    --negative: hsl(4, 85%, 44%);
    --negative-bg: hsl(5, 75%, 97%);
    --notice: hsl(25, 100%, 44%);
    --notice-bg: hsl(23, 100%, 97%);
    --information: hsl(200, 100%, 41%);
    --information-bg: hsl(198, 85%, 95%);

    /* Blade's own font stack, elevation shadows, radius and spacing scale */
    --font: 'Inter', -apple-system, 'Segoe UI', Arial, sans-serif;
    --mono: 'Menlo', 'Cascadia Mono', Consolas, 'Roboto Mono', monospace;
    --shadow-low: 0px 2px 4px 0px hsla(200, 10%, 18%, 0.06);
    --shadow-mid: 0px 16px 12px 0px hsla(200, 10%, 18%, 0.06);
    --radius-s: 8px;
    --radius-m: 12px;
    --radius-l: 16px;
    --radius-pill: 9999px;
    --sp-2: 4px; --sp-3: 8px; --sp-4: 12px; --sp-5: 16px; --sp-6: 20px; --sp-7: 24px; --sp-8: 32px;
  }
  * { box-sizing: border-box; }
  body {
    margin:0; background:var(--bg); color:var(--ink); font-family:var(--font);
    font-size:14px; -webkit-font-smoothing:antialiased; display:flex; min-height:100vh;
  }

  /* ---- Sidebar rail: mirrors Razorpay's own dashboard shell structure ---- */
  aside.rail {
    width:220px; flex-shrink:0; background:var(--deep); color:hsl(0,0%,100%);
    padding:var(--sp-6) var(--sp-5); display:flex; flex-direction:column; gap:var(--sp-7);
  }
  aside.rail .brand { display:flex; align-items:center; gap:var(--sp-3); }
  aside.rail .brand .mark {
    width:30px; height:30px; border-radius:var(--radius-s); background:var(--primary);
    display:flex; align-items:center; justify-content:center; font-weight:700; font-size:13px; flex-shrink:0;
  }
  aside.rail .brand .name { font-weight:600; font-size:14px; letter-spacing:-0.01em; line-height:1.3; }
  aside.rail nav { display:flex; flex-direction:column; gap:2px; }
  aside.rail nav a {
    display:flex; align-items:center; gap:var(--sp-3); padding:var(--sp-3) var(--sp-4);
    border-radius:var(--radius-s); color:hsla(0,0%,100%,0.6); text-decoration:none; font-size:13px; font-weight:500;
  }
  aside.rail nav a.active { background:var(--deep-raised); color:#fff; }
  aside.rail .env-tag {
    margin-top:auto; font-family:var(--mono); font-size:10.5px; letter-spacing:0.04em; text-transform:uppercase;
    background:hsla(0,0%,100%,0.10); padding:var(--sp-3) var(--sp-4); border-radius:var(--radius-s); color:hsla(0,0%,100%,0.75); line-height:1.5;
  }

  main { flex:1; min-width:0; padding:var(--sp-8) var(--sp-8) 60px; max-width:1240px; }
  h1 { font-size:20px; margin:0 0 2px; letter-spacing:-0.01em; }
  p.sub { color:var(--muted); margin:0 0 var(--sp-7); font-size:13px; max-width:72ch; line-height:1.55; }
  p.sub code { font-family:var(--mono); background:var(--border-subtle); padding:1px 5px; border-radius:4px; }

  /* ---- Overview row: donut chart (real data) + stat tiles ---- */
  .overview { display:flex; gap:var(--sp-6); margin-bottom:var(--sp-7); flex-wrap:wrap; align-items:stretch; }
  .donut-card {
    background:var(--panel); border:1px solid var(--border); border-radius:var(--radius-m);
    box-shadow:var(--shadow-low); padding:var(--sp-6); display:flex; align-items:center; gap:var(--sp-6); min-width:280px;
  }
  .donut { width:96px; height:96px; border-radius:50%; flex-shrink:0; position:relative; }
  .donut::after {
    content:""; position:absolute; inset:14px; background:var(--panel); border-radius:50%;
    display:flex; align-items:center; justify-content:center;
  }
  .donut .donut-label {
    position:absolute; inset:14px; display:flex; flex-direction:column; align-items:center; justify-content:center;
  }
  .donut .donut-label b { font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:18px; line-height:1; }
  .donut .donut-label span { font-size:9px; color:var(--muted); text-transform:uppercase; letter-spacing:0.04em; margin-top:2px; }
  .legend { display:flex; flex-direction:column; gap:6px; font-size:12px; }
  .legend .row { display:flex; align-items:center; gap:8px; }
  .legend .swatch { width:9px; height:9px; border-radius:2px; flex-shrink:0; }
  .legend .pct { font-family:var(--mono); font-variant-numeric:tabular-nums; color:var(--muted); margin-left:auto; padding-left:14px; }

  .stats { display:flex; gap:var(--sp-5); flex-wrap:wrap; flex:1; }
  .stat {
    background:var(--panel); border:1px solid var(--border); border-radius:var(--radius-m);
    box-shadow:var(--shadow-low); padding:var(--sp-5) var(--sp-6); font-size:12.5px; color:var(--muted);
    min-width:150px; flex:1;
  }
  .stat b { display:block; font-family:var(--mono); font-variant-numeric:tabular-nums; font-size:24px; color:var(--ink); font-weight:600; }

  .panel { background:var(--panel); border:1px solid var(--border); border-radius:var(--radius-m); box-shadow:var(--shadow-low); overflow:hidden; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:var(--sp-5) var(--sp-5); border-bottom:1px solid var(--border-subtle); font-size:13px; vertical-align:top; }
  tbody tr:last-child td { border-bottom:none; }
  tbody tr { transition:background 0.12s; }
  tbody tr:hover { background:hsl(0,0%,98.5%); }
  th {
    background:var(--bg); font-family:var(--mono); font-size:10.5px; text-transform:uppercase;
    letter-spacing:0.06em; color:var(--faint); border-bottom:1px solid var(--border); font-weight:600;
  }
  td.id-cell { font-family:var(--mono); font-variant-numeric:tabular-nums; color:var(--ink); }
  td.amount-cell { font-family:var(--mono); font-variant-numeric:tabular-nums; text-align:right; }

  /* ---- Pill badges: Blade's max border-radius (9999px), semantic colors ---- */
  .pill {
    display:inline-flex; align-items:center; gap:4px; font-size:11px; font-weight:600;
    padding:3px 10px; border-radius:var(--radius-pill); letter-spacing:0.01em; white-space:nowrap;
  }
  .pill.notice { background:var(--notice-bg); color:var(--notice); }
  .pill.information { background:var(--information-bg); color:var(--information); }
  .pill.negative { background:var(--negative-bg); color:var(--negative); }
  .pill.positive { background:var(--positive-bg); color:var(--positive); }
  .cat { font-family:var(--mono); font-size:10.5px; padding:2px 7px; border-radius:var(--radius-s); background:var(--bg); color:var(--muted); border:1px solid var(--border); }

  .narration { font-family:var(--mono); font-size:12px; color:var(--muted); margin-top:5px; }
  .note-text { margin-top:8px; font-size:12.5px; color:var(--notice); background:var(--notice-bg); border-radius:var(--radius-s); padding:6px 10px; display:inline-block; }
  details { margin-top:6px; font-size:12px; }
  details summary { cursor:pointer; color:var(--primary); font-weight:500; list-style:none; }
  details summary::-webkit-details-marker { display:none; }
  details summary:before { content:"▸ "; font-size:10px; }
  details[open] summary:before { content:"▾ "; }
  details summary:hover { color:var(--primary-strong); }
  details > div { font-family:var(--mono); color:var(--muted); padding:3px 0 3px 14px; border-left:2px solid var(--border); margin-top:4px; }

  form { margin:0 0 6px; display:inline-block; }
  button {
    font-family:var(--font); font-size:12.5px; font-weight:600; padding:7px 13px;
    border-radius:var(--radius-s); border:1px solid transparent; cursor:pointer; transition:filter 0.1s, box-shadow 0.1s;
    display:inline-flex; align-items:center; gap:6px;
  }
  button svg { width:13px; height:13px; flex-shrink:0; }
  button:hover { filter:brightness(0.96); }
  button:focus-visible { outline:2px solid var(--primary); outline-offset:2px; }
  button.approve { background:var(--positive); color:#fff; }
  button.approve:hover { box-shadow:0 0 0 3px var(--positive-bg); }
  button.reject { background:var(--panel); color:var(--negative); border-color:var(--negative-bg); }
  button.reject:hover { background:var(--negative-bg); }
  button.pending { background:var(--panel); color:var(--notice); border:1px solid var(--border); }
  button.pending:hover { background:var(--notice-bg); }
  input[type=text] {
    font-family:var(--font); font-size:12.5px; padding:6px 10px; border:1px solid var(--border);
    border-radius:var(--radius-s); width:180px;
  }
  input[type=text]:focus-visible { outline:2px solid var(--primary); outline-offset:1px; border-color:var(--primary); }

  .empty-state { padding:48px; text-align:center; color:var(--muted); }
  footer.credit { color:var(--faint); font-size:11px; margin-top:var(--sp-6); }
"""

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


def render_status_pill(status):
    label, tone = STATUS_LABELS.get(status, (status, "information"))
    return f'<span class="pill {tone}">{escape(label)}</span>'


def render_donut():
    """Queries every persisted row's status and renders a proportional
    breakdown of the full batch, not just the open queue."""
    all_rows = db.get_all_exceptions()
    total = len(all_rows)
    if total == 0:
        return "<p class='sub'>No batch persisted yet -- run report.py first.</p>"

    counts = Counter(r["status"] for r in all_rows)
    order = ["MATCHED", "MATCHED_WITH_VARIANCE", "MATCHED_EXACT_REFERENCE",
             "MATCHED_LEARNED_PATTERN", "MATCHED_AI_ASSISTED", "MATCHED_LOW_CONFIDENCE", "EXCEPTION"]

    stops, cursor, legend_rows = [], 0.0, []
    for status in order:
        c = counts.get(status, 0)
        if not c:
            continue
        pct = 100 * c / total
        label, tone = STATUS_LABELS[status]
        color = SWATCH_HEX[tone]
        stops.append(f"{color} {cursor:.2f}% {cursor + pct:.2f}%")
        cursor += pct
        legend_rows.append(
            f'<div class="row"><span class="swatch" style="background:{color}"></span>'
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


def render_row(r):
    replay_log = json.loads(r["replay_log"] or "[]")
    replay_html = "".join(f"<div>{escape(s)}</div>" for s in replay_log) or "<div>(no stages recorded)</div>"
    note_html = (f'<div class="note-text">Note: "{escape(r["resolution_note"])}"</div>'
                 if r["resolution_note"] else "")
    narration_html = (f'<div class="narration">narration: "{escape(r["narration"])}"</div>'
                       if r["narration"] else "")
    net = "" if r["net_amount"] is None else f'{r["net_amount"]:,.2f}'

    return f"""
    <tr>
      <td class="id-cell">{escape(r["order_id"] or "(none)")}</td>
      <td class="id-cell">{escape(r["settlement_id"] or "(none)")}</td>
      <td class="amount-cell">{net}</td>
      <td>{render_status_pill(r["status"])}</td>
      <td><span class="cat">{escape(r["category"] or "")}</span></td>
      <td>{escape(r["reason"] or "")}
          {narration_html}
          <details><summary>Replay log</summary>{replay_html}</details>
          {note_html}
      </td>
      <td>
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
        </form>
      </td>
    </tr>"""


def render_index():
    open_rows = db.get_open_exceptions()
    all_rows = db.get_all_exceptions()
    resolved_count = sum(1 for r in all_rows if r["resolution_status"] != "OPEN")
    body_rows = "".join(render_row(r) for r in open_rows) or (
        '<tr><td colspan="7" class="empty-state">No open exceptions -- everything is either '
        'clean-matched or already resolved.</td></tr>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Reconciliation Review Queue</title>
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
    <nav>
      <a class="active">Review queue</a>
    </nav>
    <div class="env-tag">Test data<br>localhost:{PORT}<br>not an official Razorpay product</div>
  </aside>
  <main>
    <h1>Review queue</h1>
    <p class="sub">Live, server-backed: confirming a fuzzy match writes straight into <code>narration_rules</code> &mdash; no export, no separate promote step. This page re-renders from SQLite on every request; refresh after any action to see the queue update.</p>

    <div class="overview">
      {render_donut()}
      <div class="stats">
        <div class="stat"><b>{len(open_rows)}</b>open, need a decision</div>
        <div class="stat"><b>{resolved_count}</b>resolved this session</div>
      </div>
    </div>

    <div class="panel">
      <table>
        <thead><tr><th>Order</th><th>Settlement</th><th>Net (₹)</th><th>Status</th><th>Category</th><th>Reason / audit trail</th><th>Action</th></tr></thead>
        <tbody>{body_rows}</tbody>
      </table>
    </div>
    <footer class="credit">Colors, type, elevation, radius &amp; spacing from Razorpay's open-source Blade design system (github.com/razorpay/blade) &mdash; see review_server.py's module docstring for exact token sources.</footer>
  </main>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default request logging

    def do_GET(self):
        if urlparse(self.path).path == "/":
            self._respond_html(render_index())
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
        self.send_header("Location", "/")
        self.end_headers()

    def _respond_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = HTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}/"
    print(f"Serving the review queue at {url}  (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
