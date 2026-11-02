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
        f = self.font(name, size)
        n = len(text)
        return f.getlength(text) / SCALE + tracking * size * max(n - 1, 0)

    def text(self, x, baseline, text, name, size, color, tracking=0.0):
        """Draw text with its baseline at `baseline`. tracking is in ems."""
        d, f = self._draw(color), self.font(name, size)
        if not tracking:
            d.text((x * SCALE, baseline * SCALE), text, font=f, fill=255, anchor="ls")
            return
        cx = x * SCALE
        for ch in text:
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


def load_poster(url, box_w, box_h):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"poster fetch failed: {e}", file=sys.stderr)
        return None
    return dither_poster(img, box_w, box_h)


def dither_poster(img, box_w, box_h):
    from PIL import ImageEnhance
    img = img.convert("RGB")
    k = min(box_w / img.width, box_h / img.height)  # fit the box, scaling up small posters too
    img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.LANCZOS)
    # E-ink pigments are duller than a monitor, so push color and contrast a bit.
    img = ImageEnhance.Color(img).enhance(1.4)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    return img.quantize(palette=palette_image(), dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")


def render(review: dict | None, cfg: dict, poster_img=None) -> Image.Image:
    W, H = cfg.get("width", 1600), cfg.get("height", 1200)
    base = Image.new("RGB", (W, H), WHITE)
    cv = Canvas(W, H)
    m = 72

    if review is None:
        cv.text(m, H / 2, "No reviews yet.", F_TITLE, 64, BLACK)
        return cv.flatten(base)

    # Poster column
    x0 = m
    if cfg.get("show_poster", True):
        pw, ph = int(W * 0.34), H - 2 * m
        if poster_img is None:
            poster_img = load_poster(review.get("poster"), pw, ph)
        else:
            poster_img = dither_poster(poster_img, pw, ph)
        if poster_img is not None:
            px = m + (pw - poster_img.width) // 2
            py = m + (ph - poster_img.height) // 2
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
    cv.text(x0, y, review["member"].upper(), F_UI_BOLD, 27, RED, tracking=0.12)
    y += 30

    tsize = 72 if cv.length(review["film"] or "", F_TITLE, 72) <= tw * 1.6 else 60
    tlines = [l for l in wrap(cv, review["film"] or "Untitled", F_TITLE, tsize, tw) if l]
    if len(tlines) > 2:
        tlines = tlines[:2]
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

    # Body
    body_bottom = (footer_top - 28) if has_footer else (H - m)
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
            cv.text(x0, yb, line, F_BODY, size, BLACK)
            yb += size * 1.42

    if has_footer:
        cv.rect(x0, footer_top, x0 + tw, footer_top + 2, BLACK)
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


def to_palette(img: Image.Image) -> Image.Image:
    # Everything is already one of the 6 colors; this just stores it as a small palette PNG.
    return img.quantize(palette=palette_image(), dither=Image.Dither.NONE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--feed-file", help="Use a local RSS file instead of fetching")
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--now", type=float, help="Override current time (unix seconds)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    reviews = []
    if args.feed_file:
        reviews = parse_feed(Path(args.feed_file).read_text(), "local")
    else:
        for u in load_usernames(cfg):
            try:
                reviews += parse_feed(fetch_feed(u), u)
            except Exception as e:
                print(f"feed {u} failed: {e}", file=sys.stderr)

    review = choose(reviews, cfg, args.now if args.now is not None else time.time())
    img = to_palette(render(review, cfg))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    (out / "frame.png").write_bytes(data)
    meta = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "review_id": review["id"] if review else None,
        "member": review["member"] if review else None,
        "film": review["film"] if review else None,
        "reviews_found": len(reviews),
    }
    (out / "frame.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta))


if __name__ == "__main__":
    main()
