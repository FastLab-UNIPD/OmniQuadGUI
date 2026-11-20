import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from local_bridge import (LocalBridge, build_launch_argv, is_same_program,  # noqa: E402
                          port_listeners, port_of)
from gi.repository import GLib  # noqa: E402


def wait_until(predicate, seconds=10):
    """Run the GLib main loop until predicate() is true (or time is up)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)
    return predicate()


def group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


class LocalBridgeTests(unittest.TestCase):
    def make_bridge(self, command):
        bridge = LocalBridge("test_bridge", command)
        bridge.ros_setup = ""            # no ROS needed for these tests
        self.addCleanup(bridge.stop, True)
        return bridge

    def test_launch_command_sources_ros_only_as_a_fallback(self):
        argv = build_launch_argv("ros2 launch pkg file.xml", "/opt/ros/jazzy/setup.bash")
        self.assertEqual(argv[:2], ["bash", "-ic"])
        self.assertEqual(argv[2], "command -v ros2 >/dev/null 2>&1 || source "
                                  "/opt/ros/jazzy/setup.bash; exec ros2 launch pkg file.xml")
        self.assertEqual(build_launch_argv("prog", "")[2], "exec prog")

    def test_output_state_and_clean_stop_of_the_whole_group(self):
        bridge = self.make_bridge("sh -c 'echo hello; sleep 30'")
        states = []
        bridge.on_state_changed = lambda: states.append(bridge.is_running())
        bridge.start()
        self.assertTrue(bridge.is_running())
        self.assertTrue(wait_until(lambda: "hello" in bridge.recent_lines))
        pid = bridge.process.pid
        bridge.stop()
        self.assertTrue(wait_until(lambda: not bridge.is_running() and states[-1] is False))
        self.assertIn("test_bridge stopped.", bridge.recent_lines)
        # The child `sleep` was stopped too (whole process group).
        self.assertTrue(wait_until(lambda: not group_alive(pid)))

    def test_a_child_that_ignores_ctrl_c_is_stopped_harder(self):
        # A background job of a non-interactive shell ignores SIGINT, like a
        # stubborn ROS node would.  The bridge must escalate to SIGTERM.
        with patch("local_bridge.STOP_GRACE_SECONDS", 1):
            bridge = self.make_bridge("sh -c 'echo ready; sleep 30 & wait'")
            bridge.start()
            self.assertTrue(wait_until(lambda: "ready" in bridge.recent_lines))
            pid = bridge.process.pid
            bridge.stop()
            self.assertTrue(wait_until(lambda: not group_alive(pid), 8))
            self.assertTrue(wait_until(lambda: not bridge.is_running()
                                       and "test_bridge stopped." in bridge.recent_lines))
            self.assertTrue(any("SIGTERM" in line for line in bridge.recent_lines))

    def test_stop_and_wait_also_ends_stubborn_children(self):
        with patch("local_bridge.STOP_GRACE_SECONDS", 1):
            bridge = self.make_bridge("sh -c 'sleep 30 & wait'")
            bridge.start()
            time.sleep(0.3)
            pid = bridge.process.pid
            bridge.stop(wait=True)
            self.assertFalse(group_alive(pid))

    def test_unexpected_exit_is_reported(self):
        bridge = self.make_bridge("sh -c 'exit 3'")
        bridge.start()
        self.assertTrue(wait_until(lambda: not bridge.is_running()
                                   and any("exit code 3" in line for line in bridge.recent_lines)))

    def test_stop_and_wait_ends_the_program(self):
        bridge = self.make_bridge("sleep 30")
        bridge.start()
        pid = bridge.process.pid
        bridge.stop(wait=True)
        self.assertFalse(bridge.is_running())
        self.assertFalse(group_alive(pid))

    def test_recognises_copies_of_the_program(self):
        command = "ros2 launch foxglove_bridge foxglove_bridge_launch.xml"
        launch = ["/usr/bin/python3", "/opt/ros/jazzy/bin/ros2", "launch",
                  "foxglove_bridge", "foxglove_bridge_launch.xml"]
        node = ["/opt/ros/jazzy/lib/foxglove_bridge/foxglove_bridge", "--ros-args"]
        self.assertTrue(is_same_program(launch, command, "foxglove_bridge"))
        self.assertTrue(is_same_program(node, command, "foxglove_bridge"))
        # Things that only MENTION the bridge are left alone.
        for argv in (["grep", "foxglove_bridge"],
                     ["vim", "foxglove_bridge_launch.xml"],
                     ["bash", "-ic", "exec " + command],
                     ["ros2", "launch", "foxglove_bridge", "other_launch.xml"],
                     []):
            self.assertFalse(is_same_program(argv, command, "foxglove_bridge"), argv)

    def start_leftover(self, argv):
        """A copy of the program that we did NOT start (like a stuck bridge)."""
        leftover = subprocess.Popen(argv, start_new_session=True,
                                    stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (leftover.kill(), leftover.wait()))
        time.sleep(0.2)
        return leftover

    def test_an_already_running_copy_is_restarted(self):
        leftover = self.start_leftover(["sleep", "4242.5"])
        bridge = self.make_bridge("sleep 4242.5")
        bridge.start()
        self.assertIsNotNone(leftover.wait(timeout=5))          # old one ended
        self.assertTrue(bridge.is_running())                    # new one runs
        self.assertNotEqual(bridge.process.pid, leftover.pid)
        self.assertTrue(any("already running (PID %d)" % leftover.pid in line
                            for line in bridge.recent_lines))
        self.assertIn("The old test_bridge was stopped.", bridge.recent_lines)
        # Our own running copy is never mistaken for a leftover.
        self.assertEqual(bridge.find_leftovers(), [])

    def test_a_leftover_that_ignores_ctrl_c_gets_sigterm(self):
        steps = ((signal.SIGINT, 0.5), (signal.SIGTERM, 2), (signal.SIGKILL, 1))
        with patch("local_bridge.LEFTOVER_STOP_STEPS", steps):
            # sh ignores SIGINT and so does the sleep it starts.
            leftover = self.start_leftover(["sh", "-c", "trap '' INT; sleep 4343.5; true"])
            bridge = self.make_bridge("sleep 4343.5")
            self.assertEqual(len(bridge.find_leftovers()), 1)
            self.assertTrue(bridge.stop_leftovers())
            self.assertEqual(bridge.find_leftovers(), [])
        leftover.kill()

    def test_other_programs_are_not_touched(self):
        other = self.start_leftover(["sleep", "4444.5"])
        bridge = self.make_bridge("sleep 4545.5")
        bridge.start()
        self.assertIsNone(other.poll())
        self.assertFalse(any("already running" in line for line in bridge.recent_lines))

    def test_port_is_read_from_the_address(self):
        self.assertEqual(port_of("ws://localhost:8765"), 8765)
        self.assertIsNone(port_of(""))
        self.assertIsNone(port_of("ws://localhost"))

    def test_port_listeners_finds_a_listening_socket_and_its_owner(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        self.assertEqual(port_listeners(port), (False, []))   # bound, not listening
        server.listen()
        self.assertEqual(port_listeners(port), (True, [os.getpid()]))

    def test_check_sees_our_program_and_its_port(self):
        with socket.socket() as probe:              # find a free port
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        script = ("import socket, time; s = socket.socket(); "
                  "s.bind(('127.0.0.1', %d)); s.listen(); "
                  "print('ready', flush=True); time.sleep(30)" % port)
        bridge = LocalBridge("test_bridge", "python3 -c " + shlex.quote(script),
                             address="ws://localhost:%d" % port)
        bridge.ros_setup = ""
        self.addCleanup(bridge.stop, True)
        self.assertEqual(bridge.check(), {"own_pid": None, "others": [],
                                          "listening": False, "port_pids": []})
        bridge.start()
        self.assertTrue(wait_until(lambda: "ready" in bridge.recent_lines))
        result = bridge.check()
        self.assertEqual(result["own_pid"], bridge.process.pid)
        self.assertEqual(result["others"], [])
        self.assertTrue(result["listening"])
        self.assertEqual(result["port_pids"], [bridge.process.pid])
        state, text, message = bridge.describe_check(result)
        self.assertEqual((state, text), ("running", None))
        self.assertIn("listens on port %d" % port, message)

    def test_describe_check_explains_every_case(self):
        bridge = LocalBridge("fxb", "true", address="ws://localhost:8765")

        def describe(own=None, others=(), listening=False, port_pids=()):
            return bridge.describe_check({"own_pid": own, "others": list(others),
                                          "listening": listening,
                                          "port_pids": list(port_pids)})
        self.assertEqual(describe(own=10, listening=True)[:2], ("running", None))
        self.assertEqual(describe(own=10)[:2], ("unknown", "STARTED, PORT 8765 NOT OPEN"))
        state, text, message = describe(others=[20, 21], listening=True)
        self.assertEqual((state, text), ("unknown", "RUNNING, NOT STARTED HERE"))
        self.assertIn("PID 20, 21", message)
        self.assertIn("Press Start", message)
        state, _, message = describe(listening=True, port_pids=[os.getpid()])
        self.assertEqual(state, "stopped")
        self.assertIn("port 8765 is already used by", message)
        self.assertIn("(PID %d)" % os.getpid(), message)
        self.assertIn("another user", describe(listening=True)[2])
        self.assertEqual(describe(), ("stopped", None, "fxb is not running."))

    def test_missing_package_gets_an_install_tip(self):
        bridge = LocalBridge("foxglove_bridge", "true", "/opt/ros/jazzy/setup.bash")
        bridge.emit("Package 'foxglove_bridge' not found: ...")
        self.assertIn("sudo apt install ros-jazzy-foxglove-bridge", bridge.recent_lines[-1])


if __name__ == "__main__":
    unittest.main()
