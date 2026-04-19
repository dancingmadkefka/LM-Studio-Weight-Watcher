from __future__ import annotations

import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from lmstudio_weight_watcher import CheckOutcome, WatcherApp


class WatcherLifecycleTests(unittest.TestCase):
    def make_app(self) -> WatcherApp:
        app = object.__new__(WatcherApp)
        app.models_root_override = None
        app.timeout_seconds = 30
        app.tolerance_seconds = 60
        app.shutting_down = False
        app.next_check_token = None
        app.topmost_reset_token = None
        app.window = None
        app.root = Mock()
        app.icon = Mock()
        return app

    def test_check_worker_skips_ui_callback_when_shutting_down(self) -> None:
        app = self.make_app()
        app.shutting_down = True

        with patch("lmstudio_weight_watcher.perform_check") as perform_check:
            perform_check.return_value = CheckOutcome(
                models_root=None,
                results=[],
                error=None,
                generated_at_utc=None,
            )
            app.check_worker(reschedule=True)

        perform_check.assert_called_once_with(
            None,
            timeout_seconds=30,
            tolerance_seconds=60,
        )
        app.root.after.assert_not_called()

    def test_check_worker_ignores_root_teardown_error(self) -> None:
        app = self.make_app()
        app.root.after.side_effect = RuntimeError("main thread is not in main loop")

        with patch("lmstudio_weight_watcher.perform_check") as perform_check:
            perform_check.return_value = CheckOutcome(
                models_root=None,
                results=[],
                error=None,
                generated_at_utc=None,
            )
            app.check_worker(reschedule=False)

        perform_check.assert_called_once_with(
            None,
            timeout_seconds=30,
            tolerance_seconds=60,
        )
        app.root.after.assert_called_once()

    def test_clear_topmost_exits_cleanly_when_window_is_gone(self) -> None:
        app = self.make_app()
        app.topmost_reset_token = "token-1"
        app.window = Mock()
        app.window.winfo_exists.return_value = False

        app._clear_topmost()

        self.assertIsNone(app.topmost_reset_token)
        app.window.attributes.assert_not_called()

    def test_quit_cancels_pending_timers(self) -> None:
        app = self.make_app()
        app.next_check_token = "check-token"
        app.topmost_reset_token = "topmost-token"
        app.window = Mock()
        app.window.winfo_exists.return_value = True

        app.quit()

        self.assertTrue(app.shutting_down)
        self.assertIsNone(app.next_check_token)
        self.assertIsNone(app.topmost_reset_token)
        app.window.after_cancel.assert_called_once_with("topmost-token")
        app.root.after_cancel.assert_called_once_with("check-token")
        app.icon.stop.assert_called_once()
        app.root.quit.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_quit_ignores_tcl_errors_during_teardown(self) -> None:
        app = self.make_app()
        app.next_check_token = "check-token"
        app.root.after_cancel.side_effect = tk.TclError("application has been destroyed")
        app.root.destroy.side_effect = tk.TclError("application has been destroyed")

        app.quit()

        self.assertTrue(app.shutting_down)
        self.assertIsNone(app.next_check_token)
        app.icon.stop.assert_called_once()
        app.root.quit.assert_called_once()
        app.root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
