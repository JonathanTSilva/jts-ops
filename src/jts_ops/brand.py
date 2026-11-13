"""README brand: the per-repo header SVG and the shared footer button, plus `jts-ops brand`.

Same visual family as the profile banner: Geist text converted to paths, accent circuit traces
with pads and a subtle dot grid. Text is outlined, so the SVGs render identically on GitHub.
The canvas is transparent (no background or panel), so one file works on any page background;
the light and dark variants differ only in the text and accent colours.
"""

import hashlib
import re
from dataclasses import dataclass
from functools import cache
from html import escape as html_escape
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

FONT_DIR = Path(__file__).parent / "fonts"
FONT_FILES = {"semibold": "Geist-SemiBold.ttf", "regular": "Geist-Regular.ttf"}

BRAND_DIR = Path("assets") / "brand"
HEADER_PREFIX = "header-"
THEME_NAMES = ("light", "dark")

HEADER_START = "<!-- brand:header:start -->"
HEADER_END = "<!-- brand:header:end -->"
FOOTER_START = "<!-- brand:footer:start -->"
FOOTER_END = "<!-- brand:footer:end -->"
SITE_URL = "https://www.jontobias.com"
BUTTON_BASE = "https://raw.githubusercontent.com/JonathanTSilva/.github/main/assets/brand"
PLACEHOLDER = "PLACEHOLDER"

THEMES = {
    "light": {"text": "#1f2328", "accent": "#4A5BD4", "pill_border": "#3A4BBE"},
    "dark": {"text": "#e6edf3", "accent": "#6272EA", "pill_border": "#7B8EF5"},
}
TRACE_OPACITY = "0.6"
DOT_OPACITY = "0.3"

W, H = 1280, 160
LEFT = 64
TEXT_MAX = 880 - LEFT  # text must stop before the circuit node at x=990 (with breathing room)
NODE = (990, 80)
NAME_SIZE, NAME_MIN = 44, 28
DESC_SIZE, DESC_MIN = 20, 12
TRACKING = -0.015


class BrandError(Exception):
    """The brand step stopped; the message says why."""


@cache
def _font(kind: str) -> Any:
    return TTFont(FONT_DIR / FONT_FILES[kind])


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def text_width(s: str, font: str, size: float, tracking: float = 0.0) -> float:
    f = _font(font)
    gs = f.getGlyphSet()
    cmap = f.getBestCmap()
    k = size / f["head"].unitsPerEm
    w = 0.0
    for ch in s:
        w += gs[cmap.get(ord(ch), cmap[ord("?")])].width * k + tracking * size
    return w - tracking * size if s else 0.0


Glyphs = dict[tuple[str, float, str], tuple[str, str]]


def text_use(
    s: str, font: str, size: float, x: float, y: float, glyphs: Glyphs, tracking: float = 0.0
) -> str:
    """`<use>` markup for s with its baseline at (x, y); outlines are shared through `glyphs`."""
    f = _font(font)
    gs = f.getGlyphSet()
    cmap = f.getBestCmap()
    k = size / f["head"].unitsPerEm
    out: list[str] = []
    cx = x
    for ch in s:
        g = cmap.get(ord(ch), cmap[ord("?")])
        key = (font, size, ch)
        if ch != " " and key not in glyphs:
            pen = SVGPathPen(gs, ntos=_fmt)
            gs[g].draw(TransformPen(pen, (k, 0, 0, -k, 0, 0)))
            glyphs[key] = (f"g{len(glyphs)}", pen.getCommands())
        if ch != " ":
            out.append(f'<use href="#{glyphs[key][0]}" x="{cx:.1f}" y="{y:.1f}"/>')
        cx += gs[g].width * k + tracking * size
    return "".join(out)


def clean_description(desc: str) -> str:
    """Drop the leading emoji (and variation selectors / spaces) from an inventory description."""
    return re.sub(r"^[^\w]+", "", desc).strip()


def wrap(text: str, font: str, size: float, maxw: float) -> list[str]:
    lines: list[str] = []
    cur = ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if cur and text_width(t, font, size) > maxw:
            lines.append(cur)
            cur = w
        else:
            cur = t
    lines.append(cur)
    return lines


