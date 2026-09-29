from __future__ import annotations

from datetime import datetime, timezone
import argparse
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tkinter as tk
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lmstudio_alert_state import load_state
from lmstudio_weight_watcher import (
    CheckOutcome,
    DependencyPreflightResult,
    RecoveryWorkerOutcome,
    UpdateWorkerOutcome,
    WatcherApp,
    format_alert_detail,
    hf_file_url,
    hf_repo_url,
    format_preflight_failure,
    main,
    probe_dependencies,
    remote_file_basename,
    split_remote_repo,
)
from lmstudio_weight_updater import CancellationToken, InstallError, ProgressEvent, RecoveryAction, UpdatePlan


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


class PreflightTests(unittest.TestCase):
    def test_preflight_reports_broken_pillow(self) -> None:
        failure = ImportError("cannot import _imaging", name="PIL._imaging")
        with patch("lmstudio_weight_watcher._import_dependency", side_effect=failure):
            result = probe_dependencies()
        message = format_preflight_failure(result)
        self.assertEqual(result.failed_import, "PIL._imaging")
        self.assertIn(sys.executable, message)
        self.assertIn(f'"{sys.executable}" -m pip install -r requirements.txt', message)

    def test_preflight_passes_with_dependencies(self) -> None:
        with patch("lmstudio_weight_watcher._import_dependency", return_value=object()):
            result = probe_dependencies()
        self.assertIsNone(result.failed_import)
        self.assertIsNone(result.error)

    def test_preflight_error_exit_code_is_distinct(self) -> None:
        args = argparse.Namespace(once=True, preflight_only=False)
        errors = io.StringIO()
        with (patch("lmstudio_weight_watcher.parse_args", return_value=args),
              patch("lmstudio_weight_watcher.probe_dependencies",
                    return_value=DependencyPreflightResult("PIL._imaging", "broken")),
              patch("lmstudio_weight_watcher.show_preflight_failure") as dialog,
              contextlib.redirect_stderr(errors)):
            code = main()
        self.assertEqual(code, 3)
        self.assertIn("PIL._imaging", errors.getvalue())
        dialog.assert_not_called()

    def test_preflight_runs_before_window_creation(self) -> None:
        order = []
        args = argparse.Namespace(
            once=False, preflight_only=False, state_file=Path("test-state.json"),
            models_root=None, check_interval_hours=6, reminder_interval_minutes=60,
            snooze_hours=4, timeout_seconds=30,
        )
        app = Mock()
        def probe():
            order.append("probe")
            return DependencyPreflightResult(modules={})
        def create_app(**_kwargs):
            order.append("window")
            return app
        with (patch("lmstudio_weight_watcher.parse_args", return_value=args),
              patch("lmstudio_weight_watcher.probe_dependencies", side_effect=probe),
              patch("lmstudio_weight_watcher.WatcherApp", side_effect=create_app)):
            self.assertEqual(main(), 0)
        self.assertEqual(order, ["probe", "window"])

    @unittest.skipUnless(os.name == "nt", "Windows batch launcher")
    def test_hidden_launcher_forwards_eleven_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy2(Path(__file__).parent.parent / "run_watcher.bat", root / "run_watcher.bat")
            (root / "lmstudio_weight_watcher.py").write_text("", encoding="utf-8")
            stub = root / "python-stub.cmd"
            stub.write_text(
                '@echo off\nif "%~2"=="--preflight-only" exit /b 0\n'
                '>"%WATCHER_STUB_OUT%" echo %*\n', encoding="ascii"
            )
            output = root / "argv.txt"
            env = os.environ.copy()
            env["LMSTUDIO_WATCHER_PYTHON"] = str(stub)
            env["WATCHER_STUB_OUT"] = str(output)
            args = [f"arg-{index}" for index in range(11)]
            completed = subprocess.run(
                ["cmd", "/c", str(root / "run_watcher.bat"), "--hidden-launch", *args],
                env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            forwarded = output.read_text(encoding="ascii")
            positions = [forwarded.index(arg) for arg in args]
            self.assertEqual(positions, sorted(positions))


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
        self.assertIn("models root unavailable", app.state["recovery_notice"]["message"])
        save_state.assert_called_once()
        app.run_check_async.assert_called_once_with(reschedule=True)

    def test_recovery_reported_actions_are_visible(self) -> None:
        app = self.make_app()
        app.run_check_async = Mock()
        actions = [
            RecoveryAction("job", "reported", f"path-{index}", "Needs inspection")
            for index in range(2)
        ]
        with patch("lmstudio_weight_watcher.save_state") as save_state:
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), actions, None))
        message = app.state["recovery_notice"]["message"]
        self.assertIn("2 need attention", message)
        app.status_var.set.assert_called_with(message)
        app.maybe_raise_pending_window.assert_called_once_with(force=True)
        save_state.assert_called_once()
        app.make_icon_image = Mock()
        app.refresh_tray_icon()
        self.assertEqual(app.make_icon_image.call_args.args[0], 1)

    def test_recovery_notice_survives_state_reload_and_reaches_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app()
            app.state_path = Path(tmp) / "state.json"
            app.run_check_async = Mock()
            action = RecoveryAction("job", "reported", "path", "Needs inspection")
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [action], None))
            app.state = load_state(app.state_path)
            self.assertTrue(app.state["recovery_notice"]["attention"])
            app.unresolved_text = Mock()
            app._refresh_unresolved()
            inserted = [call.args[1] for call in app.unresolved_text.insert.call_args_list]
            self.assertIn("Recovery\n", inserted)
            self.assertIn(app.state["recovery_notice"]["message"] + "\n", inserted)

    def test_recovery_message_survives_following_check(self) -> None:
        app = self.make_app()
        app.run_check_async = Mock()
        action = RecoveryAction("job", "reported", "path", "Needs inspection")
        with patch("lmstudio_weight_watcher.save_state"):
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [action], None))
            message = app.state["recovery_notice"]["message"]
            app.run_check_async = WatcherApp.run_check_async.__get__(app)
            app.make_icon_image = Mock()
            with patch("lmstudio_weight_watcher.threading.Thread"):
                app.run_check_async(reschedule=False)
            app.status_var.set.assert_called_with(message)
            app.finish_check(CheckOutcome(Path("models"), [], None, datetime.now(timezone.utc)),
                             reschedule=False)
        self.assertEqual(app.state["recovery_notice"]["message"], message)
        app.status_var.set.assert_called_with(message)

    def test_recovery_notice_survives_state_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self.make_app()
            app.state_path = Path(tmpdir) / "state.json"
            app.run_check_async = Mock()
            action = RecoveryAction("job", "reported", "path", "Needs inspection")
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [action], None))
            reloaded = load_state(app.state_path)
            self.assertEqual(reloaded["recovery_notice"], app.state["recovery_notice"])
            self.assertTrue(reloaded["recovery_notice"]["attention"])

    def test_recovery_error_is_persisted_and_redacted(self) -> None:
        app = self.make_app()
        app.run_check_async = Mock()
        with patch("lmstudio_weight_watcher.save_state"):
            app.finish_recovery(RecoveryWorkerOutcome(None, [], "hf_token=secret123"))
            message = app.state["recovery_notice"]["message"]
            app.finish_check(CheckOutcome(None, [], None, datetime.now(timezone.utc)),
                             reschedule=False)
        self.assertNotIn("secret123", message)
        self.assertIn("Recovery check failed", message)
        app.status_var.set.assert_called_with(message)

    def test_recovery_restored_is_informational(self) -> None:
        app = self.make_app()
        app.run_check_async = Mock()
        action = RecoveryAction("job", "restored", "path", "Restored")
        with patch("lmstudio_weight_watcher.save_state"):
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [action], None))
        self.assertIn("1 restored", app.state["recovery_notice"]["message"])
        self.assertFalse(app.state["recovery_notice"]["attention"])
        app.maybe_raise_pending_window.assert_called_once_with(force=False)
        app.make_icon_image = Mock()
        app.refresh_tray_icon()
        self.assertEqual(app.make_icon_image.call_args.args[0], 0)

    def test_stale_manifest_temp_cleanup_is_reported(self) -> None:
        app = self.make_app()
        app.run_check_async = Mock()
        action = RecoveryAction("unknown", "temp-cleaned", "temp-path", "Removed stale manifest temp file.")
        with patch("lmstudio_weight_watcher.save_state"):
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [action], None))
        notice = app.state["recovery_notice"]
        self.assertIn("1 stale manifest temp file removed", notice["message"])
        self.assertFalse(notice["attention"])

    def test_backup_cleanup_is_not_reported_as_temp_cleanup(self) -> None:
        app = self.make_app()
        app.run_check_async = Mock()
        action = RecoveryAction("job", "cleaned", "backup-path", "Removed validated rollback file.")
        with patch("lmstudio_weight_watcher.save_state"):
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [action], None))
        self.assertNotIn("temp file", app.state["recovery_notice"]["message"])

    def test_recovery_quit_after_update_persists_notice(self) -> None:
        app = self.make_app()
        app.quit_after_update = True
        app.quit = Mock()
        app.run_check_async = Mock()
        action = RecoveryAction("job", "reported", "path", "Needs inspection")
        with patch("lmstudio_weight_watcher.save_state") as save_state:
            app.quit.side_effect = lambda: self.assertTrue(save_state.called)
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [action], None))
        app.quit.assert_called_once()
        app.run_check_async.assert_not_called()
        self.assertTrue(app.state["recovery_notice"]["attention"])

    def test_recovery_no_actions_stays_quiet(self) -> None:
        app = self.make_app()
        app.run_check_async = Mock()
        with patch("lmstudio_weight_watcher.save_state") as save_state:
            app.finish_recovery(RecoveryWorkerOutcome(Path("models"), [], None))
        self.assertNotIn("recovery_notice", app.state)
        app.status_var.set.assert_not_called()
        app.maybe_raise_pending_window.assert_called_once_with(force=False)
        save_state.assert_not_called()

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

    def test_finish_update_applies_returned_results(self) -> None:
        app = self.make_app()
        app.state["alerts"] = {"model": {"status": "pending"}}
        fresh = Mock(model_key="model", status="up-to-date")
        with patch("lmstudio_weight_watcher.save_state"):
            app.finish_update(UpdateWorkerOutcome(True, False, "Done", [fresh]))
        self.assertEqual(app.state["alerts"], {})
        self.assertEqual(app.state["last_summary"]["checked"], 1)
        self.assertEqual(app.state["last_summary"]["up_to_date"], 1)

    def test_update_worker_keeps_failure_refresh_results(self) -> None:
        app = self.make_app()
        fresh = Mock(model_key="model", status="up-to-date")

        def failed_run(_plan, *, failure_results_out, **_kwargs):
            failure_results_out.append(fresh)
            raise InstallError("install failed")

        with (patch("lmstudio_weight_watcher.load_hash_cache", return_value={}),
              patch("lmstudio_weight_watcher.run_update_plan", side_effect=failed_run),
              patch("lmstudio_weight_watcher.save_state")):
            app.update_worker(self.make_plan(), CancellationToken())
            app.root.after.assert_called_once()
            app.root.after.call_args.args[1]()
        self.assertEqual(app.state["last_summary"]["up_to_date"], 1)
        self.assertFalse(app.state["last_update"]["success"])

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

    def test_update_selected_filters_removed_remote(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ("updatable", "removed")
        app._alerts_by_key = {
            "updatable": {"model_key": "updatable", "check_status": "update-available"},
            "removed": {"model_key": "removed", "check_status": "removed-remote"},
        }
        app.prepare_update_async = Mock()
        app.update_selected()
        app.prepare_update_async.assert_called_once_with(["updatable"])

    def test_update_selected_no_updatable_shows_status(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ("removed",)
        app._alerts_by_key = {
            "removed": {"model_key": "removed", "check_status": "removed-remote"},
        }
        app.prepare_update_async = Mock()
        app.update_selected()
        app.prepare_update_async.assert_not_called()
        app.status_var.set.assert_called_once()

    def test_update_all_pending_filters_removed_remote(self) -> None:
        app = self.make_app()
        app.state["alerts"] = {
            "updatable": {
                "model_key": "updatable",
                "status": "pending",
                "check_status": "update-available",
            },
            "removed": {
                "model_key": "removed",
                "status": "pending",
                "check_status": "removed-remote",
            },
        }
        app.prepare_update_async = Mock()
        app.update_all_pending()
        app.prepare_update_async.assert_called_once_with(["updatable"])

    def test_update_all_pending_no_updatable_shows_status(self) -> None:
        app = self.make_app()
        app.state["alerts"] = {
            "removed": {
                "model_key": "removed",
                "status": "pending",
                "check_status": "removed-remote",
            },
        }
        app.prepare_update_async = Mock()
        app.update_all_pending()
        app.prepare_update_async.assert_not_called()
        app.status_var.set.assert_called_once()

    def test_refresh_action_states_disables_update_for_removed_only(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ("removed",)
        app._alerts_by_key = {
            "removed": {"model_key": "removed", "check_status": "removed-remote"},
        }
        app.state["alerts"] = {
            "removed": {
                "model_key": "removed",
                "status": "pending",
                "check_status": "removed-remote",
            },
        }
        app.check_button = Mock()
        app.update_selected_button = Mock()
        app.update_all_button = Mock()
        app.cancel_update_button = Mock()
        app._refresh_action_states()
        app.update_selected_button.configure.assert_called_once_with(state=tk.DISABLED)
        app.update_all_button.configure.assert_called_once_with(state=tk.DISABLED)
        app.check_button.configure.assert_called_once_with(state=tk.NORMAL)

    def test_refresh_action_states_enables_update_for_updatable(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ("updatable",)
        app._alerts_by_key = {
            "updatable": {"model_key": "updatable", "check_status": "update-available"},
        }
        app.state["alerts"] = {
            "updatable": {
                "model_key": "updatable",
                "status": "pending",
                "check_status": "update-available",
            },
        }
        app.check_button = Mock()
        app.update_selected_button = Mock()
        app.update_all_button = Mock()
        app.cancel_update_button = Mock()
        app._refresh_action_states()
        app.update_selected_button.configure.assert_called_once_with(state=tk.NORMAL)
        app.update_all_button.configure.assert_called_once_with(state=tk.NORMAL)
        app.check_button.configure.assert_called_once_with(state=tk.NORMAL)

    def test_format_status_removed_remote_pending(self) -> None:
        app = self.make_app()
        alert = {"model_key": "removed", "check_status": "removed-remote"}
        self.assertEqual(
            app._format_status(alert, "pending", datetime.now(timezone.utc)),
            "Removed upstream",
        )

    def test_format_status_removed_remote_keeps_snooze_and_ack(self) -> None:
        # A removed-upstream model that was snoozed or acknowledged must say so;
        # otherwise it silently re-appears as a fresh warning forever.
        app = self.make_app()
        alert = {
            "model_key": "removed",
            "check_status": "removed-remote",
            "snoozed_until_utc": "2026-04-19T12:00:00Z",
        }
        self.assertTrue(
            app._format_status(alert, "snoozed", datetime.now(timezone.utc)).startswith(
                "Removed upstream · snoozed"
            )
        )
        self.assertEqual(
            app._format_status(alert, "acknowledged", datetime.now(timezone.utc)),
            "Removed upstream · acknowledged",
        )

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
