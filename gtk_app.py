"""Ubuntu GTK 3 / VTE interface for OmniQuadGUI."""

import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from diagnostics import (CONTAINER_LIST, CommandConfigError, export_csv, load_commands, load_config,
                         save_endpoint, topic_list_command, topic_names)


# ---- GTK and VTE imports ---------------------------------------------------
# GTK is the graphical toolkit, VTE is the terminal widget that we embed
# in the window (the same widget used by GNOME Terminal).
import gi
gi.require_version("Gtk", "3.0")      # make sure we get GTK 3, not GTK 4
gi.require_version("Vte", "2.91")     # the VTE version Ubuntu ships for GTK 3
from gi.repository import Gtk, GLib, Vte, Pango, Gdk, GdkPixbuf

# The Foxglove bridge that runs on THIS computer (not on the drone).
from local_bridge import LocalBridge

# Colours, icons, button styles and the rotor loading animation.
from ui_theme import (GREEN_COLOR, RED_COLOR, BridgeStatus, ConnectionStatus, RotorSpinner, apply_theme,
                      load_icon, make_button, make_card, make_column_title, style_terminal,
                      style_window)


# ----------------------------------------------------------------------------
# Constants and small helpers
# ----------------------------------------------------------------------------

# Where this script lives, so config.json is always found even if the
# app is started from another folder.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_FILE = os.path.join(APP_DIR, "images", "omniquadgui.png")   # app logo

# How long (seconds) the quick connection check may take before we give
# up and report "cannot reach the drone".
CONNECT_TIMEOUT_SECONDS = 10
# While connected, the header bar's status dot is refreshed this often
# (seconds) with a tiny "are you still there?" command to the drone.
STATUS_CHECK_SECONDS = 5
MAX_LOG_LINES = 5000
MAX_LOG_CHARS = 512_000


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


def create_local_bridge(commands, config):
    """The local program from commands.json ("local_bridge"), or None."""
    local = commands.get("local_bridge")
    if not local:
        return None
    return LocalBridge(local["name"], local["command"], config.get("ros_setup", ""),
                       local.get("address", ""))


class WidgetGroup:
    """Several widgets that are switched on / off together.

    Used for "all the controls that talk to the drone": they are greyed
    out while a drone command runs or after the connection ended -- but
    the local Foxglove bridge card is NOT part of the group, so it stays
    usable all the time.
    """

    def __init__(self, *widgets):
        self.widgets = widgets

    def set_sensitive(self, enabled):
        for widget in self.widgets:
            widget.set_sensitive(enabled)

    def get_sensitive(self):
        return all(widget.get_sensitive() for widget in self.widgets)


def feed_text(terminal, text):
    """Print our own message text into a VTE terminal.

    A terminal needs "\r\n" (back to the start of the line + new line);
    a bare "\n" only moves DOWN, so every line would start where the
    previous one ended (a "staircase" of text).
    """
    terminal.feed(text.replace("\r\n", "\n").replace("\n", "\r\n").encode())


def fit_window_to_content(window, min_width, min_height, parts):
    """Give `window` a starting size that shows ALL of its contents.

    GTK can tell how much room the widgets inside the window would like
    ("natural size").  `parts` are the widgets stacked from top to
    bottom in the window (header bar, then the content), and we add
    up their sizes.  (We cannot ask the window itself: with a header
    bar, GTK only knows the window's size after it is on screen.)
    The window opens at least that big (and at least min_width x
    min_height), but never larger than the usable part of the screen
    (the "work area": the screen minus top bar / dock).
    Call this after all widgets were added, before window.show_all().
    """
    sizes = [part.get_preferred_size() for part in parts]
    minimum = Gtk.Requisition()
    minimum.width = max(size[0].width for size in sizes)
    minimum.height = sum(size[0].height for size in sizes)
    width = max(min_width, max(size[1].width for size in sizes))
    height = max(min_height, sum(size[1].height for size in sizes))

    display = Gdk.Display.get_default()
    monitor = None
    if display is not None:
        monitor = display.get_primary_monitor() or display.get_monitor(0)
    if monitor is not None:
        workarea = monitor.get_workarea()
        # Leave a small margin for the window's own title bar.
        width = min(width, workarea.width)
        height = min(height, workarea.height - 40)

    # Never go below what the widgets absolutely need.
    width = max(width, minimum.width)
    height = max(height, minimum.height)
    window.set_default_size(width, height)
    return width, height


def describe_network(ip):
    """Which network does this PC use to reach `ip`?  (any thread)

    Returns (short, details), for example:
        ("Wi-Fi: LabNet", "Interface wlp4s0 · this PC 192.168.1.40")
    1. `ip route get <ip>` tells which network interface (wlp4s0, eth0,
       ...) the packets leave through, and this PC's own address there.
    2. NetworkManager (`nmcli`) knows that interface's connection name
       (the Wi-Fi name, or e.g. "Wired connection 1").
    Every step is optional: if a tool is missing we show what we know.
    """
    device = ""
    local_ip = ""
    try:
        route = subprocess.run(["ip", "route", "get", ip], capture_output=True,
                               text=True, timeout=3).stdout.split()
        # Output looks like: "192.168.1.50 dev wlp4s0 src 192.168.1.40 ..."
        if "dev" in route:
            device = route[route.index("dev") + 1]
        if "src" in route:
            local_ip = route[route.index("src") + 1]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    if not device:
        return "network unknown", "Could not find a network route to " + ip

    kind = ""
    name = ""
    try:
        lines = subprocess.run(
            ["nmcli", "-g", "GENERAL.TYPE,GENERAL.CONNECTION", "device", "show", device],
            capture_output=True, text=True, timeout=3).stdout.splitlines()
        if len(lines) >= 2:
            kind, name = lines[0].strip(), lines[1].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    kind_text = {"wifi": "Wi-Fi", "ethernet": "Ethernet"}.get(kind, kind or device)
    if device == "lo":
        short = "this computer"
    elif name:
        short = kind_text + ": " + name
    else:
        short = kind_text
    details = "Interface " + device
    if local_ip:
        details += " · this PC " + local_ip
    return short, details


