"""Draw the Family Table app icons: a steaming bowl on a little table.

Run from the project folder:  python tools/make_icons.py
Writes PNGs and an SVG into static/icons/ (and the browser-tab favicon).
The shapes are drawn with Pillow (already a dependency), so no design tools are needed.
"""

# math draws the wavy steam lines.
import math

# Path builds output file locations.
from pathlib import Path

# Pillow draws and resizes the images; ImageChops combines masks.
from PIL import Image, ImageChops, ImageDraw


ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "icons"

TERRACOTTA = "#A8432A"
CREAM = "#F4EDE1"
CARD = "#FFFDF8"

GRID = 1024     # all shapes are laid out on a 1024 x 1024 grid
SUPER = 4       # draw 4x larger, then shrink, for smooth edges

# Steam: three gentle S-curves (centre x, top y, bottom y).
STEAM = [(432, 236, 352), (512, 206, 352), (592, 236, 352)]
STEAM_WIDTH, STEAM_SWAY = 26, 16
RIM = (322, 394, 702, 426)            # bowl rim (rounded bar)
BOWL = (342, 270, 682, 570)           # bowl body: bottom half of this ellipse
BAND = (452, 470)                     # terracotta stripe across the bowl (y range)
TOP = (192, 576, 832, 632)            # table top
LEGS = [((262, 318), (272, 308)), ((706, 762), (716, 752))]   # (top x range, bottom x range)
LEG_Y = (626, 812)


def steam_points(cx, y0, y1, steps=48):
    """Points along one wavy steam line."""
    return [(cx + STEAM_SWAY * math.sin(t / steps * 2 * math.pi), y0 + (y1 - y0) * t / steps)
            for t in range(steps + 1)]


def draw_motif(size, scale):
    """Transparent image of the bowl-and-table drawing; `scale` shrinks it toward the centre."""
    px = size * SUPER
    k = px / GRID * scale
    off = px / 2 - GRID / 2 * k
    p = lambda x, y: (off + x * k, off + y * k)
    box = lambda b: (*p(b[0], b[1]), *p(b[2], b[3]))

    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Steam: stamp overlapping dots along each curve (smoother than a wide polyline).
    r = STEAM_WIDTH * k / 2
    for cx, y0, y1 in STEAM:
        for x, y in (p(*pt) for pt in steam_points(cx, y0, y1, 240)):
            d.ellipse((x - r, y - r, x + r, y + r), fill=CREAM)

    # Bowl body, then the stripe cut across it, then the rim on top.
    d.pieslice(box(BOWL), 0, 180, fill=CARD)
    bowl_mask = Image.new("L", (px, px), 0)
    ImageDraw.Draw(bowl_mask).pieslice(box(BOWL), 0, 180, fill=255)
    band = Image.new("L", (px, px), 0)
    ImageDraw.Draw(band).rectangle(box((0, BAND[0], GRID, BAND[1])), fill=255)
    stripe = Image.new("RGBA", (px, px), TERRACOTTA)
    img.paste(stripe, (0, 0), ImageChops.multiply(bowl_mask, band))   # stripe only inside the bowl
    d.rounded_rectangle(box(RIM), radius=16 * k, fill=CARD)

    d.rounded_rectangle(box(TOP), radius=28 * k, fill=CARD)
    for (tx0, tx1), (bx0, bx1) in LEGS:
        d.polygon([p(tx0, LEG_Y[0]), p(tx1, LEG_Y[0]), p(bx1, LEG_Y[1] - 18), p(bx0, LEG_Y[1] - 18)], fill=CARD)
        d.ellipse(box((bx0, LEG_Y[1] - 36, bx1, LEG_Y[1])), fill=CARD)
    return img


def make_icon(size, scale=1.0, corner=0.0):
    """Terracotta square with the motif; `corner` rounds it (0 = full-bleed square)."""
    px = size * SUPER
    bg = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    ImageDraw.Draw(bg).rounded_rectangle((0, 0, px - 1, px - 1), radius=corner * px, fill=TERRACOTTA)
    bg.alpha_composite(draw_motif(size, scale))
    icon = bg.resize((size, size), Image.LANCZOS)
    return icon if corner else icon.convert("RGB")   # full-bleed icons have no transparency


def svg(scale=1.0, corner=0.22):
    """The same drawing as an SVG (used as the browser-tab favicon)."""
    def path(cx, y0, y1):
        pts = steam_points(cx, y0, y1, 24)
        return "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    steam = "".join(f'<path d="{path(*s)}"/>' for s in STEAM)
    legs = "".join(
        f'<path d="M{tx0} {LEG_Y[0]}H{tx1}L{bx1} {LEG_Y[1] - 18}A18 18 0 0 1 {bx0} {LEG_Y[1] - 18}Z"/>'
        for (tx0, tx1), (bx0, bx1) in LEGS)
    cx, cy, rx, ry = 512, 420, 170, 150
    bowl = f"M{cx - rx} {cy}A{rx} {ry} 0 0 0 {cx + rx} {cy}Z"
    r = corner * GRID
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {GRID} {GRID}">'
        f'<defs><clipPath id="b"><path d="{bowl}"/></clipPath></defs>'
        f'<rect width="{GRID}" height="{GRID}" rx="{r:.0f}" fill="{TERRACOTTA}"/>'
        f'<g fill="none" stroke="{CREAM}" stroke-width="{STEAM_WIDTH}" stroke-linecap="round">{steam}</g>'
        f'<g fill="{CARD}"><path d="{bowl}"/>'
        f'<rect x="0" y="{BAND[0]}" width="{GRID}" height="{BAND[1] - BAND[0]}" fill="{TERRACOTTA}" clip-path="url(#b)"/>'
        f'<rect x="{RIM[0]}" y="{RIM[1]}" width="{RIM[2] - RIM[0]}" height="{RIM[3] - RIM[1]}" rx="16"/>'
        f'<rect x="{TOP[0]}" y="{TOP[1]}" width="{TOP[2] - TOP[0]}" height="{TOP[3] - TOP[1]}" rx="28"/>'
        f'{legs}</g></svg>\n'
    )


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    # Standard icons (browsers, desktop install): rounded corners.
    for size in (192, 512):
        make_icon(size, corner=0.22).save(OUT / f"icon-{size}.png", optimize=True)
    # Maskable icons: full bleed, drawing kept inside the central "safe zone" circle.
    for size in (192, 512):
        make_icon(size, scale=0.74).save(OUT / f"icon-maskable-{size}.png", optimize=True)
    # iPhone home screen: opaque square; iOS rounds the corners itself.
    make_icon(180).save(OUT / "apple-touch-icon.png", optimize=True)
    (ROOT / "static" / "favicon.svg").write_text(svg())
    print("Wrote icons to", OUT)


if __name__ == "__main__":
    main()
