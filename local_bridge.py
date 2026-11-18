"""Run a ROS 2 program on THIS computer (the Foxglove bridge).

Unlike everything else in the app, this program does not run on the
drone: it is started here, as a normal child process, when OmniQuadGUI
starts, and stopped when OmniQuadGUI closes.

Why it is started through `bash -i`:
  main.py removes ROS from the app's OWN environment (with ROS sourced,
  GTK froze).  But `ros2` needs that environment.  An interactive bash
  (`bash -i`) reads ~/.bashrc, which is where ROS is normally sourced
  (and where e.g. ROS_DOMAIN_ID is set), so the bridge gets exactly the
  ROS setup you have in a normal terminal -- while the app stays clean.
  If `ros2` is still not found, /opt/ros/<distro>/setup.bash is sourced
  as a fallback (or the file named by "ros_setup" in config.json).

How it is stopped:
  The program runs in its own process group, so one signal reaches
  `ros2 launch` AND the nodes it started.  First Ctrl+C (SIGINT, the
  polite way: ros2 launch shuts its nodes down cleanly), then SIGTERM,
  then SIGKILL if it still refuses to end.
"""

import collections
import glob
import os
import shlex
import signal
import subprocess
import threading
import time

import gi
gi.require_version("GLib", "2.0")
from gi.repository import GLib


# Harmless warnings an interactive bash prints when it has no terminal;
# they would only confuse in the log, so they are dropped.
BASH_NOISE = (
    "bash: cannot set terminal process group",
    "bash: no job control in this shell",
)

# Seconds to wait for a clean stop before using the next, harder signal.
STOP_GRACE_SECONDS = 5


def find_ros_setup(configured=""):
    """The ROS setup script to fall back on ("" if none is installed)."""
    if configured:
        return configured
    candidates = sorted(glob.glob("/opt/ros/*/setup.bash"))
    return candidates[-1] if candidates else ""


def build_launch_argv(command, ros_setup):
    """The exact program + arguments used to start `command`.

    Example result:
        ["bash", "-ic",
         "command -v ros2 >/dev/null 2>&1 || source /opt/ros/jazzy/setup.bash; "
         "exec ros2 launch foxglove_bridge foxglove_bridge_launch.xml"]
    `exec` replaces bash with the command, so stopping it is simple.
    """
    script = ""
    if ros_setup:
        script += ("command -v ros2 >/dev/null 2>&1 || source "
                   + shlex.quote(ros_setup) + "; ")
    script += "exec " + command
    return ["bash", "-ic", script]


