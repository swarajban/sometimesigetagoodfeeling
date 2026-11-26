"""Intro titles: rasterised with Pillow to a transparent image the renderer overlays on the opening footage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from goodfeeling.schemas.edl import Title

FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
TITLE_VERSION = "t1"
MAX_WIDTH = 0.84


@dataclass(frozen=True)
class Face:
    file: str
    weight: int
    size: float
    tracking: float
    upper: bool
    opacity: float


@dataclass(frozen=True)
class Style:
    name: Face
    rest: Face
    gap: float
    leading: float
    color: tuple[int, int, int]


STYLES = {
    "film": Style(
        name=Face("CormorantGaramond[wght].ttf", 500, 0.074, 0.04, False, 1.0),
        rest=Face("Jost[wght].ttf", 500, 0.022, 0.42, True, 0.95),
        gap=0.66,
        leading=2.0,
        color=(247, 243, 236),
    ),
}


def _font(face: Face, px: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONTS / face.file), px)
    if font.get_variation_axes():
        font.set_variation_by_axes([face.weight])
    return font


def _width(text: str, font: ImageFont.FreeTypeFont, tracking: float) -> float:
    return font.getlength(text) + tracking * (len(text) - 1)


def _balanced_split(text: str) -> list[str]:
    words = text.split()
    if len(words) < 2:
        return [text]
    cut = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    return [" ".join(words[:cut]), " ".join(words[cut:])]


def render_title(title: Title, width: int, height: int, out: Path) -> Path:
    """The title at the output's size as an RGBA PNG, centred; sizes follow the short side so 1080p and 4K match.

    A name too wide for the frame (a reel) breaks onto two lines before anything is scaled down.
    """
    style = STYLES[title.style]
    base = min(width, height)
    limit = MAX_WIDTH * width
    name_font = _font(style.name, round(style.name.size * base))
    rest_font = _font(style.rest, round(style.rest.size * base))
    name_track, rest_track = style.name.tracking * name_font.size, style.rest.tracking * rest_font.size
    name = [title.lines[0]]
    if _width(name[0], name_font, name_track) > limit:
        name = _balanced_split(name[0])
    rest = [line.upper() if style.rest.upper else line for line in title.lines[1:]]
    widest = max([_width(t, name_font, name_track) for t in name] + [_width(t, rest_font, rest_track) for t in rest])
    if widest > limit:
        scale = limit / widest
        name_font = _font(style.name, round(name_font.size * scale))
        rest_font = _font(style.rest, round(rest_font.size * scale))
        name_track, rest_track = name_track * scale, rest_track * scale
    rows: list[tuple[str, ImageFont.FreeTypeFont, float, float, float]] = []
    y = 0.0
    for i, text in enumerate(name):
        y += name_font.size * 1.12 if i else 0.0
        rows.append((text, name_font, name_track, style.name.opacity, y))
    for i, text in enumerate(rest):
        y += name_font.size * style.gap + rest_font.size if i == 0 else rest_font.size * style.leading
        rows.append((text, rest_font, rest_track, style.rest.opacity, y))
    ascent = name_font.getmetrics()[0] * 0.72
    top = (height - (y + ascent)) / 2 + ascent
    text_mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(text_mask)
    for text, font, track, opacity, row_y in rows:
        x = (width - _width(text, font, track)) / 2
        for i, char in enumerate(text):
            position = (x + font.getlength(text[:i]) + track * i, top + row_y)
            draw.text(position, char, font=font, fill=round(255 * opacity), anchor="ls")
    px = name_font.size
    glow = text_mask.filter(ImageFilter.MaxFilter(2 * max(1, round(px * 0.06)) + 1))
    shadow = glow.filter(ImageFilter.GaussianBlur(px * 0.2)).point(lambda v: round(v * 0.62))
    veil = glow.filter(ImageFilter.GaussianBlur(px * 1.0)).point(lambda v: min(255, round(v * 0.8)))
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    image.putalpha(ImageChops.lighter(shadow, veil))
    fill = Image.new("RGBA", (width, height), (*style.color, 255))
    fill.putalpha(text_mask)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(image, fill).save(out)
    return out
