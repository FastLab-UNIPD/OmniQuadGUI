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
from gi.repository import Gtk, GLib, Vte, Pango


# ----------------------------------------------------------------------------
# Constants and small helpers
# ----------------------------------------------------------------------------

# Where this script lives, so config.json is always found even if the
# app is started from another folder.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
COMMANDS_FILE = os.path.join(APP_DIR, "commands.json")

# How long (seconds) the quick connection check may take before we give
# up and report "cannot reach the drone".
CONNECT_TIMEOUT_SECONDS = 10


def load_config():
    """Read config.json.  If it is missing or broken, use safe defaults."""
    config = {
        "user": "rock",     # login name on the drone
        "ip": "",           # last successfully used IP (empty at first)
        "dry_run": True,    # stage 2 will use this for the shutdown button
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

        self.show_start_window()

    # ------------------------------------------------------------------
    # Start window
    # ------------------------------------------------------------------
    def show_start_window(self):
        """The first window: ask for the drone IP and the password."""
        window = Gtk.Window(title="OmniQuadGUI - connect to drone")
        window.set_border_width(12)
        window.set_default_size(430, 220)
        window.connect("destroy", Gtk.main_quit)

        # A vertical box: one row per input field.
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        window.add(vbox)

        vbox.add(Gtk.Label(label="Connect to the drone's onboard computer (SSH)"))

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
        user = self.config["user"]

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
        # Create a private temp folder that will hold the ssh "control
        # socket".  In stage 2 the background commands reuse the logged-in
        # connection through this socket (no second password needed).
        self.ssh_tmp_dir = tempfile.mkdtemp(prefix="omniquad-")
        self.control_socket = os.path.join(self.ssh_tmp_dir, "ssh-ctl")

        window = Gtk.Window(title="OmniQuadGUI - " + user + "@" + ip)
        window.set_default_size(1250, 750)
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

        # Now start the REAL interactive ssh session inside the terminal.
        self.start_ssh_session(ip, user)

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
        # If error is None, everything is fine and ssh output will simply
        # appear in the terminal.  Nothing more to do here.

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
        """Create the right-hand column: host buttons + container list."""
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.set_border_width(10)
        panel.set_size_request(300, -1)

        panel.add(Gtk.Label(label="Host commands (run on the drone)"))

        # The buttons are built from commands.json, not hard-coded.
        for entry in self.commands.get("host_buttons", []):
            button = Gtk.Button(label=entry["label"])
            action = entry.get("action", "")
            if action == "docker_ps":
                button.connect("clicked", self.on_check_containers)
            elif action == "start_container":
                button.connect("clicked", self.on_start_container)
            elif action == "stop_container":
                button.connect("clicked", self.on_stop_container)
            elif action == "shutdown":
                button.connect("clicked", self.on_shutdown_clicked)
                self.shutdown_button = button
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
        panel.add(Gtk.Label(label="Containers running (last check):"))
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

        panel.add(Gtk.Label(
            label="(Container shell and ROS buttons arrive in stage 3.)"))

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

    def set_drone_buttons_enabled(self, enabled):
        """Enable/disable all buttons that talk to the drone."""
        for button in self.drone_buttons:
            button.set_sensitive(enabled)

    # ------------------------------------------------------------------
    # Button: Check running containers
    # ------------------------------------------------------------------
    def on_check_containers(self, button):
        self.set_drone_buttons_enabled(False)
        self.run_ssh_background(
            'docker ps --format "{{.Names}}|{{.Status}}"',
            self.on_containers_result)

    def on_containers_result(self, success, returncode, output):
        """Show the docker ps result as a list in the window."""
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
            return
        for line in lines:
            if "|" in line:
                name, status = line.split("|", 1)
                self.add_container_row(name.strip(), status.strip())
        self.append_log("Found " + str(len(lines)) + " running container(s).")

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
        self.terminal.feed_child(b"\x03")   # Ctrl+C

        def type_exit():
            self.terminal.feed_child(b"exit\n")
            self.inside_container_shell = False
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

        # Abort if the check itself failed OR the container is still up.
        if (not success) or (name in output.splitlines()):
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

    # ------------------------------------------------------------------
    # Closing the app cleanly
    # ------------------------------------------------------------------
    def on_main_window_closed(self, window, event):
        """Runs when the user closes the main window.

        We ask the ssh master connection to exit (`ssh -O exit`), then
        remove the private temp folder with the control socket.
        """
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
