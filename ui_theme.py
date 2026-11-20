"""Look and feel of the Ubuntu (GTK 3) interface.

Everything that only changes how the app LOOKS lives here, so gtk_app.py
can stay focused on what the buttons DO:

  * apply_theme()     - the colours, gradients and rounded corners (CSS)
  * load_icon()       - the line icons from images/icons/*.svg
  * make_button()     - a button with an icon on the left and its text
  * make_card()       - a rounded "card" box with a small title
  * RotorSpinner      - the loading animation: a spinning drone rotor

GTK styles widgets with CSS, very much like a web page.  A widget gets
one or more "style classes" (e.g. "oq-btn", "oq-primary"), and the CSS
below says what each class looks like.
"""

import os

import gi
gi.require_version("Gtk", "3.0")      # GTK 3, like gtk_app.py
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango


APP_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(APP_DIR, "images", "icons")


# ----------------------------------------------------------------------------
# Colours
# ----------------------------------------------------------------------------
# One small palette for the whole app: Ubuntu orange on warm dark greys.
# The CSS below uses the same values.
TEXT_COLOR = "#f2f2f2"       # normal text
DIM_TEXT_COLOR = "#a3a3a3"   # titles, secondary text
ACCENT_COLOR = "#e95420"     # Ubuntu orange: main buttons, selection, rotor
GREEN_COLOR = "#3ddc84"      # "good / running / start"
AMBER_COLOR = "#f6b73c"      # "waiting / demo / stop the command"
RED_COLOR = "#ff5d5d"        # "stop / problem"
TERMINAL_BACKGROUND = "#141414"

# Which colour the ICON gets on each kind of button.  Filled (gradient)
# buttons get a white icon, normal buttons a soft orange one.
ICON_COLOR_FOR_STYLE = {
    "neutral": "#f59a6c",
    "primary": "#ffffff",
    "danger": "#ffffff",
    "warning": AMBER_COLOR,
}
# Every icon turns this muted grey while its button is disabled.
DISABLED_ICON_COLOR = "#5a5a5a"


