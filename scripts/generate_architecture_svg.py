"""
Generates assets/architecture.svg -- a genuinely horizontal flowchart of
the actual mechanism, not a restated list of pass names. Redesigned after
real feedback twice: v1 read as tall/vertical despite a wide canvas (the
branch and persistence sat in stacked rows below the main spine); v2 kept
everything on one spine but had zone backgrounds guessed ahead of the
real node positions, so they didn't line up, and the canvas was too
narrow for its own content (the last two nodes ran off the right edge).
This version computes every node's x position FIRST, derives the zone
backgrounds and final canvas width from those real positions, and
measures every label against its own box before placing it (wrap_text)
so nothing can run past its border. Colors are the site's own real
status tokens (positive/notice/negative/information from
review_server.py's :root CSS), not a separate palette invented for this
one asset.
"""
import colorsys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def hsl(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


INK = hsl(222, 25, 14)
MUTED = hsl(222, 12, 42)
FAINT = hsl(215, 10, 63)
LINE = "#D7DEE7"
PRIMARY = hsl(204, 100, 50)
PRIMARY_STRONG = hsl(204, 100, 38)
PRIMARY_BG = hsl(204, 100, 96)
NOTICE = hsl(25, 100, 44)
NOTICE_BG = hsl(23, 100, 96)
POSITIVE = hsl(150, 100, 28)
POSITIVE_BG = hsl(150, 60, 95)
INFORMATION = hsl(200, 100, 41)
INFORMATION_BG = hsl(198, 85, 96)
WHITE = "#FFFFFF"

FONT = "'Inter',-apple-system,'Segoe UI',Arial,sans-serif"
FONT_HEAD = "'Inter Tight','Inter',-apple-system,'Segoe UI',sans-serif"

SPINE_Y = 210
NODE_H = 76
MARGIN = 36


def wrap_text(s, box_w, font_size, bold=False, pad=14):
    avg_char_w = font_size * (0.62 if bold else 0.56)
    max_chars = max(1, int((box_w - pad * 2) / avg_char_w))
    words = s.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > max_chars and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def rrect(x, y, w, h, fill, stroke, rx=12, sw=1.6, dash=None, filter_id=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    f = f' filter="url(#{filter_id})"' if filter_id else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}{f}/>'


def centered_lines(cx, top_y, lines, size, weight, fill, line_h=None, font=FONT):
    line_h = line_h or size * 1.35
    return "".join(
        f'<text x="{cx}" y="{top_y + i * line_h}" font-family="{font}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="middle">{ln}</text>'
        for i, ln in enumerate(lines)
    )


def node(x, y, w, h, title, sub, fill, stroke, title_color=None, sub_color=MUTED,
         title_size=13.5, sub_size=11, rx=12, filter_id="softShadow"):
    title_color = title_color or INK
    out = [rrect(x, y, w, h, fill, stroke, rx=rx, filter_id=filter_id)]
    cx = x + w / 2
    title_lines = wrap_text(title, w, title_size, bold=True)
    sub_lines = wrap_text(sub, w, sub_size) if sub else []
    block_h = len(title_lines) * (title_size * 1.3) + (len(sub_lines) * (sub_size * 1.35) if sub_lines else 0)
    top = y + h / 2 - block_h / 2 + title_size * 0.9
    out.append(centered_lines(cx, top, title_lines, title_size, 700, title_color, line_h=title_size * 1.3, font=FONT_HEAD))
    if sub_lines:
        sub_top = top + (len(title_lines) - 1) * title_size * 1.3 + title_size * 1.05
        out.append(centered_lines(cx, sub_top, sub_lines, sub_size, 500, sub_color, line_h=sub_size * 1.35))
    return "".join(out)


def icon_check(cx, cy, r, color):
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>'
            f'<path d="M{cx-r*0.45},{cy} l{r*0.3},{r*0.35} l{r*0.55},{-r*0.6}" fill="none" stroke="white" stroke-width="{r*0.22}" stroke-linecap="round" stroke-linejoin="round"/>')


def icon_bolt(cx, cy, r, color):
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>'
            f'<path d="M{cx+r*0.12},{cy-r*0.55} L{cx-r*0.32},{cy+r*0.1} L{cx-r*0.02},{cy+r*0.1} '
            f'L{cx-r*0.18},{cy+r*0.55} L{cx+r*0.38},{cy-r*0.05} L{cx+r*0.08},{cy-r*0.05} Z" fill="white"/>')


