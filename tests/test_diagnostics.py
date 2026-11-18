import json
import csv
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import Mock

from diagnostics import (COMMANDS_FILE, export_csv, load_commands, load_config,
                         save_endpoint, topic_list_command, topic_names)


class DiagnosticTests(unittest.TestCase):
    def test_settings_keep_safe_defaults_for_invalid_values(self):
        path = Mock(spec=Path)
        empty = Mock(spec=Path)
        empty.read_text.return_value = "{}"
        for contents in ("null", "[1]", "{", '{"dry_run": 0}', '{"dry_run": "false"}'):
            with self.subTest(contents=contents):
                path.read_text.return_value = contents
                self.assertIs(load_config(path, empty)["dry_run"], True)
        path.read_text.return_value = '{"dry_run": false, "user": "operator"}'
        self.assertIs(load_config(path, empty)["dry_run"], False)
        self.assertEqual(load_config(path, empty)["user"], "operator")

    def temporary_file(self):
        fd, name = tempfile.mkstemp()
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_endpoint_only_overrides_connection_fields(self):
        path = self.temporary_file()
        save_endpoint("tester", "bench.local", path)
        self.assertEqual(json.loads(path.read_text()), {"ip": "bench.local", "user": "tester"})
        path.write_text('{"user":"tester","ip":"bench.local","dry_run":false}')
        config = Mock(spec=Path)
        config.read_text.return_value = '{"user":"original","dry_run":true}'
        self.assertEqual(load_config(config, path)["user"], "tester")
        self.assertIs(load_config(config, path)["dry_run"], True)

    def test_csv_round_trip_and_failed_export_preserves_previous_file(self):
        path = self.temporary_file()
        rows = [("bridge,one", "Up"), ("télémétrie", "=1+1")]
        export_csv(path, ["Container", "Status"], rows)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(list(csv.reader(stream)), [
                ["Container", "Status"], ["bridge,one", "Up"], ["télémétrie", "'=1+1"]])
        def interrupted():
            yield rows[0]
            raise OSError("interrupted")
        before = path.read_bytes()
        with self.assertRaises(OSError):
            export_csv(path, ["Container", "Status"], interrupted())
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_button_points_to_the_setting(self):
        path = Mock(spec=Path)
        path.read_text.return_value = json.dumps({"container_name": "test", "host_buttons": [{}]})
        with self.assertRaisesRegex(ValueError, r"host_buttons\[0\]"):
            load_commands(path)

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
