import json
from pathlib import Path
import shlex
import unittest
from unittest.mock import Mock

from diagnostics import COMMANDS_FILE, load_config, topic_list_command, topic_names


class DiagnosticTests(unittest.TestCase):
    def test_settings_keep_safe_defaults_for_invalid_values(self):
        path = Mock(spec=Path)
        for contents in ("null", "[1]", "{", '{"dry_run": 0}', '{"dry_run": "false"}'):
            with self.subTest(contents=contents):
                path.read_text.return_value = contents
                self.assertIs(load_config(path)["dry_run"], True)
        path.read_text.return_value = '{"dry_run": false, "user": "operator"}'
        self.assertIs(load_config(path)["dry_run"], False)
        self.assertEqual(load_config(path)["user"], "operator")

    def test_configured_topic_command_is_one_bash_argument(self):
        config = json.loads(COMMANDS_FILE.read_text(encoding="utf-8"))
        command = topic_list_command(config)
        self.assertEqual(shlex.split(command), [
            "docker", "exec", "--", config["container_name"], "bash", "-ic", "ros2 topic list"])

    def test_only_unique_ros_names_are_returned(self):
        output = "bash: warning\n/Odometry\r\n /tf_static \n/Odometry\n/invalid name\n"
        self.assertEqual(topic_names(output), ["/Odometry", "/tf_static"])


if __name__ == "__main__":
    unittest.main()
