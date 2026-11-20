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

A bridge that is already running:
  If an earlier OmniQuadGUI was killed hard (or a bridge was started by
  hand in a terminal), a bridge can still be running when we start.
  Ours would then fail with "address already in use".  So before every
  start, copies of the program that belong to this user but were not
  started by us are looked for in /proc, stopped (same signal order as
  above) and then a fresh one is started -- a "restart".
"""

import collections
import glob
import os
import shlex
import signal
import subprocess
import threading
import time
import urllib.parse

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

# Stopping a bridge that was ALREADY running happens before ours starts
# and blocks the app meanwhile, so it uses shorter waits per signal.
LEFTOVER_STOP_STEPS = ((signal.SIGINT, 3), (signal.SIGTERM, 2), (signal.SIGKILL, 1))


def is_same_program(argv, command, name):
    """Is the process with this argv a copy of our program?

    True if
      * the command line contains `command` (the first word may have a
        path, e.g. "/opt/ros/jazzy/bin/ros2 launch foxglove_bridge ..."
        run by python3), or
      * the program itself is called `name` (the node executable
        ".../lib/foxglove_bridge/foxglove_bridge").
    """
    if not argv:
        return False
    if name and os.path.basename(argv[0]) == name:
        return True
    words = shlex.split(command)
    if not words:
        return False
    first, rest = os.path.basename(words[0]), words[1:]
    for i, arg in enumerate(argv):
        if (os.path.basename(arg) == first
                and argv[i + 1:i + 1 + len(rest)] == rest):
            return True
    return False


def read_process(pid):
    """(argv, uid, process group, state letter) of a pid, or None if gone."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            raw = f.read()
        with open("/proc/%d/stat" % pid) as f:
            stat = f.read()
        uid = os.stat("/proc/%d" % pid).st_uid
    except OSError:
        return None
    argv = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
    # stat is "pid (name) STATE ppid pgrp ..."; the name may contain spaces.
    fields = stat[stat.rfind(")") + 2:].split()
    return argv, uid, int(fields[2]), fields[0]


def all_pids():
    return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]


def pid_alive(pid):
    """Running (a finished "zombie" process counts as gone)."""
    info = read_process(pid)
    return info is not None and info[3] not in ("Z", "X")


def port_of(address):
    """8765 for "ws://localhost:8765" (None if the address has no port)."""
    try:
        return urllib.parse.urlparse(address).port
    except ValueError:
        return None


