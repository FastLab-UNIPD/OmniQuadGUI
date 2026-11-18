#!/usr/bin/env python3
"""Launch the native OmniQuadGUI interface for this desktop."""

import os
import sys


def clean_own_environment():
    """Fallback cleanup: remove ROS 2 leftovers from THIS app's environment.

    A user shell that sources ROS ("source /opt/ros/jazzy/setup.bash")
    fills the environment with ROS_* variables, AMENT_PREFIX_PATH and a
    PYTHONPATH entry under /opt/ros/.  This app (GTK/VTE + ssh) behaves
    unreliably in such an environment: it would then only start properly
    from the desktop icon, where the shell environment is clean.  Taking
    those variables away here makes the app behave the SAME no matter how
    it is started (desktop icon, launch script, or from any terminal).

    This only changes what OUR app and the processes it starts see --
    it does not touch the terminal it was started from.
    """
    for key in list(os.environ):
        if key.startswith("ROS_") or key == "AMENT_PREFIX_PATH":
            os.environ.pop(key, None)
    # Keep the rest of PYTHONPATH, but drop entries that point into a
    # ROS installation (e.g. /opt/ros/jazzy/lib/python3.12/site-packages).
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(":")
             if p and not p.startswith("/opt/ros/")]
    if parts:
        os.environ["PYTHONPATH"] = ":".join(parts)
    else:
        os.environ.pop("PYTHONPATH", None)


# ---- Start the app in a CLEAN environment (like the desktop icon) ----
# A desktop icon is started by GNOME with a small, clean set of
# variables, and the app was ALWAYS reliable that way.  Started from a
# normal terminal, however, the shell carries much more (ROS Jazzy is
# sourced there: PYTHONPATH, ROS_* variables AND a long LD_LIBRARY_PATH
# full of /opt/ros/... vendor libraries).  Even with the ROS variables
# removed by hand, that LD_LIBRARY_PATH could still make GTK/VTE load
# incompatible library copies -> the window appeared but nothing was
# clickable any more.  Removing variables one by one always risks
# missing the next one, so we now do what the desktop icon does:
# restart our own process with ONLY a small whitelist of variables.
# (This also protects against conda/venv environments in the terminal.)

# Marker so the relaunched copy does NOT relaunch itself again
# (that would be an endless loop).
CLEAN_ENV_MARKER = "OMNIQUAD_CLEAN_ENV"

# Everything the GUI and ssh actually need on Ubuntu.  Anything not in
# this list (ROS_*, LD_LIBRARY_PATH, PYTHONPATH, conda, ...) is thrown
# away, exactly like a fresh desktop-icon start.
ENV_VARS_TO_KEEP = [
    # graphics / desktop session
    "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_CURRENT_DESKTOP", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
    # user identity and locale
    "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TMPDIR",
    # harmless terminal info
    "TERM", "COLORTERM",
]

# A sane standard PATH (used if the original PATH is empty after we
# remove the ROS / conda / virtualenv entries from it).
STANDARD_PATH_ENTRIES = [
    "/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
    "/usr/bin", "/sbin", "/bin", "/snap/bin",
]


def relaunch_with_clean_environment():
    """Replace this process with a copy of itself in a clean environment.

    os.execve never returns: the same python3 starts main.py again, but
    now with only ENV_VARS_TO_KEEP (+ a cleaned PATH), which is exactly
    the situation the app is always happy in.  If the restart fails
    (very unusual), we simply continue with the fallback cleanup above.
    """
    new_env = {}
    for name in ENV_VARS_TO_KEEP:
        if name in os.environ:
            new_env[name] = os.environ[name]
    # Icon themes, gsettings and gio need a data dir; set the standard
    # one if the variable was missing for some reason.
    if "XDG_DATA_DIRS" not in new_env:
        new_env["XDG_DATA_DIRS"] = "/usr/local/share:/usr/share"
    # Keep the PATH, but without ROS / conda / virtualenv directories --
    # those can carry programs and libraries that confuse the app.
    path_parts = []
    for part in os.environ.get("PATH", "").split(":"):
        part_low = part.lower()
        if not part:
            continue
        if ("/opt/ros" in part_low or "conda" in part_low
                or "/envs/" in part_low or "/venv" in part_low):
            continue
        if part not in path_parts:
            path_parts.append(part)
    for part in STANDARD_PATH_ENTRIES:
        if part not in path_parts:
            path_parts.append(part)
    new_env["PATH"] = ":".join(path_parts)
    # Mark the new copy so it does not clean-restart again.
    new_env[CLEAN_ENV_MARKER] = "1"

    exe = sys.executable or "/usr/bin/python3"
    script = os.path.abspath(__file__)
    try:
        os.execve(exe, [exe, script] + sys.argv[1:], new_env)
    except OSError:
        pass   # could not restart: fall through to the simple cleanup


def main():
    if sys.platform == "win32":
        from windows_app import main as run
    else:
        if os.environ.get(CLEAN_ENV_MARKER) != "1":
            relaunch_with_clean_environment()
        clean_own_environment()
        from gtk_app import main as run
    run()


if __name__ == "__main__":
    main()
