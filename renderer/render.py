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

def wrap(draw, text, fnt, max_w):
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        words, line = para.split(), ""
        for w in words:
            trial = f"{line} {w}".strip()
            if draw.textlength(trial, font=fnt) <= max_w:
                line = trial
            else:
                if line:
                    lines.append(line)
                line = w
        lines.append(line)
    # collapse repeated blank lines
    out = []
    for l in lines:
        if l == "" and (not out or out[-1] == ""):
            continue
        out.append(l)
    return out


def fit_text(draw, text, max_w, max_h, sizes=(46, 42, 38, 34, 30, 27, 24)):
    """Largest font size where the review fits; truncates at the smallest size."""
    for size in sizes:
        fnt = font("DejaVuSerif.ttf", size)
        lh = int(size * 1.38)
        lines = wrap(draw, text, fnt, max_w)
        if len(lines) * lh <= max_h:
            return fnt, lh, lines
    max_lines = max_h // lh
    lines = lines[:max_lines]
    while lines and draw.textlength(lines[-1] + " …", font=fnt) > max_w:
        lines[-1] = lines[-1].rsplit(" ", 1)[0]
    if lines:
        lines[-1] = lines[-1].rstrip(" ,.;:") + " …"
    return fnt, lh, lines


def star(draw, cx, cy, r, fill):
    import math
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    draw.polygon(pts, fill=fill)


def draw_rating(draw, x, y, rating, size=34, color=GREEN):
    """Stars in Letterboxd green; half stars drawn by clipping to the left half."""
    full, half = int(rating), (rating - int(rating)) >= 0.5
    step = size * 2.1
    for i in range(full):
        star(draw, x + size + i * step, y + size, size, color)
    if half:
        img = Image.new("1", (size * 2 + 2, size * 2 + 2), 0)
        star(ImageDraw.Draw(img), size + 1, size + 1, size, 1)
        img = img.crop((0, 0, size + 1, size * 2 + 2))
        draw.bitmap((x + full * step, y - 1), img, fill=color)
    return x + (full + (1 if half else 0)) * step


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
    img.thumbnail((box_w, box_h), Image.LANCZOS)
    # Boost saturation/contrast a bit: e-ink pigments are duller than a monitor.
    from PIL import ImageEnhance
    img = ImageEnhance.Color(img).enhance(1.4)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    return img.quantize(palette=palette_image(), dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")


def render(review: dict | None, cfg: dict, poster_img=None) -> Image.Image:
    W, H = cfg.get("width", 1600), cfg.get("height", 1200)
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"  # no anti-aliasing: gray edges turn into speckle on 6 colors

    m = 70
    if review is None:
        d.text((m, H // 2 - 40), "No reviews yet.", font=font("DejaVuSerif-Bold.ttf", 64), fill=BLACK)
        return img

    x0 = m
    if cfg.get("show_poster", True):
        pw, ph = int(W * 0.34), H - 2 * m
        if poster_img is None:
            poster_img = load_poster(review.get("poster"), pw, ph)
        if poster_img is not None:
            px = m + (pw - poster_img.width) // 2
            py = m + (ph - poster_img.height) // 2
            img.paste(poster_img, (px, py))
            x0 = m + pw + 60
    text_w = W - x0 - m

    y = m
    who = font("DejaVuSans-Bold.ttf", 34)
    d.text((x0, y), review["member"].upper(), font=who, fill=RED)
    y += 56

    title_font = font("DejaVuSerif-Bold.ttf", 64)
    title = review["film"] or "Untitled"
    for line in wrap(d, title, title_font, text_w)[:2]:
        d.text((x0, y), line, font=title_font, fill=BLACK)
        y += 76
    if review["year"]:
        d.text((x0, y), review["year"], font=font("DejaVuSans.ttf", 36), fill=BLUE)
        y += 54

    y += 10
    xr = x0
    if review["rating"]:
        xr = draw_rating(d, x0, y, review["rating"])
    if review["liked"]:
        hx = xr + 20
        d.text((hx, y - 6), "♥", font=font("DejaVuSans.ttf", 66), fill=RED)
    if review["rating"] or review["liked"]:
        y += 90

    d.rectangle((x0, y, x0 + 120, y + 6), fill=BLACK)
    y += 40

    footer_h = 60
    if review["spoiler"] and cfg.get("hide_spoilers", True):
        d.text((x0, y), "This review contains spoilers,\nso it's hidden here.",
               font=font("DejaVuSerif-Italic.ttf", 44), fill=BLACK, spacing=16)
    else:
        fnt, lh, lines = fit_text(d, review["text"], text_w, H - m - footer_h - y)
        for line in lines:
            d.text((x0, y), line, font=fnt, fill=BLACK)
            y += lh

    footer = []
    if review["watched"]:
        footer.append(f"Watched {review['watched']}")
    if review["rewatch"]:
        footer.append("Rewatch")
    if footer:
        d.text((x0, H - m - 34), "  ·  ".join(footer), font=font("DejaVuSans.ttf", 30), fill=BLUE)
    return img


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
        for u in cfg["usernames"]:
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