def icon_person(cx, cy, r, color):
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>'
            f'<circle cx="{cx}" cy="{cy-r*0.28}" r="{r*0.28}" fill="white"/>'
            f'<path d="M{cx-r*0.42},{cy+r*0.55} a{r*0.42},{r*0.38} 0 0 1 {r*0.84},0 Z" fill="white"/>')


def icon_db(cx, cy, r, color):
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>'
            f'<ellipse cx="{cx}" cy="{cy-r*0.32}" rx="{r*0.52}" ry="{r*0.2}" fill="none" stroke="white" stroke-width="{r*0.16}"/>'
            f'<path d="M{cx-r*0.52},{cy-r*0.32} v{r*0.64} c0,{r*0.11} {r*0.23},{r*0.2} {r*0.52},{r*0.2} '
            f's{r*0.52},{-r*0.09} {r*0.52},{-r*0.2} v-{r*0.64}" fill="none" stroke="white" stroke-width="{r*0.16}"/>')


# ============================================================ Layout pass
# Every x position computed here, before a single element is drawn --
# zones and the canvas width both derive from these, not the other way
# around, which is exactly the ordering bug the previous version had.

sx, sw_, sh, sgap = MARGIN, 320, 56, 16
sy0 = SPINE_Y - (sh * 3 + sgap * 2) / 2
merge_x = sx + sw_ + 56

px0, pw, pgap = merge_x + 46, 128, 22
pstep = pw + pgap
passes = [
    ("Pass 1", "UTR + amount + date"),
    ("Pass 2", "order_id lookup"),
    ("Pass 2.5", "Learned pattern"),
    ("Pass 2.6", "Learned template"),
    ("Pass 2.75", "Exact digit ref"),
    ("Pass 3", "Fuzzy shortlist"),
]
pass_x = [px0 + i * pstep for i in range(len(passes))]
last_pass_x = pass_x[-1]
pattern_x = pass_x[2]  # Pass 2.5 -- the feedback loop's destination

gate_x = last_pass_x + pstep + 16
gate_w, gate_h = 210, 108
gate_y = SPINE_Y - (gate_h - NODE_H) / 2

# The branch is a real vertical fork now, not a squeeze near the gate --
# clear separation above and below the spine so it reads as an actual
# decision point, the one place in this whole diagram where "vertical"
# earns its keep.
fork_x = gate_x + gate_w + 64
fork_w, fork_h = 190, 60
auto_y = SPINE_Y - fork_h - 34
review_y = SPINE_Y + NODE_H + 34

merge2_x = fork_x + fork_w + 56
persist_x = merge2_x + 34
persist_w = 220

app_x = persist_x + persist_w + 56
app_w = 220

W = app_x + app_w + MARGIN
loop_bottom = review_y + fork_h + 92
H = loop_bottom + 52

# Zone bands, derived from the real content they contain, each with a
# little breathing room -- not guessed ahead of time.
zone_top, zone_bottom = min(auto_y, sy0) - 40, loop_bottom + 14
zones = [
    (MARGIN - 12, px0 - 24, INFORMATION_BG, "3 REAL INPUTS"),
    (px0 - 12, gate_x - 24, PRIMARY_BG, "SIX DETERMINISTIC PASSES -- CHEAPEST, MOST CERTAIN FIRST"),
    (gate_x - 12, merge2_x + 2, NOTICE_BG, "THE ONE STEP A MODEL TOUCHES"),
    (merge2_x + 14, app_x + app_w + 12, POSITIVE_BG, "PERSISTED, THEN LIVE"),
]

# ================================================================ Render

el = [
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
    f'aria-label="A settlement row flows through three sources into six deterministic matching passes; '
    f'only what none of them resolve reaches a confidence-gated model, which is still checked by a human '
    f'before it counts as resolved. Every decision is persisted, and a human confirmation feeds back into '
    f'the pattern store so the same case never needs a model again.">',
    f'<defs>'
    f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">'
    f'<path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker>'
    f'<marker id="arrowNotice" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">'
    f'<path d="M0,0 L10,5 L0,10 z" fill="{NOTICE}"/></marker>'
    f'<marker id="arrowPositive" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">'
    f'<path d="M0,0 L10,5 L0,10 z" fill="{POSITIVE}"/></marker>'
    f'<marker id="arrowPrimary" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">'
    f'<path d="M0,0 L10,5 L0,10 z" fill="{PRIMARY_STRONG}"/></marker>'
    f'<filter id="softShadow" x="-40%" y="-40%" width="180%" height="220%">'
    f'<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#1B202D" flood-opacity="0.10"/></filter>'
    f'<filter id="heroShadow" x="-60%" y="-60%" width="220%" height="260%">'
    f'<feDropShadow dx="0" dy="4" stdDeviation="7" flood-color="{NOTICE}" flood-opacity="0.35"/></filter>'
    f'<linearGradient id="heroFill" x1="0" y1="0" x2="1" y2="1">'
    f'<stop offset="0%" stop-color="{hsl(28, 100, 55)}"/><stop offset="100%" stop-color="{NOTICE}"/></linearGradient>'
    f'</defs>',
    f'<rect x="0" y="0" width="{W}" height="{H}" fill="{WHITE}"/>',
]

