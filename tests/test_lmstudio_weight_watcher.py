from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

from lmstudio_alert_state import (
    acknowledge_alerts,
    load_state,
    restore_alerts,
    set_alert_statuses,
    snooze_alerts,
)
from lmstudio_weight_watcher import (
    CheckOutcome,
    DependencyPreflightResult,
    RecoveryWorkerOutcome,
    UpdateWorkerOutcome,
    WatcherApp,
    alert_kind,
    alert_sort_key,
    filter_matches,
    fit_text,
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
        app.toaster = Mock()
        return app

    def detail_content(self, app: WatcherApp) -> str:
        return "".join(call.args[1] for call in app.detail_text.insert.call_args_list)

    def test_render_detail_without_artifacts(self) -> None:
        app = self.make_app()
        app.detail_text = Mock()
        app._render_detail({
            "model_key": "model", "display_name": "Test Model",
            "remote_repo": "org/repo", "remote_file": "model.gguf",
            "local_path": "C:/models/model.gguf", "artifacts": [],
            "last_commit_title": "Update model",
        })
        detail = self.detail_content(app)
        self.assertIn("Test Model\n", detail)
        self.assertIn("Repository\norg/repo", detail)
        self.assertIn("Last upstream commit\n\u201cUpdate model\u201d", detail)
        self.assertIn("Local file\nC:/models/model.gguf", detail)
        self.assertNotIn("FILES CHECKED", detail)
        app.detail_text.configure.assert_any_call(state=tk.DISABLED)

    def test_render_detail_removed_remote_with_suggestions(self) -> None:
        app = self.make_app()
        app.detail_text = Mock()
        app._render_detail({
            "model_key": "removed", "display_name": "Removed Model",
            "remote_repo": "org/repo", "remote_file": "missing.gguf",
            "local_path": "C:/models/missing.gguf",
            "check_status": "removed-remote",
            "artifacts": [{"status": "removed-remote", "label": "missing.gguf"}],
            "suggestions": ["model-Q4.gguf"],
        })
        detail = self.detail_content(app)
        self.assertIn("missing.gguf \u2014 deleted upstream, local copy kept", detail)
        self.assertIn("This exact file was deleted upstream", detail)
        self.assertIn("updating is not offered", detail)
        self.assertIn("Upstream still offers these quants", detail)
        self.assertIn("model-Q4.gguf", detail)

    def test_render_detail_missing_local_shows_expected_destination(self) -> None:
        app = self.make_app()
        app.detail_text = Mock()
        expected = "C:/models/expected.gguf"
        app._render_detail({
            "model_key": "missing", "display_name": "Missing Local Model",
            "remote_repo": "org/repo", "remote_file": "expected.gguf",
            "local_path": expected, "check_status": "update-available",
            "artifacts": [{"status": "missing-local", "label": "expected.gguf"}],
        })
        detail = self.detail_content(app)
        self.assertIn(f"Local file\n{expected}", detail)
        self.assertIn("expected.gguf \u2014 missing on this PC", detail)
        self.assertIn("A file this model needs is missing on this PC", detail)
        self.assertNotIn("deleted upstream", detail)

    def test_on_tree_select_without_selection(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ()
        app.selection_var = Mock()
        app.detail_text = Mock()
        app._refresh_action_states = Mock()
        app._on_tree_select()
        app.selection_var.set.assert_called_once_with("No selection")
        self.assertIn("Select a model to see the source", self.detail_content(app))
        app._refresh_action_states.assert_called_once()

    def test_on_tree_select_resolves_selected_alert(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ("model",)
        app._alerts_by_key = {
            "model": {
                "model_key": "model", "display_name": "Selected Model",
                "remote_repo": "org/repo", "remote_file": "selected.gguf",
                "local_path": "C:/models/selected.gguf",
            }
        }
        app.selection_var = Mock()
        app.detail_text = Mock()
        app._refresh_action_states = Mock()
        app._on_tree_select()
        app.selection_var.set.assert_called_once_with("1 model selected")
        self.assertIn("Selected Model\n", self.detail_content(app))
        self.assertIn("Local file\nC:/models/selected.gguf", self.detail_content(app))
        app._refresh_action_states.assert_called_once()

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
            app.last_models_root = Path("models")
            specs = app._banner_specs()
            recovery = [spec for spec in specs if spec["key"] == "recovery"]
            self.assertEqual(len(recovery), 1)
            self.assertEqual(recovery[0]["kind"], "danger")
            self.assertEqual(recovery[0]["message"], app.state["recovery_notice"]["message"])
            app.refresh_ui = Mock()
            recovery[0]["on_close"]()
            self.assertNotIn("recovery_notice", load_state(app.state_path))

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

    def stub_create_window(self, app: WatcherApp) -> Mock:
        """Install a fake create_window that assigns a Mock Toplevel."""
        window = Mock()
        window.winfo_exists.return_value = True
        app.create_window = Mock(side_effect=lambda: setattr(app, "window", window))
        return window

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
        self.stub_create_window(app)
        with (
            patch("lmstudio_weight_watcher.ask_update_confirmation", return_value=False),
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
        self.stub_create_window(app)
        worker = Mock()
        with (
            patch("lmstudio_weight_watcher.ask_update_confirmation", return_value=True),
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
        self.stub_create_window(app)
        with patch(
            "lmstudio_weight_watcher.ask_update_confirmation",
            side_effect=tk.TclError("window destroyed"),
        ):
            app.finish_prepare_update(self.make_plan(), None)
        self.assertFalse(app.update_in_progress)
        self.assertIsNone(app.active_update_plan)
        app.status_var.set.assert_called_once()

    def test_tray_path_confirmation_creates_real_parent_window(self) -> None:
        # Update All from the tray menu runs with no window open. The
        # confirmation dialog must still get a real parent: the app must
        # create its window first rather than ask parentless over the
        # withdrawn root.
        app = self.make_app()
        app.update_in_progress = True
        app.update_cancellation = CancellationToken()
        app.update_model_status = {"model": "Queued"}
        window = self.stub_create_window(app)
        with (
            patch(
                "lmstudio_weight_watcher.ask_update_confirmation", return_value=False
            ) as ask,
            patch("lmstudio_weight_watcher.threading.Thread") as thread,
        ):
            app.finish_prepare_update(self.make_plan(), None)
        app.create_window.assert_called_once()
        parent = ask.call_args.args[0]
        self.assertIsNotNone(parent)
        self.assertIs(parent, window)
        # No forbidden focus-steal API on the freshly created window.
        window.focus_force.assert_not_called()
        window.attributes.assert_not_called()
        window.lift.assert_not_called()
        thread.assert_not_called()

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
        app.update_button = Mock()
        app.cancel_update_button = Mock()
        app._refresh_action_states()
        app.update_button.configure.assert_any_call(state=tk.DISABLED)
        app.check_button.configure.assert_any_call(state=tk.NORMAL)
        # With nothing selected the same button covers every pending model,
        # and removed-upstream models are never counted as updatable.
        app.tree.selection.return_value = ()
        app.update_button.reset_mock()
        app._refresh_action_states()
        app.update_button.configure.assert_any_call(state=tk.DISABLED)
        app.update_button.configure.assert_any_call(text="Update all")

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
        app.update_button = Mock()
        app.cancel_update_button = Mock()
        app._refresh_action_states()
        app.update_button.configure.assert_any_call(state=tk.NORMAL)
        app.update_button.configure.assert_any_call(text="Update")
        app.check_button.configure.assert_any_call(state=tk.NORMAL)
        app.tree.selection.return_value = ()
        app.update_button.reset_mock()
        app._refresh_action_states()
        app.update_button.configure.assert_any_call(state=tk.NORMAL)
        app.update_button.configure.assert_any_call(text="Update all (1)")

    def test_refresh_action_states_disables_update_while_busy(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ()
        app.state["alerts"] = {
            "updatable": {
                "model_key": "updatable",
                "status": "pending",
                "check_status": "update-available",
            },
        }
        app.check_in_progress = True
        app.check_button = Mock()
        app.update_button = Mock()
        app._refresh_action_states()
        app.update_button.configure.assert_any_call(state=tk.DISABLED)
        app.check_button.configure.assert_any_call(state=tk.DISABLED)
        app.check_button.configure.assert_any_call(text="Checking\u2026")

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
            "Removed upstream \u00b7 dismissed",
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

    def test_show_window_never_focus_forces_or_topmosts(self) -> None:
        app = self.make_app()
        app.window = Mock()
        app.window.winfo_exists.return_value = True
        app.refresh_tree = Mock()

        app.show_window()

        app.window.deiconify.assert_called_once()
        app.window.lift.assert_called_once()
        app.window.attributes.assert_not_called()
        app.window.focus_force.assert_not_called()

    def test_maybe_raise_pending_window_toasts_instead_of_raising(self) -> None:
        app = self.make_app()
        app.maybe_raise_pending_window = WatcherApp.maybe_raise_pending_window.__get__(
            app
        )
        app.reminder_interval = timedelta(minutes=60)
        app.state = {
            "alerts": {
                "m1": {
                    "model_key": "m1",
                    "display_name": "Model One",
                    "status": "pending",
                    "check_status": "update-available",
                }
            },
            "active_update": None,
        }
        with (
            patch("lmstudio_weight_watcher.reminder_due", return_value=True),
            patch("lmstudio_weight_watcher.record_reminder") as record_reminder,
            patch("lmstudio_weight_watcher.save_state") as save_state,
            patch.object(WatcherApp, "show_window") as show_window,
        ):
            app.maybe_raise_pending_window(force=False)

        self.assertEqual(app.toaster.notify.call_count, 1)
        alert_key = app.toaster.notify.call_args.args[0]
        self.assertEqual(alert_key, (("m1", "update-available"),))
        body = app.toaster.notify.call_args.args[1]
        self.assertIn("Model One", body)
        self.assertIn("update", body)
        show_window.assert_not_called()
        app.root.bell.assert_not_called()
        record_reminder.assert_called_once()
        save_state.assert_called_once()

    def test_maybe_raise_pending_window_dedupes_unchanged_state(self) -> None:
        app = self.make_app()
        app.maybe_raise_pending_window = WatcherApp.maybe_raise_pending_window.__get__(
            app
        )
        app.reminder_interval = timedelta(minutes=60)
        app.state = {
            "alerts": {
                "m1": {
                    "model_key": "m1",
                    "display_name": "Model One",
                    "status": "pending",
                    "check_status": "update-available",
                }
            },
            "active_update": None,
        }
        with (
            patch("lmstudio_weight_watcher.reminder_due", return_value=True),
            patch(
                "lmstudio_weight_watcher.record_reminder",
                side_effect=lambda state, now_utc: state,
            ),
            patch("lmstudio_weight_watcher.save_state"),
        ):
            app.maybe_raise_pending_window(force=False)
            app.maybe_raise_pending_window(force=False)

        self.assertEqual(app.toaster.notify.call_count, 2)
        first_key = app.toaster.notify.call_args_list[0].args[0]
        second_key = app.toaster.notify.call_args_list[1].args[0]
        self.assertEqual(first_key, second_key)
        # Dedupe happens inside WatchToaster; identical keys mean no second toast.

    def test_quit_cancels_pending_timers(self) -> None:
        app = self.make_app()
        app.next_check_token = "check-token"

        app.quit()

        self.assertTrue(app.shutting_down)
        self.assertIsNone(app.next_check_token)
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


class WatcherPresentationTests(unittest.TestCase):
    def make_app(self) -> WatcherApp:
        app = object.__new__(WatcherApp)
        app.state = {"alerts": {}, "active_update": None}
        app.state_path = Path("test-state.json")
        app.update_model_status = {}
        app.update_in_progress = False
        app.check_in_progress = False
        app.recovery_in_progress = False
        app.update_cancellable = False
        app.update_commit_started = threading.Event()
        app.last_models_root = None
        app._alerts_by_key = {}
        app.status_var = Mock()
        app.refresh_ui = Mock()
        return app

    def test_sort_puts_updates_first_then_removed_snoozed_dismissed(self) -> None:
        alerts = [
            {"model_key": "d", "display_name": "D", "status": "acknowledged"},
            {"model_key": "s", "display_name": "S", "status": "snoozed"},
            {"model_key": "r", "display_name": "R", "status": "pending",
             "check_status": "removed-remote"},
            {"model_key": "u", "display_name": "U", "status": "pending",
             "check_status": "update-available"},
        ]
        ordered = [a["model_key"] for a in sorted(alerts, key=alert_sort_key)]
        self.assertEqual(ordered, ["u", "r", "s", "d"])

    def test_filters(self) -> None:
        pending = {"status": "pending"}
        snoozed = {"status": "snoozed"}
        dismissed = {"status": "acknowledged"}
        self.assertTrue(filter_matches("all", dismissed))
        self.assertTrue(filter_matches("attention", pending))
        self.assertFalse(filter_matches("attention", snoozed))
        self.assertTrue(filter_matches("snoozed", snoozed))
        self.assertTrue(filter_matches("dismissed", dismissed))
        self.assertFalse(filter_matches("unchecked", pending))

    def test_alert_kind_prefers_live_update_status(self) -> None:
        alert = {"status": "pending", "check_status": "update-available"}
        self.assertEqual(alert_kind(alert), "update")
        self.assertEqual(alert_kind(alert, "Downloading 40%"), "busy")
        self.assertEqual(alert_kind(alert, "Waiting for model unload"), "failed")
        self.assertEqual(alert_kind(alert, "Updated"), "ok")
        self.assertEqual(alert_kind({"status": "pending", "check_status": "removed-remote"}), "removed")
        self.assertEqual(alert_kind({"status": "snoozed"}), "snoozed")

    def test_fit_text_adds_ellipsis_only_when_needed(self) -> None:
        font = Mock()
        font.measure.side_effect = lambda text: len(text) * 10
        self.assertEqual(fit_text("short", font, 100), "short")
        fitted = fit_text("a very long model name", font, 100)
        self.assertTrue(fitted.endswith("\u2026"))
        self.assertLessEqual(len(fitted) * 10, 100)

    def test_tray_tooltip_fits_windows_limit(self) -> None:
        app = self.make_app()
        app.state["alerts"] = {
            f"m{i}": {"model_key": f"m{i}", "status": "pending",
                      "check_status": "update-available"}
            for i in range(3)
        }
        app.state["last_checked_utc"] = "2026-01-01T00:00:00Z"
        text = app.tray_tooltip_text()
        self.assertIn("3 updates available", text)
        self.assertLessEqual(len(text), 127)

    def test_make_icon_image_kinds(self) -> None:
        app = self.make_app()
        with patch("lmstudio_weight_watcher.tray_icon_image") as image:
            app.make_icon_image(0, busy=False)
            app.make_icon_image(2, busy=False)
            app.make_icon_image(1, busy=False, removed_only=True)
            app.make_icon_image(2, busy=True)
        kinds = [call.args[0] for call in image.call_args_list]
        self.assertEqual(kinds, ["ok", "pending", "removed", "busy"])

    def test_snooze_all_leaves_dismissed_alerts_alone(self) -> None:
        app = self.make_app()
        app.state["alerts"] = {
            "p": {"model_key": "p", "status": "pending"},
            "d": {"model_key": "d", "status": "acknowledged"},
        }
        with patch("lmstudio_weight_watcher.save_state"):
            app.snooze_all()
        self.assertEqual(app.state["alerts"]["p"]["status"], "snoozed")
        self.assertEqual(app.state["alerts"]["d"]["status"], "acknowledged")

    def test_undo_restores_previous_statuses(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ("p", "s")
        app.state["alerts"] = {
            "p": {"model_key": "p", "status": "pending", "snoozed_until_utc": None},
            "s": {"model_key": "s", "status": "snoozed",
                  "snoozed_until_utc": "2030-01-01T00:00:00Z"},
        }
        app._alerts_by_key = dict(app.state["alerts"])
        with patch("lmstudio_weight_watcher.save_state"):
            app.acknowledge_selected()
            self.assertEqual(app.state["alerts"]["p"]["status"], "acknowledged")
            self.assertEqual(app.state["alerts"]["s"]["status"], "acknowledged")
            app.undo_last_triage()
        self.assertEqual(app.state["alerts"]["p"]["status"], "pending")
        self.assertEqual(app.state["alerts"]["s"]["status"], "snoozed")
        self.assertEqual(app.state["alerts"]["s"]["snoozed_until_utc"], "2030-01-01T00:00:00Z")

    def test_failed_update_banner_offers_retry_for_still_updatable(self) -> None:
        app = self.make_app()
        app.state["alerts"] = {
            "m": {"model_key": "m", "status": "pending", "check_status": "update-available"},
        }
        app.state["last_update"] = {
            "job_id": "job-1", "model_keys": ["m"], "model_names": ["Model"],
            "success": False, "cancelled": False,
            "message": "Unload the model in LM Studio and try again.",
            "completed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        specs = app._banner_specs()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["kind"], "warn")
        self.assertIn("Unload Model in LM Studio", specs[0]["title"])
        app.prepare_update_async = Mock()
        specs[0]["actions"][0].command()
        app.prepare_update_async.assert_called_once_with(["m"])
        specs[0]["on_close"]()
        self.assertEqual(app._banner_specs(), [])

    def test_check_error_banner_is_redacted(self) -> None:
        app = self.make_app()
        app.state["last_error"] = "request failed with hf_token=secret123"
        specs = app._banner_specs()
        self.assertEqual(specs[0]["key"], "check-error")
        self.assertNotIn("secret123", specs[0]["title"])

    def test_progress_text_reports_rate_and_eta(self) -> None:
        app = self.make_app()
        first = ProgressEvent("downloading", "Downloading weights", artifact="weights",
                              bytes_completed=0, bytes_total=100 * 1024 * 1024)
        second = ProgressEvent("downloading", "Downloading weights", artifact="weights",
                               bytes_completed=50 * 1024 * 1024, bytes_total=100 * 1024 * 1024)
        with patch("lmstudio_weight_watcher.time.monotonic", side_effect=[100.0, 101.0]):
            app._progress_text(first)
            text = app._progress_text(second)
        self.assertIn("50.0 MiB of 100.0 MiB", text)
        self.assertIn("50.0 MiB/s", text)
        self.assertIn("about 1 s left", text)

    def test_unresolved_only_selection_never_acts_on_everything(self) -> None:
        app = self.make_app()
        app.tree = Mock()
        app.tree.selection.return_value = ("unresolved::x",)
        app.state["alerts"] = {
            "p": {"model_key": "p", "status": "pending", "check_status": "update-available"},
        }
        app._alerts_by_key = dict(app.state["alerts"])
        app.dismiss_button = Mock()
        app.snooze_button = Mock()
        app.update_button = Mock()
        app._refresh_action_states()
        app.dismiss_button.configure.assert_any_call(state=tk.DISABLED)
        app.snooze_button.configure.assert_any_call(state=tk.DISABLED)
        app.update_button.configure.assert_any_call(state=tk.DISABLED)
        app.prepare_update_async = Mock()
        with patch("lmstudio_weight_watcher.save_state"):
            app._on_dismiss_clicked()
            app._on_snooze_clicked()
            app._on_update_clicked()
        self.assertEqual(app.state["alerts"]["p"]["status"], "pending")
        app.prepare_update_async.assert_not_called()

    def test_destroy_window_forgets_window_widgets(self) -> None:
        app = self.make_app()
        app.window = Mock()
        for name in WatcherApp._WINDOW_WIDGETS:
            setattr(app, name, Mock())
        app._destroy_window()
        for name in WatcherApp._WINDOW_WIDGETS:
            self.assertIsNone(getattr(app, name), name)
        app.update_in_progress = True
        app.update_cancellable = True
        app._refresh_action_states()  # must not touch destroyed widgets


class UpdateConfirmationDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - headless CI
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.parent = tk.Toplevel(self.root)

    def tearDown(self) -> None:
        self.root.destroy()

    def summary(self, models: int = 1, files: int = 1):
        from watcher_ui import ConfirmModel, ConfirmSummary

        return ConfirmSummary(
            models=[
                ConfirmModel(f"Model {i}", 1024, [f"shard {j}" for j in range(files)], "fix")
                for i in range(models)
            ],
            download_bytes=1024, total_bytes=1024, available_bytes=4096, required_bytes=2048,
        )

    def run_dialog(self, action, summary=None) -> tuple[bool, dict]:
        from watcher_ui import ask_update_confirmation

        seen: dict = {}

        def drive() -> None:
            dialogs = [c for c in self.parent.winfo_children() if isinstance(c, tk.Toplevel)]
            if not dialogs:
                self.root.after(50, drive)
                return
            dialog = dialogs[0]
            dialog.update_idletasks()
            seen["height"] = dialog.winfo_reqheight()
            seen["screen"] = dialog.winfo_screenheight()
            action(dialog)

        def give_up() -> None:
            for child in self.parent.winfo_children():
                if isinstance(child, tk.Toplevel):
                    seen["timed_out"] = True
                    child.destroy()

        self.root.after(100, drive)
        self.root.after(8000, give_up)
        result = ask_update_confirmation(self.parent, self.summary() if summary is None else summary,
                                         None, lambda n: f"{n} B")
        return result, seen

    @staticmethod
    def buttons(dialog) -> dict:
        found = {}
        stack = list(dialog.winfo_children())
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if isinstance(widget, tk.ttk.Button):
                found[widget.cget("text")] = widget
        return found

    def test_enter_on_cancel_does_not_confirm(self) -> None:
        def press_enter_on_cancel(dialog) -> None:
            self.buttons(dialog)["Cancel"].focus_set()
            dialog.update()
            dialog._lmww_on_return()

        result, seen = self.run_dialog(press_enter_on_cancel)
        self.assertNotIn("timed_out", seen)
        self.assertFalse(result)

    def test_enter_on_default_button_confirms(self) -> None:
        def press_enter(dialog) -> None:
            dialog.update()
            dialog._lmww_on_return()

        result, seen = self.run_dialog(press_enter)
        self.assertNotIn("timed_out", seen)
        self.assertTrue(result)

    def test_escape_cancels(self) -> None:
        result, seen = self.run_dialog(lambda dialog: dialog._lmww_on_escape())
        self.assertNotIn("timed_out", seen)
        self.assertFalse(result)

    def test_many_models_fit_on_screen(self) -> None:
        def close(dialog) -> None:
            self.buttons(dialog)["Cancel"].invoke()

        result, seen = self.run_dialog(close, self.summary(models=40, files=16))
        self.assertFalse(result)
        self.assertLessEqual(seen["height"], seen["screen"])


class AlertTriageStateTests(unittest.TestCase):
    def test_restore_and_set_statuses(self) -> None:
        now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        state = {"alerts": {"a": {"status": "pending"}, "b": {"status": "pending"}}}
        state = snooze_alerts(state, now_utc=now, hours=4, model_keys=["a"])
        state = acknowledge_alerts(state, ["b"])
        restored = restore_alerts(state, ["a", "b"])
        self.assertEqual(restored["alerts"]["a"]["status"], "pending")
        self.assertIsNone(restored["alerts"]["a"]["snoozed_until_utc"])
        self.assertEqual(restored["alerts"]["b"]["status"], "pending")
        back = set_alert_statuses(restored, {
            "a": ("snoozed", "2030-01-01T04:00:00Z"),
            "b": ("bogus", None),
            "gone": ("pending", None),
        })
        self.assertEqual(back["alerts"]["a"]["status"], "snoozed")
        self.assertEqual(back["alerts"]["a"]["snoozed_until_utc"], "2030-01-01T04:00:00Z")
        self.assertEqual(back["alerts"]["b"]["status"], "pending")
        self.assertNotIn("gone", back["alerts"])


if __name__ == "__main__":
    unittest.main()
