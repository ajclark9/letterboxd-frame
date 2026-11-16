#!/bin/bash
# Sets up (or re-applies) everything the frame needs on the Pi. Safe to run again any time.
#
#   sudo bash ~/letterboxd-frame/pi/install.sh                      # render on the Pi, fake display
#   sudo bash ~/letterboxd-frame/pi/install.sh --display inky       # ...on the Inky Impression
#   sudo bash ~/letterboxd-frame/pi/install.sh --wifi-setup         # add the Wi-Fi setup hotspot
#   sudo bash ~/letterboxd-frame/pi/install.sh --url https://<you>.github.io/<repo>/frame.png
#                                                                   # download GitHub's render instead
#
# Choices are remembered in /etc/letterboxd-frame.conf, so later runs (including the hourly
# auto-update) keep them. Installs:
#   letterboxd-frame.timer   checks for a new review every minute
#   letterboxd-update.timer  pulls this repo from GitHub every hour
#   comitup (--wifi-setup)   opens a "Letterboxd-Frame-Setup" hotspot when no known Wi-Fi is around
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo bash $0 $*"
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF=/etc/letterboxd-frame.conf
UNITS=/etc/systemd/system
HOTSPOT_DEFAULT="Letterboxd-Frame-Setup"

# Defaults, then whatever was saved last time, then this run's options.
FRAME_USER="${SUDO_USER:-$(stat -c %U "$REPO_DIR")}"
REPO="$REPO_DIR"
PANEL=fake
MODE=local
URL=""
HOTSPOT="$HOTSPOT_DEFAULT"
# shellcheck disable=SC1090
[ -f "$CONF" ] && source "$CONF"
REPO="$REPO_DIR"
WIFI_SETUP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --display) PANEL="$2"; shift 2 ;;
        --url) MODE=remote; URL="$2"; shift 2 ;;
        --local) MODE=local; shift ;;
        --wifi-setup) WIFI_SETUP=1; shift ;;
        --hotspot-name) HOTSPOT="$2"; shift 2 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

case "$PANEL" in fake|inky|waveshare) ;; *) echo "--display must be fake, inky or waveshare"; exit 1 ;; esac
if [ "$MODE" = remote ] && [ -z "$URL" ]; then echo "--url needs the frame.png address"; exit 1; fi
if [[ ! "$HOTSPOT" =~ ^[A-Za-z0-9-]+$ ]]; then echo "--hotspot-name: letters, numbers and dashes only"; exit 1; fi

echo "user: $FRAME_USER   repo: $REPO   display: $PANEL   mode: $MODE"
{
    printf 'FRAME_USER=%q\n' "$FRAME_USER"
    printf 'REPO=%q\n' "$REPO"
    printf 'PANEL=%q\n' "$PANEL"
    printf 'MODE=%q\n' "$MODE"
    printf 'URL=%q\n' "$URL"
    printf 'HOTSPOT=%q\n' "$HOTSPOT"
} > "$CONF"

REBOOT_NEEDED=0
as_user() { sudo -H -u "$FRAME_USER" "$@"; }   # -H: use their home, so pip/git find ~/.local etc.

# --- Python packages ------------------------------------------------------------------------
if ! as_user python3 -c "import PIL, requests" 2>/dev/null; then
    echo "Installing Pillow and requests..."
    apt-get install -y python3-pil python3-requests
fi

# --- Panel hardware -------------------------------------------------------------------------
if [ "$PANEL" = inky ]; then
    raspi-config nonint do_spi 0
    raspi-config nonint do_i2c 0
    BOOT_CONFIG=/boot/firmware/config.txt
    [ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt
    if ! grep -q '^dtoverlay=spi0-0cs' "$BOOT_CONFIG"; then
        echo "dtoverlay=spi0-0cs" >> "$BOOT_CONFIG"
        REBOOT_NEEDED=1
    fi
    if ! as_user python3 -c "import inky" 2>/dev/null; then
        echo "Installing Pimoroni's inky library (takes a few minutes on a Zero)..."
        apt-get install -y python3-pip python3-dev build-essential python3-numpy python3-spidev
        as_user pip install --quiet inky --break-system-packages
    fi
fi

# --- systemd units --------------------------------------------------------------------------
if [ "$MODE" = local ]; then
    ARGS="--local --display $PANEL"
else
    ARGS="--url $URL --display $PANEL"
fi
fill() {
    sed -e "s|@USER@|$FRAME_USER|g" -e "s|@REPO@|$REPO|g" -e "s|@ARGS@|$ARGS|g" "$REPO/pi/$1" > "$UNITS/$1"
}
fill letterboxd-frame.service
fill letterboxd-frame.timer
fill letterboxd-update.service
fill letterboxd-update.timer
install -m 755 -o root -g root "$REPO/pi/wifi-callback.sh" /usr/local/bin/letterboxd-wifi-callback
systemctl daemon-reload
systemctl enable --now letterboxd-frame.timer letterboxd-update.timer

# Older setups pulled updates with cron; the update timer replaces that.
if as_user crontab -l 2>/dev/null | grep -q 'letterboxd-frame.*git pull'; then
    { as_user crontab -l | grep -v 'letterboxd-frame.*git pull' || true; } | as_user crontab -
    echo "Removed the old git pull line from crontab (the update timer does this now)."
fi

# --- Wi-Fi setup hotspot (comitup) ----------------------------------------------------------
set_comitup() {  # set "key: value" in /etc/comitup.conf, replacing a commented or old line
    local key="$1" value="$2" file=/etc/comitup.conf
    touch "$file"
    if grep -Eq "^#?[[:space:]]*$key:" "$file"; then
        sed -i -E "0,/^#?[[:space:]]*$key:.*/s||$key: $value|" "$file"
    else
        echo "$key: $value" >> "$file"
    fi
}
if [ "$WIFI_SETUP" = 1 ] && ! dpkg -s comitup >/dev/null 2>&1; then
    echo "Installing comitup..."
    apt-get install -y comitup
    # comitup runs its own DHCP/DNS for the hotspot; a separately running dnsmasq would clash.
    if systemctl list-unit-files dnsmasq.service >/dev/null 2>&1; then
        systemctl mask dnsmasq.service || true
    fi
    if [ -f /etc/wpa_supplicant/wpa_supplicant.conf ]; then
        mv /etc/wpa_supplicant/wpa_supplicant.conf /etc/wpa_supplicant/wpa_supplicant.conf.disabled
    fi
    REBOOT_NEEDED=1
fi
if dpkg -s comitup >/dev/null 2>&1; then
    set_comitup ap_name "$HOTSPOT"
    set_comitup external_callback /usr/local/bin/letterboxd-wifi-callback
    echo "Wi-Fi setup hotspot: $HOTSPOT"
fi

# --- Done -----------------------------------------------------------------------------------
systemctl start --no-block letterboxd-frame.service
echo
echo "Installed. Watch it with:  journalctl -u letterboxd-frame -f"
if [ "$REBOOT_NEEDED" = 1 ]; then
    echo "Reboot needed for the hardware/Wi-Fi changes:  sudo reboot"
fi
