"""
Generates the two raster assets this project needs but had none of before:
assets/og-image.png (1200x630, for link-preview cards -- Slack/WhatsApp/
email/LinkedIn all need a real PNG/JPG here, not the SVG wordmark this repo
already has, since Open Graph crawlers don't reliably render SVG) and
assets/favicon.png (the browser-tab icon). Colors are pulled from the same
tokens review_server.py's own CSS defines (--bg/--ink/--primary/
--primary-strong), not invented separately, so the share card and the
actual site read as the same product. Run once; the output is committed
like any other asset, not regenerated per request.
"""
import colorsys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
FONT_DIR = Path("C:/Windows/Fonts")


def hsl(h, s, l):
    r, g, b = colorsys.hls_to_rgb(h / 360, l / 100, s / 100)
    return (round(r * 255), round(g * 255), round(b * 255))


BG = hsl(210, 45, 97.5)
INK = hsl(222, 25, 14)
MUTED = hsl(222, 12, 42)
PRIMARY = hsl(204, 100, 50)
PRIMARY_STRONG = hsl(204, 100, 38)
WHITE = (255, 255, 255)


def font(name, size):
    return ImageFont.truetype(str(FONT_DIR / name), size)


def make_og_image():
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # A soft radial-ish accent band along the top-left, echoing the site's
    # own page-head gradient treatment, kept subtle -- the card's job is to
    # read at thumbnail size, not to be a poster.
    draw.rectangle([0, 0, W, 10], fill=PRIMARY)

    pad = 72
    draw.text((pad, 88), "Settlement Reconciliation Engine", font=font("segoeuib.ttf", 54), fill=INK)
    draw.text((pad, 160), "A deterministic-first AI Finance Controller", font=font("segoeui.ttf", 30), fill=MUTED)

    # The one number this whole project is built to defend: measured on a
    # real, cited 525-row batch, not a marketing claim -- see README.
    draw.text((pad, 268), "87.6%", font=font("segoeuib.ttf", 130), fill=PRIMARY_STRONG)
    draw.text((pad + 4, 410), "resolved, zero AI auto-applied", font=font("segoeui.ttf", 32), fill=INK)

    draw.line([(pad, 480), (W - pad, 480)], fill=(220, 228, 238), width=2)
    draw.text((pad, 510), "Multi-source reconciliation \u00b7 Settlement Q&A \u00b7 Tax-line matcher \u00b7 Cash forecast",
               font=font("segoeui.ttf", 24), fill=MUTED)
    draw.text((pad, 552), "Razorpay AI Buildathon 2026 \u00b7 Track 04", font=font("segoeuib.ttf", 24), fill=PRIMARY_STRONG)

    out = ASSETS / "og-image.png"
    img.save(out, "PNG")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


def make_favicon():
    # A rounded square in the site's own brand blue with a white checkmark
    # -- "verified," the one idea every feature in this project actually
    # rests on. Drawn at 512px and let the browser/OS downscale, rather
    # than hand-tuning multiple sizes for a hackathon favicon.
    S = 512
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = 110
    draw.rounded_rectangle([0, 0, S, S], radius=r, fill=PRIMARY)
    draw.line([(140, 265), (225, 350), (380, 165)], fill=WHITE, width=46, joint="curve")
    out = ASSETS / "favicon.png"
    img.save(out, "PNG")
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    ASSETS.mkdir(exist_ok=True)
    make_og_image()
    make_favicon()