def port_listeners(port):
    """Who listens on TCP `port` on this computer?

    Returns (listening, pids).  `listening` comes from /proc/net/tcp(6),
    so nothing connects to the bridge (no noise in its log).  `pids` are
    the processes holding that socket -- only those of our own user can
    be seen, so it can be empty even when `listening` is True.
    """
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            # fields: sl, local "ADDR:PORT" (hex), remote, state (0A = LISTEN), ..., inode
            if len(fields) > 9 and fields[3] == "0A" \
                    and int(fields[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(fields[9])
    wanted = {"socket:[%s]" % inode for inode in inodes}
    pids = []
    if wanted:
        for pid in all_pids():
            try:
                fds = os.listdir("/proc/%d/fd" % pid)
            except OSError:
                continue                 # gone, or another user's process
            for fd in fds:
                try:
                    if os.readlink("/proc/%d/fd/%s" % (pid, fd)) in wanted:
                        pids.append(pid)
                        break
                except OSError:
                    continue
    return bool(inodes), pids


def program_name(pid):
    """Short name of a process, e.g. "foxglove_bridge" ("?" if unknown)."""
    info = read_process(pid)
    if info is None or not info[0]:
        return "?"
    return os.path.basename(info[0][0])


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

    def __init__(self, name, command, ros_setup="", address=""):
        self.name = name
        self.command = command
        self.port = port_of(address)      # checked by check(); None = skip
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
        """Start the program (does nothing if it already runs).

        A copy that is already running but was not started by us is
        stopped first, so the new one gets the port (see module doc).
        """
        if self.is_running():
            return
        self.stop_leftovers()
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

    def check(self):
        """A fresh look at the bridge, for the refresh button.

        Only reads /proc, changes nothing, and is safe to run in a
        background thread.  Returns a dict:
          own_pid    -- pid of the copy WE started, or None
          others     -- pids of copies not started by us
          listening  -- something listens on the port (None = no port known)
          port_pids  -- who holds the port (own user's processes only)
        """
        process = self.process
        own_pid = process.pid if process is not None and process.poll() is None else None
        listening, port_pids = (None, [])
        if self.port is not None:
            listening, port_pids = port_listeners(self.port)
        return {"own_pid": own_pid, "others": self.find_leftovers(),
                "listening": listening, "port_pids": port_pids}

    def describe_check(self, result):
        """(state, label text or None, log message) for a check() result.

        state is "running", "stopped" or "unknown" (as BridgeStatus uses).
        """
        port = self.port
        on_port = (" and listens on port %d" % port) if port else ""
        holders = ", ".join("%s (PID %d)" % (program_name(pid), pid)
                            for pid in result["port_pids"])
        others = ", ".join(map(str, result["others"]))
        if result["own_pid"] is not None:
            if result["listening"] is False:
                return ("unknown", "STARTED, PORT %d NOT OPEN" % port,
                        "%s runs (PID %d) but nothing listens on port %d. "
                        "It may still be starting; if this stays, see the "
                        "lines above for the reason." % (self.name, result["own_pid"], port))
            message = "%s is running (PID %d)%s." % (self.name, result["own_pid"], on_port)
            if others:
                message += (" Another copy not started by this app is also "
                            "running (PID " + others + ").")
            return "running", None, message
        if others:
            return ("unknown", "RUNNING, NOT STARTED HERE",
                    "A %s not started by this app is running (PID %s)%s. "
                    "Press Start to restart it from here."
                    % (self.name, others, on_port if result["listening"] else ""))
        if result["listening"]:
            return ("stopped", None,
                    "%s is not running, but port %d is already used by %s. "
                    "The bridge cannot start until that program ends."
                    % (self.name, port, holders or "a program of another user"))
        return "stopped", None, self.name + " is not running."

    def find_leftovers(self):
        """PIDs of copies of our program that run for this user, not by us."""
        own_group = None
        if self.process is not None:
            own_group = self.process.pid          # we start it as group leader
        me = os.getpid()
        found = []
        for pid in all_pids():
            info = read_process(pid)
            if info is None or pid == me:
                continue
            argv, uid, group, state = info
            if (uid == os.getuid() and group != own_group
                    and state not in ("Z", "X")
                    and is_same_program(argv, self.command, self.name)):
                found.append(pid)
        return found

    def stop_leftovers(self):
        """Stop already-running copies (Ctrl+C, then SIGTERM, then SIGKILL).

        A copy that leads its own process group (like the bridge an
        earlier OmniQuadGUI started) is stopped as a whole group, so
        the nodes it started go too.  Returns True if none is left.
        """
        pids = self.find_leftovers()
        if not pids:
            return True
        self.emit("A " + self.name + " is already running (PID "
                  + ", ".join(map(str, pids)) + "); restarting it.")
        groups = set()
        for pid in pids:
            info = read_process(pid)
            if info is not None and info[2] == pid and pid != os.getpgrp():
                groups.add(pid)
        # Watch everything in those groups, not only the matching pids.
        watched = set(pids)
        for pid in all_pids():
            info = read_process(pid)
            if info is not None and info[2] in groups:
                watched.add(pid)
        for sig, seconds in LEFTOVER_STOP_STEPS:
            for pid in watched:
                try:
                    if pid in groups:
                        os.killpg(pid, sig)
                    else:
                        os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                watched = {pid for pid in watched if pid_alive(pid)}
                if not watched:
                    self.emit("The old " + self.name + " was stopped.")
                    return True
                time.sleep(0.05)
        self.emit("Could not stop the old " + self.name + " (PID "
                  + ", ".join(map(str, sorted(watched))) + ").")
        return False

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
            return ("Tip: another program already uses this port. Bridges of "
                    "your own user are restarted automatically, so it is "
                    "probably a different program or another user's bridge "
                    "(find it with: ss -ltnp).")
        return ""

    def notify_state(self):
        if self.on_state_changed is not None:
            self.on_state_changed()
