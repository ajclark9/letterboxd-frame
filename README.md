# Letterboxd e-ink frame

```
GitHub Actions (every 5 min)           GitHub Pages                 Raspberry Pi (every minute)
fetch RSS → pick review → render  →   frame.png on 'frame'   ←    GET frame.png
→ publish only if image changed        branch (static, no sleep)   refresh panel only if changed
```

## 1. Try the renderer on your laptop

```bash
pip install -r renderer/requirements.txt
python renderer/render.py                                        # live feeds from config.json
open out/frame.png
```

Edit `config.json`:

| key | what it does |
|---|---|
| `usernames_csv_url` | published Google Sheet CSV of usernames (see below); leave `""` to skip |
| `usernames` | Letterboxd usernames to pull reviews from (fallback if the sheet fails) |
| `mode` | `"rotate"` cycles through recent reviews; `"latest"` always shows the newest |
| `rotate_minutes` | how long each review stays up in rotate mode |
| `rotate_pool` | how many of the newest reviews to rotate through |
| `hide_spoilers` | swap spoiler reviews for a "hidden" message |
| `show_poster` | dithered poster on the left |
| `logo` | off by default; set to an image path like `"assets/logo.png"` to show a logo |
| `logo_position` | `"footer"` (default), `"under-poster"` or `"top-right"` |
| `logo_height` / `logo_style` | logo size in pixels; `"line"` (default, art only) or `"boxed"` (original image) |
| `poster_realism` | 0–1, default 0.5: how much poster dithering accounts for the panel's muted real colors |
| `poster_color` / `poster_contrast` | default 1.2 / 1.1: saturation and contrast boost before dithering |

### Letting someone else manage the list (Google Sheet)

1. Make a Google Sheet. Put Letterboxd usernames in **column A**, one per row. A header row is fine.
   It accepts `name`, `@name`, or a pasted profile link, and ignores blank rows and rows starting with `#`.
2. **File → Share → Publish to web** → pick the sheet tab → **Comma-separated values (.csv)** → Publish. Copy the link.
3. Paste that link into `usernames_csv_url` in `config.json` and push.
4. Share the sheet (edit access) with whoever owns the frame.

Edits show up on the frame within about an hour: Google takes a few minutes to update the
published CSV, plus up to 30 minutes for the next workflow run, plus the Pi's 5-minute check.
If the sheet can't be read or is empty, the `usernames` list in `config.json` is used instead.
The published link is public but unguessable, and only contains usernames.

Only reviews with text are shown; diary entries without a review and list activity are skipped.
`width`/`height` are 1600×1200; swap them for portrait.

Fonts are bundled in `fonts/` (Literata for the review, Inter for labels, both under the SIL Open
Font License). Text and shapes are drawn at 3× and snapped to solid colors, so edges stay clean
without anti-aliasing fringes that the panel would turn into speckle.

`python renderer/render.py --preview` also writes `out/frame_preview.png`, an approximation of how the
frame looks on the real panel (grayish white, muted colors). The fake display on the Pi writes
`displayed_preview.png` next to `displayed.png`.

Printing `out/frame.png` at real size (about 27 × 20 cm) is the best preview of how the panel will look.

## 2. Put it on GitHub

1. Create a **public** repo (free Pages + unlimited Actions minutes) and push this folder to your default branch (`main` or `master`).
2. **Settings → Actions → General → Workflow permissions:** pick *Read and write permissions*.
3. **Actions tab → Render frame → Run workflow.** This creates a `frame` branch.
4. **Settings → Pages:** Source *Deploy from a branch*, branch `frame`, folder `/ (root)`.
5. A minute later, `https://YOUR-USERNAME.github.io/YOUR-REPO/` shows the current frame and
   `.../frame.png` is what the Pi downloads.

The workflow only pushes when the image actually changed, and it force-pushes a single commit
so the `frame` branch never piles up old images.

