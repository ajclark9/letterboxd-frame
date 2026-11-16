#!/bin/bash
# Run hourly by letterboxd-update.timer (as root). Pulls the latest code, re-installs the Pi
# pieces if anything under pi/ changed, and asks the frame to redraw with the new code.
# Everything is inside main() so bash has read the whole script before git pull replaces it.
main() {
    set -uo pipefail
    source /etc/letterboxd-frame.conf
    cd "$REPO" || exit 1
    as_user() { sudo -H -u "$FRAME_USER" "$@"; }

    before=$(as_user git rev-parse HEAD)
    if ! as_user git pull -q --ff-only; then
        echo "git pull failed (offline, or local edits on the Pi?). Will try again next hour."
        exit 0
    fi
    after=$(as_user git rev-parse HEAD)
    if [ "$before" = "$after" ]; then
        echo "already up to date"
        exit 0
    fi
    echo "updated ${before:0:7} -> ${after:0:7}"

    if as_user git diff --name-only "$before" "$after" | grep -q '^pi/'; then
        echo "Pi files changed, re-installing"
        bash "$REPO/pi/install.sh"
    fi
    systemctl start --no-block letterboxd-frame.service
}
main "$@"
exit