def explain_ssh_error(returncode, output, ip, user):
    """Turn a failed SSH attempt into one clear, plain-English sentence.

    `output` is whatever ssh/sshpass printed (already lowercase-friendly).
    We look for well-known phrases inside it and pick a matching message.
    """
    text = output.lower()

    if returncode == 5 or "permission denied" in text:
        return ("Login failed: the password (or the user name) is wrong.\n"
                "Check the user name and password in the login window, then try again.")
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

    def __init__(self, demo=False, local_bridge=None):
        # Use the logo as the window icon (taskbar / title bar) for ALL
        # windows this app opens.
        try:
            Gtk.Window.set_default_icon_from_file(LOGO_FILE)
        except Exception:
            pass   # logo missing: windows just keep the default icon

        # ---- shared state ----
        self.config = load_config()          # user / ip / dry_run
        self.commands = load_commands()
        # The local Foxglove bridge.  main() creates it ONCE and passes it
        # in, so it keeps running across "Reconnect" (a new OmniQuadApp).
        self.local_bridge = local_bridge or create_local_bridge(self.commands, self.config)
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
        self.shutdown_button = None
        self.shutdown_confirm_box = None
        self.log_textview = None
        self.log_buffer = None
        # Set to True once "sudo shutdown" was really sent, so the
        # "connection lost" message in the terminal stays calm.
        self.session_ended = False
        self.closed = False
        self.reconnect_requested = False
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
        # Separate echo windows (stage 3): one per running echo command,
        # so several topics can be echoed at the same time.
        self.echo_windows = []

        if demo:
            self.open_demo()
        else:
            self.show_start_window()

    # ------------------------------------------------------------------
    # Start window
    # ------------------------------------------------------------------
    def show_start_window(self):
        """The first window: ask for the drone IP and the password."""
        window = Gtk.Window(title="OmniQuadGUI - connect to drone")
        style_window(window)          # dark app look (see ui_theme.py)
        window.set_border_width(20)
        window.set_default_size(440, 260)
        window.connect("destroy", Gtk.main_quit)
        self.start_window = window

        # ---- Background: the app logo, faded, behind the login form ---
        # Gtk.Overlay stacks widgets: the FIRST child added (the logo)
        # is drawn at the bottom, later overlay children (the form) are
        # drawn on top of it.
        overlay = Gtk.Overlay()
        window.add(overlay)

        logo_image = Gtk.Image()
        try:
            logo_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(LOGO_FILE, 240, 240, True)
            logo_image.set_from_pixbuf(logo_pixbuf)
        except GLib.Error:
            pass  # Keep the login form available if the logo cannot be loaded.
        logo_image.set_opacity(0.12)     # very light: background only
        logo_image.set_halign(Gtk.Align.CENTER)
        logo_image.set_valign(Gtk.Align.CENTER)
        overlay.add(logo_image)          # bottom layer

        # A vertical box: title, the input fields, then the buttons.
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        overlay.add_overlay(vbox)        # drawn on top of the logo

        vbox.pack_start(make_column_title(
            "OmniQuadGUI", "Connect to the drone's onboard computer (SSH)"),
            False, False, 0)

        # The three fields sit in a grid, so the labels on the left and
        # the text boxes on the right line up exactly.  Each text box
        # shows a small icon at its left edge.
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.set_margin_top(6)
        vbox.pack_start(grid, False, False, 0)

        def add_field_row(row, text, icon_name):
            label = Gtk.Label(label=text)
            label.set_xalign(0.0)
            label.get_style_context().add_class("oq-dim")
            entry = Gtk.Entry()
            entry.set_hexpand(True)
            icon = load_icon(icon_name, 16, "#8d98ab")
            if icon is not None:
                entry.set_icon_from_gicon(Gtk.EntryIconPosition.PRIMARY, icon.props.gicon)
            entry.connect("activate", self.on_connect_clicked)
            grid.attach(label, 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            return entry

        # -- user row --
        self.user_entry = add_field_row(0, "User", "user")
        self.user_entry.set_text(self.config["user"])  # default: rock

        # -- IP row --  (Enter in any field acts like clicking Connect)
        self.ip_entry = add_field_row(1, "Drone IP", "globe")
        self.ip_entry.set_text(self.config["ip"])      # remembered last time
        self.ip_entry.set_placeholder_text("e.g. 192.168.1.50")

        # -- password row --
        self.password_entry = add_field_row(2, "Password", "lock")
        self.password_entry.set_visibility(False)      # mask the characters

        # -- Connect button --
        self.connect_button = make_button(
            "Connect", icon="connect", style="primary", centered=True)
        self.connect_button.set_margin_top(6)
        self.connect_button.connect("clicked", self.on_connect_clicked)
        vbox.pack_start(self.connect_button, False, False, 0)

        # -- status / error label, with the rotor while connecting --
        # Wraps and can grow tall, so plain-English error messages fit.
        status_row = Gtk.Box(spacing=8)
        self.connect_spinner = RotorSpinner()
        self.connect_spinner.set_valign(Gtk.Align.START)
        self.connect_spinner.set_no_show_all(True)   # visible only while connecting
        status_row.pack_start(self.connect_spinner, False, False, 0)
        self.status_label = Gtk.Label(label="")
        self.status_label.set_line_wrap(True)
        self.status_label.set_xalign(0.0)  # left-aligned text
        status_row.pack_start(self.status_label, True, True, 0)
        vbox.pack_start(status_row, False, False, 0)

        self.demo_button = make_button(
            "Open offline demo", icon="demo", centered=True)
        self.demo_button.connect("clicked", lambda button: self.open_demo())
        vbox.pack_start(self.demo_button, False, False, 0)

        # A Gtk.Overlay only asks for the size of its BOTTOM child (the
        # logo), never for the form drawn on top of it.  Without help the
        # window is too small and the lower part of the form is cut off.
        # A SizeGroup makes the logo ask for at least the form's size, so
        # the window always fits the whole form (also when a long error
        # message makes the status label taller).
        self.start_size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.BOTH)
        self.start_size_group.add_widget(logo_image)
        self.start_size_group.add_widget(vbox)

        window.show_all()

    def open_demo(self):
        self.demo_mode = True
        self.container_running = True
        self.open_main_window("demo", self.config["demo_user"])

    # ------------------------------------------------------------------
    # Connecting
    # ------------------------------------------------------------------
    def on_connect_clicked(self, widget):
        """Called when Connect is clicked (or Enter is pressed).

        We do the real SSH check in a BACKGROUND THREAD so the window
        never freezes.  When the check is finished, the thread asks the
        main thread (via GLib.idle_add) to continue.
        """
        if not self.connect_button.get_sensitive():
            return
        ip = self.ip_entry.get_text().strip()
        password = self.password_entry.get_text()
        user = self.user_entry.get_text().strip()

        # ---- Demo login: open the GUI WITHOUT connecting ----
        # If the typed user and password match the demo credentials from
        # config.json, we skip ssh completely.  Nothing is written to
        # config.json (especially not a fake IP).
        if (user == self.config["demo_user"]
                and password == self.config["demo_password"]):
            self.open_demo()
            return

        # ---- Normal login: a real ssh connection is required ----
        self.demo_mode = False

        # Simple sanity checks before we even try ssh.
        if not ip:
            self.status_label.set_text("Please type the drone's IP address first.")
            return
        if not user or user.startswith("-") or "@" in user:
            self.status_label.set_text("Please enter a valid SSH user name.")
            return
        if not password:
            self.status_label.set_text("Please type the password first.")
            return

        # Disable the button while checking, show what we are doing.
        self.connect_button.set_sensitive(False)
        self.demo_button.set_sensitive(False)
        self.status_label.set_text("Connecting to " + user + "@" + ip + " ...")
        self.connect_spinner.show()
        self.connect_spinner.start()

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
                text=True, errors="replace",
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
        except OSError as error:
            GLib.idle_add(self.on_connect_finished, ip, user, password, False, str(error))
            return

        # Jump back into the MAIN thread to touch the GUI.
        GLib.idle_add(
            self.on_connect_finished, ip, user, password,
            success, explain_ssh_error(returncode, output, ip, user),
        )

    def on_connect_finished(self, ip, user, password, success, message):
        """MAIN THREAD: the background check is done; continue here."""
        self.connect_spinner.stop()
        self.connect_spinner.hide()
        if not success:
            # Re-enable the button and show the plain-English error.
            self.connect_button.set_sensitive(True)
            self.demo_button.set_sensitive(True)
            self.status_label.set_text(message)
            return False  # tells GLib.idle_add: do not call me again

        # Success: remember the working IP for next time.
        self.config["user"] = user
        self.config["ip"] = ip

        # Remember what we need for the real session.
        self.password = password
        self.open_main_window(ip, user)
        try:
            save_endpoint(user, ip)
        except OSError as error:
            self.append_log("Could not remember this connection: " + str(error))
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
            self.password_entry.set_text("")
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
        style_window(window)          # dark app look (see ui_theme.py)
        # The starting size is chosen later (fit_window_to_content), once
        # all the buttons exist and GTK knows how much room they need.
        window.connect("delete-event", self.on_main_window_closed)

        # ---- Header bar (the window's title bar) -----------------------
        # Shows the app name and, on the right, a thin connection strip:
        # a green/amber/red dot, who we are connected to, and the network
        # this PC uses to reach the drone (see ConnectionStatus).
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("OmniQuadGUI")
        try:
            header_logo = Gtk.Image.new_from_pixbuf(
                GdkPixbuf.Pixbuf.new_from_file_at_scale(LOGO_FILE, 26, 26, True))
            header.pack_start(header_logo)
        except GLib.Error:
            pass   # no logo: the header bar simply has none
        self.connection_status = ConnectionStatus()
        self.status_check_running = False
        if self.demo_mode:
            self.connection_status.set_state("waiting", "Demo")
            self.connection_status.set_target(user + " (simulated)")
            self.connection_status.set_network(
                "no network used", "Offline demo: nothing connects to a drone.")
        else:
            self.connection_status.set_state("waiting", "Connecting…")
            self.connection_status.set_target(user + "@" + ip)
            self.connection_status.set_network("…")
            self.refresh_network_info()
        header.pack_end(self.connection_status)
        window.set_titlebar(header)

        # A Paned widget splits the window into two resizable parts.
        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        window.add(layout)
        layout.pack_start(pane, True, True, 0)

        # ---- Footer bar: CSV export on the left, Reconnect on the right
        footer = Gtk.ActionBar()
        layout.pack_end(footer, False, False, 0)
        export_button = make_button("Export topics CSV…", icon="export")
        export_button.connect("clicked", self.on_export_csv, "topics")
        footer.pack_start(export_button)
        self.reconnect_button = make_button(
            "Reconnect…", icon="reconnect", style="primary")
        self.reconnect_button.set_no_show_all(True)
        self.reconnect_button.connect("clicked", self.on_reconnect)
        footer.pack_end(self.reconnect_button)

        # -- LEFT: a vertical split: terminal on top, log area below -----
        # Paned = a divider the user can drag; it starts at 50/50.
        left_pane = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        left_pane.set_margin_start(10)
        left_pane.set_margin_top(10)
        left_pane.set_margin_bottom(10)
        # resize=True: extra window width goes to the terminal side;
        # shrink=False: the terminal side is never cut off.
        pane.pack1(left_pane, True, False)

        terminal_card, terminal_body = make_card("Drone terminal", icon="terminal")
        terminal_card.set_margin_bottom(4)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC,   # horizontal scrollbar if needed
            Gtk.PolicyType.ALWAYS,      # vertical scrollbar (always useful)
        )
        scrolled.set_size_request(240, -1)
        terminal_body.pack_start(scrolled, True, True, 0)
        # resize=True: the terminal pane grows/shrinks with the window.
        left_pane.pack1(terminal_card, True, False)

        terminal = Vte.Terminal()
        # A slightly bigger, fixed-size font makes the output readable.
        terminal.set_font(Pango.FontDescription("Monospace 11"))
        # Keep the last 5000 lines visible when scrolling back.
        terminal.set_scrollback_lines(5000)
        style_terminal(terminal)      # dark colours matching the window
        self.terminal = terminal
        scrolled.add(terminal)

        # -- LOG AREA: every background command and its output lands here.
        # It sits directly under the terminal in the lower half.
        follow = Gtk.CheckButton(label="Follow log")
        follow.set_active(True)
        log_card, log_body = make_card(
            "Background command log", icon="log", extra=follow)
        log_card.set_margin_top(4)
        log_scrolled = Gtk.ScrolledWindow()
        log_scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC,
            Gtk.PolicyType.AUTOMATIC,
        )
        log_scrolled.get_style_context().add_class("oq-well")
        # Minimum height; the divider decides the rest.
        log_scrolled.set_size_request(-1, 120)
        self.log_textview = Gtk.TextView()
        self.log_textview.set_editable(False)    # read-only
        self.log_textview.set_cursor_visible(False)
        self.log_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_textview.set_monospace(True)
        self.log_textview.set_left_margin(8)
        self.log_textview.set_top_margin(6)
        self.log_buffer = self.log_textview.get_buffer()
        log_scrolled.add(self.log_textview)
        adjustment = log_scrolled.get_vadjustment()
        def scroll_to_latest(*args):
            if follow.get_active():
                adjustment.set_value(max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()))
        adjustment.connect("changed", scroll_to_latest)
        follow.connect("toggled", scroll_to_latest)
        log_body.pack_start(log_scrolled, True, True, 0)
        left_pane.pack2(log_card, True, False)

        # Put the divider in the middle (50/50) once the window has its
        # real size.  GLib.idle_add runs it after the window is shown.
        def split_left_half():
            if self.closed:
                return False
            height = window.get_allocated_height()
            if height > 0:
                left_pane.set_position(height // 2)
            return False   # run only once

        # -- RIGHT: all the button cards (see build_right_panel) ----------
        right_panel = self.build_right_panel()
        sidebar = Gtk.ScrolledWindow()
        # NEVER scroll sideways: the sidebar then always asks for the full
        # width of the button columns, so no button is hidden at startup.
        # (Vertical scrolling stays possible for very small screens.)
        sidebar.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar.set_overlay_scrolling(True)
        sidebar.add(right_panel)
        # resize=False, shrink=False: extra window width goes to the
        # terminal, and the divider can never squeeze the buttons away.
        pane.pack2(sidebar, False, False)
        sidebar.set_propagate_natural_width(True)
        sidebar.set_propagate_natural_height(True)

        # Open the window big enough for ALL the buttons, but never
        # bigger than the screen (see fit_window_to_content).
        # GTK reports a size of 0 for hidden widgets, so make the CONTENT
        # visible first (the window itself only appears further below).
        # (Then hide again what must stay hidden, or it would be measured
        # too: e.g. the Confirm shutdown pair would widen the host grid.)
        header.show_all()
        layout.show_all()
        self.hide_until_needed()
        window_width, window_height = fit_window_to_content(
            window, 1360, 720, parts=[header, layout])
        # Put the left/right divider so the button panel gets exactly the
        # width it needs and the terminal gets all the rest.
        sidebar_width = sidebar.get_preferred_width()[1]
        handle_width = pane.style_get_property("handle-size")
        pane.set_position(window_width - sidebar_width - handle_width)

        self.main_window = window
        # show() and NOT show_all(): the content is already visible (see
        # above), and show_all() would briefly reveal the hidden widgets
        # again -- the window would then open too wide and stay that way.
        window.show()

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
            self.controls.set_sensitive(False)
            self.start_ssh_session(ip, user)

    def hide_until_needed(self):
        """Hide the widgets that only appear in special moments.

        show_all() makes EVERY widget visible, including the shutdown
        Confirm/Cancel pair (it must only appear after the user clicks
        "Shutdown") and the loading rotors (only while loading).
        """
        self.shutdown_confirm_box.hide()
        self.topics_spinner.hide()

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
        feed_text(self.terminal, banner)

    def on_ssh_child_exited(self, terminal, status, *args):
        """Called when the ssh process inside the terminal ends."""
        if self.closed:
            return
        self.session_ended = True
        self.controls.set_sensitive(False)
        self.reconnect_button.show()
        self.connection_status.set_state("offline", "Disconnected")
        if self.shutdown_was_sent:
            # The drone closed the connection on purpose: this is the
            # expected result of "shutdown now", not an error.
            text = ("\n--- Connection closed (expected after shutdown). ---\n"
                    "The drone is shutting down.\n")
        else:
            text = ("\n--- The ssh session has ended (exit status "
                    + str(status) + "). ---\n"
                    "Choose Reconnect to start a new session.\n")
        feed_text(terminal, text)

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

    def build_spawn_envv(self):
        """Build the environment list for ssh processes started here.

        Copies the normal environment and adds SSHPASS, so `sshpass -e`
        (or a fallback password prompt) can always authenticate.  The
        password never appears on a command line or on disk.
        """
        envv = []
        for key, value in os.environ.items():
            if key == "SSHPASS":
                continue          # replace any pre-existing value
            envv.append(key + "=" + value)
        envv.append("SSHPASS=" + self.password)
        return envv

    def start_ssh_session(self, ip, user):
        """Start `sshpass -e ssh ... rock@<ip>` inside the VTE terminal.

        This session is the "ControlMaster": it stays open as long as the
        window is open, and every later background command can reuse it
        through the control socket.
        """
        # Build the exact environment the ssh process will see.  We copy
        # the normal environment, then add SSHPASS.  The password never
        # appears on a command line and is never written to disk.
        envv = self.build_spawn_envv()

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
        if self.closed:
            return
        if error is not None:
            # The program could not even be started (e.g. sshpass missing).
            feed_text(self.terminal, "Could not start ssh: " + error.message + "\n")
            self.session_ended = True
            self.reconnect_button.show()
            self.connection_status.set_state("offline", "Disconnected")
        else:
            # Connected! First thing: check the container state in the
            # background, so the "Enter container" button reflects
            # reality from the start.
            # VTE reports process creation before SSH authenticates. Wait for
            # the master socket before sending the first background request.
            deadline = time.monotonic() + 15
            def wait_for_master():
                if self.session_ended:
                    return False
                if os.path.exists(self.control_socket):
                    self.controls.set_sensitive(True)
                    self.connection_status.set_state("live", "Connected")
                    # From now on, re-check the connection regularly.
                    GLib.timeout_add_seconds(STATUS_CHECK_SECONDS, self.check_connection)
                    self.on_check_containers(None)
                    return False
                if time.monotonic() >= deadline:
                    self.append_log("The SSH control connection is not ready. Choose Reconnect to try again.")
                    self.reconnect_button.show()
                    self.connection_status.set_state("offline", "Not ready")
                    return False
                return True
            GLib.timeout_add(100, wait_for_master)

    # ==================================================================
    # STAGE 2: background command channel, log area and host buttons
    # ==================================================================

    # ------------------------------------------------------------------
    # Connection status in the header bar
    # ------------------------------------------------------------------
    def refresh_network_info(self):
        """Look up the network to the drone in the background, then show it."""
        ip = self.config["ip"]

        def worker():
            short, details = describe_network(ip)
            GLib.idle_add(self.show_network_info, short, details)

        threading.Thread(target=worker, daemon=True).start()

    def show_network_info(self, short, details):
        if not self.closed:
            self.connection_status.set_network(
                short, "Drone " + self.config["ip"] + "\n" + details)
        return False

    def check_connection(self):
        """Every few seconds: is the drone still answering?  (MAIN THREAD)

        Sends the harmless command `true` through the existing master
        connection (no password, nothing typed into the terminal, nothing
        written to the log).  Answer -> green "Connected"; no answer
        within a few seconds -> red "No response" (e.g. Wi-Fi dropped).
        The network name is refreshed at the same time.
        Returns False to stop the timer once the session is over.
        """
        if self.closed or self.session_ended:
            return False
        if self.status_check_running:
            return True          # the previous check is still waiting
        self.status_check_running = True
        argv = build_ssh_argv(
            self.config["user"], self.config["ip"],
            extra_options=("ConnectTimeout=4", "BatchMode=yes",
                           "ControlPath=" + self.control_socket),
            remote_command=["true"])
        ip = self.config["ip"]

        def worker():
            try:
                answered = subprocess.run(argv, capture_output=True,
                                          timeout=6).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                answered = False
            short, details = describe_network(ip)
            GLib.idle_add(self.on_connection_checked, answered, short, details)

        threading.Thread(target=worker, daemon=True).start()
        return True              # keep the timer running

    def on_connection_checked(self, answered, short, details):
        self.status_check_running = False
        if self.closed or self.session_ended:
            return False
        if answered:
            self.connection_status.set_state("live", "Connected")
        else:
            self.connection_status.set_state("offline", "No response")
        self.show_network_info(short, details)
        return False

    # ------------------------------------------------------------------
    # The log area under the terminal
    # ------------------------------------------------------------------
    def append_log(self, text):
        """Add one entry to the log area (MAIN THREAD ONLY).

        Called either directly from button handlers or through
        GLib.idle_add from background threads.
        """
        if self.closed:
            return
        stamp = time.strftime("[%H:%M:%S] ")
        buffer = self.log_buffer
        buffer.insert(buffer.get_end_iter(), stamp + text[-MAX_LOG_CHARS:].rstrip() + "\n")
        excess = buffer.get_char_count() - MAX_LOG_CHARS
        if excess > 0:
            buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_offset(excess))
        excess_lines = buffer.get_line_count() - MAX_LOG_LINES
        if excess_lines > 0:
            buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_line(excess_lines))

    # ------------------------------------------------------------------
    # The right-hand button panel
    # ------------------------------------------------------------------
    def build_right_panel(self):
        """Create the right-hand area.

            +-----------------------+
            | DRONE HOST            |      (free for now)
            | [● bridge status  ⟳] |
            | [Start/Stop bridge]   |
            +-----------------------+--------------------------+
            | CONTAINER SHELL       | ROS TOPICS               |
            | DIAGNOSTICS           |  (tall list, most room)  |
            | [Stop current command]|                          |
            | [Shutdown]            |                          |
            +-----------------------+--------------------------+

        Each block is a rounded "card" (see make_card in ui_theme.py);
        every button shows an icon and its text.
        """
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(10)
        # These lists are filled by the builders below.
        self.container_shell_buttons = []   # all shell buttons except Enter
        # The big "Stop current command" button: bottom of the shell
        # column; create it first so the builders can see it.
        self.stop_command_button = self.make_stop_command_button()

        # Top row: the small Drone host card, then the local Foxglove
        # bridge card; the rest of the row is left EMPTY for now.
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        host_card = self.build_host_panel()
        top.pack_start(host_card, False, False, 0)
        local_card = self.build_local_bridge_panel()
        if local_card is not None:
            top.pack_start(local_card, False, False, 0)
        outer.pack_start(top, False, False, 0)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        # The shell column keeps its natural width; the topics card gets
        # all the remaining width (expand=True), so topic names fit.
        shell_panel = self.build_shell_panel()
        bottom.pack_start(shell_panel, False, False, 0)
        bottom.pack_start(self.build_topics_panel(), True, True, 0)
        outer.pack_start(bottom, True, True, 0)

        # The host card is exactly as wide as the shell column under it,
        # so the two line up.
        # (The Foxglove card gets the same width, so the top row is tidy.)
        self.left_column_width = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.left_column_width.add_widget(host_card)
        self.left_column_width.add_widget(shell_panel)
        if local_card is not None:
            self.left_column_width.add_widget(local_card)

        # Everything that talks to the DRONE is switched on/off together
        # (set_drone_buttons_enabled).  The local Foxglove card is not in
        # this group: it must keep working even without a connection.
        self.controls = WidgetGroup(host_card, bottom)

        # The ROS buttons start DISABLED: they only work inside the
        # container shell.  "Enter" additionally needs the container to
        # actually be RUNNING (checked with docker ps in the background);
        # until that first check has succeeded it stays disabled, too.
        self.update_shell_button_state()
        return outer

    # ------------------------------------------------------------------
    # Local Foxglove bridge (runs on THIS computer, not on the drone)
    # ------------------------------------------------------------------
    def build_local_bridge_panel(self):
        """The FOXGLOVE BRIDGE card (top row, next to Drone host).

            FOXGLOVE BRIDGE
            [~] foxglove_bridge                  (refresh)
                RUNNING · ws://localhost:8765
            [ Stop Foxglove bridge ]

        Same look as the Drone host card: a status label (green running /
        red stopped) and ONE Start/Stop button.  The label follows the
        program the app started; the refresh button looks closer (is the
        port really open?  is a copy running that the app did not
        start?) and explains the result in the log.
        Returns None if commands.json has no "local_bridge" section.
        """
        if self.local_bridge is None:
            return None
        local = self.commands["local_bridge"]
        card, body = make_card(local.get("title", "Local bridge"), icon="laptop")
        body.set_spacing(8)

        address = local.get("address", "")
        self.local_bridge_status = BridgeStatus(
            self.local_bridge.name, icon="broadcast",
            refresh_tooltip="Check the " + local.get("title", "local bridge")
                            + " (process and port)",
            on_refresh=self.on_check_local_bridge,
            running_text="RUNNING · " + address if address else "RUNNING")
        if address:
            self.local_bridge_status.set_tooltip_text(
                "Runs on this computer. Connect Foxglove to " + address)
        body.pack_start(self.local_bridge_status, False, False, 0)

        self.start_local_button = make_button(
            local.get("start_label", "Start"), icon="play", icon_color=GREEN_COLOR)
        self.start_local_button.connect("clicked", self.on_start_local_bridge)
        self.stop_local_button = make_button(
            local.get("stop_label", "Stop"), icon="stop", icon_color=RED_COLOR)
        self.stop_local_button.connect("clicked", self.on_stop_local_bridge)
        # Show both first: a Gtk.Stack can only switch to a visible child.
        self.start_local_button.show_all()
        self.stop_local_button.show_all()
        self.local_toggle = Gtk.Stack()
        self.local_toggle.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.local_toggle.add_named(self.start_local_button, "start")
        self.local_toggle.add_named(self.stop_local_button, "stop")
        body.pack_start(self.local_toggle, False, False, 0)

        # From now on, this window shows the bridge's output and state.
        # First the lines printed before the window opened, then live.
        for line in self.local_bridge.recent_lines:
            self.show_local_bridge_output(line)
        self.local_bridge.on_output = self.show_local_bridge_output
        self.local_bridge.on_state_changed = self.update_local_bridge_widgets
        self.update_local_bridge_widgets()
        return card

    def show_local_bridge_output(self, line):
        """One line printed by the Foxglove bridge -> the command log."""
        self.append_log("[foxglove] " + line)

    def update_local_bridge_widgets(self):
        """Show the local bridge's state in its card."""
        if self.closed:
            return
        running = self.local_bridge.is_running()
        self.local_bridge_status.set_checking(False)
        self.local_bridge_status.set_state("running" if running else "stopped")
        self.local_toggle.set_visible_child_name("stop" if running else "start")
        self.start_local_button.set_sensitive(True)
        self.stop_local_button.set_sensitive(True)

    def on_start_local_bridge(self, button):
        self.local_bridge.start()

    def on_check_local_bridge(self, button):
        """Refresh button: look at the bridge (in the background)."""
        self.local_bridge_status.set_checking(True)
        bridge = self.local_bridge

        def work():
            result = bridge.check()
            GLib.idle_add(self.on_local_bridge_checked, result)
        threading.Thread(target=work, daemon=True).start()

    def on_local_bridge_checked(self, result):
        """MAIN THREAD: show what the refresh check found."""
        if self.closed:
            return False
        if self.local_bridge.stopping and self.local_bridge.is_running():
            return False          # a Stop is in progress; it updates the card
        # It may have started or ended while we looked: trust "now".
        if (result["own_pid"] is not None) != self.local_bridge.is_running():
            self.update_local_bridge_widgets()
            return False
        state, text, message = self.local_bridge.describe_check(result)
        self.local_bridge_status.set_checking(False)
        self.local_bridge_status.set_state(state, text)
        self.show_local_bridge_output(message)
        return False

    def on_stop_local_bridge(self, button):
        # Stopping takes a moment (ros2 launch shuts its nodes down):
        # spin the rotor and block the button until it has ended.
        self.stop_local_button.set_sensitive(False)
        self.local_bridge_status.set_checking(True)
        self.local_bridge.stop()

    def make_stop_command_button(self):
        """Create the BIG 'Stop current command' button (sends Ctrl+C).

        It belongs to the container shell buttons (enabled only while
        inside the container shell) and sits at the bottom of the shell
        column, because it is needed often (echo/hz run forever until
        stopped).  Amber outline = "stop", but not dangerous.
        """
        entry = self.find_terminal_entry("stop_command")
        button = make_button(entry.get("label", "Stop current command"),
                             icon=entry.get("icon"), style="warning",
                             big=True, centered=True)
        button.connect("clicked", self.on_stop_command)
        self.container_shell_buttons.append(button)
        return button

    def build_host_panel(self):
        """The small DRONE HOST card (top left).

            DRONE HOST
            [=] px4_fastlio_bridge            (refresh)
                RUNNING
            [ Stop Fast-LIO container ]

        * The status "label" is green when the bridge container runs,
          red (grey text) when it does not, grey when unknown.  Its
          refresh button re-checks by hand (docker ps in the background).
        * ONE button starts or stops the bridge: it shows "Start" while
          the bridge is stopped and "Stop" while it runs.  When the state
          is unknown it shows a disabled "Start" -- we never start the
          bridge blind.
        Shutdown lives at the bottom of the shell column (build_power_area).
        """
        card, body = make_card("Drone host", icon="server")
        body.set_spacing(8)

        # The buttons are defined in commands.json (labels + icons).
        entries = {}
        for entry in self.commands.get("host_buttons", []):
            entries[entry.get("action", "")] = entry

        refresh = entries.get("docker_ps", {})
        self.bridge_status = BridgeStatus(
            self.commands["container_name"],
            refresh.get("label", "Check running containers"),
            self.on_check_containers)
        body.pack_start(self.bridge_status, False, False, 0)

        # The Start/Stop toggle: a Gtk.Stack holds both buttons and shows
        # only the one that fits the bridge's state (update_bridge_widgets).
        start = entries.get("start_container", {})
        stop = entries.get("stop_container", {})
        self.start_bridge_button = make_button(
            start.get("label", "Start"), icon=start.get("icon"), icon_color=GREEN_COLOR)
        self.start_bridge_button.connect("clicked", self.on_start_container)
        self.stop_bridge_button = make_button(
            stop.get("label", "Stop"), icon=stop.get("icon"), icon_color=RED_COLOR)
        self.stop_bridge_button.connect("clicked", self.on_stop_container)
        # A Gtk.Stack can only switch to a child that is already visible,
        # so show both buttons first (otherwise it starts on "Start").
        self.start_bridge_button.show_all()
        self.stop_bridge_button.show_all()
        self.bridge_toggle = Gtk.Stack()
        self.bridge_toggle.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.bridge_toggle.add_named(self.start_bridge_button, "start")
        self.bridge_toggle.add_named(self.stop_bridge_button, "stop")
        body.pack_start(self.bridge_toggle, False, False, 0)

        shutdown = entries.get("shutdown")
        if shutdown is not None:
            # Red: the only irreversible action.  Placed at the bottom of
            # the shell column, under "Stop current command".
            # big=True: as tall as "Stop current command" above it.
            self.shutdown_button = make_button(
                shutdown["label"], icon=shutdown.get("icon"), style="danger",
                big=True, centered=True)
            self.shutdown_button.connect("clicked", self.on_shutdown_clicked)

        # Any other host action in commands.json: keep the button, but it
        # only logs a note (safer than crashing).
        known = ("docker_ps", "start_container", "stop_container", "shutdown")
        for action, entry in entries.items():
            if action not in known:
                button = make_button(entry["label"], icon=entry.get("icon"))
                button.connect("clicked", self.on_unknown_action, action)
                body.pack_start(button, False, False, 0)

        self.update_bridge_widgets()
        return card

    def update_bridge_widgets(self):
        """Show the bridge state (self.container_running) in the card.

        True  -> green label, "Stop" button
        False -> red label, "Start" button
        None  -> grey label (unknown), disabled "Start" button
        """
        if self.container_running is True:
            self.bridge_status.set_state("running")
            self.bridge_toggle.set_visible_child_name("stop")
        else:
            self.bridge_status.set_state(
                "stopped" if self.container_running is False else "unknown")
            self.bridge_toggle.set_visible_child_name("start")
        unknown = self.container_running is None
        self.start_bridge_button.set_sensitive(not unknown)
        self.start_bridge_button.set_tooltip_text(
            "Bridge state unknown: use the refresh button first." if unknown else None)

    def build_power_area(self):
        """Shutdown, or the "Confirm shutdown" + cancel pair after one click."""
        power = Gtk.Box(spacing=6)
        if self.shutdown_button is not None:
            power.pack_start(self.shutdown_button, True, True, 0)
        self.shutdown_confirm_box = Gtk.Box(spacing=6)
        confirm = make_button("Confirm shutdown", icon="power",
                              style="danger", big=True, centered=True)
        confirm.connect("clicked", self.on_shutdown_confirm)
        cancel = make_button("", icon="close", big=True, centered=True)
        cancel.set_tooltip_text("Cancel the shutdown")
        cancel.connect("clicked", self.on_shutdown_cancel)
        self.shutdown_confirm_box.pack_start(confirm, True, True, 0)
        self.shutdown_confirm_box.pack_start(cancel, False, False, 0)
        power.pack_start(self.shutdown_confirm_box, True, True, 0)
        # Hidden until "Shutdown" is clicked (see hide_until_needed).
        self.shutdown_confirm_box.hide()
        return power

    def build_shell_panel(self):
        """Bottom-left column: container shell, diagnostics, Ctrl+C."""
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        panel.set_size_request(290, -1)

        shell_card, shell_body = make_card("Container shell", icon="terminal")
        checks_card, checks_body = make_card("Diagnostics", icon="gauge")

        # These buttons are typed INTO the terminal, so they only make
        # sense when the user is inside the container shell.  The "Enter"
        # button enables them; "Leave" disables them again.
        # (container_shell_buttons was initialized in build_right_panel.)
        self.enter_container_button = None
        self.selected_topic = ""            # topic clicked in the list
        self.echo_selected_button = None    # placed under the topic list
        self.topics_request = None
        self.list_topics_button = None      # placed above the topic list

        for entry in self.commands.get("terminal_buttons", []):
            action = entry.get("action", "")
            label = entry["label"]
            icon = entry.get("icon")
            if action == "enter_container":
                button = make_button(label, icon=icon, style="primary")
                button.connect("clicked", self.on_enter_container)
                self.enter_container_button = button
                shell_body.pack_start(button, False, False, 0)
            elif action == "leave_container":
                button = make_button(label, icon=icon)
                button.connect("clicked", self.on_leave_container)
                self.container_shell_buttons.append(button)
                shell_body.pack_start(button, False, False, 0)
            elif action == "stop_command":
                # Created separately (make_stop_command_button) and
                # placed at the bottom of this column, see below.
                continue
            elif action == "list_topics":
                # Goes into the ROS topics card, right above the list it
                # fills (see build_topics_panel).
                button = make_button(label, icon=icon)
                button.connect("clicked", self.on_list_topics)
                self.container_shell_buttons.append(button)
                self.list_topics_button = button
            elif action == "echo_selected" and not entry.get("command"):
                # No fixed command: echo whatever topic is selected.
                # Goes into the ROS topics card, right BELOW the list.
                button = make_button(label, icon=icon, style="primary")
                button.connect("clicked", self.on_echo_selected_topic)
                self.container_shell_buttons.append(button)
                self.echo_selected_button = button
            else:
                # A fixed command typed into the terminal on each click.
                button = make_button(label, icon=icon)
                button.connect("clicked", self.on_send_to_terminal,
                               entry.get("command", ""))
                self.container_shell_buttons.append(button)
                checks_body.pack_start(button, False, False, 0)
        panel.pack_start(shell_card, False, False, 0)
        panel.pack_start(checks_card, False, False, 0)

        # Bottom of the column (pack_end fills from the bottom up):
        # Shutdown at the very bottom, "Stop current command" above it.
        panel.pack_end(self.build_power_area(), False, False, 0)
        panel.pack_end(self.stop_command_button, False, False, 0)
        return panel

    def build_topics_panel(self):
        """Bottom-right: the ROS topics card, as tall and wide as possible.

        Top to bottom in the order you use it:
        [List available topics] / filter / the topic list / [Echo selected].
        The rotor spins in the card title while the list is fetched.
        """
        self.topics_spinner = RotorSpinner()
        topics_card, topics_body = make_card(
            "ROS topics", icon="broadcast", extra=self.topics_spinner)
        # At least this wide, so long topic names stay readable.
        topics_card.set_size_request(380, -1)
        if self.list_topics_button is not None:
            topics_body.pack_start(self.list_topics_button, False, False, 0)

        self.topic_filter = Gtk.SearchEntry()
        self.topic_filter.set_placeholder_text("Filter topics by name")
        topics_body.pack_start(self.topic_filter, False, False, 0)
        topics_scrolled = Gtk.ScrolledWindow()
        topics_scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        topics_scrolled.get_style_context().add_class("oq-well")
        topics_scrolled.set_size_request(-1, 160)
        self.topics_listbox = Gtk.ListBox()
        # SINGLE selection: one topic can be highlighted and echoed.
        self.topics_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.topics_listbox.connect("row-selected", self.on_topic_selected)
        self.topics_listbox.set_filter_func(self.topic_matches_filter)
        self.topic_filter.connect("changed", self.on_topic_filter_changed)
        topics_scrolled.add(self.topics_listbox)
        # expand=True: the list takes all the spare height of the card.
        topics_body.pack_start(topics_scrolled, True, True, 0)

        # "Echo selected topic" right below the list it acts on.
        if self.echo_selected_button is not None:
            topics_body.pack_start(self.echo_selected_button, False, False, 0)
        return topics_card

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
        if self.session_ended:
            return
        if self.demo_mode:
            # Demo: no drone is contacted at all.  The answer is produced
            # in the main thread with simulated output, so the whole GUI
            # (log, bridge status, stop/shutdown sequences) can be tried
            # out without any connection.
            GLib.idle_add(self.demo_run, ssh_command, on_done)
            return

        def worker():
            argv = [
                "ssh",
                "-o", "ConnectTimeout=10",
                "-o", "BatchMode=yes",
                "-o", "ControlPath=" + self.control_socket,
                self.config["user"] + "@" + self.config["ip"],
                ssh_command,
            ]
            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True, errors="replace",
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
            def deliver():
                if not self.session_ended:
                    on_done(success, returncode, result.stdout if success else output)
                return False
            GLib.idle_add(deliver)

        threading.Thread(target=worker, daemon=True).start()

    def demo_run(self, ssh_command, on_done):
        """MAIN THREAD (demo only): answer a command with simulated output."""
        if self.session_ended:
            return False
        success, returncode, output = self.demo_answer(ssh_command)
        self.append_log("[demo] $ " + ssh_command + "\n" + output)
        on_done(success, returncode, output)
        # Keep the "Enter container" button in sync with the simulated
        # container state.
        self.container_running = self.demo_container_running
        self.update_bridge_widgets()
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
        self.controls.set_sensitive(enabled and not self.session_ended and not self.shutdown_was_sent)

    # ------------------------------------------------------------------
    # Bridge status check (refresh button; also automatic)
    # ------------------------------------------------------------------
    def on_check_containers(self, button):
        """Is the bridge container running?  (docker ps in the background)"""
        self.set_drone_buttons_enabled(False)
        # The rotor replaces the refresh button while the check runs.
        self.bridge_status.set_checking(True)
        self.run_ssh_background(
            CONTAINER_LIST,
            self.on_containers_result)

    def on_containers_result(self, success, returncode, output):
        """Turn the docker ps answer into the bridge state."""
        self.bridge_status.set_checking(False)
        self.set_drone_buttons_enabled(True)

        if not success:
            self.set_container_state(None)
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
            self.set_container_state(False)
            return
        self.append_log("Found " + str(len(lines)) + " running container(s).")

        # Remember whether OUR container is among them: this decides
        # whether "Enter PX4 bridge container" may be used.
        names = [line.split("|", 1)[0].strip() for line in lines]
        self.set_container_state(self.commands["container_name"] in names)

    def set_container_state(self, running):
        """running: True / False / None (unknown).  Updates the whole GUI."""
        self.container_running = running
        if running is False and self.inside_container_shell:
            self.clear_shell_state()
        self.update_bridge_widgets()
        self.update_shell_button_state()

    def clear_shell_state(self):
        self.inside_container_shell = False
        self.topics_request = None
        self.topics_spinner.stop()
        self.topics_spinner.hide()
        self.clear_topic_list()

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
        # Immediately check again, so the status label shows whether the
        # Fast-LIO container is really up.
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
        self.set_container_state(name in output.splitlines() if success else None)
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
        self.set_container_state(name in output.splitlines() if success else None)
        if not success:
            self.append_log(
                "WARNING: could not verify the container state "
                "(docker ps failed). Check again with the refresh button "
                "next to the bridge status!")
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
            if self.session_ended:
                return False
            if not self.demo_mode:
                self.terminal.feed_child(b"exit\n")
            self.clear_shell_state()
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
        self.set_container_state(name in output.splitlines() if success else None)
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
                "-o", "BatchMode=yes",
                "-o", "ControlPath=" + self.control_socket,
                self.config["user"] + "@" + self.config["ip"],
                "sudo -S -p '' shutdown now",
            ]
            try:
                # The password + Enter goes through stdin.  The
                # connection will drop during the command; ssh may then
                # exit with an error code -- that is EXPECTED here.
                result = subprocess.run(
                    argv,
                    input=self.password + "\n",
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (subprocess.TimeoutExpired, OSError):
                GLib.idle_add(self.on_shutdown_unconfirmed)
                return
            if result.returncode == 0:
                GLib.idle_add(self.on_shutdown_sent)
            else:
                GLib.idle_add(self.on_shutdown_unconfirmed)

        threading.Thread(target=worker, daemon=True).start()

    def on_shutdown_unconfirmed(self):
        if self.closed:
            return False
        self.append_log("Shutdown could not be confirmed. Check the onboard computer before retrying.")
        self.set_drone_buttons_enabled(True)
        return False

    def on_shutdown_sent(self):
        """MAIN THREAD: the shutdown command was sent."""
        if self.closed:
            return False
        self.shutdown_was_sent = True
        self.append_log("Shutdown command accepted.")
        # The drone is going down: no button may talk to it anymore.
        self.set_drone_buttons_enabled(False)
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

    def find_terminal_entry(self, action):
        """Look up a terminal button's whole entry in commands.json."""
        for entry in self.commands.get("terminal_buttons", []):
            if entry.get("action") == action:
                return entry
        return {}

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
        if self.enter_container_button is not None:
            self.enter_container_button.set_sensitive((not inside) and can_enter)
        for shell_button in self.container_shell_buttons:
            shell_button.set_sensitive(inside)
        if self.list_topics_button is not None:
            self.list_topics_button.set_sensitive(inside and not self.topics_request)
        if self.echo_selected_button is not None:
            self.echo_selected_button.set_sensitive(inside and bool(self.selected_topic))

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
        self.set_drone_buttons_enabled(False)
        self.leave_container_shell(self.on_left_container_shell)

    def on_left_container_shell(self):
        self.set_drone_buttons_enabled(True)
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
        if self.topics_request or not self.inside_container_shell:
            return
        try:
            command = topic_list_command(self.commands)
        except ValueError as error:
            self.append_log(str(error))
            return
        request = object()
        self.topics_request = request
        self.clear_topic_list()
        self.update_shell_button_state()
        self.topics_spinner.show()
        self.topics_spinner.start()

        def completed(success, returncode, output):
            if self.topics_request is request:
                self.on_topics_result(success, returncode, output)

        self.run_ssh_background(command, completed, timeout_seconds=30)

    def on_topics_result(self, success, returncode, output):
        self.topics_request = None
        self.update_shell_button_state()
        self.topics_spinner.stop()
        self.topics_spinner.hide()
        if not success:
            self.clear_topic_list()
            self.selected_topic = ""
            self.append_log(
                "Could not list ROS topics (see the log above for "
                "details).")
            return
        if self.inside_container_shell:
            self.fill_topic_list(topic_names(output))

    def fill_topic_list(self, topics):
        """Put the topics into the clickable list and log the count."""
        self.selected_topic = ""     # the old selection is gone
        self.clear_topic_list()
        for topic in topics:
            self.add_topic_row(topic)
        if not topics:
            self.append_log("No ROS topics found. Check that the nodes are running and refresh.")
            return
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
        row.show_all()

    def topic_matches_filter(self, row):
        return self.topic_filter.get_text().casefold() in row.get_child().get_text().casefold()

    def on_topic_filter_changed(self, entry):
        self.topics_listbox.invalidate_filter()
        selected = self.topics_listbox.get_selected_row()
        if selected is not None and not self.topic_matches_filter(selected):
            self.topics_listbox.unselect_row(selected)

    def on_topic_selected(self, listbox, row):
        """A topic row was clicked: remember it for 'Echo selected topic'."""
        self.selected_topic = row.get_child().get_text() if row else ""
        if self.echo_selected_button is not None:
            self.echo_selected_button.set_sensitive(self.inside_container_shell and bool(row))

    # ------------------------------------------------------------------
    # Button: Echo selected topic
    # ------------------------------------------------------------------
    def on_echo_selected_topic(self, button):
        if not self.selected_topic:
            self.append_log(
                "No topic selected: click a topic in the ROS topics "
                "list first.")
            return
        # Each echo runs in its own terminal window (several at once).
        self.open_echo_window("ros2 topic echo " + self.selected_topic)

    # ------------------------------------------------------------------
    # Buttons with fixed commands (typed into the terminal)
    # ------------------------------------------------------------------
    def on_send_to_terminal(self, button, command):
        """Type a fixed command from commands.json into the terminal."""
        if not command:
            self.append_log(
                "This button has no command configured in commands.json.")
            return
        if command.startswith("ros2 topic echo"):
            # Echo commands run FOREVER until stopped: give each one
            # its own terminal window, so several can run at once.
            self.open_echo_window(command)
            return
        self.type_in_terminal(command)

    # ------------------------------------------------------------------
    # Separate echo terminal windows (one per echo command)
    # ------------------------------------------------------------------
    def open_echo_window(self, command):
        """Open a NEW terminal window that runs one echo command.

        Every click gets its own window and its own ssh session (the
        session reuses the master connection through the control
        socket, so no password is needed).  That way several topics can
        be echoed at the same time, and each window can be closed
        (or Ctrl+C'd) independently.
        """
        topic = command.replace("ros2 topic echo ", "")
        window = Gtk.Window(title="Echo - " + topic)
        style_window(window)
        window.set_default_size(750, 500)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.ALWAYS)
        terminal = Vte.Terminal()
        terminal.set_font(Pango.FontDescription("Monospace 11"))
        terminal.set_scrollback_lines(5000)
        style_terminal(terminal)
        scrolled.add(terminal)
        window.add(scrolled)
        self.echo_windows.append(window)
        # Remove it from our list when the user closes the window.
        window.connect("destroy", self.on_echo_window_destroy)
        window.show_all()
        terminal.grab_focus()

        if self.demo_mode:
            # Demo: no ssh session exists.  Show what WOULD happen.
            feed_text(
                terminal,
                "DEMO MODE - no ssh session is open.\n"
                "A real connection would run, in a separate terminal:\n"
                "  docker exec -it px4_fastlio_bridge bash\n"
                "  " + command + "\n")
            return

        # Start the echo directly in its container over the master socket.
        remote = shlex.join(["docker", "exec", "-it", self.commands["container_name"],
                             "bash", "-ic", command])
        argv = ["ssh", "-tt", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ControlPath=" + self.control_socket,
                self.config["user"] + "@" + self.config["ip"], remote]

        def spawn_done(spawn_terminal, pid, error, *args):
            if error is not None:
                feed_text(spawn_terminal, "Could not start ssh: " + error.message + "\n")

        terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,
            None,
            argv,
            None,
            GLib.SpawnFlags.SEARCH_PATH,
            None,
            None,
            -1,
            None,
            spawn_done,
        )

    def on_echo_window_destroy(self, window):
        """Forget the window when it is closed (the echo stops with it)."""
        if window in self.echo_windows:
            self.echo_windows.remove(window)

    def on_export_csv(self, button, kind="topics"):
        """Save the topics shown in the list (filter applied) as CSV."""
        columns = ("Topic",)
        rows = [(row.get_child().get_text(),) for row in self.topics_listbox.get_children()
                if row.get_child_visible()]
        if not rows:
            self.append_log("No visible " + kind + " to export.")
            return
        dialog = Gtk.FileChooserDialog(title="Export visible " + kind, transient_for=self.main_window,
                                       action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(("demo-" if self.demo_mode else "") + kind + ".csv")
        try:
            if dialog.run() == Gtk.ResponseType.ACCEPT:
                path = dialog.get_filename()
                export_csv(path, columns, rows)
                self.append_log(f"Exported {len(rows)} rows to {path}")
        except OSError as error:
            self.append_log("Could not save CSV: " + str(error))
        finally:
            dialog.destroy()

    def on_reconnect(self, button):
        self.reconnect_requested = True
        self.main_window.close()

    # ------------------------------------------------------------------
    # Closing the app cleanly
    # ------------------------------------------------------------------
    def on_main_window_closed(self, window, event):
        """Runs when the user closes the main window.

        We ask the ssh master connection to exit (`ssh -O exit`), then
        remove the private temp folder with the control socket.
        """
        self.session_ended = True
        self.closed = True
        self.password = ""
        # The local bridge keeps running (it belongs to the program, not
        # to this window); just stop sending its output here.
        if self.local_bridge is not None:
            self.local_bridge.on_output = None
            self.local_bridge.on_state_changed = None
        for echo in self.echo_windows.copy():
            echo.destroy()
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


def main(demo=False):
    local_bridge = None
    try:
        apply_theme()
        # The local Foxglove bridge starts together with the program (if
        # "autostart" is true in commands.json) and keeps running until
        # the program closes -- also across "Reconnect".
        commands = load_commands()
        local_bridge = create_local_bridge(commands, load_config())
        if local_bridge is not None and commands["local_bridge"].get("autostart", True):
            local_bridge.start()
        while True:
            app = OmniQuadApp(demo=demo, local_bridge=local_bridge)
            demo = False
            def close_window():
                (app.main_window or app.start_window).close()
                return True
            # Ctrl+C (SIGINT), `kill` (SIGTERM) and a closed terminal
            # (SIGHUP) all close the window the normal way, so the
            # `finally` below still stops the Foxglove bridge.  (Without
            # this, SIGTERM ended Python at once and left it running.)
            handlers = [GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, close_window)
                        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)]
            try:
                Gtk.main()
            finally:
                for handler in handlers:
                    GLib.source_remove(handler)
            if not app.reconnect_requested:
                break
    except Exception as error:
        # A programming error should never just vanish (especially when
        # the app is started from a desktop icon, where there is no
        # console to print to).  Show it in a window instead.
        import traceback
        title = "OmniQuadGUI stopped unexpectedly"
        text = traceback.format_exc()
        if isinstance(error, CommandConfigError):
            title = "Check the command configuration"
            text = str(error)
        print(text)  # also visible if the app was started from a terminal
        try:
            dialog = Gtk.MessageDialog(
                parent=None,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=title,
            )
            dialog.format_secondary_text(text[-1500:])
            dialog.run()
            dialog.destroy()
        except Exception:
            pass  # even the dialog failed -- nothing more we can do
    finally:
        # The program ends (normally or after an error): stop the local
        # Foxglove bridge cleanly, so it never keeps running on its own.
        if local_bridge is not None:
            local_bridge.stop(wait=True)