for zx0, zx1, zc, zlabel in zones:
    el.append(f'<rect x="{zx0}" y="{zone_top}" width="{zx1-zx0}" height="{zone_bottom-zone_top}" rx="18" fill="{zc}"/>')
    el.append(f'<text x="{zx0+18}" y="{zone_top+22}" font-family="{FONT}" font-size="11" font-weight="700" '
               f'letter-spacing="0.7" fill="{MUTED}">{zlabel}</text>')

# ---- 1. Sources --------------------------------------------------------
for i, label in enumerate(["Settlement Recon API", "Bank statement CSV", "Internal ledger CSV"]):
    y = sy0 + i * (sh + sgap)
    el.append(node(sx, y, sw_, sh, label, "", WHITE, INFORMATION, title_size=13.5))
    el.append(icon_db(sx + 24, y + sh / 2, 11, INFORMATION))

merge_y = sy0 + (sh * 3 + sgap * 2) / 2
for i in range(3):
    y = sy0 + i * (sh + sgap) + sh / 2
    el.append(f'<path d="M{sx+sw_+10},{y} C {sx+sw_+30},{y} {merge_x-15},{merge_y} {merge_x},{merge_y}" '
               f'fill="none" stroke="{LINE}" stroke-width="1.6"/>')
el.append(f'<line x1="{merge_x}" y1="{merge_y}" x2="{px0-10}" y2="{SPINE_Y+NODE_H/2}" stroke="{LINE}" stroke-width="1.6" marker-end="url(#arrow)"/>')

# ---- 2. Six deterministic passes ---------------------------------------
for i, ((name, sub), x) in enumerate(zip(passes, pass_x)):
    el.append(node(x, SPINE_Y, pw, NODE_H, name, sub, WHITE, LINE, title_color=PRIMARY_STRONG, title_size=13.5, sub_size=11))
    if i > 0:
        prev_x = pass_x[i - 1]
        el.append(f'<line x1="{prev_x+pw}" y1="{SPINE_Y+NODE_H/2}" x2="{x}" y2="{SPINE_Y+NODE_H/2}" stroke="{LINE}" stroke-width="1.6" marker-end="url(#arrow)"/>')

# ---- 3. Pass 4 -- the hero node -----------------------------------------
el.append(f'<line x1="{last_pass_x+pw}" y1="{SPINE_Y+NODE_H/2}" x2="{gate_x}" y2="{gate_y+gate_h/2}" '
           f'stroke="{NOTICE}" stroke-width="2" marker-end="url(#arrowNotice)"/>')
el.append(f'<text x="{(last_pass_x+pw+gate_x)/2}" y="{SPINE_Y-16}" font-family="{FONT}" font-size="10.5" '
           f'fill="{MUTED}" text-anchor="middle">shortlist</text>')
el.append(rrect(gate_x, gate_y, gate_w, gate_h, "url(#heroFill)", "none", rx=18, filter_id="heroShadow"))
el.append(icon_bolt(gate_x + 30, gate_y + gate_h / 2, 15, "rgba(255,255,255,0.3)"))
el.append(centered_lines(gate_x + gate_w / 2 + 14, gate_y + 31, ["Pass 4"], 16.5, 800, WHITE, font=FONT_HEAD))
for i, ln in enumerate(wrap_text("Confidence-gated arbiter", gate_w - 30, 11)):
    el.append(centered_lines(gate_x + gate_w / 2 + 14, gate_y + 51 + i * 14, [ln], 11, 600, "rgba(255,255,255,0.94)"))

