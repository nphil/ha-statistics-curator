"""Deterministic Pillow renderer for the Statistics Curator brand kit.

Writes the eight images Home Assistant serves straight from
``custom_components/statistics_curator/brand/`` (no brands-repo submission needed):

    icon.png        256x256      dark_icon.png        256x256
    icon@2x.png     512x512      dark_icon@2x.png     512x512
    logo.png        864x256      dark_logo.png        864x256
    logo@2x.png     1728x512     dark_logo@2x.png     1728x512

The mark: three statistics bars on one baseline. Stale history crumbles off
the first bar into small squares, and an amber tick marks the rest as settled.
Flat colour only, no gradients, no text in the icon. ``../icon.svg`` is the
same mark as hand-authored vector art; this script does not read it, so keep
the numbers below in sync with it.

Light variant ("icon", "logo"): blue plate, white mark, amber tick.
Dark variant ("dark_icon", "dark_logo"): pale plate, blue mark, so it keeps
its contrast on the dark Home Assistant header.

Pillow only, 4x supersampling, no randomness: re-running produces
byte-identical PNGs.

Usage: python3 tools/render_brand.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "custom_components" / "statistics_curator" / "brand"

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Liberation Sans Bold, then DejaVu Bold, then Pillow's default font."""
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


# ---------------------------------------------------------------------------
# Geometry, in a 256x256 reference space (identical numbers to ../icon.svg).
# Boxes are (x0, y0, x1, y1, corner radius).
# ---------------------------------------------------------------------------

PLATE_RX = 56.0

BARS = (  # all end on the baseline y = 208
    (40.0, 144.0, 88.0, 208.0, 12.0),
    (104.0, 112.0, 152.0, 208.0, 12.0),
    (168.0, 128.0, 216.0, 208.0, 12.0),
)
CRUMBS = (  # stale history leaving the first bar, shrinking as it goes
    (40.0, 114.0, 56.0, 130.0, 5.0),
    (62.0, 96.0, 74.0, 108.0, 4.0),
    (80.0, 78.0, 89.0, 87.0, 3.0),
)
TICK = ((136.0, 66.0), (158.0, 88.0), (202.0, 40.0))  # polyline
TICK_R = 9.0  # half the stroke width

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

LIGHT_VARIANT = {
    "plate": (0x25, 0x4B, 0xE8),
    "mark": (0xFF, 0xFF, 0xFF),
    "accent": (0xFF, 0xB8, 0x2E),
}
DARK_VARIANT = {
    "plate": (0xEE, 0xF2, 0xFF),
    "mark": (0x25, 0x4B, 0xE8),
    "accent": (0xF5, 0x8A, 0x00),
}

WORDMARK_ON_LIGHT_BG = (0x12, 0x1E, 0x4A)  # logo.png
SUBMARK_ON_LIGHT_BG = (0x25, 0x4B, 0xE8)
WORDMARK_ON_DARK_BG = (0xF3, 0xF6, 0xFF)  # dark_logo.png
SUBMARK_ON_DARK_BG = (0x8F, 0xA8, 0xFF)

SS = 4  # supersampling factor


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _capsule(draw, a, b, r, k, fill):
    """Round-capped stroke from point a to point b, radius r."""
    (x0, y0), (x1, y1) = a, b
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    nx, ny = -dy / length * r, dx / length * r
    draw.polygon(
        [((x0 + nx) * k, (y0 + ny) * k), ((x1 + nx) * k, (y1 + ny) * k),
         ((x1 - nx) * k, (y1 - ny) * k), ((x0 - nx) * k, (y0 - ny) * k)],
        fill=fill,
    )
    for cx, cy in (a, b):
        draw.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)


