#!/usr/bin/env python3
"""Render the profile README cards as animated SVGs.

    python scripts/render_cards.py            -> cards/*.svg        (animated)
    STATIC=1 python scripts/render_cards.py   -> cards-static/*.svg (final frame)
                                                 + preview.html     (README layout)

The cards are displayed through <img> tags, so everything here works without
scripts or network access: an inline <style> block with CSS keyframes, SMIL
<animate> for purely decorative loops, and fonts subset with pyftsubset
(fontTools.subset) and embedded as base64 WOFF data URIs.

Motion rules
  * Every entrance animation uses `from` keyframes only, so the element's
    natural (un-animated) state IS the final frame.
  * Entrance delays are staggered per card (CARD_DELAY) and per element, so the
    page plays top to bottom.
  * prefers-reduced-motion turns all CSS animation off and hides the SMIL group.
  * STATIC=1 leaves out the keyframes and SMIL completely.

Data: data/contributions.json, refreshed daily by scripts/fetch_data.py.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC", "") == "1"
OUT_DIR = ROOT / ("cards-static" if STATIC else "cards")
DATA_FILE = ROOT / "data" / "contributions.json"
FONT_DIR = ROOT / "fonts"

# --------------------------------------------------------------------------
# Brand. Everything visual comes from this block, so retheming is one edit.
# (Taken from the existing README: coral #FF6B6B accent, green status colour,
#  Fira Code for terminal bits. Inter is the text face.)
# --------------------------------------------------------------------------
class T:
    bg = "#0d1117"
    panel = "#12161d"
    chip = "#171c25"
    border = "#262c37"
    text = "#eceef2"
    muted = "#8b93a1"
    faint = "#3d4452"
    brand = "#FF6B6B"
    green = "#6BCB77"
    cell0 = "#1a1f28"          # empty contribution cell


FONTS = {  # (css family, weight) -> file in fonts/
    ("inter", 500): "inter-latin-500-normal.woff",
    ("inter", 800): "inter-latin-800-normal.woff",
    ("fira", 400): "fira-code-latin-400-normal.woff",
    ("fira", 600): "fira-code-latin-600-normal.woff",
}
SANS5, SANS8 = ("inter", 500), ("inter", 800)
MONO4, MONO6 = ("fira", 400), ("fira", 600)
FALLBACK = {"inter": "system-ui, -apple-system, Segoe UI, sans-serif",
            "fira": "ui-monospace, SFMono-Regular, Menlo, monospace"}

# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------
NAME = ("Gaurav", "Debnath")
HANDLE = "L3NNX"
ONE_LINER = "Full-stack developer building robust, scalable web apps from Guwahati, India."
FLOW = ["Think", "Build", "Ship"]
HERO_CHIPS = ["React", "Next.js", "Node.js", "Spring Boot", "PostgreSQL", "MongoDB"]
STATUS = "Open to collaborate"

TILES = [  # slug, label, sub, monogram
    ("linkedin", "LinkedIn", "Let's connect", "in"),
    ("discord", "Discord", "Join the server", "dc"),
    ("email", "Email", "gauravboy1122@gmail.com", "@"),
]

# Extra, hand-written facts per featured repo (from the resume). GitHub data
# (description, language, stars) overrides these when present in the JSON.
REPO_META = {
    "LUXE": dict(
        description="Full-stack e-commerce platform with JWT auth, role-based access and atomic order transactions.",
        language="TypeScript",
        stack=["React 19", "TypeScript", "Express", "Prisma", "PostgreSQL"]),
    "CordBin": dict(
        description="Encrypted file storage built on Discord: AES-256-GCM per-file keys, chunked uploads, token sharing.",
        language=None,
        stack=["React", "Express", "MongoDB", "Discord.js"]),
    "Ephem": dict(
        description="Self-destructing file sharing with GridFS streaming, TTL indexes and rate limiting.",
        language="JavaScript",
        stack=["React", "Vite", "Express", "MongoDB GridFS"]),
}
LANG_COLORS = {"TypeScript": "#3178c6", "JavaScript": "#f1e05a", "Java": "#b07219",
               "Python": "#3572A5", "CSS": "#663399", "HTML": "#e34c26", "EJS": "#a91e50"}

# Card order == page order; delays are seconds from image load.
CARD_DELAY = {"hero": 0.0, "tiles": 0.75, "activity": 1.35, "repo": 2.1, "graph": 3.5}
REPO_STEP = 0.35

W = 880  # width of full-width cards (viewBox units)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def esc(s) -> str:
    return html.escape(str(s), quote=True)


def n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def mix(a: str, b: str, t: float) -> str:
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


@lru_cache(None)
def _metrics(font):
    f = TTFont(FONT_DIR / FONTS[font])
    return f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm


def text_w(s: str, font, size: float, ls: float = 0.0) -> float:
    cmap, hmtx, upm = _metrics(font)
    total = 0
    for ch in s:
        g = cmap.get(ord(ch))
        total += hmtx[g][0] if g else upm * 0.5
    return total / upm * size + ls * len(s)


def fit_size(s, font, size, max_w, min_size=13):
    while size > min_size and text_w(s, font, size) > max_w:
        size -= 0.5
    return size


def wrap(s, font, size, max_w, max_lines):
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and text_w(trial, font, size) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and text_w(last + "…", font, size) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


@lru_cache(None)
def subset_woff_b64(font, chars: str) -> str:
    """Subset a font to `chars` with pyftsubset and return base64 WOFF."""
    src = FONT_DIR / FONTS[font]
    with tempfile.TemporaryDirectory() as td:
        txt, out = Path(td) / "chars.txt", Path(td) / "out.woff"
        txt.write_text(chars, encoding="utf-8")
        features = "kern" if font[0] == "inter" else ""
        subprocess.run(
            [sys.executable, "-m", "fontTools.subset", str(src),  # == pyftsubset
             f"--text-file={txt}", "--flavor=woff", f"--output-file={out}",
             f"--layout-features={features}", "--no-hinting", "--name-IDs="],
            check=True, capture_output=True)
        return base64.b64encode(out.read_bytes()).decode()


# --------------------------------------------------------------------------
# Card: collects markup + the glyphs each font needs, then assembles the SVG
# --------------------------------------------------------------------------
ANIM_CSS = """
@keyframes up{from{opacity:0;transform:translateY(12px)}}
@keyframes fade{from{opacity:0}}
@keyframes grow{from{transform:scaleY(0)}}
@keyframes pop{from{opacity:0;transform:scale(.6)}}
@keyframes type{from{clip-path:inset(0 100% 0 0)}}
@keyframes col{from{opacity:0;transform:translateY(8px)}}
.up,.fade,.grow,.pop,.type,.col{animation-fill-mode:both;animation-duration:.7s;animation-timing-function:cubic-bezier(.2,.7,.2,1)}
.up{animation-name:up}
.fade{animation-name:fade}
.grow{animation-name:grow;animation-duration:.9s;transform-box:fill-box;transform-origin:50% 100%}
.pop{animation-name:pop;animation-duration:.5s;transform-box:fill-box;transform-origin:50% 50%}
.type{animation-name:type;animation-duration:1s;animation-timing-function:steps(17,end)}
.col{animation-name:col;animation-duration:.45s}
@media (prefers-reduced-motion:reduce){.up,.fade,.grow,.pop,.type,.col{animation:none!important}.smil{display:none}}
"""


class Card:
    def __init__(self, name, w, h, base, label):
        self.name, self.w, self.h, self.base, self.label = name, w, h, base, label
        self.defs, self.body, self.smil = [], [], []
        self.used = defaultdict(set)

    # animation attributes: empty in STATIC mode so the markup is the final frame
    def a(self, cls, delay=0.0):
        if STATIC or not cls:
            return ""
        return f' class="{cls}" style="animation-delay:{n(self.base + delay)}s"'

    def _use(self, font, s):
        cmap = _metrics(font)[0]
        for ch in s:
            if ord(ch) not in cmap:
                print(f"  warning: {font} has no glyph for {ch!r} ({self.name})", file=sys.stderr)
        self.used[font].update(s)

    def text(self, x, y, s, font, size, fill, anim="", delay=0.0, anchor="start", ls=0.0, extra=""):
        return self.runs(x, y, [(s, fill)], font, size, anim, delay, anchor, ls, extra)

    def runs(self, x, y, runs, font, size, anim="", delay=0.0, anchor="start", ls=0.0, extra=""):
        fam, wt = font
        attrs = (f'x="{n(x)}" y="{n(y)}" font-family="{fam}, {FALLBACK[fam]}" '
                 f'font-weight="{wt}" font-size="{n(size)}"')
        if anchor != "start":
            attrs += f' text-anchor="{anchor}"'
        if ls:
            attrs += f' letter-spacing="{n(ls)}"'
        inner = ""
        for s, fill in runs:
            self._use(font, s)
            inner += f'<tspan fill="{fill}">{esc(s)}</tspan>'
        return f'<text {attrs}{self.a(anim, delay)} {extra}>{inner}</text>'

    def add(self, s):
        self.body.append(s)

    def svg(self) -> str:
        faces = ""
        for font in sorted(self.used):
            chars = "".join(sorted(self.used[font] | {" "}))
            b64 = subset_woff_b64(font, chars)
            faces += (f"@font-face{{font-family:{font[0]};font-weight:{font[1]};"
                      f"src:url(data:font/woff;base64,{b64}) format('woff')}}\n")
        css = faces + ("" if STATIC else ANIM_CSS)
        smil = "" if STATIC or not self.smil else f'<g class="smil">{"".join(self.smil)}</g>'
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{esc(self.label)}">\n'
            f'<title>{esc(self.label)}</title>\n<style>{css}</style>\n'
            f'<defs>{"".join(self.defs)}</defs>\n{"".join(self.body)}\n{smil}\n</svg>\n')


def card_bg(c, rx=18):
    c.add(f'<rect x=".5" y=".5" width="{c.w - 1}" height="{c.h - 1}" rx="{rx}" '
          f'fill="{T.panel}" stroke="{T.border}"/>')


def arrow_ne(x, y, size=12, color=None, width=2):
    """Diagonal 'open link' arrow, top-left corner at (x, y)."""
    color = color or T.muted
    return (f'<path d="M{n(x)} {n(y + size)} L{n(x + size)} {n(y)} M{n(x + size * .15)} {n(y)} '
            f'H{n(x + size)} V{n(y + size * .85)}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>')


def arrow_e(x, y, length=16, color=None):
    color = color or T.faint
    return (f'<path d="M{n(x)} {n(y)} H{n(x + length)} M{n(x + length - 5)} {n(y - 5)} '
            f'L{n(x + length)} {n(y)} L{n(x + length - 5)} {n(y + 5)}" fill="none" '
            f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')


def star_path(cx, cy, r=6):
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.42
        pts.append(f"{n(cx + rad * math.cos(ang))},{n(cy + rad * math.sin(ang))}")
    return f'<polygon points="{" ".join(pts)}" fill="{T.muted}"/>'


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def load_data():
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    days = sorted(((dt.date.fromisoformat(d["date"]), int(d["count"])) for d in data["days"]))
    if data.get("sample"):
        print("  note: data/contributions.json is SAMPLE data; run scripts/fetch_data.py "
              "(or let the Action run) for real numbers.", file=sys.stderr)
    return data, days


def streaks(days):
    counts = [c for _, c in days]
    best = run = 0
    for c in counts:
        run = run + 1 if c > 0 else 0
        best = max(best, run)
    i = len(counts) - 1
    if i >= 0 and counts[i] == 0:  # today may simply not have happened yet
        i -= 1
    cur = 0
    while i >= 0 and counts[i] > 0:
        cur += 1
        i -= 1
    return cur, best


def month_totals(days):
    od = OrderedDict()
    for d, c in days:
        od[(d.year, d.month)] = od.get((d.year, d.month), 0) + c
    out = []
    for idx, ((y, m), v) in enumerate(od.items()):
        name = dt.date(y, m, 1).strftime("%b")
        if idx == 0 or m == 1:
            name += f" ’{str(y)[2:]}"
        out.append((name, v))
    return out


def nice_max(m):
    if m <= 0:
        return 10
    mag = 10 ** int(math.floor(math.log10(m)))
    for f in (1, 1.5, 2, 3, 4, 5, 6, 8, 10):
        if f * mag >= m:
            return f * mag
    return 10 * mag


def fmt_day(d):
    return f"{d:%b} {d.day}"


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------
def load_portrait():
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = ROOT / "assets" / f"portrait.{ext}"
        if p.exists():
            try:
                from PIL import Image
                im = Image.open(p).convert("RGB")
                s = min(im.size)
                l, t = (im.width - s) // 2, (im.height - s) // 3  # bias towards the top (faces)
                im = im.crop((l, max(0, t), l + s, max(0, t) + s)).resize((320, 320))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=82)
                return "image/jpeg", base64.b64encode(buf.getvalue()).decode()
            except ImportError:
                mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "webp": "image/webp"}[ext]
                return mime, base64.b64encode(p.read_bytes()).decode()
    return None


def load_ascii_art():
    """Convert assets/ascii-art.txt into a PNG image and return (mime, base64).

    Each character is mapped to a brightness value; the result is a grayscale
    PNG rendered with Pillow.  Returns None if the file is missing or Pillow is
    not available.
    """
    p = ROOT / "assets" / "ascii-art.txt"
    if not p.exists():
        return None
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None

    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return None

    # Brightness map: '#' darkest → ' ' lightest (matches typical ASCII art)
    BRIGHT = {c: v for c, v in zip(
        "@%#*+=-:. ",
        [0, 20, 35, 60, 90, 120, 150, 180, 210, 240]
    )}

    cols = max(len(l) for l in lines)
    rows = len(lines)

    img = PILImage.new("L", (cols, rows), 240)
    px = img.load()
    for y, line in enumerate(lines):
        for x, ch in enumerate(line):
            px[x, y] = BRIGHT.get(ch, 128)

    # Scale up so it looks good at card dimensions (144 × 144 display px)
    OUT_SIZE = 288
    img = img.resize((OUT_SIZE, OUT_SIZE), PILImage.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return "image/png", base64.b64encode(buf.getvalue()).decode()


def build_hero():
    h = 300
    c = Card("hero", W, h, CARD_DELAY["hero"], f"{' '.join(NAME)}, full-stack developer. Think, build, ship.")
    portrait = load_portrait()
    ascii_art = None if portrait else load_ascii_art()
    has_right = portrait or ascii_art
    right = 650 if has_right else 832
    c.defs.append(
        f'<clipPath id="card"><rect width="{W}" height="{h}" rx="18"/></clipPath>'
        f'<linearGradient id="sweep" x1="0" x2="1"><stop offset="0" stop-color="{T.brand}" stop-opacity="0"/>'
        f'<stop offset=".5" stop-color="{T.brand}"/><stop offset="1" stop-color="{T.brand}" stop-opacity="0"/></linearGradient>'
        f'<radialGradient id="glow"><stop offset="0" stop-color="{T.brand}" stop-opacity=".26"/>'
        f'<stop offset="1" stop-color="{T.brand}" stop-opacity="0"/></radialGradient>'
        f'<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1" fill="{T.faint}"/></pattern>'
        f'<linearGradient id="fadeL" x1="0" x2="1"><stop offset="0" stop-color="{T.panel}"/>'
        f'<stop offset=".5" stop-color="{T.panel}"/><stop offset="1" stop-color="{T.panel}" stop-opacity="0"/></linearGradient>')
    card_bg(c)
    c.add('<g clip-path="url(#card)">'
          f'<rect width="{W}" height="{h}" fill="url(#dots)" opacity=".55"/>'
          f'<rect width="{W}" height="{h}" fill="url(#fadeL)"/>'
          f'<circle cx="800" cy="40" r="260" fill="url(#glow)"/></g>')

    # terminal line (typed in)
    term = [("~/", T.muted), (HANDLE, T.muted), (" $ ", T.brand), ("whoami", T.text)]
    term_s = "".join(s for s, _ in term)
    c.add(f'<g{c.a("type", 0.15)}>{c.runs(48, 54, term, MONO4, 14)}</g>')
    caret_x = 48 + text_w(term_s, MONO4, 14) + 4
    c.smil.append(
        f'<rect x="{n(caret_x)}" y="41" width="8" height="17" fill="{T.brand}" opacity="0">'
        f'<animate attributeName="opacity" values="1;1;0;0" keyTimes="0;.5;.5;1" dur="1.1s" begin="1.1s" repeatCount="indefinite"/></rect>')

    # status pill
    pill_w = text_w(STATUS, MONO4, 12) + 44
    px = 760 - pill_w / 2 if has_right else 832 - pill_w
    py = 30
    c.add(f'<g{c.a("fade", 0.9)}><rect x="{n(px)}" y="{py}" width="{n(pill_w)}" height="28" rx="14" '
          f'fill="{T.chip}" stroke="{T.border}"/>'
          f'<circle cx="{n(px + 18)}" cy="{py + 14}" r="4" fill="{T.green}"/>'
          f'{c.text(px + 30, py + 18.5, STATUS, MONO4, 12, T.muted)}</g>')
    c.smil.append(
        f'<circle cx="{n(px + 18)}" cy="{py + 14}" r="4" fill="none" stroke="{T.green}" stroke-width="1.5" opacity="0">'
        f'<animate attributeName="r" values="4;11" dur="2.2s" begin="1.4s" repeatCount="indefinite"/>'
        f'<animate attributeName="opacity" values=".7;0" dur="2.2s" begin="1.4s" repeatCount="indefinite"/></circle>')

    # name + one-liner
    nm = f"{NAME[0]} {NAME[1]}"
    size = fit_size(nm, SANS8, 60, right - 48)
    c.add(c.runs(46, 130, [(NAME[0] + " ", T.text), (NAME[1], T.brand)], SANS8, size, "up", 0.3, ls=-1.5))
    tag_size = fit_size(ONE_LINER, SANS5, 19, right - 48)
    c.add(c.text(48, 170, ONE_LINER, SANS5, tag_size, T.muted, "up", 0.5))

    # Think -> Build -> Ship
    x, y = 48, 214
    for i, word in enumerate(FLOW):
        c.add(c.text(x, y, word, MONO6, 16, T.brand, "up", 0.65 + i * 0.12))
        x += text_w(word, MONO6, 16) + 12
        if i < len(FLOW) - 1:
            c.add(f'<g{c.a("fade", 0.7 + i * 0.12)}>{arrow_e(x, y - 5.5, 18)}</g>')
            x += 18 + 12

    # stack chips
    x, cy = 48, 240
    for i, chip in enumerate(HERO_CHIPS):
        w = text_w(chip, MONO4, 12) + 26
        if x + w > right:
            break
        c.add(f'<g{c.a("pop", 0.95 + i * 0.07)}><rect x="{n(x)}" y="{cy}" width="{n(w)}" height="28" rx="14" '
              f'fill="{T.chip}" stroke="{T.border}"/>{c.text(x + 13, cy + 18.5, chip, MONO4, 12, T.text)}</g>')
        x += w + 8

    if portrait:
        mime, b64 = portrait
        cx, cy2, r = 760, 176, 72
        c.defs.append(f'<clipPath id="pc"><circle cx="{cx}" cy="{cy2}" r="{r}"/></clipPath>')
        c.add(f'<g{c.a("pop", 0.4)}><circle cx="{cx}" cy="{cy2}" r="{r + 8}" fill="none" stroke="{T.border}" stroke-width="2"/>'
              f'<image href="data:{mime};base64,{b64}" x="{cx - r}" y="{cy2 - r}" width="{2 * r}" height="{2 * r}" '
              f'clip-path="url(#pc)" preserveAspectRatio="xMidYMid slice"/></g>')
        c.smil.append(
            f'<circle cx="{cx}" cy="{cy2}" r="{r + 8}" fill="none" stroke="{T.brand}" stroke-width="2" '
            f'stroke-dasharray="40 412" stroke-linecap="round" opacity="0">'
            f'<set attributeName="opacity" to="1" begin="1s"/><animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy2}" to="360 {cx} {cy2}" dur="14s" repeatCount="indefinite"/></circle>')
    elif ascii_art:
        mime, b64 = ascii_art
        # Render ASCII art in a rounded rect on the right, with a subtle brand tint overlay
        ax, ay, aw, ah = 672, 18, 180, 264
        c.defs.append(f'<clipPath id="ac"><rect x="{ax}" y="{ay}" width="{aw}" height="{ah}" rx="12"/></clipPath>')
        c.add(f'<g{c.a("pop", 0.4)}>'
              f'<rect x="{ax}" y="{ay}" width="{aw}" height="{ah}" rx="12" fill="{T.chip}" stroke="{T.border}"/>'
              f'<image href="data:{mime};base64,{b64}" x="{ax}" y="{ay}" width="{aw}" height="{ah}" '
              f'clip-path="url(#ac)" preserveAspectRatio="xMidYMid slice"/>'
              # brand colour overlay at low opacity for the signature coral tint
              f'<rect x="{ax}" y="{ay}" width="{aw}" height="{ah}" rx="12" '
              f'fill="{T.brand}" opacity=".08"/>'
              f'</g>')

    # decorative light sweep along the top edge
    c.smil.append(
        f'<g clip-path="url(#card)"><rect x="-240" y="0" width="240" height="2" fill="url(#sweep)">'
        f'<animate attributeName="x" from="-240" to="{W}" dur="7s" begin="1.5s" repeatCount="indefinite"/></rect></g>')
    return c


def build_tile(i, slug, label, sub, mono):
    w, h = 290, 128
    c = Card(f"tile-{slug}", w, h, CARD_DELAY["tiles"] + i * 0.12, f"{label}: {sub}")
    card_bg(c, 16)
    c.add(f'<g{c.a("up", 0)}>'
          f'<rect x="22" y="22" width="40" height="40" rx="11" fill="{T.brand}" fill-opacity=".14"/>'
          f'{c.text(42, 47.8, mono, MONO6, 16, T.brand, anchor="middle")}'
          f'{arrow_ne(254, 24, 12, T.muted)}'
          f'{c.text(22, 96, label, SANS8, 19, T.text)}'
          f'{c.text(22, 115, sub, MONO4, 12, T.muted)}</g>')
    c.smil.append(
        f'<rect x="22" y="70" width="20" height="2" rx="1" fill="{T.brand}" opacity="0">'
        f'<set attributeName="opacity" to="1" begin="{n(CARD_DELAY["tiles"] + i * 0.12 + 0.8)}s"/>'
        f'<animate attributeName="width" values="20;56;20" dur="3.6s" begin="{n(2.5 + i * 0.4)}s" repeatCount="indefinite"/></rect>')
    return c


def build_activity(data, days):
    h = 330
    c = Card("activity", W, h, CARD_DELAY["activity"], "Contributions per month, with streak, active days and busiest day")
    card_bg(c)
    cur, best = streaks(days)
    active = sum(1 for _, v in days if v > 0)
    peak_d, peak_v = max(days, key=lambda t: (t[1], t[0]))
    total = data.get("total") or sum(v for _, v in days)
    months = month_totals(days)
    c.defs.append(
        f'<linearGradient id="bar" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{T.brand}" stop-opacity=".9"/>'
        f'<stop offset="1" stop-color="{T.brand}" stop-opacity=".35"/></linearGradient>')

    c.add(c.text(32, 52, "Contributions per month", SANS8, 22, T.text, "up", 0))
    c.add(c.text(32, 75, f"{total:,} contributions · {dt.date.today().year}", MONO4, 12, T.muted, "up", 0.1))

    stats = [
        (f"{cur}", "DAY STREAK", f"longest {best} days", T.brand),
        (f"{active}", "ACTIVE DAYS", f"of {len(days)} ({round(100 * active / len(days))}%)", T.text),
        (f"{peak_v}", "BUSIEST DAY", fmt_day(peak_d), T.text),
    ]
    for i, (num, lab, sub, col) in enumerate(stats):
        x = 408 + i * 148
        c.add(c.text(x, 58, num, SANS8, 34, col, "up", 0.15 + i * 0.1))
        c.add(c.text(x, 80, lab, MONO6, 11, T.muted, "up", 0.2 + i * 0.1, ls=0.8))
        c.add(c.text(x, 97, sub, MONO4, 11, T.muted, "up", 0.25 + i * 0.1))

    top, base_y = 146, 284
    top_h = base_y - top
    nm = nice_max(max(v for _, v in months))
    for frac in (0, 0.5, 1):
        y = base_y - top_h * frac
        dash = "" if frac == 0 else ' stroke-dasharray="3 6"'
        c.add(f'<line x1="32" y1="{n(y)}" x2="{W - 32}" y2="{n(y)}" stroke="{T.border}"{dash}/>')
    c.add(c.text(W - 32, top - 6, f"{n(nm)}", MONO4, 10, T.muted, anchor="end"))

    slot = (W - 64) / len(months)
    bw = min(40.0, slot * 0.58)
    peak_idx = max(range(len(months)), key=lambda i: (months[i][1], i))
    for i, (label, v) in enumerate(months):
        cx = 32 + slot * (i + 0.5)
        bh = max(3.0, v / nm * top_h) if v > 0 else 2.0
        hot = i == peak_idx
        fill = T.brand if hot else ("url(#bar)" if v > 0 else T.faint)
        d = 0.45 + i * 0.05
        c.add(f'<rect x="{n(cx - bw / 2)}" y="{n(base_y - bh)}" width="{n(bw)}" height="{n(bh)}" '
              f'rx="{n(min(6, bw / 2))}" fill="{fill}"{c.a("grow", d)}/>')
        c.add(c.text(cx, base_y - bh - 9, str(v), MONO6 if hot else MONO4, 12,
                     T.brand if hot else T.muted, "fade", d + 0.5, anchor="middle"))
        c.add(c.text(cx, base_y + 24, label, MONO4, 11, T.text if hot else T.muted, "fade", d + 0.2, anchor="middle"))
    return c


def build_repo(i, repo):
    h = 150
    name = repo["name"]
    meta = REPO_META.get(name, {})
    c = Card(f"repo-{name.lower()}", W, h, CARD_DELAY["repo"] + i * REPO_STEP, f"{HANDLE}/{name} on GitHub")
    card_bg(c)
    c.add(f'<rect x="0" y="26" width="4" height="98" rx="2" fill="{T.brand}"{c.a("grow", 0)}/>')
    # repo glyph
    c.add(f'<g{c.a("fade", 0.1)}><path d="M34 26 a3 3 0 0 1 3-3 h14 v19 h-14 a3 3 0 0 0 -3 3 z M34 45 a3 3 0 0 1 3-3" '
          f'fill="none" stroke="{T.muted}" stroke-width="1.8" stroke-linejoin="round"/></g>')
    owner = repo.get("owner", HANDLE) + " /"
    c.add(c.text(62, 42, owner, MONO4, 13, T.muted, "up", 0.1))
    nx = 62 + text_w(owner, MONO4, 13) + 8
    c.add(c.text(nx, 43, name, SANS8, 23, T.text, "up", 0.15, ls=-0.4))
    c.add(f'<g{c.a("fade", 0.3)}>{arrow_ne(W - 44, 28, 13, T.brand)}</g>')

    desc = repo.get("description") or meta.get("description") or ""
    lines = wrap(desc, SANS5, 15, 700, 2) if desc else []
    for k, line in enumerate(lines):
        c.add(c.text(62, 76 + k * 22, line, SANS5, 15, T.muted, "up", 0.25 + k * 0.08))

    # meta row: language, stars, stack chips (right aligned)
    my, x = 128, 62
    lang = repo.get("language") or meta.get("language")
    if lang:
        col = repo.get("language_color") or LANG_COLORS.get(lang, T.muted)
        c.add(f'<g{c.a("fade", 0.5)}><circle cx="{x + 5}" cy="{my - 4}" r="5" fill="{col}"/>'
              f'{c.text(x + 17, my, lang, MONO4, 12, T.muted)}</g>')
        x += 17 + text_w(lang, MONO4, 12) + 22
    stars = repo.get("stars")
    if stars is not None:
        c.add(f'<g{c.a("fade", 0.55)}>{star_path(x + 6, my - 4, 6.5)}{c.text(x + 18, my, str(stars), MONO4, 12, T.muted)}</g>')
        x += 18 + text_w(str(stars), MONO4, 12) + 22
    chips = list(meta.get("stack", []))
    widths = [text_w(t, MONO4, 11) + 20 for t in chips]
    while chips and x + sum(widths) + 8 * (len(chips) - 1) > W - 32:
        chips.pop()
        widths.pop()
    cx = W - 32 - (sum(widths) + 8 * (len(chips) - 1)) if chips else 0
    for k, (t, w) in enumerate(zip(chips, widths)):
        c.add(f'<g{c.a("pop", 0.6 + k * 0.06)}><rect x="{n(cx)}" y="{my - 17}" width="{n(w)}" height="24" rx="12" '
              f'fill="{T.chip}" stroke="{T.border}"/>{c.text(cx + 10, my - 0.5, t, MONO4, 11, T.text)}</g>')
        cx += w + 8
    return c


def build_graph(data, days):
    h = 228
    c = Card("graph", W, h, CARD_DELAY["graph"], "Contribution graph for this year")
    card_bg(c)
    total = data.get("total") or sum(v for _, v in days)
    c.add(c.text(28, 44, "Contribution graph", SANS8, 20, T.text, "up", 0))
    c.add(c.text(W - 28, 44, f"{total:,} contributions in {dt.date.today().year}", MONO4, 12, T.muted, "up", 0.1, anchor="end"))

    # Pad days forward to Dec 31 of current year (future days = 0 contributions)
    year = dt.date.today().year
    end_of_year = dt.date(year, 12, 31)
    if days:
        last_date = days[-1][0]
        pad = last_date + dt.timedelta(days=1)
        while pad <= end_of_year:
            days = list(days) + [(pad, 0)]
            pad += dt.timedelta(days=1)

    # weeks start on Sunday (GitHub style)
    first = days[0][0]
    offset = (first.weekday() + 1) % 7
    cols = math.ceil((offset + len(days)) / 7)
    x0, y0, gutter = 60, 92, 3
    pitch = min(15.0, (W - x0 - 28) / cols)
    cell = pitch - gutter

    nz = sorted(v for _, v in days if v > 0)
    q = lambda p: nz[int(p * (len(nz) - 1))] if nz else 0
    t1, t2, t3 = q(.25), q(.5), q(.75)
    shades = [T.cell0] + [mix(T.cell0, T.brand, t) for t in (.32, .58, .8, 1.0)]

    def level(v):
        return 0 if v == 0 else 1 if v <= t1 else 2 if v <= t2 else 3 if v <= t3 else 4

    colgroups = defaultdict(list)
    month_marks = []
    for k, (d, v) in enumerate(days):
        idx = offset + k
        col, row = idx // 7, idx % 7
        colgroups[col].append(
            f'<rect x="{n(x0 + col * pitch)}" y="{n(y0 + row * pitch)}" width="{n(cell)}" height="{n(cell)}" '
            f'rx="2.5" fill="{shades[level(v)]}"/>')
        if d.day <= 7 and row == 0 or (k == 0):
            if not month_marks or month_marks[-1][1] != d.month:
                month_marks.append((col, d.month))
    # drop a first label that would collide with the next one
    if len(month_marks) > 1 and month_marks[1][0] - month_marks[0][0] < 3:
        month_marks.pop(0)
    for col, m in month_marks:
        c.add(c.text(x0 + col * pitch, y0 - 10, dt.date(2000, m, 1).strftime("%b"), MONO4, 10, T.muted, "fade", 0.2 + col * 0.012))
    for row, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.add(c.text(28, y0 + row * pitch + cell - 1.5, lab, MONO4, 10, T.muted, "fade", 0.2))

    for col in range(cols):
        c.add(f'<g{c.a("col", 0.25 + col * 0.014)}>{"".join(colgroups[col])}</g>')

    # legend
    ly = y0 + 7 * pitch + 16
    lx = W - 28
    c.add(c.text(lx, ly + 8.5, "More", MONO4, 10, T.muted, "fade", 1.2, anchor="end"))
    lx -= text_w("More", MONO4, 10) + 8
    for lv in range(4, -1, -1):
        lx -= 14
        c.add(f'<rect x="{n(lx)}" y="{n(ly)}" width="11" height="11" rx="2.5" fill="{shades[lv]}"{c.a("fade", 1.2)}/>')
        lx -= 3
    c.add(c.text(lx - 5, ly + 8.5, "Less", MONO4, 10, T.muted, "fade", 1.2, anchor="end"))

    # decorative scan line over the grid
    gx, gw = x0, cols * pitch
    c.defs.append(
        f'<linearGradient id="scan" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
        f'<stop offset=".5" stop-color="#fff" stop-opacity=".14"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
        f'<clipPath id="gridclip"><rect x="{gx}" y="{y0 - 2}" width="{n(gw)}" height="{n(7 * pitch + 2)}"/></clipPath>')
    c.smil.append(
        f'<g clip-path="url(#gridclip)"><rect x="{gx - 80}" y="{y0 - 2}" width="80" height="{n(7 * pitch + 2)}" fill="url(#scan)">'
        f'<animate attributeName="x" from="{gx - 80}" to="{n(gx + gw)}" dur="6s" begin="4.5s" repeatCount="indefinite"/></rect></g>')
    return c


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def validate(name, svg):
    ET.fromstring(svg)  # well-formed
    if re.search(r"<script|<foreignObject|@import", svg, re.I):
        raise SystemExit(f"{name}: scripts/foreignObject/@import don't work in <img> SVG")
    if re.search(r'(?:href|src)\s*=\s*"(?!data:|#)', svg) or re.search(r"url\((?!data:|#|['\"]#)", svg):
        raise SystemExit(f"{name}: external reference found; <img> SVGs cannot load them")
    if len(svg) > 500_000:
        print(f"  warning: {name} is {len(svg) // 1024} KB", file=sys.stderr)


def write_preview():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").replace('"cards/', f'"{OUT_DIR.name}/')
    (ROOT / "preview.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Profile preview (static)</title>"
        f"<style>body{{margin:0;background:{T.bg};color:#e6edf3;font:16px system-ui}}"
        ".wrap{box-sizing:border-box;max-width:928px;margin:0 auto;padding:32px}"
        "img{max-width:100%;vertical-align:middle}p{margin:0 0 16px}</style>"
        f'<div class="wrap">{readme}</div>', encoding="utf-8")


def main():
    data, days = load_data()
    repos = data.get("repos") or [{"name": n_} for n_ in REPO_META]
    cards = [build_hero()]
    cards += [build_tile(i, *t) for i, t in enumerate(TILES)]
    cards.append(build_activity(data, days))
    cards += [build_repo(i, r) for i, r in enumerate(repos)]
    cards.append(build_graph(data, days))

    OUT_DIR.mkdir(exist_ok=True)
    for old in OUT_DIR.glob("*.svg"):
        old.unlink()
    for c in cards:
        svg = c.svg()
        validate(c.name, svg)
        (OUT_DIR / f"{c.name}.svg").write_text(svg, encoding="utf-8")
        print(f"  {OUT_DIR.name}/{c.name}.svg  {len(svg) / 1024:.1f} KB")
    if STATIC:
        write_preview()
        print("STATIC=1: final frames in cards-static/, open preview.html to screenshot. "
              "Don't commit these; the Action renders the animated cards/.")


if __name__ == "__main__":
    main()