class LocalBridge:
    """Start, watch and stop one local program.

    The GUI connects two functions (both are called in the MAIN thread):
      on_output(line)     -- one line the program printed
      on_state_changed()  -- the program started or ended
    The last lines are also kept in `recent_lines`, so a window that
    opens later can still show what happened before.
    """

    def __init__(self, name, command, ros_setup=""):
        self.name = name
        self.command = command
        self.ros_setup = find_ros_setup(ros_setup)
        self.process = None
        self.stopping = False             # True after we asked it to stop
        self.recent_lines = collections.deque(maxlen=300)
        self.on_output = None
        self.on_state_changed = None

    # ------------------------------------------------------------------
    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        """Start the program (does nothing if it already runs)."""
        if self.is_running():
            return
        env = os.environ.copy()
        # Print every line at once (no buffering), and without colour
        # codes, so the log shows clean text as it happens.
        env["PYTHONUNBUFFERED"] = "1"
        env["RCUTILS_COLORIZED_OUTPUT"] = "0"
        env["RCUTILS_LOGGING_BUFFERED_STREAM"] = "0"
        self.stopping = False
        self.emit("$ " + self.command)
        try:
            self.process = subprocess.Popen(
                build_launch_argv(self.command, self.ros_setup),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,      # errors into the same stream
                text=True, errors="replace", bufsize=1,
                env=env,
                start_new_session=True,        # own process group
            )
        except OSError as error:
            self.process = None
            self.emit("Could not start " + self.name + ": " + str(error))
            self.notify_state()
            return
        # A background thread reads the output line by line.
        threading.Thread(target=self.read_output, args=(self.process,),
                         daemon=True).start()
        self.notify_state()

    def stop(self, wait=False):
        """Ask the program to end.

        wait=False (buttons): returns at once; harder signals follow
        automatically if it does not end in time.
        wait=True (app closing): blocks until it has really ended.
        """
        if self.process is None:
            return
        self.stopping = True
        process = self.process
        self.signal_group(process, signal.SIGINT)
        if wait:
            # Polite stop first; if anything of it is still alive after
            # the grace time, insist with SIGTERM, then SIGKILL.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if self.wait_for_group(process, STOP_GRACE_SECONDS):
                    return
                self.signal_group(process, sig)
            self.wait_for_group(process, STOP_GRACE_SECONDS)
        else:
            GLib.timeout_add_seconds(STOP_GRACE_SECONDS, self.stop_harder, process,
                                     signal.SIGTERM)

    def stop_harder(self, process, sig):
        """Timer: is ANY part of it still running after the stop?  Insist.

        We check the whole process group, not only the main program: a
        node that ignores Ctrl+C would otherwise keep running unnoticed.
        """
        process.poll()                  # tidy up the main program if it ended
        if self.group_alive(process):
            self.emit(self.name + " did not stop in time; sending "
                      + signal.Signals(sig).name + ".")
            self.signal_group(process, sig)
            if sig == signal.SIGTERM:
                GLib.timeout_add_seconds(STOP_GRACE_SECONDS, self.stop_harder,
                                         process, signal.SIGKILL)
        return False      # run this timer only once

    def wait_for_group(self, process, seconds):
        """Wait up to `seconds` until NOTHING of the program is left."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            process.poll()
            if not self.group_alive(process):
                return True
            time.sleep(0.05)
        return False

    @staticmethod
    def group_alive(process):
        """Is anything of the program (or what it started) still running?"""
        try:
            os.killpg(process.pid, 0)   # signal 0 = "only check"
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def signal_group(process, sig):
        """Send `sig` to the program AND everything it started."""
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass          # already gone

    # ------------------------------------------------------------------
    def read_output(self, process):
        """BACKGROUND THREAD: pass each printed line to the main thread."""
        for line in process.stdout:
            line = line.rstrip()
            if line and not line.startswith(BASH_NOISE):
                GLib.idle_add(self.emit, line)
        # The output ends when the program AND everything it started have
        # closed it.
        process.stdout.close()
        code = process.wait()
        GLib.idle_add(self.on_process_ended, process, code)

    def on_process_ended(self, process, code):
        """MAIN THREAD: the program has ended."""
        if process is self.process:
            self.process = None
        if self.stopping:
            self.emit(self.name + " stopped.")
        else:
            self.emit(self.name + " ended unexpectedly (exit code "
                      + str(code) + "). See the lines above for the reason.")
        self.notify_state()
        return False

    def emit(self, line):
        """MAIN THREAD: remember one output line and show it in the GUI."""
        self.recent_lines.append(line)
        if self.on_output is not None:
            self.on_output(line)
        hint = self.hint_for(line)
        if hint:
            self.recent_lines.append(hint)
            if self.on_output is not None:
                self.on_output(hint)
        return False      # for GLib.idle_add: run only once

    def hint_for(self, line):
        """A plain-English tip for the most common problems (or "")."""
        text = line.lower()
        if "package 'foxglove_bridge' not found" in text:
            distro = self.ros_setup.split("/")[3] if self.ros_setup.count("/") >= 4 else "<distro>"
            return ("Tip: the Foxglove bridge is not installed. Install it with:\n"
                    "  sudo apt install ros-" + distro + "-foxglove-bridge")
        if "ros2: command not found" in text:
            return ("Tip: ROS 2 was not found. Source ROS in ~/.bashrc, or set "
                    "\"ros_setup\" in config.json to your setup.bash.")
        if "address already in use" in text:
            return ("Tip: another Foxglove bridge is probably already running "
                    "(port in use). Stop it, then start this one again.")
        return ""

    def notify_state(self):
        if self.on_state_changed is not None:
            self.on_state_changed()
