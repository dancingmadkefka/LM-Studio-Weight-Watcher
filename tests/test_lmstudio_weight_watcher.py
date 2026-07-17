from __future__ import annotations

import tkinter as tk
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lmstudio_weight_watcher import (
    CheckOutcome,
    RecoveryWorkerOutcome,
    UpdateWorkerOutcome,
    WatcherApp,
    format_alert_detail,
    hf_file_url,
    hf_repo_url,
    remote_file_basename,
    split_remote_repo,
)
from lmstudio_weight_updater import CancellationToken, ProgressEvent, UpdatePlan


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
        app.last_models_root = None
        app.hash_cache_path = None
        app.timeout_seconds = 30
        app.shutting_down = False
        app.check_in_progress = False
        app.recovery_in_progress = False
        app.update_in_progress = False
        app.update_cancellable = False
        app.update_cancellation = None
        app.update_thread = None
        app.update_commit_started = threading.Event()
        app.update_model_status = {}
        app.active_update_plan = None
        app.last_update_phase = None
        app.last_update_state_save_monotonic = 0.0
        app.last_progress_post_monotonic = 0.0
        app.last_progress_post_phase = None
        app.quit_after_update = False
        app.next_check_token = None
        app.topmost_reset_token = None
        app.window = None
        app.root = Mock()
        app.icon = Mock()
        app.state_path = Path("test-state.json")
        app.state = {"alerts": {}, "active_update": None}
        app._alerts_by_key = {}
        app.status_var = Mock()
        app.update_progress_var = Mock()
        app.refresh_ui = Mock()
        app.schedule_next_check = Mock()
        app.maybe_raise_pending_window = Mock()
        return app

    def test_startup_recovery_runs_in_background(self) -> None:
        app = self.make_app()
        worker = Mock()
        with patch("lmstudio_weight_watcher.threading.Thread") as thread:
            thread.return_value = worker
            app.after_start()
        self.assertTrue(app.recovery_in_progress)
        self.assertEqual(thread.call_args.kwargs["target"], app.recovery_worker)
        worker.start.assert_called_once()

    def test_recovery_worker_only_marshals_result_to_tk(self) -> None:
        app = self.make_app()
        models_root = Path("models")
        with (
            patch("lmstudio_weight_watcher.discover_models_root", return_value=models_root),
            patch("lmstudio_weight_watcher.recover_interrupted_jobs", return_value=[]),
        ):
            app.recovery_worker()
        app.root.after.assert_called_once()
        self.assertIsNone(app.last_models_root)
        app.status_var.set.assert_not_called()

    def test_finish_recovery_starts_initial_check(self) -> None:
        app = self.make_app()
        app.recovery_in_progress = True
        app.run_check_async = Mock()
        app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [], None))
        self.assertFalse(app.recovery_in_progress)
        app.run_check_async.assert_called_once_with(reschedule=True)

    def test_failed_recovery_preserves_active_recovery_metadata(self) -> None:
        app = self.make_app()
        app.recovery_in_progress = True
        app.state["active_update"] = {"job_id": "job-1"}
        app.run_check_async = Mock()
        with patch("lmstudio_weight_watcher.save_state") as save_state:
            app.finish_recovery(
                RecoveryWorkerOutcome(None, [], "models root unavailable")
            )
        self.assertEqual(app.state["active_update"]["job_id"], "job-1")
        save_state.assert_not_called()
        app.run_check_async.assert_called_once_with(reschedule=True)

    def make_plan(self) -> UpdatePlan:
        return UpdatePlan(
            job_id="job-1",
            models_root=Path("models"),
            staging_root=Path("staging"),
            manifest_path=Path("staging/manifest.json"),
            artifacts=(),
            selected_model_keys=("model",),
            selected_model_names=("Model",),
            total_bytes=1024,
            remaining_download_bytes=512,
            available_bytes=4096,
            safety_reserve_bytes=256,
        )

    def test_check_does_not_start_while_update_is_active(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        with patch("lmstudio_weight_watcher.threading.Thread") as thread:
            app.run_check_async(reschedule=True)
        thread.assert_not_called()
        self.assertFalse(app.check_in_progress)
        app.schedule_next_check.assert_called_once()

    def test_prepare_update_runs_planning_in_worker_thread(self) -> None:
        app = self.make_app()
        app.state["alerts"] = {"model": {"model_key": "model"}}
        with patch("lmstudio_weight_watcher.threading.Thread") as thread:
            worker = Mock()
            thread.return_value = worker
            app.prepare_update_async(["model"])
        self.assertTrue(app.update_in_progress)
        self.assertIsInstance(app.update_cancellation, CancellationToken)
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["target"], app.prepare_update_worker)
        worker.start.assert_called_once()

    def test_cancel_during_planning_sets_shared_token(self) -> None:
        app = self.make_app()
        app.state["alerts"] = {"model": {"model_key": "model"}}
        with patch("lmstudio_weight_watcher.threading.Thread") as thread:
            thread.return_value = Mock()
            app.prepare_update_async(["model"])
        token = app.update_cancellation
        app.cancel_update()
        self.assertIsNotNone(token)
        self.assertTrue(token.cancelled)
        self.assertFalse(app.update_cancellable)

    def test_confirmation_decline_does_not_start_executor(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_cancellation = CancellationToken()
        app.update_model_status = {"model": "Queued"}
        with (
            patch("lmstudio_weight_watcher.messagebox.askyesno", return_value=False),
            patch("lmstudio_weight_watcher.threading.Thread") as thread,
        ):
            app.finish_prepare_update(self.make_plan(), None)
        thread.assert_not_called()
        self.assertFalse(app.update_in_progress)
        self.assertIsNone(app.active_update_plan)
        self.assertEqual(app.update_model_status, {})

    def test_confirmation_accept_starts_worker_and_records_active_update(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_cancellation = CancellationToken()
        app.update_model_status = {"model": "Queued"}
        worker = Mock()
        with (
            patch("lmstudio_weight_watcher.messagebox.askyesno", return_value=True),
            patch("lmstudio_weight_watcher.threading.Thread") as thread,
            patch("lmstudio_weight_watcher.save_state") as save_state,
        ):
            thread.return_value = worker
            app.finish_prepare_update(self.make_plan(), None)
        self.assertEqual(app.state["active_update"]["job_id"], "job-1")
        save_state.assert_called_once()
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["target"], app.update_worker)
        worker.start.assert_called_once()

    def test_confirmation_window_failure_resets_update_state(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_cancellation = CancellationToken()
        app.update_model_status = {"model": "Queued"}
        with patch(
            "lmstudio_weight_watcher.messagebox.askyesno",
            side_effect=tk.TclError("window destroyed"),
        ):
            app.finish_prepare_update(self.make_plan(), None)
        self.assertFalse(app.update_in_progress)
        self.assertIsNone(app.active_update_plan)
        app.status_var.set.assert_called_once()

    def test_progress_callback_is_marshalled_to_tk_thread(self) -> None:
        app = self.make_app()
        event = ProgressEvent("downloading", "Downloading", bytes_completed=1, bytes_total=2)
        app.post_update_progress(event)
        app.root.after.assert_called_once()
        self.assertEqual(app.root.after.call_args.args[0], 0)

    def test_progress_callback_skips_destroyed_app(self) -> None:
        app = self.make_app()
        app.shutting_down = True
        app.post_update_progress(ProgressEvent("downloading", "Downloading"))
        app.root.after.assert_not_called()

    def test_install_progress_disables_cancel_and_persists_phase(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_cancellable = True
        app.update_model_status = {"model": "Downloading"}
        app.state["active_update"] = {
            "job_id": "job-1",
            "model_keys": ["model"],
            "model_names": ["Model"],
        }
        event = ProgressEvent(
            "installing",
            "Installing verified file",
            bytes_completed=512,
            bytes_total=1024,
            cancellable=False,
            model_keys=("model",),
        )
        with patch("lmstudio_weight_watcher.save_state") as save_state:
            app.handle_update_progress(event)
        self.assertFalse(app.update_cancellable)
        self.assertEqual(app.update_model_status["model"], "Installing")
        self.assertEqual(app.state["active_update"]["phase"], "installing")
        save_state.assert_called_once()

    def test_finish_update_clears_active_state_and_records_success(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_model_status = {"model": "Installing"}
        app.state = {
            "alerts": {},
            "active_update": {
                "job_id": "job-1",
                "model_keys": ["model"],
                "model_names": ["Model"],
            },
        }
        with patch("lmstudio_weight_watcher.save_state"):
            app.finish_update(UpdateWorkerOutcome(True, False, "Done", []))
        self.assertFalse(app.update_in_progress)
        self.assertIsNone(app.state["active_update"])
        self.assertTrue(app.state["last_update"]["success"])
        app.refresh_ui.assert_called()
        app.schedule_next_check.assert_called_once()

    def test_locked_update_failure_shows_unload_guidance(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_model_status = {"model": "Installing"}
        app.state["active_update"] = {
            "job_id": "job-1",
            "model_keys": ["model"],
            "model_names": ["Model"],
        }
        with patch("lmstudio_weight_watcher.save_state"):
            app.finish_update(
                UpdateWorkerOutcome(
                    False,
                    False,
                    "Unload the model in LM Studio and try again.",
                    [],
                )
            )
        self.assertEqual(app.update_model_status["model"], "Waiting for model unload")
        app.status_var.set.assert_called_with(
            "Update failed: Unload the model in LM Studio and try again."
        )

    def test_selected_keys_exclude_synthetic_tree_rows(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ("model", "I001")
        app._alerts_by_key = {"model": {"model_key": "model"}}
        self.assertEqual(app.selected_model_keys(), ["model"])

    def test_quit_defers_while_install_is_non_cancellable(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_cancellable = False
        app.quit()
        self.assertFalse(app.shutting_down)
        self.assertTrue(app.quit_after_update)
        app.icon.stop.assert_not_called()

    def test_quit_defers_as_soon_as_worker_enters_install_phase(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_cancellable = True
        app.update_cancellation = CancellationToken()
        app.post_update_progress(
            ProgressEvent("installing", "Installing", cancellable=False)
        )

        # Deliberately do not execute the queued root.after callback.
        app.quit()

        self.assertTrue(app.update_commit_started.is_set())
        self.assertTrue(app.quit_after_update)
        self.assertFalse(app.shutting_down)
        self.assertFalse(app.update_cancellation.cancelled)
        app.icon.stop.assert_not_called()

    def test_quit_defers_during_startup_recovery(self) -> None:
        app = self.make_app()
        app.recovery_in_progress = True
        app.quit()
        self.assertTrue(app.quit_after_update)
        self.assertFalse(app.shutting_down)
        app.icon.stop.assert_not_called()

    def test_quit_cancels_download_before_shutdown(self) -> None:
        app = self.make_app()
        app.update_in_progress = True
        app.update_cancellable = True
        app.update_cancellation = CancellationToken()
        app.quit()
        self.assertTrue(app.update_cancellation.cancelled)
        self.assertTrue(app.shutting_down)
        app.icon.stop.assert_called_once()

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