def _balance(text: str, font: str, size: float, maxw: float) -> list[str]:
    """Wrap into 2 lines, split as evenly as the width allows."""
    lines = wrap(text, font, size, maxw)
    if len(lines) != 2:
        return lines
    words = text.split()
    best: tuple[float, list[str]] | None = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        wa, wb = text_width(a, font, size), text_width(b, font, size)
        if max(wa, wb) <= maxw and (best is None or max(wa, wb) < best[0]):
            best = (max(wa, wb), [a, b])
    return best[1] if best else lines


def _name_lines(name: str) -> tuple[int, list[str]]:
    """The repo name at 44px, shrunk to fit; split at hyphens only if 28px is still too wide."""
    size = NAME_SIZE
    while size > NAME_MIN and text_width(name, "semibold", size, TRACKING) > TEXT_MAX:
        size -= 1
    if text_width(name, "semibold", size, TRACKING) <= TEXT_MAX:
        return size, [name]
    parts = name.split("-")
    lines: list[str] = []
    cur = ""
    for i, part in enumerate(parts):
        piece = part + ("-" if i < len(parts) - 1 else "")
        if cur and text_width(cur + piece, "semibold", size, TRACKING) > TEXT_MAX:
            lines.append(cur)
            cur = piece
        else:
            cur += piece
    lines.append(cur)
    return size, lines


def _motif(accent: str) -> tuple[str, str, str]:
    """Compact circuit: one source node, five traces; the last two branch off other traces.

    Traces stop at the edge of each ring, so the rings can stay hollow on a transparent canvas.
    """
    traces = [
        "M996 80H1040L1070 50H1175.5",  # up and out
        "M996 80H1209.5",  # straight
        "M996 80H1040L1070 110H1155.5",  # down and out
        "M1070 50V28H1119.5",  # branch off trace 1 at its bend
        "M1110 110L1130 130H1209.5",  # branch off trace 3
    ]
    pads = [(1180, 50), (1214, 80), (1160, 110), (1124, 28), (1214, 130)]
    joints = [(1070, 50), (1110, 110)]
    tr = "".join(f'<path d="{d}"/>' for d in traces)
    pd = "".join(f'<circle cx="{x}" cy="{y}" r="4.5"/>' for x, y in pads)
    jd = "".join(
        f'<circle cx="{x}" cy="{y}" r="2.5" fill="{accent}" stroke="none"/>' for x, y in joints
    )
    return tr, pd, jd


def header_svg(name: str, description: str, theme: str) -> str:
    """The repo header on a transparent 1280x160 canvas; `theme` is `light` or `dark`."""
    if theme not in THEMES:
        raise BrandError(f"unknown theme {theme!r}, expected light or dark")
    t = THEMES[theme]
    glyphs: Glyphs = {}
    description = clean_description(description)

    ns, name_lines = _name_lines(name)
    ds = DESC_SIZE
    while True:
        one = text_width(description, "regular", ds) <= TEXT_MAX
        lines = [description] if one else _balance(description, "regular", ds, TEXT_MAX)
        fits = all(text_width(ln, "regular", ds) <= TEXT_MAX for ln in lines)
        if (len(lines) <= 2 and fits) or ds <= DESC_MIN:
            break
        ds -= 1
    lh = ds * 1.4
    nlh = ns * 1.1
    block = (
        0.71 * ns + (len(name_lines) - 1) * nlh + 24 + 0.71 * ds + (len(lines) - 1) * lh + 0.2 * ds
    )
    y0 = (H - block) / 2
    nb = y0 + 0.71 * ns
    n_d = "".join(
        text_use(ln, "semibold", ns, LEFT, round(nb + i * nlh, 1), glyphs, TRACKING)
        for i, ln in enumerate(name_lines)
    )
    base = nb + (len(name_lines) - 1) * nlh + 24 + 0.71 * ds
    d_d = "".join(
        text_use(ln, "regular", ds, LEFT, round(base + i * lh, 1), glyphs)
        for i, ln in enumerate(lines)
    )

    tr, pd, jd = _motif(t["accent"])
    nx, ny = NODE
    gdefs = "".join(f'<path id="{i}" d="{d}"/>' for i, d in glyphs.values())
    title = escape(f"{name}: {description}")
    gx = int(W * 0.55)  # the dot grid fades in from here, so its rect never covers the canvas
    fade = (0.9 * W - gx) / (W - gx)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-labelledby="t">
