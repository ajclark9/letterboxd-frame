# Letterboxd e-ink frame

```
GitHub Actions (every 30 min)          GitHub Pages                 Raspberry Pi (every 5 min)
fetch RSS → pick review → render  →   frame.png on 'frame'   ←    GET frame.png
→ publish only if image changed        branch (static, no sleep)   refresh panel only if changed
```

## 1. Try the renderer on your laptop

```bash
pip install -r renderer/requirements.txt
python renderer/render.py --feed-file renderer/sample_feed.xml   # offline sample
python renderer/render.py                                        # live feeds from config.json
open out/frame.png
```

Edit `config.json`:

| key | what it does |
|---|---|
| `usernames` | Letterboxd usernames to pull reviews from |
| `mode` | `"rotate"` cycles through recent reviews; `"latest"` always shows the newest |
| `rotate_minutes` | how long each review stays up in rotate mode |
| `rotate_pool` | how many of the newest reviews to rotate through |
| `hide_spoilers` | swap spoiler reviews for a "hidden" message |
| `show_poster` | dithered poster on the left |

Only reviews with text are shown; diary entries without a review and list activity are skipped.
`width`/`height` are 1600×1200; swap them for portrait.

Printing `out/frame.png` at real size (about 27 × 20 cm) is the best preview of how the panel will look.

## 2. Put it on GitHub

1. Create a **public** repo (free Pages + unlimited Actions minutes) and push this folder to `main`.
2. **Settings → Actions → General → Workflow permissions:** pick *Read and write permissions*.
3. **Actions tab → Render frame → Run workflow.** This creates a `frame` branch.
4. **Settings → Pages:** Source *Deploy from a branch*, branch `frame`, folder `/ (root)`.
5. A minute later, `https://YOUR-USERNAME.github.io/YOUR-REPO/` shows the current frame and
   `.../frame.png` is what the Pi downloads.

The workflow only pushes when the image actually changed, and it force-pushes a single commit
so the `frame` branch never piles up old images.

**Heads up:** GitHub turns off scheduled workflows in public repos after 60 days with no repo
activity. If the frame stops updating, check the Actions tab and re-enable it. Any push to
`main` resets the clock.

## 3. Set up the Pi

On Raspberry Pi OS Lite (set Wi-Fi and SSH in Raspberry Pi Imager):

```bash
sudo apt install -y python3-pil git
git clone https://github.com/YOUR-USERNAME/YOUR-REPO.git ~/letterboxd-frame

# test by hand with the fake display (saves ~/.letterboxd-frame/displayed.png)
python3 ~/letterboxd-frame/pi/frame.py --url https://YOUR-USERNAME.github.io/YOUR-REPO/frame.png
```

Run it every 5 minutes with systemd (edit the URL and `User=` in the .service first):

```bash
sudo cp ~/letterboxd-frame/pi/letterboxd-frame.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now letterboxd-frame.timer
journalctl -u letterboxd-frame -f      # watch it work
```

A oneshot service plus a timer means two runs can never overlap, even with the 25-second refresh.

To watch the fake display from your phone while you wait for the panel:

```bash
cd ~/.letterboxd-frame && python3 -m http.server 8000   # then open http://<pi-ip>:8000/displayed.png
```

## 4. When the panel arrives

Enable SPI and I2C: `sudo raspi-config` → Interface Options.

- **Pimoroni Inky Impression:** `pip install inky --break-system-packages` (or a venv), then change
  `--display fake` to `--display inky` in the service file.
- **Waveshare 13.3" (E):** download Waveshare's Python driver for that panel, copy its `lib` folder
  to `pi/lib`, check the module name in `show_waveshare()` against their example, then use
  `--display waveshare`.

Run once by hand with `--force` to push the current image, then `sudo systemctl daemon-reload`.
