#!/usr/bin/env python3
"""Fetch Letterboxd RSS feeds, pick a review, render it for a Spectra 6 e-ink panel.

Usage:
  python renderer/render.py                      # fetch live feeds from config.json
  python renderer/render.py --feed-file x.xml    # use a local RSS file (offline testing)

Writes:
  out/frame.png    palette PNG using only the 6 Spectra colors (what the Pi downloads)
  out/frame.json   metadata about what's shown (handy for debugging)
"""
import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

# Spectra 6 colors as the panel drivers expect them.
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)
SPECTRA = [BLACK, WHITE, YELLOW, RED, BLUE, GREEN]
# How those 6 pigments actually look on the panel (measured values from Pimoroni's inky
# driver for the 13.3" Spectra 6). Posters are dithered against these so the dots mix to
# the right tones, then written out using the pure colors above.
PANEL = [(0, 0, 0), (161, 164, 165), (208, 190, 71), (156, 72, 75), (61, 59, 94), (58, 91, 70)]

NS = {
    "letterboxd": "https://letterboxd.com",
    "dc": "http://purl.org/dc/elements/1.1/",
}

FONT_DIRS = [
    ROOT / "fonts",
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
]


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        p = d / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    raise FileNotFoundError(f"Font {name} not found in {FONT_DIRS}")


# ---------- RSS parsing ----------

class _TextExtractor(HTMLParser):
    """Turns the RSS description HTML into paragraphs, grabbing the poster URL."""

    def __init__(self):
        super().__init__()
        self.paras, self.cur, self.poster = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag == "img" and not self.poster:
            self.poster = dict(attrs).get("src")
        if tag == "br":
            self.cur.append("\n")

    def handle_endtag(self, tag):
        if tag == "p":
            text = "".join(self.cur).strip()
            if text:
                self.paras.append(text)
            self.cur = []

    def handle_data(self, data):
        self.cur.append(data)