# ----------------------------------------------------------------------------
# The stylesheet
# ----------------------------------------------------------------------------
# "@define-color" makes a named colour that later rules can use as @name.
# "linear-gradient(to bottom, A, B)" paints a soft top-to-bottom gradient,
# which gives the buttons a little depth.  Everything is limited to
# windows that carry the class "oq-window", so dialogs and menus keep the
# normal system look.
THEME_CSS = """
@define-color oq_bg            #1e1e1e;
@define-color oq_surface       #272727;
@define-color oq_border        #383838;
@define-color oq_text          #f2f2f2;
@define-color oq_text_dim      #a3a3a3;
@define-color oq_accent        #e95420;
@define-color oq_well          #171717;

/* ---- window, header bar, footer -------------------------------------- */
window.oq-window,
window.oq-window > box,
window.oq-window paned {
    background-color: @oq_bg;
    color: @oq_text;
}
window.oq-window headerbar {
    background-image: linear-gradient(to bottom, #2e2e2e, #262626);
    border-bottom: 1px solid #151515;
    box-shadow: inset 0 -1px rgba(233, 84, 32, 0.35);
    color: @oq_text;
}
window.oq-window headerbar .title { font-weight: 700; }
window.oq-window actionbar > revealer > box {
    background-image: linear-gradient(to bottom, #262626, #222222);
    border-top: 1px solid @oq_border;
    padding: 6px 10px;
}
window.oq-window paned > separator {
    background-color: @oq_bg;
    background-image: none;
}

/* ---- connection status in the header bar -------------------------------- */
.oq-status {
    background-color: rgba(255, 255, 255, 0.04);
    border: 1px solid #3c3c3c;
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 12px;
}
.oq-status separator {
    background-color: #464646;
    min-width: 1px;
    margin: 3px 0;
}
.oq-status .oq-status-state { font-weight: 700; }
.oq-status .oq-status-live     { color: #6be3a2; }
.oq-status .oq-status-waiting  { color: #f8c766; }
.oq-status .oq-status-offline  { color: #ff8a8a; }
.oq-status .oq-dim { color: @oq_text_dim; }

/* ---- bridge status "label" in the Drone host card ---------------------- */
.oq-bridge {
    border-radius: 8px;
    border: 1px solid #3a3a3a;
    background-color: #2a2a2a;
    padding: 4px 4px 4px 10px;
    transition: all 200ms ease-out;
}
.oq-bridge label { color: #8f8f8f; }               /* grey text ...      */
.oq-bridge .oq-bridge-state { font-size: 11px; font-weight: 700; }
.oq-bridge-running {                                /* ... green, normal  */
    background-image: linear-gradient(to bottom, rgba(61, 220, 132, 0.24), rgba(61, 220, 132, 0.12));
    border-color: #2f9e62;
}
.oq-bridge-running label { color: @oq_text; }
.oq-bridge-running .oq-bridge-state { color: #7ee8ae; }
.oq-bridge-stopped {                                /* red, grey text     */
    background-image: linear-gradient(to bottom, rgba(255, 93, 93, 0.22), rgba(255, 93, 93, 0.10));
    border-color: #a8323a;
}

/* A small icon-only button with no frame (the refresh arrow). */
button.oq-icon-btn {
    background-image: none;
    background-color: transparent;
    border: none;
    box-shadow: none;
    border-radius: 6px;
    padding: 4px;
    min-height: 0;
    min-width: 0;
}
button.oq-icon-btn:hover { background-color: rgba(255, 255, 255, 0.10); }
button.oq-icon-btn:active { background-color: rgba(255, 255, 255, 0.16); }

/* ---- cards (rounded groups of buttons) --------------------------------- */
frame.oq-card {
    background-color: @oq_surface;
    border: 1px solid @oq_border;
    border-radius: 12px;
}
frame.oq-card > border { border: none; }
.oq-card-title {
    color: #f59a6c;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1px;
}
.oq-column-title {
    font-size: 15px;
    font-weight: 700;
}
.oq-column-subtitle {
    color: @oq_text_dim;
    font-size: 12px;
}
.oq-dim { color: @oq_text_dim; }

/* ---- buttons ----------------------------------------------------------- */
button.oq-btn {
    background-color: transparent;
    background-image: linear-gradient(to bottom, #3a3a3a, #313131);
    border: 1px solid #474747;
    border-radius: 8px;
    box-shadow: inset 0 1px rgba(255, 255, 255, 0.06),
                0 1px 2px rgba(0, 0, 0, 0.35);
    color: @oq_text;
    text-shadow: none;
    -gtk-icon-shadow: none;
    padding: 5px 12px;
    min-height: 20px;
    transition: all 150ms ease-out;
}
button.oq-btn:hover {
    background-image: linear-gradient(to bottom, #444444, #393939);
    border-color: #b8603a;
}
button.oq-btn:active {
    background-image: linear-gradient(to bottom, #2a2a2a, #333333);
    box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.45);
}
button.oq-btn:disabled {
    background-image: none;
    background-color: #252525;
    border-color: #303030;
    box-shadow: none;
    color: #5f5f5f;
}

/* Orange: the main action of a group (Connect, Enter, Echo, Reconnect). */
button.oq-primary {
    background-image: linear-gradient(to bottom, #f4733d, #d9461a);
    border-color: #c03d12;
    color: #ffffff;
}
button.oq-primary:hover {
    background-image: linear-gradient(to bottom, #f78552, #e4521f);
    border-color: #c03d12;
}
button.oq-primary:active { background-image: linear-gradient(to bottom, #cc4316, #ee6430); }

/* Red: dangerous / irreversible actions (Shutdown, Confirm shutdown). */
button.oq-danger {
    background-image: linear-gradient(to bottom, #e8434e, #b8202c);
    border-color: #9c1a25;
    color: #ffffff;
}
button.oq-danger:hover {
    background-image: linear-gradient(to bottom, #f2565f, #c62834);
    border-color: #9c1a25;
}
button.oq-danger:active { background-image: linear-gradient(to bottom, #a91d28, #db3843); }

/* Amber outline: "Stop current command" (Ctrl+C) - used often, not
   dangerous, and clearly different from the orange buttons. */
button.oq-warning {
    background-image: linear-gradient(to bottom, rgba(246, 183, 60, 0.12), rgba(246, 183, 60, 0.05));
    border: 1px solid #c98f22;
    color: #f8c766;
}
button.oq-warning:hover {
    background-image: linear-gradient(to bottom, rgba(246, 183, 60, 0.22), rgba(246, 183, 60, 0.10));
    border-color: #f6b73c;
}

/* All buttons look the same "switched off" grey when disabled. */
button.oq-primary:disabled, button.oq-danger:disabled, button.oq-warning:disabled {
    background-image: none;
    background-color: #252525;
    border-color: #303030;
    color: #5f5f5f;
}

button.oq-big {
    font-size: 15px;
    font-weight: 700;
    padding: 10px 14px;
}

/* ---- text fields, lists, log ------------------------------------------- */
window.oq-window entry {
    background-color: @oq_well;
    background-image: none;
    border: 1px solid #3a3a3a;
    border-radius: 8px;
    box-shadow: none;
    color: @oq_text;
    min-height: 32px;
}
window.oq-window entry:focus {
    border-color: @oq_accent;
    box-shadow: 0 0 0 1px @oq_accent;
}
window.oq-window entry image { color: @oq_text_dim; }
window.oq-window entry selection {
    background-color: @oq_accent;
    color: #ffffff;
}

scrolledwindow.oq-well {
    background-color: @oq_well;
    border: 1px solid #323232;
    border-radius: 8px;
}
scrolledwindow.oq-well viewport,
scrolledwindow.oq-well list {
    background-color: transparent;
    color: @oq_text;
}
scrolledwindow.oq-well list row {
    padding: 5px 10px;
    border-radius: 6px;
}
scrolledwindow.oq-well list row:hover { background-color: #2a2a2a; }
scrolledwindow.oq-well list row:selected {
    background-image: linear-gradient(to right, #d9461a, #f4733d);
    color: #ffffff;
}
scrolledwindow.oq-well list row:selected label { color: #ffffff; }

window.oq-window textview,
window.oq-window textview text {
    background-color: @oq_well;
    color: #d0d0d0;
}
window.oq-window checkbutton { color: @oq_text_dim; }
"""


