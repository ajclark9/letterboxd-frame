#!/usr/bin/env python3
"""Runs on the Pi: get the frame image, refresh the panel only if it changed.

Two ways to get the image:
  python3 frame.py --url https://<you>.github.io/<repo>/frame.png   # rendered by GitHub Actions
  python3 frame.py --local                                          # render right here on the Pi

  add --display inky       # Pimoroni Inky Impression
  or  --display waveshare  # Waveshare 13.3" (E)
  (default --display fake just saves the image, for testing without a panel)

Designed to run once and exit (systemd timer calls it every few minutes).
A failed download never touches the screen: e-ink keeps the last image.
"""
import argparse
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

STATE_DIR = Path.home() / ".letterboxd-frame"
STATE_FILE = STATE_DIR / "state.json"


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def fetch(url, etag, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "letterboxd-frame-pi/1.0"})
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None, etag
        raise


# ---------- Displays ----------

def show_fake(img):
    """Pretends to be the panel: saves the image and waits like a real refresh."""
    out = STATE_DIR / "displayed.png"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"[fake] refreshing panel... (saved {out})")
    time.sleep(25)
    print("[fake] refresh done")


def show_inky(img):
    from inky.auto import auto  # pip install inky
    display = auto()
    if img.size != display.resolution:
        img = img.resize(display.resolution)
    display.set_image(img)
    display.show()


def show_waveshare(img):
    # Waveshare ships the 13.3" (E) driver as a separate download. Copy its python
    # lib folder next to this script, then check the module/class names below
    # against their example (they've varied between releases).
    sys.path.insert(0, str(Path(__file__).parent / "lib"))
    import epd13in3E  # noqa
    epd = epd13in3E.EPD()
    epd.Init()
    epd.display(epd.getbuffer(img))
    epd.sleep()


DISPLAYS = {"fake": show_fake, "inky": show_inky, "waveshare": show_waveshare}


REPO = Path(__file__).resolve().parent.parent


def get_remote(args, state):
    """Returns PNG bytes, or None when there is nothing new / it failed."""
    try:
        data, etag = fetch(args.url, None if args.force else state.get("etag"), args.timeout)
    except Exception as e:
        print(f"fetch failed, leaving screen alone: {e}")
        return None
    state["etag"] = etag
    if data is None:
        print("304 not modified")
    return data


def get_local(args, state):
    """Fetch feeds and render on the Pi. Returns PNG bytes, or None."""
    now = time.time()
    since = now - state.get("last_fetch", 0)
    if since < args.fetch_every * 60 and not args.force:
        print(f"checked feeds {since / 60:.1f} min ago, next check after {args.fetch_every} min")
        return None

    sys.path.insert(0, str(REPO / "renderer"))
    import render  # noqa: E402  (renderer/render.py from this repo)

    cfg = json.loads(Path(args.config).read_text())
    if args.scale:
        cfg["render_scale"] = args.scale
    try:
        reviews = render.gather_reviews(cfg)
    except Exception as e:
        print(f"couldn't load feeds, leaving screen alone: {e}")
        return None
    state["last_fetch"] = now

    key = render.selection_key(render.choose(reviews, cfg, now), cfg)
    if key == state.get("selection") and not args.force:
        print("same review as last time, skipping render")
        return None

    t0 = time.time()
    data, meta = render.build_frame(cfg, reviews, now)
    print(f"rendered {meta['member']}: {meta['film']} in {time.time() - t0:.1f}s")
    state["selection"] = key
    return data


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="Download the frame rendered by GitHub Actions")
    src.add_argument("--local", action="store_true", help="Fetch feeds and render on this Pi")
    ap.add_argument("--display", choices=DISPLAYS, default="fake")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--force", action="store_true", help="Refresh even if unchanged")
    ap.add_argument("--config", default=str(REPO / "config.json"), help="--local: config file")
    ap.add_argument("--fetch-every", type=float, default=10,
                    help="--local: minutes between feed checks (default 10)")
    ap.add_argument("--scale", type=int, help="--local: supersampling, 2 is faster than the default 3")
    args = ap.parse_args()

    state = load_state()
    data = get_local(args, state) if args.local else get_remote(args, state)
    if data is None:
        save_state(state)
        return

    digest = hashlib.sha256(data).hexdigest()
    if digest == state.get("sha256") and not args.force:
        print("same image as last time, skipping refresh")
        save_state(state)
        return

    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        print(f"bad image, leaving screen alone: {e}")
        return

    DISPLAYS[args.display](img)
    state.update(sha256=digest, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    save_state(state)
    print("panel updated")


if __name__ == "__main__":
    main()
