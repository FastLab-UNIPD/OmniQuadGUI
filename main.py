#!/usr/bin/env python3
"""
OmniQuadGUI -- stage 2
======================

A small GTK 3 desktop app that opens an SSH session (in an embedded
terminal) to the onboard computer of a drone (Radxa Rock 5B, ROS 2,
Docker).  Stage 1 gave us the start window, the connection check and
the always-visible terminal.  Stage 2 adds:

    4. A log area under the terminal: every command that the app runs
       in the background, and its output, appears there.
    5. "Host buttons" on the right:
         - Check running containers (shows a list in the window)
         - Start Fast-LIO container
         - Stop Fast-LIO container (a SAFE multi-step sequence)
         - Shutdown (two clicks: arm, then confirm; with dry_run it only
           prints what it would do)
       All of these run in background threads through the SAME ssh
       connection (ControlPath socket), so the window never freezes
       and no second password is ever needed.

Stage 3 will add the container shell and ROS topic buttons.

It does NOT control flight.  Use it only while the drone sits on the
desk with rotors removed / battery disconnected.

Dependencies (install once on Ubuntu):
    sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 sshpass

Run with:
    python3 main.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

# ---- GTK and VTE imports ---------------------------------------------------
# GTK is the graphical toolkit, VTE is the terminal widget that we embed
# in the window (the same widget used by GNOME Terminal).
import gi
gi.require_version("Gtk", "3.0")      # make sure we get GTK 3, not GTK 4
gi.require_version("Vte", "2.91")     # the VTE version Ubuntu ships for GTK 3
from gi.repository import Gtk, GLib, Vte, Pango, Gdk, GdkPixbuf


# ----------------------------------------------------------------------------
# Constants and small helpers
# ----------------------------------------------------------------------------

# Where this script lives, so config.json is always found even if the
# app is started from another folder.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
COMMANDS_FILE = os.path.join(APP_DIR, "commands.json")
LOGO_FILE = os.path.join(APP_DIR, "omniquadgui.png")   # app logo

# How long (seconds) the quick connection check may take before we give
# up and report "cannot reach the drone".
CONNECT_TIMEOUT_SECONDS = 10


def load_config():
    """Read config.json.  If it is missing or broken, use safe defaults."""
    config = {
        "user": "rock",     # login name on the drone
        "ip": "",           # last successfully used IP (empty at first)
        "dry_run": True,    # stage 2 will use this for the shutdown button
        # Demo login: with this user + password the GUI opens WITHOUT
        # connecting to the drone (commands are simulated).
        "demo_user": "demo",
        "demo_password": "demo",
    }
    try:
        with open(CONFIG_FILE, "r") as f:
            stored = json.load(f)
        # Only accept values we actually know about.
        for key in config:
            if key in stored:
                config[key] = stored[key]
    except (OSError, ValueError):
        pass  # first run, or the file is damaged: just use the defaults
    return config


def save_config(config):
    """Write config.json back to disk.

    IMPORTANT: the password is NEVER saved here (or anywhere else).
    Only harmless settings like the last-used IP are stored.
    """
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
    except OSError as error:
        # Not fatal -- the app still works, we just could not remember
        # the IP for next time.
        print("Could not save config.json:", error)


def build_ssh_argv(user, ip, extra_options=(), remote_command=None):
    """Build the list of arguments for an `ssh` command.

    Returns something like:
        ["ssh", "-o", "ControlMaster=yes", ..., "rock@192.168.1.50", "ls"]

    `remote_command` is either None (interactive login shell) or a list
    of words for a one-shot command on the drone.
    """
    argv = ["ssh"]
    for option in extra_options:
        argv.extend(["-o", option])
    argv.append(user + "@" + ip)
    if remote_command is not None:
        argv.extend(remote_command)
    return argv


def make_dot_button(dot_color, text):
    """A button whose label starts with a coloured dot (●).

    The dot is a coloured character inside the label, done with Pango
    markup.  Example: a green dot before "Start Fast-LIO container".
    """
    button = Gtk.Button()
    label = Gtk.Label()
    label.set_markup(
        '<span foreground="' + dot_color + '">&#9679;</span> ' + text)
    button.add(label)
    return button


def make_emoji_button(prefix, text):
    """A button whose label starts with an emoji (e.g. a whale or skull).

    `prefix` is a short string such as "\N{SPOUTING WHALE}".  The emoji
    comes from the system emoji font, so no colour markup is needed.
    """
    button = Gtk.Button()
    label = Gtk.Label()
    label.set_markup(prefix + " " + text)
    button.add(label)
    return button


def load_button_styles():
    """Register a few small CSS styles used by the buttons.

    The shutdown button gets a red background with white text (the same
    red family as GTK's destructive-action style, which the "Confirm
    shutdown" button uses).  The white "color" is the TEXT colour; the
    red only paints the background, never the text itself.
    """
    css = b"""
    .warm-action {
        background-color: #e01b24;
        color: #ffffff;
    }
    .warm-action:hover {
        background-color: #c81e26;
    }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def explain_ssh_error(returncode, output, ip, user):
    """Turn a failed SSH attempt into one clear, plain-English sentence.

    `output` is whatever ssh/sshpass printed (already lowercase-friendly).
    We look for well-known phrases inside it and pick a matching message.
    """
    text = output.lower()

    if returncode == 5 or "permission denied" in text:
        return ("Login failed: the password (or the user name) is wrong.\n"
                "Check the password and the user in config.json, then try again.")
    if "connection refused" in text:
        return ("The drone answered at " + ip + ", but nothing is listening "
                "on SSH port 22.\nThe SSH server on the drone may not be running.")
    if "timed out" in text or "connection timed out" in text:
        return ("Could not reach the drone at " + ip + " within "
                + str(CONNECT_TIMEOUT_SECONDS) + " seconds.\n"
                "Check that the drone computer is powered on and that this "
                "computer is on the same network (same Wi-Fi / same switch).")
    if "no route to host" in text or "network is unreachable" in text:
        return ("This computer cannot find a network path to " + ip + ".\n"
                "Check the IP address and that both machines are on the "
                "same network.")
    if "host key verification failed" in text:
        return ("The drone's identity key changed since the last connection.\n"
                "If you know why (reinstalled the system), remove the old "
                "entry with:\n  ssh-keygen -R " + ip)
    if "could not resolve hostname" in text or "name or service not known" in text:
        return ("The text \"" + ip + "\" is not a valid IP address or name.\n"
                "Check what you typed in the IP field.")

    # Unknown problem: show the exit code and the raw ssh output so the
    # user can still see what happened.
    return ("Connection failed (unknown reason, ssh exit code "
            + str(returncode) + ").\nTechnical details:\n" + output.strip())


# ----------------------------------------------------------------------------
# The application
# ----------------------------------------------------------------------------
# Everything lives in one class so a beginner can follow the flow from top
# to bottom: start window -> connect check -> main window with terminal.

class OmniQuadApp:

    def __init__(self):
        # Button colours and styles (dot buttons, warm shutdown button).
        load_button_styles()

        # Use the logo as the window icon (taskbar / title bar) for ALL
        # windows this app opens.
        try:
            Gtk.Window.set_default_icon_from_file(LOGO_FILE)
        except Exception:
            pass   # logo missing: windows just keep the default icon

        # ---- shared state ----
        self.config = load_config()          # user / ip / dry_run
        self.commands = load_config_commands()
        self.password = ""                   # only ever kept in memory
        self.control_socket = ""             # path of the shared ssh socket
        self.ssh_tmp_dir = ""                # folder that holds that socket
        self.main_window = None
        self.terminal = None

        # ---- state for stage 2 ----
        # True ONLY while we are inside the container's bash shell in the
        # terminal (set by "Enter container" in stage 3).  A bare `exit`
        # may only be typed when this is set, otherwise we would close
        # the whole ssh session.
        self.inside_container_shell = False
        # All buttons that talk to the drone; disabled after a real
        # shutdown was sent (the drone is off then).
        self.drone_buttons = []
        self.shutdown_button = None
        self.shutdown_confirm_box = None
        self.log_textview = None
        self.log_buffer = None
        self.container_listbox = None
        # Set to True once "sudo shutdown" was really sent, so the
        # "connection lost" message in the terminal stays calm.
        self.shutdown_was_sent = False
        # Demo mode state: pretend the Fast-LIO container is running.
        self.demo_container_running = True
        # Is the Fast-LIO container running (according to the last
        # docker ps check)?  None = not checked yet.  The "Enter
        # container" button is only usable when this is True.
        self.container_running = None
        # Demo mode: True when the demo login was used.  The GUI is then
        # fully usable but NOTHING connects to a drone; all remote
        # commands get simulated answers.
        self.demo_mode = False
        # The first window, so we can close it when the main one opens.
        self.start_window = None

        self.show_start_window()

    # ------------------------------------------------------------------
    # Start window
    # ------------------------------------------------------------------
    def show_start_window(self):
        """The first window: ask for the drone IP and the password."""
        window = Gtk.Window(title="OmniQuadGUI - connect to drone")
        window.set_border_width(12)
        window.set_default_size(430, 260)
        window.connect("destroy", Gtk.main_quit)
        self.start_window = window

        # ---- Background: the app logo, faded, behind the login form ---
        # Gtk.Overlay stacks widgets: the FIRST child added (the logo)
        # is drawn at the bottom, later overlay children (the form) are
        # drawn on top of it.
        overlay = Gtk.Overlay()
        window.add(overlay)

        logo_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            LOGO_FILE, 240, 240, True)   # True = keep the aspect ratio
        logo_image = Gtk.Image.new_from_pixbuf(logo_pixbuf)
        logo_image.set_opacity(0.2)      # very light: background only
        logo_image.set_halign(Gtk.Align.CENTER)
        logo_image.set_valign(Gtk.Align.CENTER)
        overlay.add(logo_image)          # bottom layer

        # A vertical box: one row per input field.
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        overlay.add_overlay(vbox)        # drawn on top of the logo

        vbox.add(Gtk.Label(label="Connect to the drone's onboard computer (SSH)"))

        # -- user row --
        hbox_user = Gtk.Box(spacing=8)
        hbox_user.add(Gtk.Label(label="User:"))
        self.user_entry = Gtk.Entry()
        self.user_entry.set_text(self.config["user"])  # default: rock
        self.user_entry.connect("activate", self.on_connect_clicked)
        hbox_user.pack_start(self.user_entry, True, True, 0)
        vbox.add(hbox_user)

        # -- IP row --
        hbox_ip = Gtk.Box(spacing=8)
        hbox_ip.add(Gtk.Label(label="Drone IP:"))
        self.ip_entry = Gtk.Entry()
        self.ip_entry.set_text(self.config["ip"])      # remembered last time
        self.ip_entry.set_placeholder_text("e.g. 192.168.1.50")
        # Pressing Enter in the IP field acts like clicking Connect.
        self.ip_entry.connect("activate", self.on_connect_clicked)
        hbox_ip.pack_start(self.ip_entry, True, True, 0)
        vbox.add(hbox_ip)

        # -- password row --
        hbox_pw = Gtk.Box(spacing=8)
        hbox_pw.add(Gtk.Label(label="Password:"))
        self.password_entry = Gtk.Entry()
        self.password_entry.set_visibility(False)      # mask the characters
        self.password_entry.connect("activate", self.on_connect_clicked)
        hbox_pw.pack_start(self.password_entry, True, True, 0)
        vbox.add(hbox_pw)

        # -- Connect button --
        self.connect_button = Gtk.Button(label="Connect")
        self.connect_button.connect("clicked", self.on_connect_clicked)
        vbox.add(self.connect_button)

        # -- status / error label --
        # Wraps and can grow tall, so plain-English error messages fit.
        self.status_label = Gtk.Label(label="")
        self.status_label.set_line_wrap(True)
        self.status_label.set_xalign(0.0)  # left-aligned text
        vbox.add(self.status_label)

        # Helpful hint: how to open the demo view without a drone.
        vbox.add(Gtk.Label(
            label="Tip: user '" + self.config["demo_user"] + "' with "
                  "password '" + self.config["demo_password"] + "' opens "
                  "a demo view without connecting (commands are simulated)."))
        hint_label = vbox.get_children()[-1]
        hint_label.set_line_wrap(True)
        hint_label.set_xalign(0.0)

        window.show_all()

    # ------------------------------------------------------------------
    # Connecting
    # ------------------------------------------------------------------
    def on_connect_clicked(self, widget):
        """Called when Connect is clicked (or Enter is pressed).

        We do the real SSH check in a BACKGROUND THREAD so the window
        never freezes.  When the check is finished, the thread asks the
        main thread (via GLib.idle_add) to continue.
        """
        ip = self.ip_entry.get_text().strip()
        password = self.password_entry.get_text()
        user = self.user_entry.get_text().strip()

        # ---- Demo login: open the GUI WITHOUT connecting ----
        # If the typed user and password match the demo credentials from
        # config.json, we skip ssh completely.  Nothing is written to
        # config.json (especially not a fake IP).
        if (user == self.config["demo_user"]
                and password == self.config["demo_password"]):
            self.demo_mode = True
            self.password = ""   # no real password exists in demo mode
            # The simulated container starts in the "running" state.
            self.container_running = True
            self.open_main_window(ip if ip else "demo", user)
            return

        # ---- Normal login: a real ssh connection is required ----
        self.demo_mode = False

        # Simple sanity checks before we even try ssh.
        if not ip:
            self.status_label.set_text("Please type the drone's IP address first.")
            return
        if not password:
            self.status_label.set_text("Please type the password first.")
            return

        # Disable the button while checking, show what we are doing.
        self.connect_button.set_sensitive(False)
        self.status_label.set_text("Connecting to " + user + "@" + ip + " ...")

        # Start the background check.  The password is handed to the
        # thread as a normal function argument and never leaves memory.
        thread = threading.Thread(
            target=self.connect_check_worker,
            args=(ip, user, password),
            daemon=True,
        )
        thread.start()

    def connect_check_worker(self, ip, user, password):
        """BACKGROUND THREAD: try one non-interactive ssh command.

        Command shape (password goes through the SSHPASS environment
        variable, never through the command line, never into any file):

            SSHPASS=<password> sshpass -e ssh \
                -o ConnectTimeout=10 \
                -o StrictHostKeyChecking=accept-new \
                rock@<ip> echo ok

        If this prints "ok", the IP, password and host key are all good.
        The real interactive session (opened afterwards in the main
        thread) will therefore log in without asking anything.
        """
        # Prepare a COPY of this process's environment with SSHPASS set.
        # sshpass -e reads the password from that variable.
        env = os.environ.copy()
        env["SSHPASS"] = password

        argv = ["sshpass", "-e"] + build_ssh_argv(
            user, ip,
            extra_options=(
                "ConnectTimeout=" + str(CONNECT_TIMEOUT_SECONDS),
                "StrictHostKeyChecking=accept-new",
            ),
            remote_command=["echo", "ok"],
        )

        try:
            result = subprocess.run(
                argv,
                env=env,
                capture_output=True,   # keep stdout/stderr out of the GUI
                text=True,
                timeout=CONNECT_TIMEOUT_SECONDS + 5,
            )
            success = (result.returncode == 0 and "ok" in result.stdout)
            output = result.stdout + result.stderr
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            success = False
            output = "connection timed out"
            returncode = 255
        except FileNotFoundError:
            # `sshpass` itself is not installed.
            GLib.idle_add(
                self.on_connect_finished, ip, user, password,
                False,
                "The program `sshpass` is not installed.\n"
                "Install it with:  sudo apt install sshpass",
            )
            return

        # Jump back into the MAIN thread to touch the GUI.
        GLib.idle_add(
            self.on_connect_finished, ip, user, password,
            success, explain_ssh_error(returncode, output, ip, user),
        )

    def on_connect_finished(self, ip, user, password, success, message):
        """MAIN THREAD: the background check is done; continue here."""
        if not success:
            # Re-enable the button and show the plain-English error.
            self.connect_button.set_sensitive(True)
            self.status_label.set_text(message)
            return False  # tells GLib.idle_add: do not call me again

        # Success: remember the working IP for next time.
        self.config["ip"] = ip
        save_config(self.config)

        # Remember what we need for the real session.
        self.password = password
        self.open_main_window(ip, user)
        return False

    # ------------------------------------------------------------------
    # Main window with the embedded terminal
    # ------------------------------------------------------------------
    def open_main_window(self, ip, user):
        """Build the main window: large terminal left, buttons right."""
        # Close the start window now that the login is done.
        # Its "destroy" signal was wired to Gtk.main_quit (quit the whole
        # app), so remove that connection first -- otherwise closing this
        # helper window would shut the program down.
        if self.start_window is not None:
            try:
                self.start_window.disconnect_by_func(Gtk.main_quit)
            except TypeError:
                pass
            self.start_window.destroy()
            self.start_window = None

        if self.demo_mode:
            # Demo: no connection, so no ssh socket is needed at all.
            self.ssh_tmp_dir = ""
            self.control_socket = ""
            title = "OmniQuadGUI - DEMO (not connected)"
        else:
            # Create a private temp folder that will hold the ssh "control
            # socket".  The background commands reuse the logged-in
            # connection through this socket (no second password needed).
            self.ssh_tmp_dir = tempfile.mkdtemp(prefix="omniquad-")
            self.control_socket = os.path.join(self.ssh_tmp_dir, "ssh-ctl")
            title = "OmniQuadGUI - " + user + "@" + ip

        window = Gtk.Window(title=title)
        # Wide enough for terminal + log on the left and the two button
        # columns on the right.
        window.set_default_size(1550, 750)
        window.connect("delete-event", self.on_main_window_closed)

        # A Paned widget splits the window into two resizable parts.
        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        window.add(pane)

        # -- LEFT: a vertical split: terminal on top, log area below -----
        # Paned = a divider the user can drag; it starts at 50/50.
        left_pane = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        pane.add1(left_pane)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC,   # horizontal scrollbar if needed
            Gtk.PolicyType.ALWAYS,      # vertical scrollbar (always useful)
        )
        scrolled.set_size_request(850, -1)  # terminal stays wide by default
        # resize=True: the terminal pane grows/shrinks with the window.
        left_pane.pack1(scrolled, True, False)

        terminal = Vte.Terminal()
        # A slightly bigger, fixed-size font makes the output readable.
        terminal.set_font(Pango.FontDescription("Monospace 11"))
        # Keep the last 5000 lines visible when scrolling back.
        terminal.set_scrollback_lines(5000)
        self.terminal = terminal
        scrolled.add(terminal)

        # -- LOG AREA: every background command and its output lands here.
        # It sits directly under the terminal in the lower half.
        log_frame = Gtk.Frame(label="Background command log")
        log_scrolled = Gtk.ScrolledWindow()
        log_scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        # Minimum height; the divider decides the rest.
        log_scrolled.set_size_request(-1, 120)
        self.log_textview = Gtk.TextView()
        self.log_textview.set_editable(False)    # read-only
        self.log_textview.set_cursor_visible(False)
        self.log_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_textview.override_font(Pango.FontDescription("Monospace 9"))
        self.log_buffer = self.log_textview.get_buffer()
        log_scrolled.add(self.log_textview)
        log_frame.add(log_scrolled)
        left_pane.pack2(log_frame, True, False)

        # Put the divider in the middle (50/50) once the window has its
        # real size.  GLib.idle_add runs it after the window is shown.
        def split_left_half():
            height = window.get_allocated_height()
            if height > 0:
                left_pane.set_position(height // 2)
            return False   # run only once

        # -- RIGHT: host buttons, container list (stage 3 adds more) -----
        button_box = self.build_right_panel()
        pane.add2(button_box)

        self.main_window = window
        window.show_all()

        # show_all() makes EVERY widget visible, including the shutdown
        # Confirm/Cancel row.  It must only appear after the user clicks
        # "Shutdown", so hide it again explicitly.
        self.shutdown_confirm_box.hide()
        # Same for the loading spinners: hidden until actually used.
        self.container_spinner.hide()
        self.topics_spinner.hide()

        # Put the left divider at 50/50 now that the real window size
        # is known (see split_left_half above).
        GLib.idle_add(split_left_half)

        # Right-click on the terminal opens a small Copy/Paste menu.
        terminal.connect("button-press-event", self.on_terminal_button_press)

        # Make sure the terminal (not the button panel) has the keyboard
        # focus right away, so you can type immediately.
        terminal.grab_focus()

        # Watch the ssh process.  If it ends for any reason (wrong
        # password, network drop, drone shut down, ...), VTE sends the
        # "child-exited" signal.  We print a clear message, otherwise the
        # terminal just looks empty and typing seems to do nothing.
        terminal.connect("child-exited", self.on_ssh_child_exited)

        # Start the session inside the terminal: the REAL interactive
        # ssh when connected, or only a demo banner in demo mode.
        if self.demo_mode:
            self.feed_demo_banner()
            # Check the (simulated) container state once, so the "Enter
            # container" button starts in the correct state.
            self.on_check_containers(None)
        else:
            self.start_ssh_session(ip, user)

    def feed_demo_banner(self):
        """In demo mode the terminal shows a banner instead of ssh.

        There is no ssh child process, so the terminal is not
        interactive here -- it only visualizes what would normally be
        the drone's shell.
        """
        banner = (
            "DEMO MODE - not connected to the drone.\n"
            "The buttons on the right work; the drone answers are SIMULATED.\n"
            "No ssh session is open, so this terminal is not interactive.\n"
        )
        self.terminal.feed(banner.encode())

    def on_ssh_child_exited(self, terminal, status, *args):
        """Called when the ssh process inside the terminal ends."""
        if self.shutdown_was_sent:
            # The drone closed the connection on purpose: this is the
            # expected result of "shutdown now", not an error.
            text = ("\n--- Connection closed (expected after shutdown). ---\n"
                    "The drone is shutting down.\n")
        else:
            text = ("\n--- The ssh session has ended (exit status "
                    + str(status) + "). ---\n"
                    "Close this window and connect again.\n")
        terminal.feed(text.encode())

    def on_terminal_button_press(self, terminal, event):
        """Right-click on the terminal opens a Copy / Paste menu.

        Keyboard shortcuts that already work in the terminal:
          Ctrl+Shift+C = copy the selection
          Ctrl+Shift+V = paste the clipboard
        (plain Ctrl+V does nothing in terminals -- that is normal).
        """
        if event.button != 3:      # 3 = right mouse button
            return False           # let the terminal handle other clicks
        menu = Gtk.Menu()
        copy_item = Gtk.MenuItem(label="Copy")
        copy_item.connect("activate",
                          lambda item: terminal.copy_clipboard())
        paste_item = Gtk.MenuItem(label="Paste")
        paste_item.connect("activate",
                           lambda item: terminal.paste_clipboard())
        menu.append(copy_item)
        menu.append(paste_item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True                # we handled the right click

    def start_ssh_session(self, ip, user):
        """Start `sshpass -e ssh ... rock@<ip>` inside the VTE terminal.

        This session is the "ControlMaster": it stays open as long as the
        window is open, and every later background command can reuse it
        through the control socket.
        """
        # Build the exact environment the ssh process will see.  We copy
        # the normal environment, then add SSHPASS.  The password never
        # appears on a command line and is never written to disk.
        envv = []
        for key, value in os.environ.items():
            if key == "SSHPASS":
                continue          # replace any pre-existing value
            envv.append(key + "=" + value)
        envv.append("SSHPASS=" + self.password)

        argv = ["sshpass", "-e"] + build_ssh_argv(
            user, ip,
            extra_options=(
                # This session IS the master connection.
                "ControlMaster=yes",
                "ControlPath=" + self.control_socket,
                # Accept unknown host keys automatically, but still warn
                # if a KNOWN key changes (safety).
                "StrictHostKeyChecking=accept-new",
            ),
        )

        # spawn_async runs `argv` with `envv` inside the terminal widget.
        # Parameter order (verified on this Ubuntu version):
        #   pty_flags, working_directory, argv, envv, spawn_flags,
        #   child_setup, child_setup_data, timeout, cancellable, callback
        self.terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,         # normal terminal behaviour
            None,                         # working directory (not needed)
            argv,                         # the command to run
            envv,                         # its environment (has SSHPASS)
            GLib.SpawnFlags.SEARCH_PATH,  # find `sshpass` in PATH
            None,                         # child setup function (not needed)
            None,                         # child setup data (not needed)
            -1,                           # use the default spawn timeout
            None,                         # cancellable (we never cancel)
            self.on_ssh_spawn_done,       # called when spawn finished/failed
        )

    def on_ssh_spawn_done(self, terminal, pid, error, *args):
        """Called by VTE once the ssh process was (or was not) started."""
        if error is not None:
            # The program could not even be started (e.g. sshpass missing).
            self.terminal.feed(
                ("Could not start ssh: " + error.message + "\n").encode()
            )
        else:
            # Connected! First thing: check the container state in the
            # background, so the "Enter container" button reflects
            # reality from the start.
            self.on_check_containers(None)

    # ==================================================================
    # STAGE 2: background command channel, log area and host buttons
    # ==================================================================

    # ------------------------------------------------------------------
    # The log area under the terminal
    # ------------------------------------------------------------------
    def append_log(self, text):
        """Add one entry to the log area (MAIN THREAD ONLY).

        Called either directly from button handlers or through
        GLib.idle_add from background threads.
        """
        stamp = time.strftime("[%H:%M:%S] ")
        end_iter = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end_iter, stamp + text.rstrip() + "\n")
        # Keep the newest lines visible.
        self.log_textview.scroll_mark_onscreen(self.log_buffer.get_insert())

    # ------------------------------------------------------------------
    # The right-hand button panel
    # ------------------------------------------------------------------
    def build_right_panel(self):
        """Create the right-hand area: TWO columns of buttons.

        Column 1: drone host commands (docker / nuc_helper / shutdown).
        Column 2: container shell + ROS buttons (typed into the
        terminal), with the topic list and the echo button together.
        """
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        columns.set_border_width(10)
        columns.pack_start(self.build_host_panel(), False, False, 0)
        columns.pack_start(self.build_ros_panel(), False, False, 0)
        return columns

    def build_host_panel(self):
        """Column 1: host commands + the container list display."""
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.set_size_request(290, -1)

        panel.add(Gtk.Label(label="Host commands (run on the drone)"))

        # The buttons are built from commands.json, not hard-coded.
        for entry in self.commands.get("host_buttons", []):
            action = entry.get("action", "")
            label = entry["label"]
            # "Start" gets a green dot, "Stop" a red dot, so you can tell
            # them apart at a glance.  The container check gets a whale,
            # and shutdown gets a skull, as quick visual reminders.
            if action == "start_container":
                button = make_dot_button("#2ecc71", label)
            elif action == "stop_container":
                button = make_dot_button("#e74c3c", label)
            elif action == "docker_ps":
                button = make_emoji_button("\N{SPOUTING WHALE}", label)
            elif action == "shutdown":
                button = make_emoji_button("\N{SKULL}", label)
            else:
                button = Gtk.Button(label=label)
            if action == "docker_ps":
                button.connect("clicked", self.on_check_containers)
            elif action == "start_container":
                button.connect("clicked", self.on_start_container)
            elif action == "stop_container":
                button.connect("clicked", self.on_stop_container)
            elif action == "shutdown":
                button.connect("clicked", self.on_shutdown_clicked)
                self.shutdown_button = button
                # Red background with white text (see load_button_styles).
                button.get_style_context().add_class("warm-action")
            else:
                # Unknown action: keep the button but make it do nothing
                # except log a note (safer than crashing).
                button.connect("clicked", self.on_unknown_action, action)
            self.drone_buttons.append(button)
            panel.add(button)

        # -- the shutdown "arm -> confirm" extra row (hidden at first) --
        # Clicking "Shutdown" hides that button and shows these two.
        self.shutdown_confirm_box = Gtk.Box(spacing=8)
        confirm = Gtk.Button(label="Confirm shutdown")
        confirm.get_style_context().add_class("destructive-action")
        confirm.connect("clicked", self.on_shutdown_confirm)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", self.on_shutdown_cancel)
        self.shutdown_confirm_box.pack_start(confirm, True, True, 0)
        self.shutdown_confirm_box.pack_start(cancel, True, True, 0)
        panel.add(self.shutdown_confirm_box)
        self.shutdown_confirm_box.hide()
        # NOTE: window.show_all() (called later in open_main_window)
        # would reveal this row again, so open_main_window hides it
        # explicitly right after showing the window.

        # -- container list (filled by "Check running containers") ------
        # The label sits in a row together with a small loading spinner
        # that appears only while the check is running.
        check_row = Gtk.Box(spacing=6)
        check_label = Gtk.Label(label="Containers running (last check):")
        check_label.set_xalign(0.0)
        self.container_spinner = Gtk.Spinner()
        check_row.pack_start(check_label, True, True, 0)
        check_row.pack_start(self.container_spinner, False, False, 0)
        panel.add(check_row)
        list_scrolled = Gtk.ScrolledWindow()
        list_scrolled.set_policy(
            Gtk.PolicyType.NEVER,       # no horizontal scrollbar needed
            Gtk.PolicyType.AUTOMATIC,
        )
        list_scrolled.set_size_request(-1, 140)
        self.container_listbox = Gtk.ListBox()
        # No selectable rows: this is a display, not a menu, and it must
        # never steal the keyboard focus from the terminal.
        self.container_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        list_scrolled.add(self.container_listbox)
        panel.add(list_scrolled)

        return panel

    def build_ros_panel(self):
        """Column 2: container shell buttons + ROS topics (clickable)."""
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.set_size_request(290, -1)

        panel.add(Gtk.Label(label="Container shell / ROS (terminal)"))

        # These buttons are typed INTO the terminal, so they only make
        # sense when the user is inside the container shell.  The "Enter"
        # button enables them; "Leave" disables them again.
        self.enter_container_button = None
        self.container_shell_buttons = []   # all buttons except "Enter"
        self.selected_topic = ""            # topic clicked in the list
        self.echo_selected_button = None    # placed next to the topic list

        for entry in self.commands.get("terminal_buttons", []):
            action = entry.get("action", "")
            label = entry["label"]
            button = Gtk.Button(label=label)
            if action == "enter_container":
                button.connect("clicked", self.on_enter_container)
                self.enter_container_button = button
            elif action == "leave_container":
                button.connect("clicked", self.on_leave_container)
                self.container_shell_buttons.append(button)
            elif action == "stop_command":
                button.connect("clicked", self.on_stop_command)
                self.container_shell_buttons.append(button)
            elif action == "list_topics":
                button.connect("clicked", self.on_list_topics)
                self.container_shell_buttons.append(button)
            elif action == "echo_selected" and not entry.get("command"):
                # No fixed command: echo whatever topic is selected.
                # This button is added AFTER the topic list below, so it
                # sits right next to what it acts on.
                button.connect("clicked", self.on_echo_selected_topic)
                self.container_shell_buttons.append(button)
                self.echo_selected_button = button
                continue   # not added to the panel in this loop
            else:
                # A fixed command typed into the terminal on each click.
                button.connect("clicked", self.on_send_to_terminal,
                               entry.get("command", ""))
                self.container_shell_buttons.append(button)
            panel.add(button)

        # ROS topic list (filled by "List available topics"), with its
        # own little loading spinner.
        topic_row = Gtk.Box(spacing=6)
        topic_label = Gtk.Label(label="ROS topics (click one to select):")
        topic_label.set_xalign(0.0)
        self.topics_spinner = Gtk.Spinner()
        topic_row.pack_start(topic_label, True, True, 0)
        topic_row.pack_start(self.topics_spinner, False, False, 0)
        panel.add(topic_row)

        topics_scrolled = Gtk.ScrolledWindow()
        topics_scrolled.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        topics_scrolled.set_size_request(-1, 120)
        self.topics_listbox = Gtk.ListBox()
        # SINGLE selection: one topic can be highlighted and echoed.
        self.topics_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.topics_listbox.connect("row-activated", self.on_topic_selected)
        topics_scrolled.add(self.topics_listbox)
        panel.add(topics_scrolled)

        # "Echo selected topic" right below the list it acts on.
        panel.add(self.echo_selected_button)

        # The ROS buttons start DISABLED: they only work inside the
        # container shell.  "Enter" additionally needs the container to
        # actually be RUNNING (checked with docker ps in the background);
        # until that first check has succeeded it stays disabled, too.
        self.update_shell_button_state()
        return panel

    def on_unknown_action(self, button, action):
        self.append_log(
            "Button with unknown action '" + action + "' in commands.json.")

    # ------------------------------------------------------------------
    # Background ssh command channel (reuses the logged-in connection)
    # ------------------------------------------------------------------
    def run_ssh_background(self, ssh_command, on_done, timeout_seconds=120):
        """Run one NON-INTERACTIVE ssh command in a background thread.

        Command shape (reuses the master connection through the control
        socket -- no password is needed a second time):

            ssh -o ControlPath=<socket> rock@<ip> "<command>"

        The GUI thread is never blocked.  When the command finishes:
          1. the command and its output are appended to the log area,
          2. `on_done(success, returncode, output)` is called in the
             MAIN thread so it can safely update the GUI.

        Never pass a password as part of `ssh_command`.
        """
        if self.demo_mode:
            # Demo: no drone is contacted at all.  The answer is produced
            # in the main thread with simulated output, so the whole GUI
            # (log, container list, stop/shutdown sequences) can be tried
            # out without any connection.
            GLib.idle_add(self.demo_run, ssh_command, on_done)
            return

        def worker():
            argv = [
                "ssh",
                "-o", "ConnectTimeout=10",
                "-o", "ControlPath=" + self.control_socket,
                self.config["user"] + "@" + self.config["ip"],
                ssh_command,
            ]
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
                success = (result.returncode == 0)
                output = result.stdout + result.stderr
                returncode = result.returncode
            except subprocess.TimeoutExpired:
                success = False
                output = ("Command did not finish within "
                          + str(timeout_seconds) + " seconds.")
                returncode = -1
            except OSError as error:
                success = False
                output = "Could not start ssh: " + str(error)
                returncode = -1

            # Back to the MAIN thread: first write to the log area ...
            GLib.idle_add(self.append_log,
                          "[remote] $ " + ssh_command + "\n" + output)
            # ... then deliver the result to the continuation callback.
            GLib.idle_add(on_done, success, returncode, output)

        threading.Thread(target=worker, daemon=True).start()

    def demo_run(self, ssh_command, on_done):
        """MAIN THREAD (demo only): answer a command with simulated output."""
        success, returncode, output = self.demo_answer(ssh_command)
        self.append_log("[demo] $ " + ssh_command + "\n" + output)
        on_done(success, returncode, output)
        # Keep the "Enter container" button in sync with the simulated
        # container state.
        self.container_running = self.demo_container_running
        self.update_shell_button_state()
        return False   # for GLib.idle_add: run only once

    def demo_answer(self, ssh_command):
        """Simulate the drone in demo mode.

        Keeps a tiny 'state' (is the container running?) so that start,
        stop and shutdown sequences behave like on the real drone.
        """
        name = self.commands["container_name"]

        # Which containers are running?
        if 'docker ps --format "{{.Names}}|{{.Status}}"' in ssh_command:
            if self.demo_container_running:
                return True, 0, name + "|Up 2 minutes (simulated)\n"
            return True, 0, ""
        if 'docker ps --format "{{.Names}}"' in ssh_command:
            if self.demo_container_running:
                return True, 0, name + "\n"
            return True, 0, ""

        # Start container: it becomes "running" in the simulated state.
        if ssh_command.startswith("./nuc_helper.sh run"):
            self.demo_container_running = True
            return True, 0, "Demo: container '" + name + "' started.\n"

        # Stop container: it becomes "not running" (or errors if it
        # was not running -- just like nuc_helper.sh does for real).
        if ssh_command.startswith("./nuc_helper.sh stoprm"):
            if self.demo_container_running:
                self.demo_container_running = False
                return True, 0, ("Demo: container '" + name
                                 + "' stopped and removed.\n")
            return False, 1, "Error: container is not running (demo).\n"

        # List ROS topics: a small, realistic-looking fake list.
        if "ros2 topic list" in ssh_command:
            return True, 0, ("/Odometry\n/livox/imu\n/livox/lidar\n/tf\n")

        # Anything else gets a neutral simulated answer.
        return True, 0, "(demo mode: simulated output for this command)\n"

    def set_drone_buttons_enabled(self, enabled):
        """Enable/disable all buttons that talk to the drone."""
        for button in self.drone_buttons:
            button.set_sensitive(enabled)

    # ------------------------------------------------------------------
    # Button: Check running containers
    # ------------------------------------------------------------------
    def on_check_containers(self, button):
        self.set_drone_buttons_enabled(False)
        # Show the small loading spinner next to the list label while
        # the check runs.
        self.container_spinner.show()
        self.container_spinner.start()
        self.run_ssh_background(
            'docker ps --format "{{.Names}}|{{.Status}}"',
            self.on_containers_result)

    def on_containers_result(self, success, returncode, output):
        """Show the docker ps result as a list in the window."""
        # The check is done: stop and hide the loading spinner.
        self.container_spinner.stop()
        self.container_spinner.hide()
        self.set_drone_buttons_enabled(True)
        self.clear_container_list()

        if not success and "permission denied" in output.lower():
            self.append_log(
                "Permission denied: the user '"
                + self.config["user"] + "' is not allowed to run docker "
                "on the drone.\n"
                "On the drone, add it to the docker group:\n"
                "  sudo usermod -aG docker " + self.config["user"]
                + "\nthen log out and back in on the drone.")
            return
        if not success:
            self.append_log(
                "Could not list containers (docker ps failed, exit code "
                + str(returncode) + "). See the log above for details.")
            return

        # Each output line looks like:  name|status
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines:
            self.append_log("No containers are running right now.")
            self.container_running = False
            self.update_shell_button_state()
            return
        for line in lines:
            if "|" in line:
                name, status = line.split("|", 1)
                self.add_container_row(name.strip(), status.strip())
        self.append_log("Found " + str(len(lines)) + " running container(s).")

        # Remember whether OUR container is among them: this decides
        # whether "Enter PX4 bridge container" may be used.
        names = [line.split("|", 1)[0].strip() for line in lines]
        self.container_running = (
            self.commands["container_name"] in names)
        self.update_shell_button_state()

    def clear_container_list(self):
        """Remove all rows from the container list display."""
        for child in self.container_listbox.get_children():
            self.container_listbox.remove(child)

    def add_container_row(self, name, status):
        """Add one 'name    status' row to the container list display."""
        row = Gtk.ListBoxRow()
        hbox = Gtk.Box(spacing=6)
        name_label = Gtk.Label(label=name)
        name_label.set_xalign(0.0)
        status_label = Gtk.Label(label=status)
        status_label.set_xalign(0.0)
        status_label.get_style_context().add_class("dim-label")
        hbox.pack_start(name_label, True, True, 0)
        hbox.pack_start(status_label, False, False, 0)
        row.add(hbox)
        self.container_listbox.add(row)
        self.container_listbox.show_all()

    # ------------------------------------------------------------------
    # Button: Start Fast-LIO container
    # ------------------------------------------------------------------
    def on_start_container(self, button):
        # Look the command up in commands.json (single source of truth).
        command = self.find_command("start_container")
        self.set_drone_buttons_enabled(False)
        self.append_log("Starting Fast-LIO container ...")
        self.run_ssh_background(command, self.on_start_result)

    def on_start_result(self, success, returncode, output):
        self.set_drone_buttons_enabled(True)
        if success:
            self.append_log("Start command finished OK.")
        else:
            self.append_log(
                "Start command FAILED (exit code "
                + str(returncode) + "). See the log above for details.")
        # Immediately check the containers again, so the list in the
        # window shows whether the Fast-LIO container is really up.
        self.on_check_containers(None)

    # ------------------------------------------------------------------
    # Button: Stop Fast-LIO container (SAFE multi-step sequence)
    # ------------------------------------------------------------------
    def on_stop_container(self, button):
        self.set_drone_buttons_enabled(False)
        self.append_log("Stop Fast-LIO container: checking state ...")
        self.run_ssh_background(
            'docker ps --format "{{.Names}}"', self.stop_check_done)

    def stop_check_done(self, success, returncode, output):
        """Step 1: is the container running at all?"""
        name = self.commands["container_name"]
        if not success:
            self.append_log(
                "Could not check the container state (docker ps failed). "
                "Stopped nothing, changed nothing.")
            self.set_drone_buttons_enabled(True)
            return
        if name not in output.splitlines():
            self.append_log(
                "Container '" + name + "' is already stopped. "
                "Nothing to do.")
            self.set_drone_buttons_enabled(True)
            return

        # Step 2: if our own "Enter container" was used, leave that shell
        # first, because `exit` inside it is safe (it leaves the
        # container, not the ssh session).
        if self.inside_container_shell:
            self.append_log("Leaving container shell first ...")
            self.leave_container_shell(self.run_stoprm)
        else:
            self.run_stoprm()

    def run_stoprm(self):
        """Step 3: let nuc_helper.sh stop and remove the container."""
        self.append_log("Running ./nuc_helper.sh stoprm ...")
        # It only errors if the container is not running, so an error
        # here is NOT fatal by itself -- the verification decides.
        self.run_ssh_background("./nuc_helper.sh stoprm", self.stoprm_done)

    def stoprm_done(self, success, returncode, output):
        """Step 4: verify the container is really gone."""
        if not success:
            self.append_log(
                "stoprm returned an error (can be normal); "
                "verifying the real state now ...")
        self.run_ssh_background(
            'docker ps --format "{{.Names}}"', self.stop_verify_done)

    def stop_verify_done(self, success, returncode, output):
        name = self.commands["container_name"]
        self.set_drone_buttons_enabled(True)
        # Update the running state from the verification result.
        self.container_running = (
            success and name in output.splitlines())
        self.update_shell_button_state()
        if not success:
            self.append_log(
                "WARNING: could not verify the container state "
                "(docker ps failed). Check manually with 'Check running "
                "containers'!")
            return
        if name in output.splitlines():
            self.append_log(
                "PROBLEM: container '" + name + "' is STILL running after "
                "stoprm! Do not assume it stopped.")
        else:
            self.append_log(
                "Verified: container '" + name + "' is no longer running. "
                "Stop complete.")

    def leave_container_shell(self, on_left):
        """Leave the container bash shell in the terminal.

        Sends Ctrl+C (stops anything running there), waits 0.5 s, then
        types `exit` (which ends the container shell, NOT the ssh
        session), and clears the inside_container_shell flag.
        `on_left` is called in the main thread afterwards.
        """
        if not self.demo_mode:
            self.terminal.feed_child(b"\x03")   # Ctrl+C

        def type_exit():
            if not self.demo_mode:
                self.terminal.feed_child(b"exit\n")
            self.inside_container_shell = False
            self.update_shell_button_state()   # ROS buttons off again
            on_left()
            return False   # tells GLib.timeout_add: do not call me again

        GLib.timeout_add(500, type_exit)    # 0.5 seconds

    # ------------------------------------------------------------------
    # Button: Shutdown (two clicks, with dry_run safety)
    # ------------------------------------------------------------------
    def on_shutdown_clicked(self, button):
        """First click: ARM. Show Confirm/Cancel instead of the button."""
        self.shutdown_button.hide()
        self.shutdown_confirm_box.show_all()

    def on_shutdown_cancel(self, button):
        """Cancel: back to the normal, harmless state."""
        self.shutdown_confirm_box.hide()
        self.shutdown_button.show()

    def on_shutdown_confirm(self, button):
        """Second click: start the real shutdown sequence."""
        self.shutdown_confirm_box.hide()
        self.shutdown_button.show()
        self.set_drone_buttons_enabled(False)
        self.append_log("SHUTDOWN SEQUENCE started ...")

        # Step 1 (same as stop steps 2-4, but ALWAYS): leave our own
        # container shell if we are inside one, then stoprm + verify.
        # This happens even when the container looks stopped already.
        if self.inside_container_shell:
            self.leave_container_shell(self.shutdown_stoprm)
        else:
            self.shutdown_stoprm()

    def shutdown_stoprm(self):
        self.run_ssh_background("./nuc_helper.sh stoprm",
                                self.shutdown_stoprm_done)

    def shutdown_stoprm_done(self, success, returncode, output):
        # The stoprm result is ignored here; only the verification
        # below may decide to abort.
        self.run_ssh_background(
            'docker ps --format "{{.Names}}"', self.shutdown_verify_done)

    def shutdown_verify_done(self, success, returncode, output):
        """SAFETY-CRITICAL: only a verified-stopped drone may shut down."""
        name = self.commands["container_name"]

        # Update the running state from the verification result, then
        # decide: abort if the check failed OR the container is still up.
        self.container_running = (
            success and name in output.splitlines())
        self.update_shell_button_state()
        still_running = (not success) or (name in output.splitlines())
        if still_running:
            message = ("SHUTDOWN ABORTED. Could NOT confirm that the "
                       "container '" + name + "' is stopped "
                       "(docker ps " +
                       ("failed)." if not success else "still shows it)."))
            self.append_log("!!!! " + message + " !!!!")
            self.show_loud_warning(message)
            self.set_drone_buttons_enabled(True)
            return

        self.append_log("Verified: container is stopped.")

        # dry_run (config.json, default true): print instead of doing it.
        if self.config["dry_run"]:
            self.append_log(
                "DRY RUN: would now run: sudo -S -p '' shutdown now "
                "(with the login password sent through ssh's standard "
                "input).\nNothing was shut down. Set \"dry_run\": false "
                "in config.json to do it for real.")
            self.set_drone_buttons_enabled(True)
            return

        # Real shutdown: password goes through the ssh process's
        # STANDARD INPUT, never in the command text, never in the log.
        self.run_shutdown_ssh()

    def run_shutdown_ssh(self):
        """Run `sudo -S -p '' shutdown now` on the drone.

        `sudo -S` reads the password from standard input, so we start
        ssh with input=self.password.  The password is nowhere in the
        command text, the log or any file.
        """
        if self.demo_mode:
            # Demo: there is no drone to shut down, but show exactly
            # what the GUI would look like afterwards.
            self.append_log(
                "Demo: shutdown simulated. Nothing happened "
                "(no drone is connected).")
            GLib.idle_add(self.on_shutdown_sent)
            return

        def worker():
            argv = [
                "ssh",
                "-o", "ConnectTimeout=10",
                "-o", "ControlPath=" + self.control_socket,
                self.config["user"] + "@" + self.config["ip"],
                "sudo -S -p '' shutdown now",
            ]
            try:
                # The password + Enter goes through stdin.  The
                # connection will drop during the command; ssh may then
                # exit with an error code -- that is EXPECTED here.
                subprocess.run(
                    argv,
                    input=self.password + "\n",
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass   # connection dropping is expected, not an error
            GLib.idle_add(self.on_shutdown_sent)

        threading.Thread(target=worker, daemon=True).start()

    def on_shutdown_sent(self):
        """MAIN THREAD: the shutdown command was sent."""
        self.shutdown_was_sent = True
        self.append_log("Shutdown sent, connection closed.")
        # The drone is going down: no button may talk to it anymore.
        for button in self.drone_buttons:
            button.set_sensitive(False)
        return False

    def show_loud_warning(self, message):
        """Pop up a big warning dialog (used when shutdown is aborted)."""
        dialog = Gtk.MessageDialog(
            parent=self.main_window,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="SAFETY WARNING - SHUTDOWN ABORTED",
        )
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    # ------------------------------------------------------------------
    # Small helper: find a command by action name in commands.json
    # ------------------------------------------------------------------
    def find_command(self, action):
        for entry in self.commands.get("host_buttons", []):
            if entry.get("action") == action:
                return entry.get("command", "")
        return ""

    def find_terminal_command(self, action):
        """Like find_command, but looks in the terminal_buttons section."""
        for entry in self.commands.get("terminal_buttons", []):
            if entry.get("action") == action:
                return entry.get("command", "")
        return ""

    # ==================================================================
    # STAGE 3: container shell buttons and ROS topic inspection
    # ==================================================================

    # ------------------------------------------------------------------
    # Helpers for typing into the terminal
    # ------------------------------------------------------------------
    def type_in_terminal(self, command):
        """Type a command (plus Enter) into the terminal, and log it.

        In demo mode there is no ssh session, so nothing is typed --
        only the log entry is written so the flow can still be tested.
        """
        self.append_log("[terminal] $ " + command)
        if self.demo_mode:
            self.append_log(
                "(demo: nothing typed - there is no ssh session)")
            return
        self.terminal.feed_child((command + "\n").encode())

    def send_ctrl_c(self):
        """Send Ctrl+C into the terminal (stops echo/hz/running command)."""
        self.append_log("[terminal] (sending Ctrl+C)")
        if self.demo_mode:
            self.append_log(
                "(demo: nothing sent - there is no ssh session)")
            return
        self.terminal.feed_child(b"\x03")   # byte 3 = Ctrl+C

    def update_shell_button_state(self):
        """Enable ROS buttons ONLY while inside the container shell.

        The 'Enter' button is the opposite: it is usable only when
        a) we are NOT inside yet, and
        b) the last docker ps check saw the container RUNNING.
           (Unknown / not running -> disabled: there is nothing to
           enter.)
        """
        inside = self.inside_container_shell
        can_enter = (self.container_running is True)
        self.enter_container_button.set_sensitive(
            (not inside) and can_enter)
        for shell_button in self.container_shell_buttons:
            shell_button.set_sensitive(inside)

    # ------------------------------------------------------------------
    # Button: Enter PX4 bridge container
    # ------------------------------------------------------------------
    def on_enter_container(self, button):
        if self.inside_container_shell:
            self.append_log("Already inside the container shell.")
            return
        command = self.find_terminal_command("enter_container")
        # NOTE: this assumes the terminal is at the drone's normal shell
        # prompt (which is true right after connecting).
        self.type_in_terminal(command)
        self.inside_container_shell = True
        self.update_shell_button_state()
        self.append_log(
            "Entered container shell. ROS buttons enabled. "
            "Use 'Leave container' to exit it -- please always use that "
            "button, never a bare 'exit'.")

    # ------------------------------------------------------------------
    # Button: Leave container
    # ------------------------------------------------------------------
    def on_leave_container(self, button):
        if not self.inside_container_shell:
            self.append_log(
                "Not inside the container shell (use 'Enter PX4 bridge "
                "container' first).")
            return
        # Block all shell buttons while leaving (double clicks could
        # type 'exit' twice).
        self.enter_container_button.set_sensitive(False)
        for shell_button in self.container_shell_buttons:
            shell_button.set_sensitive(False)
        self.leave_container_shell(self.on_left_container_shell)

    def on_left_container_shell(self):
        self.append_log("Left the container shell. ROS buttons disabled.")

    # ------------------------------------------------------------------
    # Button: Stop current command (Ctrl+C, needed for echo/hz)
    # ------------------------------------------------------------------
    def on_stop_command(self, button):
        self.send_ctrl_c()

    # ------------------------------------------------------------------
    # Button: List available topics (typed into terminal, then fetched)
    # ------------------------------------------------------------------
    def on_list_topics(self, button):
        """List ROS topics.

        On the real drone the command is typed INTO the terminal (we are
        inside the container there, so `ros2` is available), and after a
        short wait the printed topics are fetched from the terminal
        screen into the clickable list.
        In demo mode the answer is simulated as usual.
        """
        self.topics_spinner.show()
        self.topics_spinner.start()
        if self.demo_mode:
            # Demo: simulated topics, no terminal behind it.
            self.run_ssh_background(
                self.find_terminal_command("list_topics"),
                self.on_topics_result)
            return
        if not self.inside_container_shell:
            self.topics_spinner.stop()
            self.topics_spinner.hide()
            self.append_log(
                "Enter the container first ('Enter PX4 bridge "
                "container'), then list the topics again.")
            return
        self.type_in_terminal("ros2 topic list")
        # ros2 needs a moment (the first call even starts its daemon).
        # After that wait, read the printed topics from the screen.
        GLib.timeout_add(3000, self.fetch_topics_from_terminal)

    def fetch_topics_from_terminal(self):
        """MAIN THREAD: read the topics that 'ros2 topic list' printed.

        Reads the terminal's screen text and keeps every line that looks
        like a ROS topic (ROS 2 topic names start with '/').
        """
        self.topics_spinner.stop()
        self.topics_spinner.hide()
        result = self.terminal.get_text_format(Vte.Format.TEXT)
        # Depending on the VTE version this returns a plain string or a
        # (text, attributes) tuple -- handle both.
        screen_text = result[0] if isinstance(result, tuple) else result
        topics = []
        seen = set()
        for line in screen_text.splitlines():
            line = line.strip()
            if line.startswith("/") and line not in seen:
                seen.add(line)
                topics.append(line)
        if not topics:
            self.clear_topic_list()
            self.selected_topic = ""
            self.append_log(
                "No topics found on the terminal screen. Is the Fast-LIO "
                "container running its ROS nodes? The first call can "
                "take a few seconds -- try again shortly.")
            return False
        self.fill_topic_list(topics)
        return False

    def on_topics_result(self, success, returncode, output):
        """Demo mode: fill the topic list from the simulated answer."""
        self.topics_spinner.stop()
        self.topics_spinner.hide()
        if not success:
            self.clear_topic_list()
            self.selected_topic = ""
            self.append_log(
                "Could not list ROS topics (see the log above for "
                "details).")
            return
        topics = [line.strip() for line in output.splitlines() if line.strip()]
        self.fill_topic_list(topics)

    def fill_topic_list(self, topics):
        """Put the topics into the clickable list and log the count."""
        self.selected_topic = ""     # the old selection is gone
        self.clear_topic_list()
        for topic in topics:
            self.add_topic_row(topic)
        self.append_log(
            "Found " + str(len(topics)) + " ROS topic(s). Click one, "
            "then use 'Echo selected topic'.")

    def clear_topic_list(self):
        for child in self.topics_listbox.get_children():
            self.topics_listbox.remove(child)

    def add_topic_row(self, topic):
        row = Gtk.ListBoxRow()
        label = Gtk.Label(label=topic)
        label.set_xalign(0.0)
        row.add(label)
        self.topics_listbox.add(row)
        self.topics_listbox.show_all()

    def on_topic_selected(self, listbox, row):
        """A topic row was clicked: remember it for 'Echo selected topic'."""
        label = row.get_child()
        self.selected_topic = label.get_text()
        self.append_log("Topic selected: " + self.selected_topic)

    # ------------------------------------------------------------------
    # Button: Echo selected topic
    # ------------------------------------------------------------------
    def on_echo_selected_topic(self, button):
        if not self.selected_topic:
            self.append_log(
                "No topic selected: click a topic in the ROS topics "
                "list first.")
            return
        self.type_in_terminal("ros2 topic echo " + self.selected_topic)

    # ------------------------------------------------------------------
    # Buttons with fixed commands (typed into the terminal)
    # ------------------------------------------------------------------
    def on_send_to_terminal(self, button, command):
        """Type a fixed command from commands.json into the terminal."""
        if not command:
            self.append_log(
                "This button has no command configured in commands.json.")
            return
        self.type_in_terminal(command)

    # ------------------------------------------------------------------
    # Closing the app cleanly
    # ------------------------------------------------------------------
    def on_main_window_closed(self, window, event):
        """Runs when the user closes the main window.

        We ask the ssh master connection to exit (`ssh -O exit`), then
        remove the private temp folder with the control socket.
        """
        if self.control_socket:
            # Normal (connected) mode: politely close the master ssh.
            try:
                subprocess.run(
                    ["ssh", "-O", "exit",
                     "-o", "ControlPath=" + self.control_socket,
                     self.config["user"] + "@" + self.config["ip"]],
                    capture_output=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass  # ssh is already gone or stubborn -- not a problem

            shutil.rmtree(self.ssh_tmp_dir, ignore_errors=True)
        Gtk.main_quit()
        return False  # allow the window to close


def load_config_commands():
    """Read commands.json (button definitions).  Stage 1 does not use the
    buttons yet, but we check early that the file exists and is valid."""
    try:
        with open(COMMANDS_FILE, "r") as f:
            return json.load(f)
    except (OSError, ValueError) as error:
        print("Warning: could not read commands.json:", error)
        return {"host_buttons": [], "terminal_buttons": []}


def main():
    # Ctrl+C in the launching terminal should also close the app.
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, 2, Gtk.main_quit)

    try:
        app = OmniQuadApp()
        Gtk.main()
    except Exception:
        # A programming error should never just vanish (especially when
        # the app is started from a desktop icon, where there is no
        # console to print to).  Show it in a window instead.
        import traceback
        text = traceback.format_exc()
        print(text)  # also visible if the app was started from a terminal
        try:
            dialog = Gtk.MessageDialog(
                parent=None,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text="OmniQuadGUI stopped unexpectedly",
            )
            dialog.format_secondary_text(
                "Please report this text to whoever maintains the app:\n\n"
                + text[-1500:]
            )
            dialog.run()
            dialog.destroy()
        except Exception:
            pass  # even the dialog failed -- nothing more we can do


if __name__ == "__main__":
    main()