def apply_theme():
    """Load the stylesheet for the whole app (call once at startup)."""
    settings = Gtk.Settings.get_default()
    if settings is not None:
        # Ask the system theme for its DARK variant, so everything we do
        # not style ourselves (scrollbars, menus, dialogs) matches too.
        settings.set_property("gtk-application-prefer-dark-theme", True)
    provider = Gtk.CssProvider()
    provider.load_from_data(THEME_CSS.encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def style_window(window):
    """Give a window the app's own look (see "oq-window" in THEME_CSS)."""
    window.get_style_context().add_class("oq-window")


# ----------------------------------------------------------------------------
# Icons
# ----------------------------------------------------------------------------
def screen_scale_factor():
    """2 on a HiDPI ("200 %") screen, otherwise 1."""
    display = Gdk.Display.get_default()
    if display is None:
        return 1
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    return monitor.get_scale_factor() if monitor is not None else 1


def svg_to_pixbuf(svg_text, size):
    """Turn SVG text into a size x size picture (a GdkPixbuf).

    Returns None if SVG support (librsvg) is not installed; callers then
    simply go without the picture, nothing breaks.
    """
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.set_size(size, size)
        loader.write(svg_text.encode("utf-8"))
        loader.close()
        return loader.get_pixbuf()
    except GLib.Error:
        return None


def load_icon_pixbuf(name, size, color):
    """Read images/icons/<name>.svg, paint it in `color`, return a pixbuf.

    The SVG files draw with the word "currentColor"; we simply replace
    that word with the colour we want before GTK draws the picture.
    On HiDPI screens the picture is made at double resolution, so it
    stays sharp.  Returns None if the file is missing or SVG support is
    not installed (the button then just shows its text).
    """
    path = os.path.join(ICON_DIR, name + ".svg")
    try:
        with open(path, encoding="utf-8") as svg_file:
            svg = svg_file.read().replace("currentColor", color)
    except OSError:
        return None
    return svg_to_pixbuf(svg, size * screen_scale_factor())


def image_from_pixbuf(pixbuf, size):
    """A Gtk.Image that shows `pixbuf` at `size` x `size` screen pixels.

    Going through a "GIcon" lets GTK use the extra pixels on a HiDPI
    screen instead of drawing the picture twice as big.
    """
    image = Gtk.Image.new_from_gicon(pixbuf, Gtk.IconSize.BUTTON)
    image.set_pixel_size(size)
    return image


def load_icon(name, size=16, color=TEXT_COLOR):
    """Return a Gtk.Image showing icon `name`, or None if it can't load."""
    pixbuf = load_icon_pixbuf(name, size, color)
    if pixbuf is None:
        return None
    return image_from_pixbuf(pixbuf, size)


# ----------------------------------------------------------------------------
# Buttons and cards
# ----------------------------------------------------------------------------
def make_button(text, icon=None, style="neutral", big=False, centered=False,
                icon_color=None):
    """A modern button: icon on the left, then the text.

    style:    "neutral" (dark grey), "primary" (Ubuntu orange),
              "danger" (red) or "warning" (amber outline).
    big:      larger text and padding (used for "Stop current command").
    centered: centre icon+text (short buttons); otherwise everything is
              left-aligned, so a column of buttons lines up neatly.
    icon_color: paint the icon in this colour instead of the style's
              usual one (e.g. a red square on a grey "Stop" button).
    """
    button = Gtk.Button()
    context = button.get_style_context()
    context.add_class("oq-btn")
    if style != "neutral":
        context.add_class("oq-" + style)
    if big:
        context.add_class("oq-big")

    content = Gtk.Box(spacing=10)
    content.set_halign(Gtk.Align.CENTER if centered else Gtk.Align.START)
    if icon:
        size = 20 if big else 16
        color = icon_color or ICON_COLOR_FOR_STYLE.get(style, TEXT_COLOR)
        normal = load_icon_pixbuf(icon, size, color)
        disabled = load_icon_pixbuf(icon, size, DISABLED_ICON_COLOR)
        if normal is not None and disabled is not None:
            image = image_from_pixbuf(normal, size)
            content.pack_start(image, False, False, 0)

            def match_icon_to_state(widget, old_flags):
                # Grey icon while the button is disabled, coloured otherwise.
                flags = widget.get_state_flags()
                picture = disabled if flags & Gtk.StateFlags.INSENSITIVE else normal
                image.set_from_gicon(picture, Gtk.IconSize.BUTTON)
                image.set_pixel_size(size)
            button.connect("state-flags-changed", match_icon_to_state)
    if text:
        label = Gtk.Label(label=text)
        label.set_xalign(0.0)
        content.pack_start(label, False, False, 0)
    button.add(content)
    return button


def make_card_title(title, icon=None, extra=None):
    """The small upper-case title row of a card: [icon] TITLE ...[extra]

    If `extra` is given (e.g. a RotorSpinner or a check box), it is
    shown at the right end of the row.
    """
    header = Gtk.Box(spacing=7)
    if icon:
        image = load_icon(icon, 14, DIM_TEXT_COLOR)
        if image is not None:
            header.pack_start(image, False, False, 0)
    title_label = Gtk.Label(label=title.upper())
    title_label.set_xalign(0.0)
    title_label.get_style_context().add_class("oq-card-title")
    header.pack_start(title_label, False, False, 0)
    if extra is not None:
        header.pack_end(extra, False, False, 0)
    return header


def make_card(title=None, icon=None, extra=None):
    """A rounded card, normally with a small upper-case title row.

    Returns (card, body): add the card to the window, and put the
    card's buttons into `body` (a vertical box).
    title=None: no title row; the caller places one itself (see
    make_card_title), e.g. beside the buttons instead of above them.
    """
    card = Gtk.Frame()
    card.set_shadow_type(Gtk.ShadowType.NONE)
    card.get_style_context().add_class("oq-card")

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    body.set_border_width(9)
    card.add(body)

    if title is not None:
        body.pack_start(make_card_title(title, icon, extra), False, False, 2)
    return card, body


def make_column_title(title, subtitle):
    """Big title + small grey subtitle above each button column."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    title_label = Gtk.Label(label=title)
    title_label.set_xalign(0.0)
    title_label.get_style_context().add_class("oq-column-title")
    subtitle_label = Gtk.Label(label=subtitle)
    subtitle_label.set_xalign(0.0)
    subtitle_label.get_style_context().add_class("oq-column-subtitle")
    box.pack_start(title_label, False, False, 0)
    box.pack_start(subtitle_label, False, False, 0)
    return box


class ConnectionStatus(Gtk.Box):
    """The thin connection strip in the header bar.

        ● Connected  |  rock@192.168.1.50  |  Wi-Fi: LabNet

    The dot and the first word show the state of the ssh connection:
      "live"     green   connected, the drone answers
      "waiting"  amber   connecting / demo mode
      "offline"  red     disconnected, or the drone stopped answering
    Hovering the strip shows more details (a "tooltip").
    """

    DOT_COLORS = {"live": GREEN_COLOR, "waiting": AMBER_COLOR, "offline": RED_COLOR}

    def __init__(self):
        super().__init__(spacing=8)
        self.set_valign(Gtk.Align.CENTER)
        self.get_style_context().add_class("oq-status")
        # One small coloured dot per state, made once.
        self.dots = {state: load_icon_pixbuf("dot", 10, color)
                     for state, color in self.DOT_COLORS.items()}
        self.dot = Gtk.Image()
        self.state_label = Gtk.Label()
        self.state_label.get_style_context().add_class("oq-status-state")
        self.target_label = Gtk.Label()
        self.network_label = Gtk.Label()
        self.network_label.get_style_context().add_class("oq-dim")
        self.network_label.set_ellipsize(Pango.EllipsizeMode.END)   # "..." if too long
        self.network_label.set_max_width_chars(28)
        self.pack_start(self.dot, False, False, 0)
        self.pack_start(self.state_label, False, False, 0)
        self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        self.pack_start(self.target_label, False, False, 0)
        self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        self.pack_start(self.network_label, False, False, 0)
        self.state = None

    def set_state(self, state, text):
        """state: "live", "waiting" or "offline"; text: e.g. "Connected"."""
        self.state = state
        context = self.state_label.get_style_context()
        for old in self.DOT_COLORS:
            context.remove_class("oq-status-" + old)
        context.add_class("oq-status-" + state)
        self.state_label.set_text(text)
        if self.dots.get(state) is not None:
            self.dot.set_from_gicon(self.dots[state], Gtk.IconSize.BUTTON)
            self.dot.set_pixel_size(10)

    def set_target(self, text):
        """Who we talk to, e.g. "rock@192.168.1.50"."""
        self.target_label.set_text(text)

    def set_network(self, text, details=""):
        """The network used to reach the drone (+ tooltip details)."""
        self.network_label.set_text(text)
        self.set_tooltip_text(details or None)


class BridgeStatus(Gtk.Box):
    """A bridge's status "label" (a rounded box):

        [=] px4_fastlio_bridge          (o)     <- refresh button
            RUNNING

    Green with normal text when the bridge runs, red with grey text when
    it does not, plain grey when the state is unknown (not checked yet,
    or the check failed).  The optional refresh button at the right
    re-checks by hand; while something is in progress the rotor spins
    there instead.

    icon:         which images/icons/ picture to show at the left.
    on_refresh:   None = no refresh button.
    running_text: the second line while running (e.g. with an address).
    """

    STATE_TEXT = {"running": "RUNNING", "stopped": "NOT RUNNING", "unknown": "STATUS UNKNOWN"}
    ICON_COLOR = {"running": GREEN_COLOR, "stopped": RED_COLOR, "unknown": "#8f8f8f"}

    def __init__(self, name, refresh_tooltip=None, on_refresh=None, icon="containers",
                 running_text="RUNNING"):
        super().__init__(spacing=10)
        self.get_style_context().add_class("oq-bridge")
        self.running_text = running_text
        # One icon per state (green / red / grey), made once.
        self.icons = {state: load_icon_pixbuf(icon, 18, color)
                      for state, color in self.ICON_COLOR.items()}
        self.icon = Gtk.Image()
        self.icon.set_valign(Gtk.Align.CENTER)
        self.pack_start(self.icon, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.set_valign(Gtk.Align.CENTER)
        name_label = Gtk.Label(label=name)
        name_label.set_xalign(0.0)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.state_label = Gtk.Label()
        self.state_label.set_xalign(0.0)
        self.state_label.get_style_context().add_class("oq-bridge-state")
        text.pack_start(name_label, False, False, 0)
        text.pack_start(self.state_label, False, False, 0)
        self.pack_start(text, True, True, 0)

        # Right end: the refresh button (or an empty space), swapped for
        # the rotor while something is in progress (a Gtk.Stack shows one
        # of its children at a time).
        if on_refresh is not None:
            self.refresh_button = Gtk.Button()
            self.refresh_button.get_style_context().add_class("oq-icon-btn")
            self.refresh_button.set_tooltip_text(refresh_tooltip)
            self.refresh_button.set_valign(Gtk.Align.CENTER)
            refresh_icon = load_icon("reconnect", 16, "#d6d6d6")
            if refresh_icon is not None:
                self.refresh_button.add(refresh_icon)
            else:
                self.refresh_button.set_label("Refresh")
            self.refresh_button.connect("clicked", on_refresh)
            idle = self.refresh_button
        else:
            self.refresh_button = None
            idle = Gtk.Box()          # nothing to show when idle
        self.spinner = RotorSpinner(size=20)
        self.action = Gtk.Stack()
        self.action.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.action.add_named(idle, "refresh")
        self.action.add_named(self.spinner, "busy")
        self.pack_end(self.action, False, False, 0)
        self.set_state("unknown")

    def set_state(self, state, text=None):
        """state: "running", "stopped" or "unknown".

        text: a different second line (e.g. "RUNNING, NOT STARTED HERE");
        None = the usual text for that state.
        """
        context = self.get_style_context()
        for old in self.STATE_TEXT:
            context.remove_class("oq-bridge-" + old)
        context.add_class("oq-bridge-" + state)
        if text is None:
            text = self.running_text if state == "running" else self.STATE_TEXT[state]
        self.state_label.set_text(text)
        if self.icons.get(state) is not None:
            self.icon.set_from_gicon(self.icons[state], Gtk.IconSize.BUTTON)
            self.icon.set_pixel_size(18)

    def set_checking(self, checking):
        """Show the spinning rotor (True) or the refresh button / nothing."""
        if checking:
            self.action.set_visible_child_name("busy")
            self.spinner.start()
        else:
            self.spinner.stop()
            self.action.set_visible_child_name("refresh")


def style_terminal(terminal):
    """Dark terminal colours that match the rest of the window."""
    foreground = Gdk.RGBA()
    foreground.parse("#d5dde9")
    background = Gdk.RGBA()
    background.parse(TERMINAL_BACKGROUND)
    cursor = Gdk.RGBA()
    cursor.parse(ACCENT_COLOR)
    terminal.set_colors(foreground, background, None)
    terminal.set_color_cursor(cursor)


# ----------------------------------------------------------------------------
# The loading animation: a spinning drone rotor
# ----------------------------------------------------------------------------
def rotor_frame_svg(angle_degrees, color, ghosts):
    """SVG text of ONE animation frame: the rotor turned by `angle_degrees`.

    Drawn in a 24x24 box centred on (0, 0):
      1. a faint disc = the blurred circle a fast rotor makes, with a
         thin ring around it (the propeller guard),
      2. `ghosts` fading copies of the blades just behind = motion blur,
      3. the three real blades (curved like a real propeller),
      4. the hub (motor) in the middle.
    """
    # One blade pointing along +x (radius 11), rounded tip, curved edge;
    # the other two are the same blade turned by 120 and 240 degrees.
    blade = ("M1.6,-2.0 C4.8,-4.2 8.9,-3.3 10.3,-1.3 "
             "C10.9,-0.1 10.2,1.3 8.8,1.6 C5.9,2.0 3.6,2.4 1.6,2.0 Z")
    all_blades = "".join('<path d="' + blade + '" transform="rotate(%d)"/>' % turn
                         for turn in (0, 120, 240))
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
             'viewBox="-12 -12 24 24">',
             '<g fill="' + color + '">',
             '<circle r="11.4" opacity="0.14"/>',
             '<circle r="11.2" fill="none" stroke="' + color + '" '
             'stroke-width="1.2" opacity="0.45"/>']
    for step in range(ghosts, 0, -1):
        opacity = 0.30 * (1 - step / (ghosts + 1))
        parts.append('<g opacity="%.2f" transform="rotate(%.1f)">%s</g>'
                     % (opacity, angle_degrees - step * 8, all_blades))
    parts.append('<g transform="rotate(%.1f)">%s</g>' % (angle_degrees, all_blades))
    parts.append('<circle r="3.1"/></g>')
    parts.append('<circle r="1.3" fill="#12161d" opacity="0.9"/></svg>')
    return "".join(parts)


class RotorSpinner(Gtk.Box):
    """A three-blade propeller (seen from above) that spins while loading.

    It is used exactly like Gtk.Spinner:  spinner.start() / spinner.stop()
    (and show() / hide() to make it appear or disappear).

    How it works: the rotor pictures for one third of a turn (the three
    blades look the same after 120 degrees) are made ONCE from SVG text (see
    rotor_frame_svg) and kept in FRAME_CACHE.  While spinning, GTK calls
    on_tick once per screen refresh and we show the picture that fits
    the current angle -- like a flip book.  When stopped, nothing runs.
    If the computer cannot draw SVG pictures, a normal Gtk.Spinner is
    used instead, so a loading indicator is always there.
    """

    TURNS_PER_SECOND = 1.4
    SYMMETRY = 120               # degrees after which the rotor looks the same
    FRAMES = 24                  # pictures per 120 degrees (5 degrees apart)
    GHOST_BLADES = 4             # motion-blur copies behind the blades
    FRAME_CACHE = {}             # (size, colour) -> list of pictures

    def __init__(self, size=20, color=ACCENT_COLOR):
        super().__init__()
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.size = size
        self.frames = self.get_frames(size, color)
        self.angle = 0.0                 # current rotation (degrees)
        self.tick_id = None              # running animation, or None
        self.last_frame_time = None
        if self.frames:
            self.image = image_from_pixbuf(self.frames[0], size)
            self.fallback = None
            self.pack_start(self.image, False, False, 0)
            self.image.show()
        else:
            self.image = None
            self.fallback = Gtk.Spinner()
            self.fallback.set_size_request(size, size)
            self.pack_start(self.fallback, False, False, 0)
            self.fallback.show()

    @classmethod
    def get_frames(cls, size, color):
        """All pictures of one third of a turn, made once and then reused."""
        key = (size, color)
        if key not in cls.FRAME_CACHE:
            pixel_size = size * screen_scale_factor()
            frames = []
            for index in range(cls.FRAMES):
                angle = index * cls.SYMMETRY / cls.FRAMES
                pixbuf = svg_to_pixbuf(
                    rotor_frame_svg(angle, color, cls.GHOST_BLADES), pixel_size)
                if pixbuf is None:      # no SVG support on this computer
                    frames = []
                    break
                frames.append(pixbuf)
            cls.FRAME_CACHE[key] = frames
        return cls.FRAME_CACHE[key]

    # -- same interface as Gtk.Spinner -----------------------------------
    def start(self):
        if self.fallback is not None:
            self.fallback.start()
        elif self.tick_id is None:
            self.last_frame_time = None
            self.tick_id = self.add_tick_callback(self.on_tick)

    def stop(self):
        if self.fallback is not None:
            self.fallback.stop()
        elif self.tick_id is not None:
            self.remove_tick_callback(self.tick_id)
            self.tick_id = None

    # -- animation --------------------------------------------------------
    def on_tick(self, widget, frame_clock):
        """Called once per screen frame: turn the rotor a little."""
        now = frame_clock.get_frame_time()        # microseconds
        if self.last_frame_time is not None:
            seconds = (now - self.last_frame_time) / 1_000_000
            self.angle = (self.angle + seconds * self.TURNS_PER_SECOND * 360) % self.SYMMETRY
        self.last_frame_time = now
        index = int(self.angle / self.SYMMETRY * self.FRAMES) % self.FRAMES
        self.image.set_from_gicon(self.frames[index], Gtk.IconSize.BUTTON)
        self.image.set_pixel_size(self.size)
        return GLib.SOURCE_CONTINUE
