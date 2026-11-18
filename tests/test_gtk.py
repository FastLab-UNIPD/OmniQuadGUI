import os
import sys
import time
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform.startswith("linux") and os.getenv("DISPLAY"), "GTK display required")
class GtkDemoTests(unittest.TestCase):
    @staticmethod
    def close_app(app):
        if not app.closed:
            with patch("gtk_app.Gtk.main_quit"):
                app.on_main_window_closed(app.main_window, None)
        app.main_window.destroy()

    def test_external_stop_invalidates_shell_and_pending_topics(self):
        from gtk_app import OmniQuadApp
        app = OmniQuadApp(demo=True)
        self.addCleanup(self.close_app, app)
        app.on_enter_container(None)
        with patch.object(app, "run_ssh_background") as remote:
            app.on_list_topics(None)
            receive = remote.call_args.args[1]
        app.on_containers_result(True, 0, "other|Up\n")
        receive(True, 0, "/obsolete\n")
        self.assertFalse(app.inside_container_shell)
        self.assertFalse(app.list_topics_button.get_sensitive())
        self.assertFalse(app.enter_container_button.get_sensitive())
        self.assertFalse(app.topics_listbox.get_children())

    def test_shutdown_requires_a_successful_check_and_explicit_live_setting(self):
        from gtk_app import OmniQuadApp
        app = OmniQuadApp(demo=True)
        self.addCleanup(self.close_app, app)
        with patch.object(app, "run_shutdown_ssh") as shutdown, patch.object(app, "show_loud_warning") as warning:
            app.config["dry_run"] = False
            app.shutdown_verify_done(False, 1, "")
            app.shutdown_verify_done(True, 0, app.commands["container_name"])
            self.assertEqual(warning.call_count, 2)
            shutdown.assert_not_called()
            app.config["dry_run"] = True
            app.shutdown_verify_done(True, 0, "")
            shutdown.assert_not_called()

    def test_bridge_label_and_toggle_follow_the_container_state(self):
        from gtk_app import GLib, OmniQuadApp
        app = OmniQuadApp(demo=True)
        self.addCleanup(self.close_app, app)
        context = GLib.MainContext.default()

        def until(predicate):
            deadline = time.monotonic() + 3
            while not predicate() and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.002)
            self.assertTrue(predicate())

        def label_state():
            classes = app.bridge_status.get_style_context().list_classes()
            return [name for name in classes if name.startswith("oq-bridge-")]

        # Demo bridge runs: green label, the toggle offers "Stop".
        until(lambda: app.container_running is True and app.controls.get_sensitive())
        self.assertEqual(label_state(), ["oq-bridge-running"])
        self.assertEqual(app.bridge_toggle.get_visible_child_name(), "stop")
        # Stop it with the toggle (full safe sequence): red label, "Start".
        app.stop_bridge_button.clicked()
        until(lambda: app.container_running is False and app.controls.get_sensitive())
        self.assertEqual(label_state(), ["oq-bridge-stopped"])
        self.assertEqual(app.bridge_toggle.get_visible_child_name(), "start")
        self.assertTrue(app.start_bridge_button.get_sensitive())
        # A failed check: unknown state, and Start is not allowed blind.
        app.on_containers_result(False, 1, "boom")
        self.assertEqual(label_state(), ["oq-bridge-unknown"])
        self.assertFalse(app.start_bridge_button.get_sensitive())
        # The refresh button checks again (demo answer: still stopped).
        app.bridge_status.refresh_button.clicked()
        until(lambda: app.container_running is False)

    def test_reconnect_opens_a_fresh_login(self):
        import gtk_app
        sessions = []
        real_app = gtk_app.OmniQuadApp

        def create(demo):
            app = real_app(demo=demo)
            sessions.append(app)
            def interact():
                if demo:
                    app.on_ssh_child_exited(app.terminal, 0)
                    app.reconnect_button.clicked()
                else:
                    app.start_window.close()
                return False
            gtk_app.GLib.idle_add(interact)
            return app

        with patch("gtk_app.OmniQuadApp", side_effect=create):
            gtk_app.main(demo=True)
        self.assertEqual(len(sessions), 2)
        self.assertTrue(sessions[0].closed)
        self.assertFalse(sessions[1].demo_mode)
        self.assertEqual(sessions[1].password, "")

    @unittest.skipUnless(os.getenv("OMNIQUAD_TEST_SSH_CONFIG"), "requires isolated loopback SSH fixture")
    def test_real_ssh_login_and_multiplexed_command(self):
        from gtk_app import GLib, OmniQuadApp
        app = OmniQuadApp()
        app.user_entry.set_text("root")
        app.ip_entry.set_text("omni-loopback")
        app.password_entry.set_text("unused-with-test-key")
        context = GLib.MainContext.default()

        def wait_for(predicate):
            deadline = time.monotonic() + 18
            while not predicate() and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.005)
            self.assertTrue(predicate(), app.status_label.get_text())

        app.connect_button.clicked()
        wait_for(lambda: app.main_window is not None and app.controls.get_sensitive())
        self.assertTrue(os.path.exists(app.control_socket))
        self.addCleanup(self.close_app, app)
        answer = []
        command = "printf '%s' \"$SSH_CONNECTION\""
        app.run_ssh_background(command, lambda *args: answer.append(args))
        wait_for(lambda: bool(answer))
        self.assertEqual(answer[0][:2], (True, 0))
        self.assertEqual(answer[0][2].split()[0], "127.0.0.1")
        app.run_ssh_background(command, lambda *args: answer.append(args))
        wait_for(lambda: len(answer) == 2)
        self.assertEqual(answer[0], answer[1])
        app.type_in_terminal("exit")
        wait_for(lambda: app.session_ended)
        self.assertTrue(app.reconnect_button.get_visible())
        with patch("gtk_app.Gtk.main_quit"):
            app.on_main_window_closed(app.main_window, None)
        self.assertFalse(os.path.exists(app.ssh_tmp_dir))

    def test_demo_refresh_selection_and_disconnect(self):
        from gtk_app import GLib, Gtk, OmniQuadApp
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
            self.addCleanup(self.close_app, app)
            until(lambda: app.container_running is True)
            app.enter_container_button.clicked()
            app.list_topics_button.clicked()
            self.assertFalse(app.list_topics_button.get_sensitive())
            until(lambda: len(app.topics_listbox.get_children()) == 4)
            self.assertFalse(app.echo_selected_button.get_sensitive())
            app.topics_listbox.select_row(app.topics_listbox.get_row_at_index(0))
            self.assertEqual(app.selected_topic, "/Odometry")
            self.assertTrue(app.echo_selected_button.get_sensitive())
            app.topic_filter.set_text("LiVoX")
            self.assertEqual(app.selected_topic, "")
            visible = [row.get_child().get_text() for row in app.topics_listbox.get_children()
                       if row.get_child_visible()]
            self.assertEqual(visible, ["/livox/imu", "/livox/lidar"])
            with patch("gtk_app.Gtk.FileChooserDialog") as chooser, patch("gtk_app.export_csv") as export:
                chooser.return_value.run.return_value = Gtk.ResponseType.ACCEPT
                chooser.return_value.get_filename.return_value = "demo.csv"
                app.on_export_csv(None, "topics")
                export.assert_called_once_with("demo.csv", ("Topic",), [("/livox/imu",), ("/livox/lidar",)])
            app.topic_filter.set_text("")
            app.list_topics_button.clicked()
            until(lambda: not app.topics_request)
            self.assertFalse(app.echo_selected_button.get_sensitive())
            app.on_leave_container(None)
            until(lambda: not app.inside_container_shell)
            self.assertFalse(app.topics_listbox.get_children())
            app.on_ssh_child_exited(app.terminal, 0)
            app.set_drone_buttons_enabled(True)
            self.assertFalse(app.controls.get_sensitive())
            self.assertTrue(app.reconnect_button.get_visible())
            with patch("gtk_app.MAX_LOG_LINES", 3), patch("gtk_app.MAX_LOG_CHARS", 30):
                app.append_log("old\n" * 15 + "latest")
                buffer = app.log_buffer
                self.assertLessEqual(buffer.get_line_count(), 3)
                self.assertLessEqual(buffer.get_char_count(), 30)
                self.assertTrue(buffer.get_text(*buffer.get_bounds(), False).endswith("latest\n"))