def render_mark(size, palette):
    """(size, size) RGBA icon: flat rounded plate with the bars/crumbs/tick mark."""
    big = size * SS
    k = big / 256.0
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=PLATE_RX * k, fill=palette["plate"] + (255,))

    mark = palette["mark"] + (255,)
    for x0, y0, x1, y1, r in BARS + CRUMBS:
        d.rounded_rectangle([x0 * k, y0 * k, x1 * k, y1 * k], radius=r * k, fill=mark)
    accent = palette["accent"] + (255,)
    for a, b in zip(TICK, TICK[1:]):
        _capsule(d, a, b, TICK_R, k, accent)

    return canvas.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Wordmark lockup
# ---------------------------------------------------------------------------

LOGO_ASPECT = 864 / 256  # width / height (about 3.4x)
LOGO_MARK_FRAC = 0.86  # mark size as a fraction of canvas height
LOGO_LEFT_FRAC = 0.05
LOGO_GAP_FRAC = 0.11
LOGO_NAME_FRAC = 0.32  # "Statistics" font size / canvas height
LOGO_SUB_FRAC = 0.32  # "Curator" font size / canvas height
LOGO_TRACK_FRAC = 0.004
LOGO_LINE_GAP_FRAC = 0.08

NAME = "Statistics"
SUB = "Curator"


def _tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(draw, x, y, text, font, fill, tracking):
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def render_logo(height, palette, name_rgb, sub_rgb):
    """RGBA lockup: mark on the left, "Statistics" over "Curator" on the right,
    transparent background, exactly `height` tall."""
    width = round(height * LOGO_ASPECT)
    mark_size = round(height * LOGO_MARK_FRAC)
    mark = render_mark(mark_size, palette)

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    left = round(height * LOGO_LEFT_FRAC)
    canvas.alpha_composite(mark, (left, (height - mark_size) // 2))

    name_font = load_font(round(height * LOGO_NAME_FRAC))
    sub_font = load_font(round(height * LOGO_SUB_FRAC))
    tracking = height * LOGO_TRACK_FRAC
    draw = ImageDraw.Draw(canvas)

    # Vertically centre the two-line block on ink bounds, not font ascent.
    n_l, n_t, n_r, n_b = draw.textbbox((0, 0), NAME, font=name_font)
    s_l, s_t, s_r, s_b = draw.textbbox((0, 0), SUB, font=sub_font)
    line_gap = height * LOGO_LINE_GAP_FRAC
    block_h = (n_b - n_t) + line_gap + (s_b - s_t)
    top = (height - block_h) / 2
    text_x = left + mark_size + height * LOGO_GAP_FRAC

    name_w = _tracked_width(draw, NAME, name_font, tracking)
    _draw_tracked(draw, text_x - n_l, top - n_t, NAME, name_font, name_rgb + (255,), tracking)
    sub_y = top + (n_b - n_t) + line_gap
    _draw_tracked(draw, text_x - s_l, sub_y - s_t, SUB, sub_font, sub_rgb + (255,), tracking)

    sub_w = _tracked_width(draw, SUB, sub_font, tracking)
    right_edge = text_x + max(name_w, sub_w)
    assert right_edge <= width - height * 0.03, f"wordmark clipped at height {height}: {right_edge} > {width}"
    return canvas


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _save_pair(master, stem):
    """<stem>@2x.png from the 512-based master, <stem>.png as an exact half."""
    master.save(BRAND_DIR / f"{stem}@2x.png")
    half = (master.width // 2, master.height // 2)
    master.resize(half, Image.LANCZOS).save(BRAND_DIR / f"{stem}.png")


def main():
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    _save_pair(render_mark(512, LIGHT_VARIANT), "icon")
    _save_pair(render_mark(512, DARK_VARIANT), "dark_icon")
    _save_pair(render_logo(512, LIGHT_VARIANT, WORDMARK_ON_LIGHT_BG, SUBMARK_ON_LIGHT_BG), "logo")
    _save_pair(render_logo(512, DARK_VARIANT, WORDMARK_ON_DARK_BG, SUBMARK_ON_DARK_BG), "dark_logo")


if __name__ == "__main__":
    main()