def parse_feed(xml_text: str, username: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    reviews = []
    for item in root.iter("item"):
        guid = (item.findtext("guid") or "").strip()
        link = (item.findtext("link") or "").strip()
        if "/list/" in link:
            continue

        ex = _TextExtractor()
        ex.feed(item.findtext("description") or "")
        paras = ex.paras
        spoiler = any("may contain spoilers" in p.lower() for p in paras)
        paras = [p for p in paras if "may contain spoilers" not in p.lower()]
        # Diary entries without a review just say "Watched on <date>."
        paras = [p for p in paras if not re.match(r"^Watched on ", p)]
        text = "\n\n".join(paras).strip()

        is_review = guid.startswith("letterboxd-review") or bool(text)
        if not is_review or not text:
            continue

        rating = item.findtext("letterboxd:memberRating", namespaces=NS)
        reviews.append({
            "id": guid or link,
            "username": username,
            "member": (item.findtext("dc:creator", namespaces=NS) or username).strip(),
            "film": (item.findtext("letterboxd:filmTitle", namespaces=NS) or "").strip(),
            "year": (item.findtext("letterboxd:filmYear", namespaces=NS) or "").strip(),
            "rating": float(rating) if rating else None,
            "rewatch": (item.findtext("letterboxd:rewatch", namespaces=NS) or "").lower() == "yes",
            "watched": (item.findtext("letterboxd:watchedDate", namespaces=NS) or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "liked": (item.findtext("letterboxd:memberLike", namespaces=NS) or "").lower() == "yes",
            "spoiler": spoiler,
            "text": html.unescape(text),
            "poster": ex.poster,
        })
    return reviews


def clean_username(cell: str) -> str | None:
    """Accepts 'name', '@name', 'letterboxd.com/name/', 'https://boxd.it/...'-style junk."""
    s = cell.strip().strip('"').strip()
    if not s or s.startswith("#"):
        return None
    m = re.search(r"letterboxd\.com/([A-Za-z0-9_]+)", s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip("/ ").lower()
    if not re.fullmatch(r"[a-z0-9_]{2,}", s):
        return None
    if s in {"username", "usernames", "letterboxd", "name", "user"}:  # header rows
        return None
    return s


def usernames_from_csv(text: str) -> list[str]:
    import csv
    names = []
    for row in csv.reader(io.StringIO(text)):
        if row:
            u = clean_username(row[0])
            if u and u not in names:
                names.append(u)
    return names


def load_usernames(cfg: dict) -> list[str]:
    url = cfg.get("usernames_csv_url")
    if url:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            names = usernames_from_csv(r.text)
            if names:
                print(f"{len(names)} usernames from sheet", file=sys.stderr)
                return names
            print("sheet had no usable usernames, using config.json", file=sys.stderr)
        except Exception as e:
            print(f"sheet fetch failed ({e}), using config.json", file=sys.stderr)
    return cfg.get("usernames", [])


def fetch_feed(username: str) -> str:
    r = requests.get(
        f"https://letterboxd.com/{username}/rss/",
        headers={"User-Agent": "letterboxd-frame/1.0 (personal e-ink frame)"},
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def pub_ts(review: dict) -> float:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(review["published"]).timestamp()
    except Exception:
        return 0.0


def choose(reviews: list[dict], cfg: dict, now: float) -> dict | None:
    if not reviews:
        return None
    reviews = sorted(reviews, key=pub_ts, reverse=True)
    if cfg.get("mode") == "latest":
        return reviews[0]
    # Stateless rotation: the time slot decides which of the N newest reviews to show.
    pool = reviews[: cfg.get("rotate_pool", 10)]
    slot = int(now // (cfg.get("rotate_minutes", 60) * 60))
    return pool[slot % len(pool)]


# ---------- Rendering ----------
#
# Everything except the poster is drawn into one grayscale mask per color at SCALE x
# resolution with normal anti-aliasing. At the end each mask is box-downsampled and
# thresholded, then filled with its solid Spectra color. That gives far smoother glyph
# and star edges than drawing with anti-aliasing off, while still using only the
# panel's 6 colors (no gray fringes that would turn into dither speckle).

SCALE = 3
THRESHOLD = 118  # a touch under 50% so thin serif strokes don't break up

F_BODY = "Literata-Text.ttf"
F_ITALIC = "Literata-Italic.ttf"
F_TITLE = "Literata-Display.ttf"
F_UI = "Inter-Medium.ttf"
F_UI_BOLD = "Inter-Bold.ttf"


# ---- Emoji ----
# The text fonts have no emoji, so any character they lack but Noto Emoji has is drawn from
# Noto Emoji (black-and-white line art, SIL Open Font License) instead of an empty box.
EMOJI_FONT = "NotoEmoji-Medium.ttf"
EMOJI_SCALE = 0.92  # emoji look a touch large next to text at the same size
# Invisible pieces that glue emoji together (zero-width joiner, presentation selectors,
# keycap, skin tones, flag tags). They belong to whatever emoji they follow.
_EMOJI_GLUE = {0x200D, 0xFE0F, 0xFE0E, 0x20E3} | set(range(0x1F3FB, 0x1F400)) | set(range(0xE0020, 0xE0080))
_glyph_cache = {}


def has_glyph(name: str, ch: str) -> bool:
    """True if font `name` really has a glyph for `ch` (not the empty-box fallback)."""
    key = (name, ch)
    if key not in _glyph_cache:
        try:
            f = font(name, 40)
        except FileNotFoundError:
            _glyph_cache[key] = False
            return False
        def mask(c):
            im = Image.new("L", (90, 90), 0)
            ImageDraw.Draw(im).text((10, 70), c, font=f, fill=255, anchor="ls")
            return im.tobytes()
        nd_key = (name, None)
        if nd_key not in _glyph_cache:
            _glyph_cache[nd_key] = mask("\U0010FFFD")  # unassigned: draws the empty box
        _glyph_cache[key] = mask(ch) != _glyph_cache[nd_key]
    return _glyph_cache[key]


def text_runs(text: str, name: str):
    """Split text into (segment, font name, size factor) runs, sending emoji to EMOJI_FONT."""
    runs, cur, cur_emoji = [], "", False
    for i, ch in enumerate(text):
        cp = ord(ch)
        nxt = ord(text[i + 1]) if i + 1 < len(text) else 0
        if cp in _EMOJI_GLUE:
            if not cur_emoji:
                continue  # stray selector after normal text: drop it rather than draw a box
            is_emoji = True
        elif ch.isspace() or cp < 0x80 and nxt not in (0xFE0F, 0x20E3):
            is_emoji = False
        elif nxt in (0xFE0F, 0x20E3) and has_glyph(EMOJI_FONT, ch):
            is_emoji = True  # e.g. heart + emoji selector, or a keycap digit
        else:
            is_emoji = not has_glyph(name, ch) and has_glyph(EMOJI_FONT, ch)
        if cur and is_emoji != cur_emoji:
            runs.append((cur, EMOJI_FONT if cur_emoji else name, EMOJI_SCALE if cur_emoji else 1.0))
            cur = ""
        cur += ch
        cur_emoji = is_emoji
    if cur:
        runs.append((cur, EMOJI_FONT if cur_emoji else name, EMOJI_SCALE if cur_emoji else 1.0))
    return runs


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.layers = {}   # color -> L image at SCALE x
        self._fonts = {}

    def _draw(self, color):
        if color not in self.layers:
            self.layers[color] = Image.new("L", (self.w * SCALE, self.h * SCALE), 0)
        return ImageDraw.Draw(self.layers[color])

    def font(self, name, size):
        key = (name, size)
        if key not in self._fonts:
            self._fonts[key] = font(name, int(round(size * SCALE)))
        return self._fonts[key]

    def length(self, text, name, size, tracking=0.0):
        total = 0.0
        for seg, fname, k in text_runs(text, name):
            total += self.font(fname, size * k).getlength(seg) / SCALE
        return total + tracking * size * max(len(text) - 1, 0)

    def text(self, x, baseline, text, name, size, color, tracking=0.0):
        """Draw text with its baseline at `baseline`. tracking is in ems.
        Emoji the main font lacks are drawn in black-and-white from the bundled emoji font."""
        d = self._draw(color)
        cx = x * SCALE
        for seg, fname, k in text_runs(text, name):
            f = self.font(fname, size * k)
            if not tracking:
                d.text((cx, baseline * SCALE), seg, font=f, fill=255, anchor="ls")
                cx += f.getlength(seg)
                continue
            for ch in seg:
                d.text((cx, baseline * SCALE), ch, font=f, fill=255, anchor="ls")
                cx += f.getlength(ch) + tracking * size * SCALE

    def polygon(self, pts, color):
        self._draw(color).polygon([(px * SCALE, py * SCALE) for px, py in pts], fill=255)

    def rect(self, x0, y0, x1, y1, color, radius=0):
        d = self._draw(color)
        box = (x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE)
        if radius:
            d.rounded_rectangle(box, radius=radius * SCALE, fill=255)
        else:
            d.rectangle(box, fill=255)

    def ellipse(self, x0, y0, x1, y1, color):
        self._draw(color).ellipse((x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE), fill=255)

    def flatten(self, base: Image.Image, order=None):
        order = order or list(self.layers)
        for color in order:
            if color not in self.layers:
                continue
            small = self.layers[color].resize((self.w, self.h), Image.BOX)
            mask = small.point(lambda v: 255 if v >= THRESHOLD else 0).convert("1")
            base.paste(color, (0, 0), mask)
        return base


def wrap(cv, text, name, size, max_w):
    """Returns lines; None marks a paragraph break."""
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        para = " ".join(para.split())
        if not para:
            continue
        if out:
            out.append(None)
        line = ""
        for w in para.split(" "):
            trial = f"{line} {w}".strip()
            if cv.length(trial, name, size) <= max_w or not line:
                line = trial
            else:
                out.append(line)
                line = w
        out.append(line)
    return out


def block_height(lines, size):
    lh, gap = size * 1.42, size * 0.62
    return sum(gap if l is None else lh for l in lines)


def fit_body(cv, text, max_w, max_h):
    sizes = (58, 54, 50, 46, 43, 40, 37, 34, 31, 29, 27, 25)
    for size in sizes:
        lines = wrap(cv, text, F_BODY, size, max_w)
        if block_height(lines, size) <= max_h:
            return size, lines
    # Smallest size still too long: cut it and end with an ellipsis.
    kept, h = [], 0.0
    for l in lines:
        step = size * (0.62 if l is None else 1.42)
        if h + step > max_h:
            break
        kept.append(l)
        h += step
    while kept and kept[-1] is None:
        kept.pop()
    if kept:
        last = kept[-1]
        while last and cv.length(last + " …", F_BODY, size) > max_w:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        kept[-1] = last.rstrip(" ,.;:—-") + " …"
    return size, kept


def star_points(cx, cy, r, inner=0.48):
    import math
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * inner
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def draw_heart(cv, x, cy, size, color):
    """Heart about `size` px tall, left edge at x, vertically centered on cy."""
    r = size * 0.29
    top = cy - size * 0.40
    cv.ellipse(x, top, x + 2 * r, top + 2 * r, color)
    cv.ellipse(x + 2 * r * 0.92, top, x + 2 * r * 1.92, top + 2 * r, color)
    w = 2 * r * 1.92
    cv.polygon([(x + r * 0.06, top + r * 1.25), (x + w - r * 0.06, top + r * 1.25),
                (x + w / 2, cy + size * 0.55)], color)
    return x + w


def draw_rating(cv, x, cy, rating, size=30, color=GREEN):
    """Letterboxd style: solid stars plus a ½ for half ratings."""
    full = int(rating)
    half = rating - full >= 0.5
    step = size * 2.25
    for i in range(full):
        cv.polygon(star_points(x + size + i * step, cy, size), color)
    end = x + full * step
    if half:
        fs = size * 1.75
        cv.text(end + size * 0.05, cy + fs * 0.36, "½", F_UI_BOLD, fs, color)
        end += cv.length("½", F_UI_BOLD, fs) + size * 0.3
    return end - (size * 0.25 if full and not half else 0)


def nice_date(iso):
    from datetime import date
    try:
        d = date.fromisoformat(iso)
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    except Exception:
        return iso


def palette_image():
    pal = Image.new("P", (1, 1))
    flat = [c for rgb in SPECTRA for c in rgb]
    pal.putpalette(flat + [0] * (768 - len(flat)))
    return pal


def load_poster(url, box_w, box_h, cfg=None):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"poster fetch failed: {e}", file=sys.stderr)
        return None
    return dither_poster(img, box_w, box_h, cfg)


def palette_from(colors):
    pal = Image.new("P", (1, 1))
    flat = [c for rgb in colors for c in rgb]
    pal.putpalette(flat + [0] * (768 - len(flat)))
    return pal


def dither_palette(realism):
    """Mix between pure RGB (0) and the panel's measured colors (1)."""
    t = max(0.0, min(1.0, float(realism)))
    return [tuple(round(r * t + p * (1 - t)) for r, p in zip(rc, pc)) for rc, pc in zip(PANEL, SPECTRA)]


def dither_poster(img, box_w, box_h, cfg=None):
    from PIL import ImageEnhance, ImageFilter
    cfg = cfg or {}
    img = img.convert("RGB")
    k = min(box_w / img.width, box_h / img.height)  # fit the box, scaling up small posters too
    img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
    img = ImageEnhance.Color(img).enhance(cfg.get("poster_color", 1.2))
    img = ImageEnhance.Contrast(img).enhance(cfg.get("poster_contrast", 1.1))
    # Dither against what the panel really shows, then store the pure colors the driver expects.
    idx = img.quantize(palette=palette_from(dither_palette(cfg.get("poster_realism", 0.5))),
                       dither=Image.Dither.FLOYDSTEINBERG)
    idx.putpalette([c for rgb in SPECTRA for c in rgb] + [0] * (768 - 18))
    return idx.convert("RGB")


def panel_preview(img: Image.Image) -> Image.Image:
    """Approximate how a frame looks on the real panel (muted pigments, gray-ish white)."""
    idx = img.convert("RGB").quantize(palette=palette_image(), dither=Image.Dither.NONE)
    idx.putpalette([c for rgb in PANEL for c in rgb] + [0] * (768 - 18))
    return idx.convert("RGB")


def load_logo(cfg: dict, height: int):
    """Logo as black-on-white line art (or the original boxed version), `height` px tall.
    Off unless config.json sets "logo" to an image path, e.g. "assets/logo.png"."""
    path = cfg.get("logo")
    if not path:
        return None
    p = Path(path) if Path(path).is_absolute() else ROOT / path
    if not p.exists():
        return None
    from PIL import ImageOps
    img = Image.open(p).convert("L")
    if cfg.get("logo_style", "line") == "line":
        # Drop the black box: keep just the light artwork, drawn in black on the page.
        bbox = img.point(lambda v: 255 if v >= 128 else 0).getbbox()
        if bbox:
            img = img.crop(bbox)
        img = ImageOps.invert(img)
    w = round(img.width * height / img.height)
    # Downscale with averaging, then snap to pure black/white so edges stay crisp on e-ink.
    return img.resize((w, height), Image.LANCZOS).point(lambda v: 255 if v >= 128 else 0)


def paste_logo(base, logo, x, y):
    """Paste only the black pixels, so the page color shows through around the art."""
    mask = logo.point(lambda v: 255 if v < 128 else 0).convert("1")
    base.paste(BLACK, (round(x), round(y)), mask)


def render(review: dict | None, cfg: dict, poster_img=None) -> Image.Image:
    W, H = cfg.get("width", 1600), cfg.get("height", 1200)
    base = Image.new("RGB", (W, H), WHITE)
    cv = Canvas(W, H)
    m = 72

    pos = cfg.get("logo_position", "footer")
    sizes = {"footer": 84, "under-poster": 120, "top-right": 120}
    logo = load_logo(cfg, int(cfg.get("logo_height", sizes.get(pos, 100))))
    logo_room = 0
    if logo is not None and pos == "top-right":
        paste_logo(base, logo, W - m - logo.width, m)
        logo_room = logo.width + 36  # header text wraps short of the logo

    if review is None:
        cv.text(m, H / 2, "No reviews yet.", F_TITLE, 64, BLACK)
        return cv.flatten(base)

    # Poster column
    x0 = m
    if cfg.get("show_poster", True):
        pw, ph = int(W * 0.34), H - 2 * m
        if poster_img is None:
            poster_img = load_poster(review.get("poster"), pw, ph, cfg)
        else:
            poster_img = dither_poster(poster_img, pw, ph, cfg)
        if poster_img is not None:
            px = m + (pw - poster_img.width) // 2
            py = m + (ph - poster_img.height) // 2
            if logo is not None and pos == "under-poster":
                py = m  # top-align the poster and center the logo in the space below it
                room = ph - poster_img.height
                if room >= logo.height + 40:
                    paste_logo(base, logo, m + (pw - logo.width) / 2,
                               m + poster_img.height + (room - logo.height) / 2)
                else:
                    pos = "footer"
            base.paste(poster_img, (px, py))
            b = 3  # crisp keyline around the dithered edge
            cv.rect(px - b, py - b, px + poster_img.width + b, py, BLACK)
            cv.rect(px - b, py + poster_img.height, px + poster_img.width + b, py + poster_img.height + b, BLACK)
            cv.rect(px - b, py, px, py + poster_img.height, BLACK)
            cv.rect(px + poster_img.width, py, px + poster_img.width + b, py + poster_img.height, BLACK)
            x0 = m + pw + 64
    tw = W - x0 - m

    # Header: member, title, year + rating + like
    y = m + 26
    head_w = tw - logo_room
    cv.text(x0, y, review["member"].upper(), F_UI_BOLD, 27, RED, tracking=0.12)
    y += 30

    # Title: up to 2 lines. Only the first line sits beside the logo, so only it is narrowed.
    def title_lines(size):
        words = (review["film"] or "Untitled").split()
        lines, line, limit = [], "", head_w
        for w in words:
            trial = f"{line} {w}".strip()
            if cv.length(trial, F_TITLE, size) <= limit or not line:
                line = trial
            else:
                lines.append(line)
                line, limit = w, tw
        lines.append(line)
        return lines

    for tsize in (72, 60):
        tlines = title_lines(tsize)
        if len(tlines) <= 2:
            break
    if len(tlines) > 2:
        tlines = [tlines[0], " ".join(tlines[1:])]
        while cv.length(tlines[1] + " …", F_TITLE, tsize) > tw:
            tlines[1] = tlines[1].rsplit(" ", 1)[0]
        tlines[1] += " …"
    for line in tlines:
        y += tsize * 1.08
        cv.text(x0, y, line, F_TITLE, tsize, BLACK)

    y += 64
    row_cy = y - 12
    xr = x0
    if review["year"]:
        cv.text(xr, y, review["year"], F_UI, 34, BLACK)
        xr += cv.length(review["year"], F_UI, 34) + 30
    if review["rating"]:
        xr = draw_rating(cv, xr, row_cy, review["rating"]) + 22
    if review["liked"]:
        draw_heart(cv, xr, row_cy, 46, RED)

    y += 40
    cv.rect(x0, y, x0 + 96, y + 5, BLACK)
    y += 34

    # Footer
    footer_base = H - m - 4
    footer_top = footer_base - 52
    has_footer = bool(review["watched"] or review["rewatch"])

    # No poster to sit under? Fall back to the footer.
    footer_logo = logo is not None and (pos == "footer" or (pos == "under-poster" and x0 == m))
    if footer_logo:
        has_footer = True
        logo_x, logo_y = W - m - logo.width, H - m - logo.height + 4

    # Body
    body_bottom = (footer_top - 28) if has_footer else (H - m)
    if footer_logo:
        body_bottom = min(body_bottom, logo_y - 24)
    if review["spoiler"] and cfg.get("hide_spoilers", True):
        s = 44
        cv.rect(x0, y + 4, x0 + cv.length("SPOILERS", F_UI_BOLD, 22, 0.14) + 32, y + 46, RED, radius=21)
        cv.text(x0 + 16, y + 33, "SPOILERS", F_UI_BOLD, 22, WHITE, tracking=0.14)
        y += 46 + s * 1.5
        cv.text(x0, y, "This review has spoilers,", F_ITALIC, s, BLACK)
        cv.text(x0, y + s * 1.4, "so it's hidden here.", F_ITALIC, s, BLACK)
    else:
        size, lines = fit_body(cv, review["text"], tw, body_bottom - y)
        yb = y + size * 1.0
        for line in lines:
            if line is None:
                yb += size * 0.62
                continue
            cv.text(x0, yb, "line", F_BODY, size, BLACK)
            yb += size * 1.42

    if has_footer:
        rule_end = (logo_x - 28) if footer_logo else (x0 + tw)
        cv.rect(x0, footer_top, rule_end, footer_top + 2, BLACK)
        if footer_logo:
            paste_logo(base, logo, logo_x, logo_y)
        fx = x0
        if review["watched"]:
            label = f"WATCHED {nice_date(review['watched']).upper()}"
            cv.text(fx, footer_base, label, F_UI, 24, BLACK, tracking=0.1)
            fx += cv.length(label, F_UI, 24, 0.1) + 22
        if review["rewatch"]:
            pill_w = cv.length("REWATCH", F_UI_BOLD, 20, 0.14) + 28
            cv.rect(fx, footer_base - 27, fx + pill_w, footer_base + 9, BLUE, radius=18)
            cv.text(fx + 14, footer_base - 2, "REWATCH", F_UI_BOLD, 20, WHITE, tracking=0.14)

    # White text sits on top of the colored pills, so it is painted last.
    return cv.flatten(base, order=[BLACK, GREEN, BLUE, RED, YELLOW, WHITE])


def render_setup(cfg: dict, network: str, address: str = "10.41.0.1") -> Image.Image:
    """Instructions shown when the frame can't find a known Wi-Fi network and opens its hotspot."""
    W, H = cfg.get("width", 1600), cfg.get("height", 1200)
    base = Image.new("RGB", (W, H), WHITE)
    cv = Canvas(W, H)
    m, x = 110, 110

    y = m + 30
    cv.text(x, y, "WI-FI SETUP", F_UI_BOLD, 30, RED, tracking=0.14)
    y += 104
    cv.text(x, y, "Let's get this device online.", F_TITLE, 84, BLACK)
    y += 70
    cv.text(x, y, "It couldn't find a Wi-Fi network it knows.", F_BODY, 40, BLACK)

    steps = [
        ("On your phone, join the Wi-Fi network", network),
        ("A setup page should open. If it doesn't, go to", f"http://{address}"),
        ("Pick your Wi-Fi network and enter its password.", None),
    ]
    y += 110
    for i, (line, bold) in enumerate(steps, 1):
        r = 38
        cv.ellipse(x, y - r - 14, x + 2 * r, y + r - 14, RED)
        num = str(i)
        cv.text(x + r - cv.length(num, F_UI_BOLD, 40) / 2, y + 2, num, F_UI_BOLD, 40, WHITE)
        tx = x + 2 * r + 36
        cv.text(tx, y, line, F_BODY, 42, BLACK)
        if bold:
            cv.text(tx, y + 64, bold, F_UI_BOLD, 46, BLACK)
            y += 64
        y += 128

    note_y = H - m
    cv.rect(x, note_y - 74, W - m, note_y - 72, BLACK)
    cv.text(x, note_y - 20, "Needs a 2.4 GHz network.",
            F_UI, 28, BLACK)
    return to_palette(cv.flatten(base, order=[BLACK, RED, WHITE]))


def to_palette(img: Image.Image) -> Image.Image:
    # Everything is already one of the 6 colors; this just stores it as a small palette PNG.
    return img.quantize(palette=palette_image(), dither=Image.Dither.NONE)


class AllFeedsFailed(RuntimeError):
    """Every feed failed (offline, Letterboxd down...). Callers should keep the old image."""


def gather_reviews(cfg: dict, feed_file: str | None = None) -> list[dict]:
    if feed_file:
        return parse_feed(Path(feed_file).read_text(), "local")
    names = load_usernames(cfg)
    reviews, ok = [], 0
    for u in names:
        try:
            reviews += parse_feed(fetch_feed(u), u)
            ok += 1
        except Exception as e:
            print(f"feed {u} failed: {e}", file=sys.stderr)
    if names and ok == 0:
        raise AllFeedsFailed(f"all {len(names)} feeds failed")
    return reviews


CODE_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]


def selection_key(review: dict | None, cfg: dict) -> str:
    """Changes whenever the rendered image would change: the review, the config or this code."""
    return hashlib.sha256(json.dumps([review, cfg, CODE_VERSION], sort_keys=True).encode()).hexdigest()


def build_frame(cfg: dict, reviews: list[dict], now: float | None = None) -> tuple[bytes, dict]:
    global SCALE
    SCALE = int(cfg.get("render_scale", SCALE))
    review = choose(reviews, cfg, time.time() if now is None else now)
    img = to_palette(render(review, cfg))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    meta = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "review_id": review["id"] if review else None,
        "member": review["member"] if review else None,
        "film": review["film"] if review else None,
        "reviews_found": len(reviews),
    }
    return data, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--feed-file", help="Use a local RSS file instead of fetching")
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--now", type=float, help="Override current time (unix seconds)")
    ap.add_argument("--preview", action="store_true",
                    help="Also write frame_preview.png simulating the real panel colors")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    try:
        reviews = gather_reviews(cfg, args.feed_file)
    except AllFeedsFailed as e:
        # Fail the run so the workflow doesn't publish a "No reviews" frame.
        sys.exit(f"{e}; not publishing")
    data, meta = build_frame(cfg, reviews, args.now)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "frame.png").write_bytes(data)
    (out / "frame.json").write_text(json.dumps(meta, indent=2))
    if args.preview:
        panel_preview(Image.open(io.BytesIO(data))).save(out / "frame_preview.png")
    print(json.dumps(meta))


if __name__ == "__main__":
    main()
