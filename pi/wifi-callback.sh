#!/bin/sh
# comitup calls this with HOTSPOT, CONNECTING or CONNECTED whenever its Wi-Fi state changes
# (external_callback in /etc/comitup.conf). install.sh copies it to
# /usr/local/bin/letterboxd-wifi-callback. We record the state and redraw the frame right away.
mkdir -p /run/letterboxd-frame
echo "$1" > /run/letterboxd-frame/wifi-state
chmod 644 /run/letterboxd-frame/wifi-state
systemctl start --no-block letterboxd-frame.service || true