# ---- 4. Compact fork ----------------------------------------------------
el.append(f'<path d="M{gate_x+gate_w},{SPINE_Y+16} C {fork_x-22},{SPINE_Y+16} {fork_x-22},{auto_y+fork_h/2} {fork_x},{auto_y+fork_h/2}" '
           f'fill="none" stroke="{FAINT}" stroke-width="1.6" stroke-dasharray="4,4" marker-end="url(#arrow)"/>')
el.append(f'<path d="M{gate_x+gate_w},{SPINE_Y+48} C {fork_x-22},{SPINE_Y+48} {fork_x-22},{review_y+fork_h/2} {fork_x},{review_y+fork_h/2}" '
           f'fill="none" stroke="{NOTICE}" stroke-width="2" marker-end="url(#arrowNotice)"/>')
el.append(node(fork_x, auto_y, fork_w, fork_h, "Auto-applied", "never taken (0%)", WHITE, LINE, title_size=13, sub_size=10.5))
el.append(node(fork_x, review_y, fork_w, fork_h, "Human review", "confirm / reject", NOTICE_BG, NOTICE, title_color=NOTICE, title_size=13, sub_size=10.5))
el.append(icon_person(fork_x + fork_w - 16, review_y + fork_h / 2, 10, NOTICE))

# ---- 5. Persistence + Review app ----------------------------------------
el.append(f'<path d="M{fork_x+fork_w},{auto_y+fork_h/2} C {merge2_x-20},{auto_y+fork_h/2} {merge2_x-20},{SPINE_Y+NODE_H/2} {merge2_x},{SPINE_Y+NODE_H/2}" '
           f'fill="none" stroke="{FAINT}" stroke-width="1.6" stroke-dasharray="4,4"/>')
el.append(f'<path d="M{fork_x+fork_w},{review_y+fork_h/2} C {merge2_x-20},{review_y+fork_h/2} {merge2_x-20},{SPINE_Y+NODE_H/2} {merge2_x},{SPINE_Y+NODE_H/2}" '
           f'fill="none" stroke="{NOTICE}" stroke-width="2"/>')
el.append(f'<line x1="{merge2_x}" y1="{SPINE_Y+NODE_H/2}" x2="{persist_x-6}" y2="{SPINE_Y+NODE_H/2}" stroke="{POSITIVE}" stroke-width="2" marker-end="url(#arrowPositive)"/>')

el.append(node(persist_x, SPINE_Y, persist_w, NODE_H, "SQLite persistence", "full audit trail", POSITIVE_BG, POSITIVE, title_color=POSITIVE, title_size=14, sub_size=11))
el.append(icon_db(persist_x + persist_w - 20, SPINE_Y + NODE_H / 2, 10, POSITIVE))

el.append(f'<line x1="{persist_x+persist_w}" y1="{SPINE_Y+NODE_H/2}" x2="{app_x-6}" y2="{SPINE_Y+NODE_H/2}" stroke="{PRIMARY_STRONG}" stroke-width="2" marker-end="url(#arrowPrimary)"/>')
el.append(f'<text x="{(persist_x+persist_w+app_x)/2}" y="{SPINE_Y-16}" font-family="{FONT}" font-size="10.5" fill="{MUTED}" text-anchor="middle">live query</text>')
el.append(node(app_x, SPINE_Y, app_w, NODE_H, "Review application", "confirm or reject", WHITE, PRIMARY, title_color=PRIMARY_STRONG, title_size=14, sub_size=11))
el.append(icon_check(app_x + app_w - 20, SPINE_Y + NODE_H / 2, 10, PRIMARY))

# ---- 6. Feedback loop -----------------------------------------------------
loop_start_x = app_x + app_w / 2
loop_end_x = pattern_x + pw / 2
el.append(f'<path d="M{loop_start_x},{SPINE_Y+NODE_H} C {loop_start_x},{loop_bottom} {loop_end_x},{loop_bottom} {loop_end_x},{SPINE_Y+NODE_H}" '
           f'fill="none" stroke="{PRIMARY_STRONG}" stroke-width="2" stroke-dasharray="6,5" marker-end="url(#arrowPrimary)"/>')
el.append(f'<text x="{(loop_start_x+loop_end_x)/2}" y="{loop_bottom+22}" font-family="{FONT}" font-size="12" '
           f'font-weight="600" fill="{PRIMARY_STRONG}" text-anchor="middle">human confirms &#8594; new pattern memorized, zero model calls next time</text>')

el.append("</svg>")
out = ASSETS / "architecture.svg"
out.write_text("\n".join(el), encoding="utf-8")
print(f"wrote {out} ({out.stat().st_size} bytes), canvas {W}x{H}")