<title id="t">{title}</title>
<defs>
<pattern id="g" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="12" cy="12" r="1" fill="{t["accent"]}"/></pattern>
<linearGradient id="f" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="{_fmt(fade)}" stop-color="#fff" stop-opacity="1"/></linearGradient>
<mask id="m"><rect x="{gx}" width="{W - gx}" height="{H}" fill="url(#f)"/></mask>
{gdefs}
</defs>
<g mask="url(#m)" opacity="{DOT_OPACITY}"><rect x="{gx}" width="{W - gx}" height="{H}" fill="url(#g)"/></g>
<g fill="none" stroke="{t["accent"]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" opacity="{TRACE_OPACITY}">{tr}</g>
<g fill="none" stroke="{t["accent"]}" stroke-width="2" opacity="{TRACE_OPACITY}">{pd}</g>
<g opacity="{TRACE_OPACITY}">{jd}</g>
<circle cx="{nx}" cy="{ny}" r="6" fill="none" stroke="{t["accent"]}" stroke-width="2"/>
<circle cx="{nx}" cy="{ny}" r="2" fill="{t["accent"]}"/>
<g fill="{t["text"]}">{n_d}</g>
<g fill="{t["accent"]}">{d_d}</g>
</svg>
'''


def button_svg(theme: str) -> str:
    """The shared footer button: an accent pill on a transparent 200x40 canvas."""
    if theme not in THEMES:
        raise BrandError(f"unknown theme {theme!r}, expected light or dark")
    t = THEMES[theme]
    glyphs: Glyphs = {}
    bw, bh = 200, 40
    size = 16
    label = "jontobias.com"
    lw = text_width(label, "semibold", size)
    gap, aw = 8, 13
    start = 32  # content begins after the circuit node mark
    total = lw + gap + aw
    x0 = start + (bw - 10 - start - total) / 2
    cy = bh / 2
    l_d = text_use(label, "semibold", size, round(x0, 1), round(cy + size * 0.355, 1), glyphs)
    ax = x0 + lw + gap
    arrow = f"M{ax:.1f} {cy}H{ax + aw:.1f}M{ax + aw - 5:.1f} {cy - 5}L{ax + aw:.1f} {cy}L{ax + aw - 5:.1f} {cy + 5}"
    gdefs = "".join(f'<path id="{i}" d="{d}"/>' for i, d in glyphs.values())
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{bw}" height="{bh}" viewBox="0 0 {bw} {bh}" role="img" aria-labelledby="t">
<title id="t">jontobias.com</title>
<defs>{gdefs}</defs>
<rect x="0.5" y="0.5" width="{bw - 1}" height="{bh - 1}" rx="{bh / 2 - 0.5}" fill="{t["accent"]}" stroke="{t["pill_border"]}"/>
<rect x="1.5" y="1.5" width="{bw - 3}" height="{bh - 3}" rx="{bh / 2 - 1.5}" fill="none" stroke="#fff" stroke-opacity="0.16"/>
<path d="M6 {cy}H15" stroke="#fff" stroke-opacity="0.7" stroke-width="1.5" stroke-linecap="round" fill="none"/>
<circle cx="19" cy="{cy}" r="3.2" fill="none" stroke="#fff" stroke-opacity="0.9" stroke-width="1.6"/>
<g fill="#fff">{l_d}</g>
<path d="{arrow}" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>
'''


def brand_hash(name: str, description: str) -> str:
    """First 8 hex chars of sha256(name + description); a new title gives a new file name."""
    return hashlib.sha256((name + clean_description(description)).encode("utf-8")).hexdigest()[:8]


def header_block(name: str, description: str, digest: str) -> str:
    """The README header block, between its markers, pointing at the hashed SVG files."""
    alt = html_escape(f"{name}: {clean_description(description)}", quote=True)
    base = f"assets/brand/{HEADER_PREFIX}{digest}"
    return (
        f"{HEADER_START}\n"
        "<picture>\n"
        f'  <source media="(prefers-color-scheme: dark)" srcset="{base}-dark.svg">\n'
        f'  <img alt="{alt}" src="{base}-light.svg" width="100%">\n'
        "</picture>\n"
        f"{HEADER_END}"
    )