**Heads up:** GitHub turns off scheduled workflows in public repos after 60 days with no repo
activity. If the frame stops updating, check the Actions tab and re-enable it. Any push to your default branch
resets the clock.

## 3. Set up the Pi

On Raspberry Pi OS Lite (set Wi-Fi, SSH and the Wi-Fi country in Raspberry Pi Imager), then over SSH:

```bash
sudo apt install -y git
git clone https://github.com/YOUR-USERNAME/YOUR-REPO.git ~/letterboxd-frame
sudo bash ~/letterboxd-frame/pi/install.sh
```

`install.sh` sets everything up for whoever ran it (no need to edit usernames or paths) and can be
re-run any time. Options are remembered in `/etc/letterboxd-frame.conf`:

| option | what it does |
|---|---|
| `--display fake`, `inky` or `waveshare` | which screen to draw on (`fake` saves `~/.letterboxd-frame/displayed.png`) |
| `--wifi-setup` | install the Wi-Fi setup hotspot (below) |
| `--url https://…/frame.png` | download GitHub's render instead of rendering on the Pi |
| `--local` | switch back to rendering on the Pi (the default) |

For the Inky Impression, `--display inky` also turns on SPI/I2C and installs Pimoroni's library.

It installs two timers:

- **letterboxd-frame** (every minute): checks the feeds at most every 10 minutes, renders only when
  the review, config or rendering code changed, and refreshes the panel only when the image changed.
  If every feed fails (Wi-Fi down, Letterboxd down) the screen is left alone.
- **letterboxd-update** (hourly): `git pull`s this repo. Pushing a change from your computer reaches
  the frame within about an hour, and the frame redraws with the new code. If anything in `pi/`
  changed, it re-runs `install.sh` with your saved options. It never overwrites local edits on the
  Pi (the pull just fails and logs it). It runs scripts from this repo as root, so keep push access
  to the repo to people you trust.

```bash
journalctl -u letterboxd-frame -f        # watch the frame
journalctl -u letterboxd-update          # update history
sudo systemctl start letterboxd-update   # pull updates right now
```

To view the fake display: `cd ~/.letterboxd-frame && python3 -m http.server 8000`, then open
`http://letterboxd-frame.local:8000/displayed_preview.png`.

### Wi-Fi setup hotspot

```bash
sudo bash ~/letterboxd-frame/pi/install.sh --wifi-setup
sudo reboot
```

This installs [comitup](https://davesteele.github.io/comitup/). On boot, if none of the Pi's saved
Wi-Fi networks are in range, it opens its own network called **Letterboxd-Frame-Setup** and the
frame shows instructions: join that network on a phone, a setup page opens (or go to
`http://10.41.0.1`), pick the Wi-Fi and enter its password. The Pi saves it, the hotspot closes
and reviews come back within a couple of minutes. Saved networks are remembered, so moving back
and forth between places just works.

- Set it up at home first, while the Pi is on a network you can SSH into.
- To test it: `sudo nmcli connection modify preconfigured connection.autoconnect no && sudo reboot`
  (run `nmcli connection show` if your saved network has a different name). The setup screen should
  appear; join the hotspot and add your Wi-Fi back through the setup page.
- The Zero 2 W only sees 2.4 GHz networks, and can't get past "click to accept" sign-in pages.
- Change the hotspot name with `--hotspot-name Some-Name` (letters, numbers and dashes).

## 4. When the panel arrives

Shut down (`sudo shutdown now`), unplug, attach the panel, power back on, then:

- **Pimoroni Inky Impression:** `sudo bash ~/letterboxd-frame/pi/install.sh --display inky`, then
  `sudo reboot` if it asks.
- **Waveshare 13.3" (E):** download Waveshare's Python driver for that panel, copy its `lib` folder
  to `pi/lib`, check the module name in `show_waveshare()` against their example, then
  `sudo bash ~/letterboxd-frame/pi/install.sh --display waveshare`.

To redraw immediately: `python3 ~/letterboxd-frame/pi/frame.py --local --display inky --force`.
