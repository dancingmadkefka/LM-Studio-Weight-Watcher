from __future__ import annotations

import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from lmstudio_weight_watcher import (
    CheckOutcome,
    WatcherApp,
    format_alert_detail,
    hf_file_url,
    hf_repo_url,
    remote_file_basename,
    split_remote_repo,
)


class HfUrlTests(unittest.TestCase):
    def test_repo_url_strips_slashes(self) -> None:
        self.assertEqual(
            hf_repo_url("unsloth/Foo-GGUF/"),
            "https://huggingface.co/unsloth/Foo-GGUF",
        )

    def test_repo_url_empty(self) -> None:
        self.assertEqual(hf_repo_url(None), "")

    def test_file_url_builds_blob_path(self) -> None:
        self.assertEqual(
            hf_file_url("unsloth/Foo", "a/b.gguf"),
            "https://huggingface.co/unsloth/Foo/blob/main/a/b.gguf",
        )

    def test_file_url_without_file_falls_back_to_repo(self) -> None:
        self.assertEqual(
            hf_file_url("unsloth/Foo", None),
            "https://huggingface.co/unsloth/Foo",
        )


class RemoteSourceFormattingTests(unittest.TestCase):
    def test_split_remote_repo(self) -> None:
        self.assertEqual(
            split_remote_repo("bartowski/Some-Model-GGUF"),
            ("bartowski", "Some-Model-GGUF"),
        )
        self.assertEqual(split_remote_repo(None), ("—", "—"))

    def test_remote_file_basename(self) -> None:
        self.assertEqual(
            remote_file_basename("folder/model-Q4_K_M.gguf"),
            "model-Q4_K_M.gguf",
        )

    def test_format_alert_detail_includes_uploader_and_paths(self) -> None:
        detail = format_alert_detail(
            {
                "display_name": "Test Model",
                "remote_repo": "bartowski/Test-GGUF",
                "remote_file": "Test-GGUF/model-Q4_K_M.gguf",
                "local_path": "C:/models/test.gguf",
                "publisher": "test-publisher",
            }
        )
        self.assertIn("Model: Test Model", detail)
        self.assertIn("Uploader: bartowski", detail)
        self.assertIn("Repository: bartowski/Test-GGUF", detail)
        self.assertIn("Remote file: Test-GGUF/model-Q4_K_M.gguf", detail)
        self.assertIn("Local file: C:/models/test.gguf", detail)


class WatcherLifecycleTests(unittest.TestCase):
    def make_app(self) -> WatcherApp:
        app = object.__new__(WatcherApp)
        app.models_root_override = None
        app.hash_cache_path = None
        app.timeout_seconds = 30
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
            hash_cache_path=None,
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
            hash_cache_path=None,
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