def footer_block() -> str:
    """The README footer block: the shared button, right-aligned, linking to the site."""
    return (
        f"{FOOTER_START}\n"
        f'<p align="right"><a href="{SITE_URL}"><picture>\n'
        '  <source media="(prefers-color-scheme: dark)" '
        f'srcset="{BUTTON_BASE}/jontobias-dark.svg">\n'
        f'  <img alt="jontobias.com" src="{BUTTON_BASE}/jontobias-light.svg" width="200">\n'
        "</picture></a></p>\n"
        f"{FOOTER_END}"
    )


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _marker_line(text: str, marker: str) -> tuple[int, int] | None:
    """Offsets of the first line that is exactly `marker`, outside fenced code blocks.

    A marker mentioned in prose or in inline code is not alone on its line, and a marker shown
    inside a ``` or ~~~ fence is documentation, so neither counts.
    """
    fence: str | None = None  # the opening fence run while inside a code block
    offset = 0
    for line in text.split("\n"):
        m = _FENCE.match(line.rstrip("\r"))
        if fence is not None:
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= len(fence)
                and not m.group(2).strip()
            ):
                fence = None
        elif m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = m.group(1)
        elif line.strip() == marker:
            start = offset + len(line) - len(line.lstrip())
            return start, start + len(marker)
        offset += len(line) + 1
    return None


def _replace_block(text: str, start: str, end: str, block: str) -> tuple[str, bool]:
    """Replace the marked block; `(text, False)` when neither marker line is there."""
    i, j = _marker_line(text, start), _marker_line(text, end)
    if i is None and j is None:
        return text, False
    if i is None or j is None or j[0] < i[0]:
        raise BrandError(f"README has a broken {start} / {end} pair; fix or remove it")
    return text[: i[0]] + block + text[j[1] :], True


def rewrite_readme(text: str, header: str, footer: str) -> str:
    """Rewrite the header and footer blocks; add a missing one at the very top or very end.

    Nothing outside the two blocks is touched.
    """
    text, found = _replace_block(text, HEADER_START, HEADER_END, header)
    if not found:
        text = f"{header}\n\n{text}" if text else f"{header}\n"
    text, found = _replace_block(text, FOOTER_START, FOOTER_END, footer)
    if not found:
        text = f"{text.rstrip(chr(10))}\n\n{footer}\n" if text.strip() else f"{footer}\n"
    return text


@dataclass(frozen=True)
class BrandResult:
    """What branding a clone does (or did): files written, files removed, README changes."""

    written: list[str]
    removed: list[str]
    readme: list[str]


def brand_clone(clone: Path, name: str, description: str, *, execute: bool) -> BrandResult:
    """Write the header SVGs into the clone and rewrite the README blocks.

    Without `execute` nothing is written. Older `header-*` files in assets/brand are removed.
    """
    digest = brand_hash(name, description)
    keep = {f"{HEADER_PREFIX}{digest}-{theme}.svg" for theme in THEME_NAMES}
    folder = clone / BRAND_DIR
    stale = (
        sorted(p.name for p in folder.glob(f"{HEADER_PREFIX}*") if p.name not in keep)
        if folder.is_dir()
        else []
    )
    written = [str(BRAND_DIR / f) for f in sorted(keep)]
    removed = [str(BRAND_DIR / f) for f in stale]

    readme_path = clone / "README.md"
    notes: list[str] = []
    new_text: str | None = None
    if readme_path.is_file():
        old = readme_path.read_text(encoding="utf-8")
        new_text = rewrite_readme(old, header_block(name, description, digest), footer_block())
        has = (
            _marker_line(old, HEADER_START) is not None,
            _marker_line(old, FOOTER_START) is not None,
        )
        notes.append(
            f"README.md: {'replace' if has[0] else 'insert at the top'} the header block, "
            f"{'replace' if has[1] else 'append at the end'} the footer block"
        )
        if new_text == old:
            new_text = None
            notes = ["README.md: already up to date"]
    elif any(p.name.upper().startswith("README") for p in clone.iterdir()):
        notes.append("README.md: none (another README file exists), left alone")
    else:
        new_text = rewrite_readme("", header_block(name, description, digest), footer_block())
        notes.append("README.md: create it with the header and footer blocks")

    if execute:
        folder.mkdir(parents=True, exist_ok=True)
        for theme in THEME_NAMES:
            svg = header_svg(name, description, theme)
            (folder / f"{HEADER_PREFIX}{digest}-{theme}.svg").write_text(svg, encoding="utf-8")
        for filename in stale:
            (folder / filename).unlink()
        if new_text is not None:
            readme_path.write_text(new_text, encoding="utf-8")
    return BrandResult(written, removed, notes)
