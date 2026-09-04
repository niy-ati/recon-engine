"""
Regenerates assets/metrics.svg from db.py's own real, currently-persisted
numbers -- the same two-bar "row resolution state" / "cash position
clarity" infographic embedded in README.md's Metrics section.

Why this exists: metrics.svg has no earlier generator committed to the
repo (it was hand-built once and then silently went stale the first time
the underlying batch changed -- exactly the DISPUTED/method addition that
prompted writing this script). A static image with real numbers baked
into it must be regenerated every time those numbers change, the same
discipline generate_architecture_svg.py already holds itself to for the
architecture diagram. Run this after `python run_all.py` whenever the
headline percentages move.

Usage: python scripts/generate_metrics_svg.py
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import settlement_qa  # noqa: E402

OUT_PATH = Path(__file__).resolve().parent.parent / "assets" / "metrics.svg"

BAR_X, BAR_WIDTH, BAR_HEIGHT = 30, 800, 28
GREEN, ORANGE, RED = "#008F47", "#E05E00", "#D01E11"


def segment_widths(pcts: list[float]) -> list[float]:
    """Three percentages that sum to ~100 -> three pixel widths that sum
    to exactly BAR_WIDTH -- the last segment absorbs rounding error so the
    bar's total width is never off by a fraction of a pixel from drift
    across three independently-rounded numbers."""
    widths = [round(BAR_WIDTH * p / 100, 1) for p in pcts[:-1]]
    widths.append(round(BAR_WIDTH - sum(widths), 1))
    return widths


def bar_svg(y: int, pcts: list[float]) -> str:
    w1, w2, w3 = segment_widths(pcts)
    x1 = BAR_X
    x2 = round(x1 + w1, 1)
    x3 = round(x2 + w2, 1)
    return (
        f'<rect x="{BAR_X}" y="{y}" width="{BAR_WIDTH}" height="{BAR_HEIGHT}" rx="14.0" fill="#F6F9FB"/>'
        f'<rect x="{x1}" y="{y}" width="{w1}" height="{BAR_HEIGHT}" fill="{GREEN}"/>'
        f'<rect x="{x2}" y="{y}" width="{w2}" height="{BAR_HEIGHT}" fill="{ORANGE}"/>'
        f'<rect x="{x3}" y="{y}" width="{w3}" height="{BAR_HEIGHT}" fill="{RED}"/>'
        f'<rect x="{BAR_X}" y="{y}" width="{BAR_WIDTH}" height="{BAR_HEIGHT}" rx="14.0" fill="none"/>'
    )


def stat_svg(x: int, y_value: int, y_label: int, value: str, color: str, label: str) -> str:
    return (
        f'<text x="{x}" y="{y_value}" font-family="\'Inter Tight\',\'Inter\',-apple-system,\'Segoe UI\',sans-serif" '
        f'font-size="26" font-weight="800" fill="#1B202D" text-anchor="start" letter-spacing="-0.01em">{value}</text>'
        f'<circle cx="{x + 7}" cy="{y_label - 5}" r="5" fill="{color}"/>'
        f'<text x="{x + 20}" y="{y_label}" font-family="\'Inter\',-apple-system,\'Segoe UI\',Arial,sans-serif" '
        f'font-size="13.5" font-weight="500" fill="#636E7E" text-anchor="start">{label}</text>'
    )


def heading_svg(y: int, title: str, right_label: str) -> str:
    return (
        f'<text x="30" y="{y}" font-family="\'Inter\',-apple-system,\'Segoe UI\',Arial,sans-serif" '
        f'font-size="13" font-weight="700" fill="#0074C2" text-anchor="start" letter-spacing="1.1">{title}</text>'
        f'<text x="830" y="{y}" font-family="\'Inter\',-apple-system,\'Segoe UI\',Arial,sans-serif" '
        f'font-size="13" font-weight="600" fill="#979FAA" text-anchor="end">{right_label}</text>'
    )


def main() -> None:
    # Read everything from the persisted DB, not a fresh reconcile() call --
    # reconcile() alone reflects the batch BEFORE failure_injection_demo.py's
    # 2 adversarial trap rows get appended (run_all.py runs that in between
    # reconcile.py and report.py), so a second, independent reconcile() call
    # here would silently disagree with report.py's own persisted total.
    # One source of truth for both halves of this image, same discipline
    # RESOLVED_STATUSES already enforces elsewhere in this codebase.
    all_rows = db.get_all_exceptions()
    if not all_rows:
        raise SystemExit("No persisted batch -- run `python run_all.py` first.")

    total = len(all_rows)
    resolved = sum(1 for r in all_rows if r["status"] in settlement_qa.RESOLVED_STATUSES)
    pending = sum(1 for r in all_rows if r["status"] == "MATCHED_LOW_CONFIDENCE")
    open_ = sum(1 for r in all_rows if r["status"] == "EXCEPTION")
    resolved_pct = round(100 * resolved / total, 1)
    pending_pct = round(100 * pending / total, 1)
    open_pct = round(100 * open_ / total, 1)

    clarity = db.compute_cash_clarity(all_rows)
    row_pcts = [resolved_pct, pending_pct, open_pct]
    cash_pcts = [clarity["resolved_pct"], clarity["pending_review_pct"], clarity["still_open_pct"]]

    body = "".join([
        heading_svg(44, "ROW RESOLUTION STATE", f"{total} rows"),
        bar_svg(74, row_pcts),
        stat_svg(30, 146, 165, f"{resolved_pct}%", GREEN, "resolved, zero human input"),
        stat_svg(330, 146, 165, f"{pending_pct}%", ORANGE, "pending human confirmation"),
        stat_svg(630, 146, 165, f"{open_pct}%", RED, "genuinely open"),
        '<line x1="30" y1="236" x2="830" y2="236" stroke="#DDE3E9" stroke-width="1.5"/>',
        heading_svg(282, "CASH POSITION CLARITY", f"Rs {clarity['at_risk']:,.2f} at risk, duplicates excluded"),
        bar_svg(312, cash_pcts),
        stat_svg(30, 384, 403, f"Rs {clarity['resolved']:,.2f}", GREEN, "resolved, trustworthy input"),
        stat_svg(330, 384, 403, f"Rs {clarity['pending_review']:,.2f}", ORANGE, "pending human confirmation"),
        stat_svg(630, 384, 403, f"Rs {clarity['still_open']:,.2f}", RED, "genuinely open"),
    ])

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 860 454" width="860" height="454">\n'
        '<rect x="0" y="0" width="860" height="454" fill="#FFFFFF"/>\n'
        f"{body}\n</svg>\n"
    )
    OUT_PATH.write_text(svg, encoding="utf-8")
    print(f"Wrote {OUT_PATH} from the current persisted batch "
          f"({total} rows, {resolved_pct}% resolved).")


if __name__ == "__main__":
    main()
