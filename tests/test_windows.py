import sys
import time
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform == "win32", "Windows native UI")
class WindowsTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from windows_app import DiagnosticsWindow
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.window = DiagnosticsWindow(self.root)
        save = patch("windows_app.save_endpoint")
        self.save_endpoint = save.start()
        self.addCleanup(save.stop)

    def test_offline_navigation_and_no_connections(self):
        with patch("windows_app.subprocess.Popen", side_effect=AssertionError("unexpected SSH")):
            self.window.actions[2].invoke()
            self.assertTrue(self.window.terminal.instate(["disabled"]))
            self.window.actions[0].invoke()
            self.assertEqual(len(self.window.table.get_children()), 1)
            self.window.actions[1].invoke()
            self.assertEqual(len(self.window.table.get_children()), 3)
            self.window.filter_text.set("LiDaR")
            shown = self.window.table.get_children()
            self.assertEqual(len(shown), 1)
            self.assertEqual(self.window.table.item(shown[0], "values")[0], "/livox/lidar")
            with patch("windows_app.filedialog.asksaveasfilename", return_value="demo.csv"), \
                 patch("windows_app.export_csv") as export:
                self.window.export_button.invoke()
                export.assert_called_once_with("demo.csv", ("Topic",), [("/livox/lidar",)])
            self.window.actions[2].invoke()
            self.assertFalse(self.window.table.get_children())
            self.assertTrue(self.window.terminal.instate(["!disabled"]))
            self.save_endpoint.assert_not_called()

    def test_request_completes_and_recovers_from_ssh_error(self):
        self.window.host.set("example.invalid")
        for code, expected in ((0, ["bridge", "Up"]), (255, [])):
            with self.subTest(code=code), patch("windows_app.subprocess.run") as run:
                run.return_value.returncode = code
                run.return_value.stdout = "bridge|Up\n"
                run.return_value.stderr = "Permission denied"
                self.window.actions[0].invoke()
                deadline = time.monotonic() + 3
                while self.window.actions[0].instate(["disabled"]) and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(0.01)
                self.assertTrue(self.window.actions[0].instate(["!disabled"]))
                args = run.call_args.args[0]
                self.assertIn("BatchMode=yes", args)
                self.assertIn("StrictHostKeyChecking=yes", args)
                children = self.window.table.get_children()
                if expected:
                    self.assertEqual(list(self.window.table.item(children[0], "values")), expected)
                else:
                    self.assertFalse(children)
                    self.assertEqual(self.window.status.get(), "Permission denied")
        self.save_endpoint.assert_called_once()
