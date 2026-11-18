import os
import sys
import time
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform.startswith("linux") and os.getenv("DISPLAY"), "GTK display required")
class GtkDemoTests(unittest.TestCase):
    def test_demo_refresh_selection_and_disconnect(self):
        from gtk_app import GLib, OmniQuadApp
        context = GLib.MainContext.default()

        def until(predicate):
            deadline = time.monotonic() + 3
            while not predicate() and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.002)
            self.assertTrue(predicate())

        with patch("gtk_app.subprocess.run", side_effect=AssertionError("unexpected SSH")):
            app = OmniQuadApp()
            app.user_entry.set_text(app.config["demo_user"])
            app.password_entry.set_text(app.config["demo_password"])
            app.connect_button.clicked()
            self.addCleanup(app.main_window.destroy)
            until(lambda: len(app.container_listbox.get_children()) == 1)
            app.enter_container_button.clicked()
            app.list_topics_button.clicked()
            self.assertFalse(app.list_topics_button.get_sensitive())
            until(lambda: len(app.topics_listbox.get_children()) == 4)
            self.assertFalse(app.echo_selected_button.get_sensitive())
            app.topics_listbox.select_row(app.topics_listbox.get_row_at_index(0))
            self.assertEqual(app.selected_topic, "/Odometry")
            self.assertTrue(app.echo_selected_button.get_sensitive())
            app.list_topics_button.clicked()
            until(lambda: not app.topics_request)
            self.assertFalse(app.echo_selected_button.get_sensitive())
            app.on_leave_container(None)
            until(lambda: not app.inside_container_shell)
            self.assertFalse(app.topics_listbox.get_children())
            app.on_ssh_child_exited(app.terminal, 0)
            app.set_drone_buttons_enabled(True)
            self.assertFalse(app.controls.get_sensitive())
