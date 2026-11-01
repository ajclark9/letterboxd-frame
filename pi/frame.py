#!/usr/bin/env python3
"""Runs on the Pi: download the rendered frame, refresh the panel only if it changed.

  python3 frame.py --url https://<you>.github.io/<repo>/frame.png --display fake
  python3 frame.py --url ... --display inky        # Pimoroni Inky Impression
  python3 frame.py --url ... --display waveshare   # Waveshare 13.3" (E)

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--display", choices=DISPLAYS, default="fake")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--force", action="store_true", help="Refresh even if unchanged")
    args = ap.parse_args()

    state = load_state()
    try:
        data, etag = fetch(args.url, None if args.force else state.get("etag"), args.timeout)
    except Exception as e:
        print(f"fetch failed, leaving screen alone: {e}")
        return

    if data is None:
        print("304 not modified")
        return

    digest = hashlib.sha256(data).hexdigest()
    if digest == state.get("sha256") and not args.force:
        print("same image as last time, skipping refresh")
        save_state({**state, "etag": etag})
        return

    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        print(f"bad image, leaving screen alone: {e}")
        return

    DISPLAYS[args.display](img)
    save_state({"etag": etag, "sha256": digest, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")})
    print("panel updated")


if __name__ == "__main__":
    main()
