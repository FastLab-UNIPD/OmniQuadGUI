#!/bin/bash
# OmniQuadGUI launcher -- portable, NOT user specific.
#
# This script lives in the same folder as main.py and starts the app
# from there, no matter where the folder is copied to or which user
# runs it.  Nothing in here contains a home-directory path.
#
# Used by OmniQuadGUI.desktop (the double-click launcher icon inside this
# folder), which passes its own location to bash and runs this script
# from next to it.

# Find the folder this script itself lives in, and go there.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR" || exit 1

# Install the app icon into the user's private icon folder the first
# time the app is started, so the launcher icon shows up without admin
# rights.  (Icon=omniquadgui in the .desktop file is looked up there.)
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
ICON_TARGET="$ICON_DIR/omniquadgui.png"
if [ -f images/omniquadgui.png ] && [ ! -f "$ICON_TARGET" ]; then
    mkdir -p "$ICON_DIR" 2>/dev/null
    cp images/omniquadgui.png "$ICON_TARGET" 2>/dev/null
    # Refresh the icon cache so the new icon is found immediately.
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" \
            >/dev/null 2>&1
    fi
fi

# Start the app by absolute path so this remains correct even if a caller
# changes directory between launching this script and starting Python.
exec /usr/bin/python3 "$APP_DIR/main.py" "$@"
