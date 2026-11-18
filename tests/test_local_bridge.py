import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from local_bridge import LocalBridge, build_launch_argv  # noqa: E402
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

    def test_missing_package_gets_an_install_tip(self):
        bridge = LocalBridge("foxglove_bridge", "true", "/opt/ros/jazzy/setup.bash")
        bridge.emit("Package 'foxglove_bridge' not found: ...")
        self.assertIn("sudo apt install ros-jazzy-foxglove-bridge", bridge.recent_lines[-1])


if __name__ == "__main__":
    unittest.main()
