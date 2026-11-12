from __future__ import annotations

import argparse
from copy import deepcopy
import importlib
import os
import shutil
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from watcher_toasts import WatchToaster
from watcher_ui import (
    AutoScrollbar,
    BannerAction,
    ConfirmModel,
    ConfirmSummary,
    Tooltip,
    app_icon_image,
    apply_titlebar_theme,
    ask_update_confirmation,
    build_banner,
    build_theme,
    configure_styles,
    enable_dpi_awareness,
    format_ago,
    format_duration,
    format_when,
    plural,
    reveal_in_file_manager,
    status_dot_image,
    system_prefers_dark,
    to_local,
    tray_icon_image,
)
from lmstudio_alert_state import (
    LEGACY_APP_NAME,
    acknowledge_alerts,
    all_alerts,
    apply_results,
    default_state_path,
    load_state,
    pending_alerts,
    record_reminder,
    record_update_finished,
    record_update_progress,
    record_update_started,
    reminder_due,
    restore_alerts,
    save_state,
    set_alert_statuses,
    snooze_alerts,
)
from lmstudio_weight_checker import (
    CheckerError,
    discover_models_root,
    filter_inventory,
    format_utc,
    load_hash_cache,
    load_lms_json,
    load_variant_lookup,
    save_hash_cache,
    run_check,
)
from lmstudio_weight_updater import (
    CancellationToken,
    ProgressEvent,
    UpdateCancelled,
    UpdatePlan,
    build_update_plan,
    format_bytes,
    recover_interrupted_jobs,
    redact_sensitive,
    run_update_plan,
)

DEFAULT_CHECK_INTERVAL_HOURS = 6
DEFAULT_REMINDER_INTERVAL_MINUTES = 60
DEFAULT_SNOOZE_HOURS = 4
DEFAULT_TIMEOUT_SECONDS = 30
APP_NAME = "LM Studio Weight Watcher"

# List filters, in chip order: (key, label).
FILTERS = (
    ("all", "All"),
    ("attention", "Needs attention"),
    ("snoozed", "Snoozed"),
    ("dismissed", "Dismissed"),
    ("unchecked", "Can't check"),
)
EMPTY_FILTER_TEXT = {
    "attention": (
        "Nothing needs your attention",
        "Snoozed and dismissed models are listed under their own filters.",
    ),
    "snoozed": ("Nothing is snoozed", "Snoozed models come back after {hours}."),
    "dismissed": (
        "Nothing is dismissed",
        "Dismissed models stay hidden until their files change upstream again.",
    ),
    "unchecked": ("Every model could be checked", ""),
}
ARTIFACT_STATUS_TEXT = {
    "update-available": "changed upstream",
    "up-to-date": "matches Hugging Face",
    "removed-remote": "deleted upstream, local copy kept",
    "missing-local": "missing on this PC",
    "unresolved": "could not be checked",
}
ARTIFACT_DOT_KIND = {
    "update-available": "update",
    "up-to-date": "ok",
    "removed-remote": "removed",
    "missing-local": "failed",
}
UNRESOLVED_IID_PREFIX = "unresolved::"
_PROGRESS_PHASE_TEXT = {
    "queued": "Queued",
    "downloading": "Downloading",
    "verifying": "Verifying",
    "installing": "Installing",
    "validating": "Validating",
    "completed": "Updated",
    "cancelled": "Cancelled",
    "failed": "Failed",
}


@dataclass
class CheckOutcome:
    models_root: Path | None
    results: list
    error: str | None
    generated_at_utc: datetime


@dataclass
class UpdateWorkerOutcome:
    success: bool
    cancelled: bool
    message: str
    results: list


@dataclass
class RecoveryWorkerOutcome:
    models_root: Path | None
    actions: list
    error: str | None


@dataclass
class DependencyPreflightResult:
    failed_import: str | None = None
    error: str | None = None
    modules: dict | None = None


def _import_dependency(name: str):
    return importlib.import_module(name)


def probe_dependencies() -> DependencyPreflightResult:
    modules = {}
    names = ["PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "PIL.ImageTk", "pystray"]
    if os.name == "nt":
        names.append("windows_toasts")
    for name in names:
        try:
            modules[name] = _import_dependency(name)
        except Exception as exc:
            return DependencyPreflightResult(
                failed_import=getattr(exc, "name", None) or name,
                error=str(exc),
            )
    return DependencyPreflightResult(modules=modules)


def format_preflight_failure(result: DependencyPreflightResult) -> str:
    requirements = Path(__file__).with_name("requirements.txt")
    command = f'"{sys.executable}" -m pip install -r requirements.txt'
    return (
        f"Watcher dependency check failed: {result.failed_import}: {result.error}\n"
        f"Interpreter: {sys.executable}\n"
        f"In {requirements.parent}, run: {command}"
    )


def show_preflight_failure(message: str) -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Persistent LM Studio Weight Watcher. Run once for scheduled checks or "
            "start a tray app with persistent alerts."
        )
    )
    parser.add_argument("--state-file", type=Path, help="Override the persistent alert state path.")
    parser.add_argument("--models-root", type=Path, help="Override the LM Studio models root folder.")
    parser.add_argument("--once", action="store_true", help="Run one check, update state, print a summary, then exit.")
    parser.add_argument("--preflight-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--hidden-launch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--check-interval-hours",
        type=int,
        default=DEFAULT_CHECK_INTERVAL_HOURS,
        help=f"How often the tray app runs a fresh check. Default: {DEFAULT_CHECK_INTERVAL_HOURS}.",
    )
    parser.add_argument(
        "--reminder-interval-minutes",
        type=int,
        default=DEFAULT_REMINDER_INTERVAL_MINUTES,
        help=(
            "How often pending alerts reopen the alerts window if still unacknowledged. "
            f"Default: {DEFAULT_REMINDER_INTERVAL_MINUTES}."
        ),
    )
    parser.add_argument(
        "--snooze-hours",
        type=int,
        default=DEFAULT_SNOOZE_HOURS,
        help=f"Default snooze duration from the tray and window actions. Default: {DEFAULT_SNOOZE_HOURS}.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout for Hugging Face requests. Default: {DEFAULT_TIMEOUT_SECONDS}.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    preflight = probe_dependencies()
    if preflight.failed_import:
        message = format_preflight_failure(preflight)
        print(message, file=sys.stderr)
        if not args.once and not args.preflight_only:
            show_preflight_failure(message)
        return 3
    if args.preflight_only:
        return 0
    if preflight.modules:
        global Image, ImageDraw, ImageFont, ImageTk, pystray
        Image = preflight.modules["PIL.Image"]
        ImageDraw = preflight.modules["PIL.ImageDraw"]
        ImageFont = preflight.modules["PIL.ImageFont"]
        ImageTk = preflight.modules.get("PIL.ImageTk")
        pystray = preflight.modules["pystray"]
    state_path = (args.state_file or default_state_path()).expanduser()

    # Migration: if legacy exists and preferred doesn't, move it.
    legacy_path = (Path(os.environ.get("APPDATA", "")) / LEGACY_APP_NAME / "state.json")
    if not args.state_file and legacy_path.is_file() and not state_path.is_file():
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_path), str(state_path))
            print(f"Migrated state from legacy location: {legacy_path} -> {state_path}")
        except Exception as exc:
            print(f"Could not migrate legacy state: {exc}")

    if args.once:
        return run_once(
            state_path=state_path,
            models_root_override=args.models_root,
            timeout_seconds=args.timeout_seconds,
            hash_cache_path=state_path.parent / "local_hash_cache.json",
        )

    enable_dpi_awareness()
    app = WatcherApp(
        state_path=state_path,
        models_root_override=args.models_root,
        check_interval=timedelta(hours=args.check_interval_hours),
        reminder_interval=timedelta(minutes=args.reminder_interval_minutes),
        snooze_hours=args.snooze_hours,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        app.start()
    except Exception as exc:
        import traceback
        error_msg = f"Application crashed at {datetime.now()}:\n{traceback.format_exc()}"
        print(error_msg, file=sys.stderr)
        return 1
    return 0


def run_once(
    *,
    state_path: Path,
    models_root_override: Path | None,
    timeout_seconds: int,
    hash_cache_path: Path,
) -> int:
    state = load_state(state_path)
    outcome = perform_check(
        models_root_override,
        timeout_seconds=timeout_seconds,
        hash_cache_path=hash_cache_path,
    )
    next_state = apply_results(
        state,
        outcome.results,
        now_utc=outcome.generated_at_utc,
        last_error=outcome.error,
    )
    save_state(state_path, next_state)

    pending = len(pending_alerts(next_state, outcome.generated_at_utc))
    unresolved = next_state["last_summary"]["unresolved"]
    checked = next_state["last_summary"]["checked"]
    print(
        f"Checked {checked} models. "
        f"Pending alerts: {pending}. "
        f"Unresolved entries: {unresolved}."
    )
    if outcome.error:
        print(f"Last error: {outcome.error}")
        return 1
    return 0


def perform_check(
    models_root_override: Path | None,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    hash_cache_path: Path | None = None,
) -> CheckOutcome:
    generated_at_utc = datetime.now(timezone.utc)
    models_root = None
    error = None
    hash_cache = load_hash_cache(hash_cache_path)

    try:
        models_root = discover_models_root(models_root_override)
        inventory = filter_inventory(
            load_lms_json(["ls", "--json"]),
            include_embeddings=False,
        )
        variant_lookup = load_variant_lookup(inventory)
        results = run_check(
            models_root=models_root,
            inventory=inventory,
            variant_lookup=variant_lookup,
            timeout_seconds=timeout_seconds,
            hash_cache=hash_cache,
        )
    except CheckerError as exc:
        error = str(exc)
        results = []
    finally:
        save_hash_cache(hash_cache_path, hash_cache)

    return CheckOutcome(
        models_root=models_root,
        results=results,
        error=error,
        generated_at_utc=generated_at_utc,
    )


class WatcherApp:
    # Defaults for window-only state, so logic paths work before (or without)
    # a window. Instances overwrite these; none of them is mutated in place.
    snooze_hours = DEFAULT_SNOOZE_HOURS
    theme = None
    detail_text = None
    tree = None
    window = None
    next_check_due = None
    _filter = "all"
    _progress_detail = ""
    _download_rate = None
    _rate_sample = None
    _plan_error = None
    _awaiting_confirmation = False
    _dismissed_update_job = None
    _undo_snapshot = None
    _session_update_jobs: frozenset = frozenset()
    _unresolved_by_iid: dict = {}
    _progress_banner = None
    _banner_signature = None
    _detail_subject: tuple = ()
    _detail_scroll = 0.0
    _last_detail_refresh = 0.0

    def __init__(
        self,
        *,
        state_path: Path,
        models_root_override: Path | None,
        check_interval: timedelta,
        reminder_interval: timedelta,
        snooze_hours: int,
        timeout_seconds: int,
    ) -> None:
        self.state_path = state_path
        self.hash_cache_path = self.state_path.parent / "local_hash_cache.json"
        self.models_root_override = models_root_override
        self.check_interval = check_interval
        self.reminder_interval = reminder_interval
        self.snooze_hours = snooze_hours
        self.timeout_seconds = timeout_seconds
        self.state = load_state(state_path)
        self.last_models_root = None
        self.check_in_progress = False
        self.recovery_in_progress = False
        self.update_in_progress = False
        self.update_cancellable = False
        self.update_cancellation: CancellationToken | None = None
        self.update_thread: threading.Thread | None = None
        self.update_commit_started = threading.Event()
        self.update_model_status: dict[str, str] = {}
        self.active_update_plan: UpdatePlan | None = None
        self.last_update_phase: str | None = None
        self.last_update_state_save_monotonic = 0.0
        self.last_progress_post_monotonic = 0.0
        self.last_progress_post_phase: str | None = None
        self.quit_after_update = False
        self.shutting_down = False
        self.next_check_token = None
        self.window = None
        self.tree = None
        self.toaster = WatchToaster()

        self.root = tk.Tk()
        self.root.withdraw()

        # Window state. Widgets exist only while the window does.
        self.theme = None
        self.detail_text = None
        self.next_check_due: datetime | None = None
        self._filter = "all"
        self._progress_detail = ""
        self._download_rate: float | None = None
        self._rate_sample: tuple[str | None, int, float] | None = None
        self._session_update_jobs = frozenset()
        self._plan_error: str | None = None
        self._awaiting_confirmation = False
        self._dismissed_update_job = None
        self._undo_snapshot = None
        self._alerts_by_key: dict[str, dict] = {}
        self._unresolved_by_iid: dict[str, dict] = {}

        self.headline_var = tk.StringVar(master=self.root, value="Starting…")
        self.subline_var = tk.StringVar(master=self.root, value="")
        self.status_var = tk.StringVar(master=self.root, value="")
        self.selection_var = tk.StringVar(master=self.root, value="No selection")
        self.update_progress_var = tk.DoubleVar(master=self.root, value=0.0)
        self.progress_title_var = tk.StringVar(master=self.root, value="")
        self.progress_detail_var = tk.StringVar(master=self.root, value="")

        self.icon = pystray.Icon(
            "lmstudio_weight_watcher",
            self.make_icon_image(0, busy=False),
            APP_NAME,
            menu=self.build_menu(),
        )

    def start(self) -> None:
        self.icon.run_detached()
        self.root.after(200, self.after_start)
        self.root.mainloop()

    def after_start(self) -> None:
        self.recovery_in_progress = True
        self.status_var.set("Checking for an interrupted update...")
        self.refresh_ui()
        threading.Thread(target=self.recovery_worker, daemon=True).start()

    # ----- Tray menu -----

    def build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda item: self.menu_status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Weight Watcher", self.on_open_alerts, default=True),
            pystray.MenuItem(
                lambda item: "Checking\u2026" if self.check_in_progress else "Check now",
                self.on_check_now,
                enabled=lambda item: not self._busy(),
            ),
            pystray.MenuItem(
                lambda item: self._tray_update_label(),
                self.on_update_all,
                enabled=lambda item: not self._busy() and bool(self._updatable_pending()),
                visible=lambda item: not self.update_in_progress,
            ),
            pystray.MenuItem(
                "Cancel update",
                self.on_cancel_update,
                enabled=lambda item: self._can_cancel_update(),
                visible=lambda item: self.update_in_progress,
            ),
            pystray.MenuItem(
                lambda item: f"Snooze all for {plural(self.snooze_hours, 'hour')}",
                self.on_snooze_all,
                enabled=lambda item: bool(self._pending_now()),
            ),
            pystray.MenuItem(
                "Dismiss all",
                self.on_acknowledge_all,
                enabled=lambda item: bool(self._pending_now()),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.on_quit),
        )

    def _busy(self) -> bool:
        return bool(self.recovery_in_progress or self.check_in_progress or self.update_in_progress)

    def _pending_now(self) -> list[dict]:
        return pending_alerts(self.state, datetime.now(timezone.utc))

    def _updatable_pending(self) -> list[dict]:
        return [
            alert for alert in self._pending_now()
            if alert.get("check_status") == "update-available"
        ]

    def _tray_update_label(self) -> str:
        count = len(self._updatable_pending())
        return f"Update all ({count})\u2026" if count else "Update all"

    def _can_cancel_update(self) -> bool:
        return bool(
            self.update_in_progress
            and self.update_cancellable
            and not self.update_commit_started.is_set()
        )

    def menu_status_text(self) -> str:
        now_utc = datetime.now(timezone.utc)
        pending_count = len(pending_alerts(self.state, now_utc))
        summary = self.state.get("last_summary", {})
        checked = summary.get("checked", 0)
        if self.recovery_in_progress:
            return "Checking an interrupted update\u2026"
        if self.update_in_progress:
            return "Updating models\u2026"
        if self.check_in_progress:
            return "Checking for updates\u2026"
        notice = self.state.get("recovery_notice")
        if isinstance(notice, dict) and notice.get("attention"):
            return "Interrupted update needs attention"
        if pending_count:
            return f"{plural(pending_count, 'model')} need attention"
        if checked:
            return f"All {plural(checked, 'model')} up to date"
        return "No check yet"

    def on_open_alerts(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.root.after(0, self.show_window)

    def on_check_now(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.root.after(0, lambda: self.run_check_async(reschedule=True))

    def on_update_all(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.root.after(0, self.update_all_pending)

    def on_cancel_update(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.root.after(0, self.cancel_update)

    def on_acknowledge_all(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.root.after(0, self.acknowledge_all)

    def on_snooze_all(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.root.after(0, self.snooze_all)

    def on_quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.root.after(0, self.quit)

    # ----- Checking -----

    def run_check_async(self, *, reschedule: bool) -> None:
        if self.check_in_progress:
            return
        if self.recovery_in_progress:
            if reschedule:
                self.schedule_next_check()
            return
        if self.update_in_progress:
            if reschedule:
                self.schedule_next_check()
            return
        self.check_in_progress = True
        notice = self.state.get("recovery_notice")
        self.status_var.set(
            notice["message"] if isinstance(notice, dict) and notice.get("message")
            else ""
        )
        self.refresh_tray_icon()
        if self.window is not None:
            self._refresh_header_metrics()
            self._refresh_action_states()
            self._refresh_banners()
        threading.Thread(
            target=self.check_worker,
            kwargs={"reschedule": reschedule},
            daemon=True,
        ).start()

    def check_worker(self, *, reschedule: bool) -> None:
        outcome = perform_check(
            self.models_root_override,
            timeout_seconds=self.timeout_seconds,
            hash_cache_path=self.hash_cache_path,
        )
        if self.shutting_down:
            return
        try:
            self.root.after(0, lambda: self.finish_check(outcome, reschedule=reschedule))
        except (RuntimeError, tk.TclError):
            return

    def finish_check(self, outcome: CheckOutcome, *, reschedule: bool) -> None:
        if self.shutting_down:
            return
        self.check_in_progress = False
        self.update_model_status = {}
        self.last_models_root = outcome.models_root
        self.state = apply_results(
            self.state,
            outcome.results,
            now_utc=outcome.generated_at_utc,
            last_error=outcome.error,
        )
        save_state(self.state_path, self.state)

        notice = self.state.get("recovery_notice")
        self.status_var.set(
            notice["message"] if isinstance(notice, dict) and notice.get("message")
            else self._check_finished_text(outcome)
        )
        self.refresh_ui()
        self.maybe_raise_pending_window(force=False)
        if reschedule:
            self.schedule_next_check()

    def _check_finished_text(self, outcome: CheckOutcome) -> str:
        when = format_local(outcome.generated_at_utc)[11:16] if outcome.generated_at_utc else ""
        if outcome.error:
            return f"Check at {when} failed." if when else "Check failed."
        statuses = [getattr(result, "status", None) for result in outcome.results]
        updates = statuses.count("update-available")
        removed = statuses.count("removed-remote")
        found = []
        if updates:
            found.append(f"{plural(updates, 'update')} available")
        if removed:
            found.append(f"{removed} removed upstream")
        summary = ", ".join(found) if found else "everything matches Hugging Face"
        return f"Checked at {when}: {summary}." if when else f"Checked: {summary}."

    def schedule_next_check(self) -> None:
        if self.shutting_down:
            return
        if self.next_check_token is not None:
            self.root.after_cancel(self.next_check_token)
            self.next_check_token = None
        delay_ms = int(self.check_interval.total_seconds() * 1000)
        self.next_check_due = datetime.now(timezone.utc) + self.check_interval
        self.next_check_token = self.root.after(delay_ms, lambda: self.run_check_async(reschedule=True))

    # ----- Updating -----

    def recovery_worker(self) -> None:
        models_root = None
        actions = []
        error = None
        try:
            models_root = discover_models_root(self.models_root_override)
            actions = recover_interrupted_jobs(models_root)
        except Exception as exc:
            error = redact_sensitive(exc)
        if self.shutting_down:
            return
        outcome = RecoveryWorkerOutcome(models_root, actions, error)
        try:
            self.root.after(0, lambda: self.finish_recovery(outcome))
        except (RuntimeError, tk.TclError):
            return

    def finish_recovery(self, outcome: RecoveryWorkerOutcome) -> None:
        if self.shutting_down:
            return
        self.recovery_in_progress = False
        self.last_models_root = outcome.models_root
        active = self.state.get("active_update")
        restored = sum(action.action == "restored" for action in outcome.actions)
        cleanup_pending = sum(action.action == "cleanup-pending" for action in outcome.actions)
        attention = sum(action.action == "reported" for action in outcome.actions)
        cleaned = sum(action.action == "temp-cleaned" for action in outcome.actions)
        error = redact_sensitive(outcome.error) if outcome.error else None
        if error or outcome.actions:
            message = (
                f"Recovery check failed: {error}" if error else
                f"Recovery: {restored} restored, {cleanup_pending} cleanup pending, "
                f"{attention} need attention"
                + (f", {cleaned} stale manifest temp {'file' if cleaned == 1 else 'files'} removed"
                   if cleaned else "")
                + "."
            )
            self.state["recovery_notice"] = {
                "message": message,
                "attention": bool(error or attention),
                "updated_utc": format_utc(datetime.now(timezone.utc)),
            }
            if isinstance(active, dict) and not error:
                self.state = record_update_finished(
                    self.state, success=False, message=message,
                    now_utc=datetime.now(timezone.utc),
                )
            save_state(self.state_path, self.state)
            self.status_var.set(message)
        elif isinstance(active, dict):
            self.state = record_update_finished(
                self.state,
                success=False,
                message="Previous update was interrupted; no recovery action was needed.",
                now_utc=datetime.now(timezone.utc),
            )
            save_state(self.state_path, self.state)
        notice = self.state.get("recovery_notice")
        self.refresh_ui()
        if self.quit_after_update:
            self.quit_after_update = False
            self.quit()
            return
        self.run_check_async(reschedule=True)
        self.maybe_raise_pending_window(
            force=isinstance(notice, dict) and bool(notice.get("attention"))
        )

    def update_selected(self) -> None:
        keys = [
            k
            for k in self.selected_model_keys()
            if self._alerts_by_key.get(k, {}).get("check_status") == "update-available"
        ]
        if not keys:
            self.status_var.set(
                "Nothing to download for the selection: only models with an "
                "available update can be updated."
            )
            return
        self.prepare_update_async(keys)

    def update_all_pending(self) -> None:
        keys = [
            alert.get("model_key")
            for alert in pending_alerts(self.state, datetime.now(timezone.utc))
            if alert.get("model_key")
            and alert.get("check_status") == "update-available"
        ]
        if not keys:
            self.status_var.set("No downloadable updates are pending.")
            return
        self.prepare_update_async(keys)

    def prepare_update_async(self, model_keys: list[str]) -> None:
        if (
            self.recovery_in_progress
            or self.check_in_progress
            or self.update_in_progress
            or isinstance(self.state.get("active_update"), dict)
            or not model_keys
        ):
            return
        alerts = [
            deepcopy(self.state.get("alerts", {}).get(key))
            for key in model_keys
            if isinstance(self.state.get("alerts", {}).get(key), dict)
        ]
        if not alerts:
            self.status_var.set("Select one or more model alerts to update.")
            return
        self.update_in_progress = True
        self.update_commit_started.clear()
        self.update_cancellable = True
        self.update_cancellation = CancellationToken()
        self.update_model_status = {key: "Queued" for key in model_keys}
        self._plan_error = None
        self.status_var.set("")
        self._progress_detail = ""
        self._download_rate = None
        self._rate_sample = None
        self.update_progress_var.set(0.0)
        self.refresh_ui()
        self.update_thread = threading.Thread(
            target=self.prepare_update_worker,
            args=(alerts,),
            daemon=True,
        )
        self.update_thread.start()

    def prepare_update_worker(self, alerts: list[dict]) -> None:
        plan = None
        error = None
        try:
            models_root = discover_models_root(self.models_root_override)
            plan = build_update_plan(alerts, models_root=models_root)
            if self.update_cancellation is not None:
                self.update_cancellation.raise_if_cancelled()
        except Exception as exc:
            error = redact_sensitive(exc)
        if self.shutting_down:
            return
        try:
            self.root.after(0, lambda: self.finish_prepare_update(plan, error))
        except (RuntimeError, tk.TclError):
            return

    def finish_prepare_update(self, plan: UpdatePlan | None, error: str | None) -> None:
        if self.shutting_down:
            return
        if error or plan is None:
            self.update_in_progress = False
            self.update_cancellable = False
            self.update_cancellation = None
            cancelled = error and "cancel" in error.lower()
            self.update_model_status = {
                key: "Cancelled" if cancelled else "Failed"
                for key in self.update_model_status
            }
            self._plan_error = None if cancelled else (error or "Could not build the update plan.")
            self.status_var.set(
                "Update cancelled before download."
                if cancelled
                else f"Update not started: {error or 'unknown planning error'}"
            )
            self.refresh_ui()
            return
        self.active_update_plan = plan
        self.last_models_root = plan.models_root
        self._awaiting_confirmation = True
        try:
            if self.window is None or not self.window.winfo_exists():
                # Tray path (Update All from the tray menu) with no window
                # open: build one so the confirmation dialog is parented to
                # a real window instead of floating parentless over the
                # withdrawn root. The new window maps by normal WM rules
                # only — the user just acted, so showing it is expected.
                self.create_window()
            elif not self.window.winfo_viewable():
                self.window.deiconify()
            self.refresh_ui()
            confirmed = ask_update_confirmation(
                self.window,
                self._confirm_summary(plan),
                getattr(self, "theme", None),
                format_bytes,
            )
            self._awaiting_confirmation = False
        except tk.TclError as exc:
            self._awaiting_confirmation = False
            self.update_in_progress = False
            self.update_cancellable = False
            self.update_cancellation = None
            self.update_model_status = {
                key: "Failed · confirmation window unavailable"
                for key in self.update_model_status
            }
            self.active_update_plan = None
            self.status_var.set(
                f"Update not started: confirmation window unavailable ({redact_sensitive(exc)})"
            )
            self.refresh_ui()
            return
        if not confirmed:
            self.update_in_progress = False
            self.update_cancellable = False
            self.update_cancellation = None
            self.update_model_status = {}
            self.active_update_plan = None
            self.status_var.set("Update cancelled before download.")
            self.refresh_ui()
            return

        self.update_cancellation = self.update_cancellation or CancellationToken()
        self.last_update_phase = "queued"
        self.state = record_update_started(
            self.state,
            job_id=plan.job_id,
            model_keys=plan.selected_model_keys,
            model_names=plan.selected_model_names,
            total_bytes=plan.total_bytes,
            now_utc=datetime.now(timezone.utc),
        )
        save_state(self.state_path, self.state)
        self.status_var.set("")
        self.update_thread = threading.Thread(
            target=self.update_worker,
            args=(plan, self.update_cancellation),
            daemon=True,
        )
        self.update_thread.start()
        self.refresh_ui()

    def _confirm_summary(self, plan: UpdatePlan) -> ConfirmSummary:
        alerts = self.state.get("alerts", {})
        models = []
        for key, name in zip(plan.selected_model_keys, plan.selected_model_names):
            artifacts = [a for a in plan.artifacts if key in a.model_keys]
            commit = alerts.get(key, {}).get("last_commit_title")
            models.append(ConfirmModel(
                name=str(name),
                size_bytes=sum(a.expected_size for a in artifacts),
                files=[a.label for a in artifacts],
                commit_title=str(commit) if commit else None,
            ))
        return ConfirmSummary(
            models=models,
            download_bytes=plan.remaining_download_bytes,
            total_bytes=plan.total_bytes,
            available_bytes=plan.available_bytes,
            required_bytes=getattr(plan, "required_bytes", 0) or 0,
        )

    def update_worker(self, plan: UpdatePlan, cancellation: CancellationToken) -> None:
        hash_cache = load_hash_cache(self.hash_cache_path)
        latest_results: list = []
        try:
            result, latest_results = run_update_plan(
                plan,
                cancellation=cancellation,
                progress=self.post_update_progress,
                timeout_seconds=self.timeout_seconds,
                hash_cache=hash_cache,
                hash_cache_path=self.hash_cache_path,
                failure_results_out=latest_results,
            )
            outcome = UpdateWorkerOutcome(True, False, result.message, latest_results)
        except UpdateCancelled as exc:
            outcome = UpdateWorkerOutcome(False, True, redact_sensitive(exc), [])
        except Exception as exc:
            outcome = UpdateWorkerOutcome(False, False, redact_sensitive(exc), latest_results)

        if self.shutting_down:
            return
        try:
            self.root.after(0, lambda: self.finish_update(outcome))
        except (RuntimeError, tk.TclError):
            return

    def post_update_progress(self, event: ProgressEvent) -> None:
        if self.shutting_down:
            return
        if not event.cancellable:
            # This runs synchronously on the update worker before the executor
            # touches installed files, closing the gap before Tk handles the event.
            self.update_commit_started.set()
        now = time.monotonic()
        completed = event.bytes_total > 0 and event.bytes_completed >= event.bytes_total
        if (
            event.phase == self.last_progress_post_phase
            and not completed
            and now - self.last_progress_post_monotonic < 0.1
        ):
            return
        self.last_progress_post_phase = event.phase
        self.last_progress_post_monotonic = now
        try:
            self.root.after(0, lambda event=event: self.handle_update_progress(event))
        except (RuntimeError, tk.TclError):
            return

    def handle_update_progress(self, event: ProgressEvent) -> None:
        if self.shutting_down:
            return
        self.update_cancellable = event.cancellable
        status_text = _PROGRESS_PHASE_TEXT.get(
            event.phase, event.phase.replace("-", " ").title()
        )
        percent = None
        if event.bytes_total:
            percent = min(100.0, 100.0 * event.bytes_completed / event.bytes_total)
            self.update_progress_var.set(percent)
        row_status = status_text
        if percent is not None and event.phase in ("downloading", "verifying"):
            row_status = f"{status_text} {int(percent)}%"
        for key in event.model_keys or tuple(self.update_model_status):
            self.update_model_status[key] = row_status
        self._progress_detail = self._progress_text(event)
        if self._progress_banner_ready():
            self.status_var.set("")
            self._refresh_progress_banner()
        else:
            self.status_var.set(self._progress_detail)
        phase_changed = event.phase != self.last_update_phase
        now_monotonic = time.monotonic()
        if phase_changed or now_monotonic - self.last_update_state_save_monotonic >= 2.0:
            self.last_update_phase = event.phase
            self.last_update_state_save_monotonic = now_monotonic
            self.state = record_update_progress(
                self.state,
                phase=event.phase,
                message=redact_sensitive(event.message),
                bytes_completed=event.bytes_completed,
                bytes_total=event.bytes_total,
                cancellable=event.cancellable,
                now_utc=datetime.now(timezone.utc),
            )
            save_state(self.state_path, self.state)
        if phase_changed:
            self.refresh_ui()
        else:
            self._refresh_row_status(event.model_keys)
            self._refresh_action_states()
            if self.window is not None and now_monotonic - self._last_detail_refresh >= 1.0:
                self._last_detail_refresh = now_monotonic
                self._on_tree_select()

    def _progress_banner_ready(self) -> bool:
        return getattr(self, "_progress_banner", None) is not None and self.window is not None

    def _progress_text(self, event: ProgressEvent) -> str:
        """'Downloading weights · 7.6 GiB of 18.1 GiB · 85 MiB/s · about 2 min left'."""
        text = event.message
        if not event.bytes_total:
            self._rate_sample = None
            return text
        text += f" \u00b7 {format_bytes(event.bytes_completed)} of {format_bytes(event.bytes_total)}"
        if event.phase != "downloading":
            self._rate_sample = None
            return text
        now = time.monotonic()
        sample = self._rate_sample
        if sample and sample[0] == event.artifact and event.bytes_completed >= sample[1]:
            elapsed = now - sample[2]
            if elapsed >= 0.5:
                rate = (event.bytes_completed - sample[1]) / elapsed
                previous = self._download_rate
                self._download_rate = rate if previous is None else previous * 0.7 + rate * 0.3
                self._rate_sample = (event.artifact, event.bytes_completed, now)
        else:
            self._rate_sample = (event.artifact, event.bytes_completed, now)
        rate = self._download_rate
        if rate and rate > 1024:
            text += f" \u00b7 {format_bytes(int(rate))}/s"
            remaining = (event.bytes_total - event.bytes_completed) / rate
            if remaining >= 1:
                text += f" \u00b7 about {format_duration(remaining)} left"
        return text

    def _refresh_row_status(self, model_keys) -> None:
        tree = self.tree
        if tree is None or self.window is None:
            return
        now_utc = datetime.now(timezone.utc)
        for key in model_keys or tuple(self.update_model_status):
            alert = self._alerts_by_key.get(key)
            if alert is None or not tree.exists(key):
                continue
            try:
                tree.set(key, "status", self._format_status(alert, alert.get("status", "pending"), now_utc))
            except tk.TclError:
                pass

    def finish_update(self, outcome: UpdateWorkerOutcome) -> None:
        if self.shutting_down:
            return
        self.update_in_progress = False
        self.update_cancellable = False
        self.update_cancellation = None
        self.update_thread = None
        self.update_commit_started.clear()
        self.active_update_plan = None
        self._progress_detail = ""
        active = self.state.get("active_update")
        if isinstance(active, dict) and active.get("job_id"):
            self._session_update_jobs = self._session_update_jobs | {active["job_id"]}
        now_utc = datetime.now(timezone.utc)
        if outcome.results:
            self.state = apply_results(
                self.state,
                outcome.results,
                now_utc=now_utc,
                last_error=None,
            )
        self.state = record_update_finished(
            self.state,
            success=outcome.success,
            cancelled=outcome.cancelled,
            message=redact_sensitive(outcome.message),
            now_utc=now_utc,
        )
        save_state(self.state_path, self.state)
        if outcome.success:
            self.status_var.set("")
            self.update_model_status = {
                key: "Updated" for key in self.update_model_status
            }
            self.update_progress_var.set(100.0)
        elif outcome.cancelled:
            self.status_var.set("")
            self.update_progress_var.set(0.0)
            self.update_model_status = {
                key: "Cancelled" for key in self.update_model_status
            }
        else:
            guidance = "Waiting for model unload" if "Unload the model" in outcome.message else "Failed"
            self.status_var.set(f"Update failed: {outcome.message}")
            self.update_model_status = {
                key: guidance for key in self.update_model_status
            }
        self.schedule_next_check()
        self.refresh_ui()
        if self.quit_after_update:
            self.quit_after_update = False
            self.quit()

    def cancel_update(self) -> None:
        if (
            not self.update_in_progress
            or not self.update_cancellable
            or self.update_commit_started.is_set()
        ):
            return
        if self.update_cancellation is not None:
            self.update_cancellation.cancel()
        self.update_cancellable = False
        self._progress_detail = "Cancelling safely\u2026 installed files stay as they are."
        self.status_var.set("" if self._progress_banner_ready() else self._progress_detail)
        self.refresh_ui()

    def maybe_raise_pending_window(self, *, force: bool) -> None:
        """Background reminder path: toast only, never steal focus/raise a window.

        Kept name for compatibility; the window is only ever shown by explicit
        user action (tray menu / tray click). One toast per alert-state change
        (deduped by WatchToaster), gated by the same reminder cadence as before.
        """
        now_utc = datetime.now(timezone.utc)
        if force or reminder_due(
            self.state,
            now_utc=now_utc,
            reminder_interval=self.reminder_interval,
        ):
            alerts = pending_alerts(self.state, now_utc)
            alert_key = tuple(
                (alert.get("model_key"), alert.get("check_status"))
                for alert in alerts
            )
            if alerts:
                names = ", ".join(
                    str(alert.get("display_name") or alert.get("model_key") or "?")
                    for alert in alerts[:3]
                )
                if len(alerts) > 3:
                    names += f" (+{len(alerts) - 3} more)"
                updates = sum(
                    1 for alert in alerts if alert.get("check_status") == "update-available"
                )
                removed = len(alerts) - updates
                parts = []
                if updates:
                    parts.append(f"{plural(updates, 'update')} available")
                if removed:
                    parts.append(f"{removed} removed upstream")
                body = (
                    f"{names}: {', '.join(parts)}. "
                    "Click the Weight Watcher tray icon to review."
                )
                self.toaster.notify(alert_key, body)
            else:
                self.toaster.clear()
            self.state = record_reminder(self.state, now_utc)
            save_state(self.state_path, self.state)

    # ----- Window -----

    def show_window(self) -> None:
        """Show the alerts window. User-action-only (tray menu / tray click).

        Never steals focus or forces itself on top: the window appears when the
        user asks for it and takes focus only by normal WM rules.
        """
        if self.shutting_down:
            return
        if (
            self.window is not None
            and self.window.winfo_exists()
            and self._theme_is_stale()
        ):
            self._destroy_window()
        if self.window is None or not self.window.winfo_exists():
            self.create_window()
        assert self.window is not None
        self.refresh_tree()
        self.window.deiconify()
        self.window.lift()

    def hide_window(self) -> None:
        if self.window and self.window.winfo_exists():
            self._hide_menus()
            self.window.withdraw()

    def _theme_is_stale(self) -> bool:
        theme = getattr(self, "theme", None)
        return theme is not None and theme.palette.dark != system_prefers_dark()

    # Widgets owned by the window; cleared on teardown so nothing touches a
    # destroyed Tk command after a theme rebuild.
    _WINDOW_WIDGETS = (
        "tree", "detail_text", "header_icon", "check_button", "more_button",
        "busy_bar", "banner_frame", "filter_var", "paned", "empty_state",
        "empty_icon", "empty_title", "empty_body", "undo_button", "snooze_button",
        "dismiss_button", "update_button", "cancel_update_button",
        "update_progressbar", "_detail_card", "_context_menu", "_more_menu",
    )

    def _destroy_window(self) -> None:
        window = self.window
        self.window = None
        for name in self._WINDOW_WIDGETS:
            setattr(self, name, None)
        self._chip_buttons = {}
        self._status_images = {}
        self._images = {}
        self._busy_shown = False
        self._banner_signature = None
        self._progress_banner = None
        self._detail_subject = ()
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def create_window(self) -> None:
        self.window = tk.Toplevel(self.root)
        window = self.window
        window.withdraw()
        self.theme = build_theme(window)
        theme = self.theme
        p = theme.palette
        px = theme.px
        window.title(APP_NAME)
        window.configure(bg=p.bg)
        window.protocol("WM_DELETE_WINDOW", self.hide_window)
        configure_styles(ttk.Style(window), theme)
        window.option_add("*Menu.background", p.surface)
        window.option_add("*Menu.foreground", p.text)
        window.option_add("*Menu.activeBackground", p.selection)
        window.option_add("*Menu.activeForeground", p.text)
        window.option_add("*Menu.font", theme.fonts["body"])
        self._images = {}
        self._set_window_icon()
        self._banner_signature = None
        self._progress_banner = None
        self._detail_links = {}

        window.columnconfigure(0, weight=1)
        window.rowconfigure(4, weight=1)

        # --- Header: overall status and the check action ---
        header = ttk.Frame(window, padding=(px(22), px(18), px(22), px(10)))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        self.header_icon = tk.Label(header, bg=p.bg, fg=p.accent, font=theme.icon_font(24))
        self.header_icon.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, px(14)))
        ttk.Label(header, textvariable=self.headline_var, style="Title.TLabel").grid(
            row=0, column=1, sticky="sw"
        )
        ttk.Label(header, textvariable=self.subline_var, style="Muted.TLabel").grid(
            row=1, column=1, sticky="nw", pady=(px(1), 0)
        )
        header_actions = ttk.Frame(header)
        header_actions.grid(row=0, column=2, rowspan=2, sticky="e", padx=(px(12), 0))
        self.check_button = ttk.Button(
            header_actions,
            text="Check now",
            command=lambda: self.run_check_async(reschedule=True),
        )
        self.check_button.pack(side=tk.LEFT)
        Tooltip(self.check_button, "Compare every installed model with Hugging Face now (F5)", theme)
        self.more_button = ttk.Button(
            header_actions,
            text=theme.glyph("more"),
            style="Icon.TButton",
            command=self._open_more_menu,
            width=3,
        )
        self.more_button.pack(side=tk.LEFT, padx=(px(6), 0))
        Tooltip(self.more_button, "More actions", theme)

        # Thin activity bar; the strip keeps its height so nothing jumps.
        busy_strip = tk.Frame(window, bg=p.bg, height=px(3))
        busy_strip.grid(row=1, column=0, sticky="ew", padx=px(22))
        self.busy_bar = ttk.Progressbar(
            busy_strip, mode="indeterminate", style="Busy.Horizontal.TProgressbar"
        )
        self._busy_shown = False

        # --- Banners (only when something needs saying) ---
        self.banner_frame = ttk.Frame(window, padding=(px(22), 0, px(22), 0))
        self.banner_frame.grid(row=2, column=0, sticky="ew")

        # --- Filter chips ---
        bar = ttk.Frame(window, padding=(px(18), px(12), px(22), px(8)))
        bar.grid(row=3, column=0, sticky="ew")
        chips = ttk.Frame(bar)
        chips.pack(side=tk.LEFT)
        self.filter_var = tk.StringVar(master=window, value=self._filter)
        self._chip_buttons = {}
        for key, _label in FILTERS:
            chip = ttk.Radiobutton(
                chips,
                style="Chip.Toolbutton",
                variable=self.filter_var,
                value=key,
                command=self._on_filter_change,
                takefocus=True,
            )
            chip.pack(side=tk.LEFT, padx=(px(4), 0))
            self._chip_buttons[key] = chip
        ttk.Label(bar, textvariable=self.selection_var, style="Status.TLabel").pack(
            side=tk.RIGHT
        )

        # --- Models list + details ---
        self.paned = ttk.Panedwindow(window, orient=tk.HORIZONTAL)
        self.paned.grid(row=4, column=0, sticky="nsew", padx=px(22))
        list_card = tk.Frame(
            self.paned, bg=p.surface, highlightthickness=1,
            highlightbackground=p.border, highlightcolor=p.border,
        )
        detail_card = tk.Frame(
            self.paned, bg=p.surface, highlightthickness=1,
            highlightbackground=p.border, highlightcolor=p.border,
        )
        self.paned.add(list_card, weight=3)
        self.paned.add(detail_card, weight=2)
        self._detail_card = detail_card
        self._detail_card_shown = True

        list_card.rowconfigure(0, weight=1)
        list_card.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            list_card,
            columns=("status", "source", "changed"),
            show=("tree", "headings"),
            style="Models.Treeview",
            selectmode="extended",
        )
        self.tree.heading("#0", text="Model", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.heading("source", text="Hugging Face repository", anchor="w")
        self.tree.heading("changed", text="Changed", anchor="w")
        for column in ("#0", "status", "source", "changed"):
            self.tree.column(column, stretch=False, anchor="w", minwidth=px(60))
        self.tree.bind("<Configure>", self._fit_columns, add="+")
        tree_scroll = AutoScrollbar(
            list_card, orient="vertical", command=self.tree.yview,
            style="Slim.Vertical.TScrollbar",
        )
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(px(4), 0), pady=(px(2), px(4)))
        tree_scroll.grid(row=0, column=1, sticky="ns", pady=px(4))
        self.tree.tag_configure("muted", foreground=p.muted)
        self.tree.tag_configure("attention", foreground=p.text)
        self._status_images = {
            name: self._photo(status_dot_image(color, size=px(9), ring=ring))
            for name, color, ring in (
                ("update", p.accent, None),
                ("removed", p.warn if p.dark else "#c98a04", None),
                ("failed", p.danger, None),
                ("ok", p.ok, None),
                ("busy", p.accent, p.accent),
                ("snoozed", p.snooze, p.snooze),
                ("dismissed", p.subtle, p.subtle),
                ("unchecked", p.subtle, None),
            )
        }
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Return>", lambda _e: self._on_update_clicked())
        self.tree.bind("<Delete>", lambda _e: self._on_dismiss_clicked())
        self.tree.bind("<Control-a>", self._select_all_rows)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self.tree.bind("<Shift-F10>", self._on_tree_menu_key)
        self.tree.bind("<App>", self._on_tree_menu_key)

        self.empty_state = tk.Frame(list_card, bg=p.surface)
        empty_inner = tk.Frame(self.empty_state, bg=p.surface)
        empty_inner.place(relx=0.5, rely=0.42, anchor="center")
        self.empty_icon = tk.Label(empty_inner, bg=p.surface, font=theme.icon_font(30))
        self.empty_icon.pack()
        self.empty_title = tk.Label(
            empty_inner, bg=p.surface, fg=p.text, font=theme.fonts["h2"]
        )
        self.empty_title.pack(pady=(px(12), px(4)))
        self.empty_body = tk.Label(
            empty_inner, bg=p.surface, fg=p.muted, font=theme.fonts["body"],
            justify="center", wraplength=px(420),
        )
        self.empty_body.pack()

        detail_card.rowconfigure(0, weight=1)
        detail_card.columnconfigure(0, weight=1)
        self.detail_text = tk.Text(
            detail_card,
            wrap="word",
            relief="flat",
            borderwidth=0,
            bg=p.surface,
            fg=p.text,
            font=theme.fonts["body"],
            padx=px(20),
            pady=px(16),
            highlightthickness=0,
            cursor="arrow",
            selectbackground=p.selection,
            selectforeground=p.text,
            inactiveselectbackground=p.selection,
            insertwidth=0,
            width=36,
            height=10,
            spacing1=0,
            spacing3=px(2),
            takefocus=False,
        )
        detail_scroll = AutoScrollbar(
            detail_card, orient="vertical", command=self.detail_text.yview,
            style="Slim.Vertical.TScrollbar",
        )
        self.detail_text.configure(yscrollcommand=detail_scroll.set)
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        detail_scroll.grid(row=0, column=1, sticky="ns", pady=px(4))
        self._configure_detail_tags()
        self.detail_text.configure(state=tk.DISABLED)

        # --- Footer: status line and the actions for the current selection ---
        footer = ttk.Frame(window, padding=(px(22), px(12), px(22), px(16)))
        footer.grid(row=5, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        status_line = ttk.Frame(footer)
        status_line.grid(row=0, column=0, sticky="ew", padx=(0, px(12)))
        ttk.Label(status_line, textvariable=self.status_var, style="Status.TLabel").pack(
            side=tk.LEFT
        )
        self.undo_button = ttk.Button(
            status_line, text="Undo", style="Link.TButton", command=self.undo_last_triage
        )
        self.snooze_button = ttk.Button(footer, command=self._on_snooze_clicked)
        self.snooze_button.grid(row=0, column=1, padx=(0, px(8)))
        Tooltip(
            self.snooze_button,
            lambda: f"Hide for {plural(self.snooze_hours, 'hour')}; it comes back after that",
            theme,
        )
        self.dismiss_button = ttk.Button(footer, command=self._on_dismiss_clicked)
        self.dismiss_button.grid(row=0, column=2, padx=(0, px(8)))
        Tooltip(
            self.dismiss_button,
            "Hide until the files change upstream again (Del)",
            theme,
        )
        self.update_button = ttk.Button(
            footer, style="Accent.TButton", command=self._on_update_clicked
        )
        self.update_button.grid(row=0, column=3)
        Tooltip(
            self.update_button,
            "Download, verify and install the new files. You confirm first (Enter)",
            theme,
        )

        window.bind("<F5>", lambda _e: self.run_check_async(reschedule=True))
        window.bind("<Control-r>", lambda _e: self.run_check_async(reschedule=True))
        window.bind("<Escape>", self._on_escape)
        window.bind("<Control-w>", lambda _e: self.hide_window())

        self._context_menu = tk.Menu(window, tearoff=0)
        self._more_menu = tk.Menu(window, tearoff=0)

        width = min(px(1180), int(window.winfo_screenwidth() * 0.9))
        height = min(px(700), int(window.winfo_screenheight() * 0.85))
        window.minsize(min(px(760), width), min(px(500), height))
        x = max(0, (window.winfo_screenwidth() - width) // 2)
        y = max(0, (window.winfo_screenheight() - height) // 3)
        window.geometry(f"{width}x{height}+{x}+{y}")
        self._sash_pending = True
        self.paned.bind("<Configure>", self._on_paned_configure, add="+")
        self.refresh_tree()
        self._refresh_action_states()
        window.deiconify()
        apply_titlebar_theme(window, p.dark)
        self._schedule_clock_tick()

    def _fit_columns(self, event=None) -> None:
        """Share the list width between columns; drop the repository when narrow."""
        tree = self.tree
        if tree is None or self.theme is None:
            return
        px = self.theme.px
        width = (event.width if event is not None else tree.winfo_width()) - px(2)
        if width <= px(100):
            return
        status_width = px(190)
        changed_width = px(92)
        show_source = width >= px(620)
        columns = ("status", "source", "changed") if show_source else ("status", "changed")
        if tuple(tree.cget("displaycolumns")) != columns:
            tree.configure(displaycolumns=columns)
        rest = max(px(150), width - status_width - changed_width)
        if show_source:
            model_width = max(px(160), int(rest * 0.55))
            tree.column("source", width=max(px(100), rest - model_width))
        else:
            model_width = rest
        tree.column("#0", width=model_width)
        tree.column("status", width=status_width)
        tree.column("changed", width=changed_width)
        self._apply_ellipsis()

    def _apply_ellipsis(self) -> None:
        """Treeview clips text hard; shorten long names and repos with an ellipsis."""
        tree = self.tree
        theme = self.theme
        if tree is None or theme is None:
            return
        font = theme.fonts["body"]
        px = theme.px
        name_room = int(tree.column("#0", "width")) - px(44)
        source_room = int(tree.column("source", "width")) - px(16)
        for iid, (name, source) in getattr(self, "_row_text", {}).items():
            if not tree.exists(iid):
                continue
            tree.item(iid, text=f"  {fit_text(name, font, name_room)}")
            tree.set(iid, "source", fit_text(source, font, source_room))

    def _on_paned_configure(self, event) -> None:
        # Place the sash once the pane has its real size; earlier calls clamp to 0.
        if getattr(self, "_sash_pending", False) and event.width > 1:
            self._sash_pending = False
            self._set_initial_sash(event.width)

    def _set_initial_sash(self, width: int) -> None:
        try:
            if self.window is None or not self._detail_card_shown:
                return
            self.paned.sashpos(0, int(width * 0.6))
        except (tk.TclError, AttributeError):
            pass

    def _photo(self, image):
        photo = ImageTk.PhotoImage(image, master=self.window)
        self._images[id(photo)] = photo
        return photo

    def _set_window_icon(self) -> None:
        try:
            icons = [self._photo(app_icon_image(size)) for size in (64, 32, 16)]
            self.window.iconphoto(False, *icons)
            self.window._lmww_icon = icons[0]
        except Exception:
            pass

    def _configure_detail_tags(self) -> None:
        theme = self.theme
        p = theme.palette
        px = theme.px
        text = self.detail_text
        text.tag_configure("h1", font=theme.fonts["h2"], spacing3=px(4))
        text.tag_configure("hint", foreground=p.muted, spacing1=px(4))
        text.tag_configure("lead", spacing1=px(10), spacing3=px(2), lmargin1=0, lmargin2=0)
        text.tag_configure("muted", foreground=p.muted)
        text.tag_configure("small", font=theme.fonts["small"])
        text.tag_configure("strong", font=theme.fonts["body_strong"])
        text.tag_configure(
            "section", font=theme.fonts["caption"], foreground=p.subtle,
            spacing1=px(18), spacing3=px(6),
        )
        text.tag_configure("key", foreground=p.muted, font=theme.fonts["small"],
                           spacing1=px(8), spacing3=px(1))
        text.tag_configure("val", spacing3=px(0))
        text.tag_configure("mono", font=theme.fonts["mono"])
        text.tag_configure("bullet", lmargin1=0, lmargin2=px(16), spacing1=px(3))
        text.tag_configure("link", foreground=p.accent_text, underline=True)
        for kind, color in (
            ("update", p.accent_text), ("removed", p.warn), ("snoozed", p.snooze),
            ("dismissed", p.muted), ("unchecked", p.muted), ("failed", p.danger),
            ("ok", p.ok), ("busy", p.accent_text),
        ):
            text.tag_configure(f"status-{kind}", foreground=color, font=theme.fonts["body_strong"])
            text.tag_configure(f"dot-{kind}", foreground=color)
        callout = {"background": p.warn_soft, "lmargin1": px(14), "lmargin2": px(14),
                   "rmargin": px(2)}
        text.tag_configure("gap", font=(theme.fonts["small"].actual("family"), 3))
        text.tag_configure("callout-head", font=theme.fonts["body_strong"], spacing1=px(10),
                           spacing3=px(4), **callout)
        text.tag_configure("callout-body", spacing3=px(2), **callout)
        text.tag_configure("callout-item", spacing3=px(2), font=theme.fonts["small"],
                           **{**callout, "lmargin1": px(26), "lmargin2": px(26)})
        text.tag_configure("callout-dot", foreground=p.warn,
                           **{**callout, "lmargin1": px(14), "lmargin2": px(26)})
        text.tag_configure("callout-end", spacing3=px(10), font=(theme.fonts["small"].actual("family"), 2),
                           **callout)
        text.tag_raise("link")
        text.tag_raise("sel")

    # ----- Window: clock, menus, keyboard -----

    def _schedule_clock_tick(self) -> None:
        if self.shutting_down or self.window is None:
            return
        try:
            self.window.after(30_000, self._clock_tick)
        except tk.TclError:
            pass

    def _clock_tick(self) -> None:
        if self.shutting_down or self.window is None:
            return
        try:
            if self.window.winfo_exists() and self.window.winfo_viewable():
                self._refresh_header_metrics()
        except tk.TclError:
            return
        self._schedule_clock_tick()

    def _hide_menus(self) -> None:
        for name in ("_context_menu", "_more_menu"):
            menu = getattr(self, name, None)
            if menu is not None:
                try:
                    menu.unpost()
                except tk.TclError:
                    pass

    def _on_escape(self, _event=None):
        if self.tree is not None and self.tree.selection():
            self.tree.selection_set(())
            return "break"
        self.hide_window()
        return "break"

    def _select_all_rows(self, _event=None):
        if self.tree is not None:
            self.tree.selection_set(self.tree.get_children())
        return "break"

    def _open_more_menu(self) -> None:
        menu = self._more_menu
        menu.delete(0, tk.END)
        now_utc = datetime.now(timezone.utc)
        pending = pending_alerts(self.state, now_utc)
        busy = self.recovery_in_progress or self.check_in_progress or self.update_in_progress
        updatable = [a for a in pending if a.get("check_status") == "update-available"]
        menu.add_command(
            label=f"Update all ({len(updatable)})" if updatable else "Update all",
            command=self.update_all_pending,
            state=tk.NORMAL if updatable and not busy else tk.DISABLED,
        )
        menu.add_command(
            label=f"Snooze everything for {plural(self.snooze_hours, 'hour')}",
            command=self.snooze_all,
            state=tk.NORMAL if pending else tk.DISABLED,
        )
        menu.add_command(
            label="Dismiss everything",
            command=self.acknowledge_all,
            state=tk.NORMAL if pending else tk.DISABLED,
        )
        menu.add_separator()
        menu.add_command(
            label="Open models folder",
            command=lambda: reveal_in_file_manager(self.last_models_root),
            state=tk.NORMAL if self.last_models_root else tk.DISABLED,
        )
        menu.add_command(
            label="Open data folder",
            command=lambda: reveal_in_file_manager(self.state_path.parent),
        )
        menu.add_separator()
        menu.add_command(label="Hide window", accelerator="Esc", command=self.hide_window)
        menu.add_command(label="Quit Weight Watcher", command=self.quit)
        button = self.more_button
        x = button.winfo_rootx() + button.winfo_width()
        y = button.winfo_rooty() + button.winfo_height() + self.theme.px(4)
        try:
            menu.tk_popup(x - self.theme.px(220), y)
        finally:
            menu.grab_release()

    def _on_tree_right_click(self, event):
        if self.tree is None:
            return None
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set((row,))
            self.tree.focus(row)
        self._popup_context_menu(event.x_root, event.y_root)
        return "break"

    def _on_tree_menu_key(self, _event=None):
        if self.tree is None:
            return "break"
        focus = self.tree.focus() or next(iter(self.tree.selection()), "")
        bbox = self.tree.bbox(focus) if focus else None
        if bbox:
            x = self.tree.winfo_rootx() + bbox[0] + self.theme.px(40)
            y = self.tree.winfo_rooty() + bbox[1] + bbox[3]
        else:
            x, y = self.tree.winfo_rootx() + 40, self.tree.winfo_rooty() + 40
        self._popup_context_menu(x, y)
        return "break"

    def _popup_context_menu(self, x: int, y: int) -> None:
        menu = self._context_menu
        menu.delete(0, tk.END)
        keys = self.selected_model_keys()
        alerts = [self._alerts_by_key[k] for k in keys]
        busy = self.recovery_in_progress or self.check_in_progress or self.update_in_progress
        updatable = [a for a in alerts if a.get("check_status") == "update-available"]
        restorable = [a for a in alerts if a.get("status") in ("snoozed", "acknowledged")]
        enabled = lambda flag: tk.NORMAL if flag else tk.DISABLED  # noqa: E731
        menu.add_command(
            label="Update…" if len(updatable) <= 1 else f"Update {len(updatable)} models…",
            accelerator="Enter", command=self.update_selected,
            state=enabled(updatable and not busy),
        )
        menu.add_command(
            label=f"Snooze for {plural(self.snooze_hours, 'hour')}",
            command=self.snooze_selected, state=enabled(alerts),
        )
        menu.add_command(label="Dismiss", accelerator="Del", command=self.acknowledge_selected,
                         state=enabled(alerts))
        menu.add_command(label="Move back to Needs attention", command=self.restore_selected,
                         state=enabled(restorable))
        menu.add_separator()
        single = self._single_selected_record()
        menu.add_command(label="Open on Hugging Face", command=self.open_selected_on_hf,
                         state=enabled(single and single.get("remote_repo")))
        menu.add_command(label="Show file in folder", command=self.reveal_selected_file,
                         state=enabled(single and single.get("local_path")))
        menu.add_command(label="Copy details", command=self.copy_selected_details,
                         state=enabled(single))
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _single_selected_record(self) -> dict | None:
        if self.tree is None:
            return None
        selection = self.tree.selection()
        if len(selection) != 1:
            return None
        iid = selection[0]
        if iid in self._alerts_by_key:
            return self._alerts_by_key[iid]
        return getattr(self, "_unresolved_by_iid", {}).get(iid)

    # ----- Window: selection and details -----

    def _on_tree_select(self, _event=None) -> None:
        selected = self.selected_model_keys()
        tree_selection = self.tree.selection() if self.tree is not None else ()
        unresolved = [
            getattr(self, "_unresolved_by_iid", {}).get(iid)
            for iid in tree_selection
            if iid in getattr(self, "_unresolved_by_iid", {})
        ]
        count = len(selected) + len(unresolved)
        if count == 0:
            self.selection_var.set("No selection")
            self._render_detail(None, selection_text=self._idle_detail_text())
        elif count == 1:
            self.selection_var.set("1 model selected")
            if unresolved:
                self._render_unresolved_detail(unresolved[0])
            else:
                self._render_detail(self._alerts_by_key.get(selected[0]))
        else:
            self.selection_var.set(f"{count} models selected")
            self._render_multi_detail([self._alerts_by_key[k] for k in selected], unresolved)
        self._refresh_action_states()

    def _idle_detail_text(self) -> str:
        return (
            "Select a model to see the source and the upstream commit.\n\n"
            "Tip: Ctrl+click or Shift+click selects several models; "
            "right-click for more actions."
        )

    def _detail_link(self, text, label: str, action) -> None:
        links = getattr(self, "_detail_links", None)
        if links is None:
            links = self._detail_links = {}
        tag = f"link-{len(links)}"
        links[tag] = action
        text.insert(tk.END, label, ("link", tag))
        try:
            text.tag_bind(tag, "<Button-1>", lambda _e, a=action: a())
            text.tag_bind(tag, "<Enter>", lambda _e: text.configure(cursor="hand2"))
            text.tag_bind(tag, "<Leave>", lambda _e: text.configure(cursor="arrow"))
        except (tk.TclError, AttributeError):
            pass

    def _detail_row(self, text, key: str, value: str, *value_tags: str) -> None:
        """Stacked label over value: survives any pane width and long paths."""
        text.insert(tk.END, f"{key}\n", ("key",))
        text.insert(tk.END, value, ("val",) + value_tags)
        text.insert(tk.END, "\n", ("val",))

    def _detail_link_row(self, text, key: str, label: str, action) -> None:
        text.insert(tk.END, f"{key}\n", ("key",))
        self._detail_link(text, label, action)
        text.insert(tk.END, "\n", ("val",))

    def _begin_detail(self):
        text = getattr(self, "detail_text", None)
        if text is None:
            return None
        subject = tuple(self.tree.selection()) if self.tree is not None else ()
        try:
            keep = float(text.yview()[0]) if subject == self._detail_subject else 0.0
        except (tk.TclError, TypeError, ValueError, IndexError):
            keep = 0.0
        self._detail_subject = subject
        self._detail_scroll = keep
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        self._detail_links = {}
        return text

    def _end_detail(self, text) -> None:
        """Lock the detail text and keep the reader's scroll position on re-render."""
        text.configure(state=tk.DISABLED)
        if self._detail_scroll:
            try:
                text.yview_moveto(self._detail_scroll)
            except (tk.TclError, TypeError):
                pass

    def _render_detail(self, alert: dict | None, *, selection_text: str | None = None) -> None:
        text = self._begin_detail()
        if text is None:
            return
        if alert is None:
            text.insert("1.0", selection_text or "Select a model to see details.", "hint")
            self._end_detail(text)
            return

        now_utc = datetime.now(timezone.utc)
        name = alert.get("display_name") or alert.get("model_key") or "Unknown model"
        kind = alert_kind(alert, self.update_model_status.get(alert.get("model_key")))
        status_raw = alert.get("status", "pending")
        text.insert(tk.END, f"{name}\n", "h1")
        text.insert(
            tk.END,
            f"{self._format_status(alert, status_raw, now_utc)}\n",
            f"status-{kind}",
        )

        check_status = alert.get("check_status")
        artifacts = alert.get("artifacts") or []
        commit_title = alert.get("last_commit_title")
        remote_mod = alert.get("remote_modified_utc")
        missing_local = any(a.get("status") == "missing-local" for a in artifacts)
        if check_status == "removed-remote":
            lead = (
                "This exact file was deleted upstream. Your copy on this PC is untouched "
                "and keeps working, but it will not get updates. There is nothing to "
                "download, so updating is not offered."
            )
        elif missing_local:
            lead = (
                "A file this model needs is missing on this PC. Updating downloads it "
                "again from Hugging Face."
            )
        else:
            lead = "Hugging Face has newer files than the copy on this PC."
            if remote_mod:
                lead += f" They changed {format_ago(remote_mod, now=now_utc)}."
        text.insert(tk.END, lead + "\n", "lead")
        if status_raw == "snoozed":
            text.insert(
                tk.END,
                "Snoozed: it comes back automatically when the snooze ends.\n",
                ("lead", "muted"),
            )
        elif status_raw == "acknowledged":
            text.insert(
                tk.END,
                "Dismissed: it comes back only if the files change upstream again.\n",
                ("lead", "muted"),
            )

        suggestions = alert.get("suggestions") or []
        if check_status == "removed-remote":
            text.insert(tk.END, "\n", "gap")
            if suggestions:
                text.insert(tk.END, "Upstream still offers these quants:\n", "callout-head")
                for suggestion in suggestions:
                    text.insert(tk.END, "• ", "callout-dot")
                    text.insert(tk.END, f"{suggestion}\n", "callout-item")
            else:
                text.insert(tk.END, "What now?\n", "callout-head")
                text.insert(
                    tk.END,
                    "Keep your copy, or pick another quant from the repository below.\n",
                    "callout-body",
                )
            text.insert(tk.END, "\n", "callout-end")

        repo = alert.get("remote_repo")
        remote_file = alert.get("remote_file")
        text.insert(tk.END, "HUGGING FACE\n", "section")
        if repo:
            self._detail_link_row(
                text, "Repository", repo, lambda r=repo: open_url(hf_repo_url(r))
            )
        if remote_file:
            self._detail_row(text, "File", remote_file, "mono")
        if commit_title or remote_mod:
            value = f"“{commit_title}”" if commit_title else "Unknown commit"
            if remote_mod:
                value += f"\n{format_when(remote_mod)} · {format_ago(remote_mod, now=now_utc)}"
            self._detail_row(text, "Last upstream commit", value)
        if not (repo or remote_file):
            text.insert(tk.END, "No source metadata for this model.\n", "muted")

        local_path = alert.get("local_path")
        text.insert(tk.END, "ON THIS PC\n", "section")
        if local_path:
            self._detail_row(text, "Local file", local_path, "mono")
            self._detail_link(
                text, "Show in folder", lambda path=local_path: reveal_in_file_manager(path)
            )
            text.insert(tk.END, "\n", ("val",))
        facts = []
        local_mod = alert.get("local_modified_utc")
        if local_mod:
            facts.append(f"Modified {format_when(local_mod)}")
        local_size = sum(int(a.get("local_size") or 0) for a in artifacts)
        if local_size:
            facts.append(format_bytes(local_size))
        if facts:
            self._detail_row(text, "On disk", " · ".join(facts))
        if alert.get("publisher"):
            self._detail_row(text, "LM Studio publisher", str(alert["publisher"]))

        if artifacts:
            text.insert(tk.END, f"FILES CHECKED ({len(artifacts)})\n", "section")
            for artifact in artifacts:
                status = artifact.get("status", "?")
                label = artifact.get("label") or artifact.get("remote_file") or "?"
                dot = ARTIFACT_DOT_KIND.get(status, "unchecked")
                text.insert(tk.END, "● ", ("bullet", f"dot-{dot}"))
                text.insert(tk.END, label, ("bullet",))
                description = ARTIFACT_STATUS_TEXT.get(status, status)
                text.insert(tk.END, f" — {description}", ("bullet", "muted"))
                ct = artifact.get("last_commit_title")
                if ct and ct != commit_title:
                    text.insert(tk.END, f" (commit: {ct})", ("bullet", "muted"))
                text.insert(tk.END, "\n", ("bullet",))

        remote_blob = alert.get("remote_sha256")
        if remote_blob:
            method = alert.get("hash_method") or "lfs-oid"
            text.insert(
                tk.END,
                f"Remote blob {remote_blob[:12]}… · verified by {method}\n",
                ("key",),
            )
        self._end_detail(text)

    def _render_unresolved_detail(self, item: dict) -> None:
        text = self._begin_detail()
        if text is None:
            return
        name = item.get("display_name") or item.get("model_key") or "Unknown model"
        text.insert(tk.END, f"{name}\n", "h1")
        text.insert(tk.END, "Can't check\n", "status-unchecked")
        text.insert(
            tk.END,
            "Weight Watcher could not compare this model with Hugging Face, so it "
            "cannot tell whether an update exists.\n",
            "lead",
        )
        message = item.get("message")
        if message:
            text.insert(tk.END, "WHAT HAPPENED\n", "section")
            text.insert(tk.END, f"{message}\n")
        text.insert(tk.END, "DETAILS\n", "section")
        if item.get("model_key"):
            self._detail_row(text, "Model key", str(item["model_key"]), "mono")
        if item.get("publisher"):
            self._detail_row(text, "LM Studio publisher", str(item["publisher"]))
        repo = item.get("remote_repo")
        if repo:
            self._detail_link_row(
                text, "Repository", repo, lambda r=repo: open_url(hf_repo_url(r))
            )
        self._end_detail(text)

    def _render_multi_detail(self, alerts: list[dict], unresolved: list[dict]) -> None:
        text = self._begin_detail()
        if text is None:
            return
        count = len(alerts) + len(unresolved)
        text.insert(tk.END, f"{count} models selected\n", "h1")
        updatable = [a for a in alerts if a.get("check_status") == "update-available"]
        removed = [a for a in alerts if a.get("check_status") == "removed-remote"]
        parts = []
        if updatable:
            parts.append(f"{len(updatable)} can be updated")
        if removed:
            parts.append(f"{len(removed)} removed upstream")
        if unresolved:
            parts.append(f"{len(unresolved)} can't be checked")
        if parts:
            text.insert(tk.END, " · ".join(parts) + "\n", "muted")
        uploaders = sorted(
            {
                split_remote_repo(alert.get("remote_repo"))[0]
                for alert in alerts
                if split_remote_repo(alert.get("remote_repo"))[0] != "—"
            }
        )
        if uploaders:
            text.insert(tk.END, f"Uploaders: {', '.join(uploaders)}\n", "muted")
        text.insert(tk.END, "SELECTION\n", "section")
        now_utc = datetime.now(timezone.utc)
        for alert in alerts:
            kind = alert_kind(alert, self.update_model_status.get(alert.get("model_key")))
            name = alert.get("display_name") or alert.get("model_key")
            text.insert(tk.END, f"{name}\n", ("bullet", "strong"))
            text.insert(
                tk.END,
                f"{self._format_status(alert, alert.get('status', 'pending'), now_utc)}\n",
                ("bullet", f"status-{kind}"),
            )
        for item in unresolved:
            text.insert(tk.END, f"{item.get('display_name') or item.get('model_key')}\n",
                        ("bullet", "strong"))
            text.insert(tk.END, "Can't check\n", ("bullet", "status-unchecked"))
        if updatable and not self.update_in_progress:
            text.insert(
                tk.END,
                "\nPress Update to review the download before anything changes.\n",
                ("lead", "muted"),
            )
        self._end_detail(text)

    def open_selected_on_hf(self) -> None:
        record = self._single_selected_record()
        if record is None:
            selected = self.selected_model_keys()
            record = self._alerts_by_key.get(selected[0]) if selected else None
        if not record or not record.get("remote_repo"):
            return
        open_url(hf_repo_url(record["remote_repo"]))

    def reveal_selected_file(self) -> None:
        record = self._single_selected_record()
        if record and record.get("local_path"):
            if not reveal_in_file_manager(record["local_path"]):
                self.status_var.set("Could not open the folder; the file may have moved.")

    def copy_selected_details(self) -> None:
        record = self._single_selected_record()
        if not record or self.window is None:
            return
        details = format_alert_detail(record)
        if record.get("message"):
            details += f"\nStatus: {record['message']}"
        self.window.clipboard_clear()
        self.window.clipboard_append(details)
        self.status_var.set("Copied the model details to the clipboard.")

    # ----- Window: actions -----

    def _has_row_selection(self) -> bool:
        """Any selected row, including 'Can't check' rows that have no actions.

        The footer acts on the selection when there is one and on everything
        pending only when nothing is selected, so a selection of non-actionable
        rows must never fall through to the 'all' scope.
        """
        if self.tree is None:
            return False
        try:
            return bool(self.tree.selection())
        except tk.TclError:
            return False

    def _on_update_clicked(self) -> None:
        if self._has_row_selection():
            self.update_selected()
        else:
            self.update_all_pending()

    def _on_snooze_clicked(self) -> None:
        if self._has_row_selection():
            self.snooze_selected()
        else:
            self.snooze_all()

    def _on_dismiss_clicked(self) -> None:
        if self._has_row_selection():
            self.acknowledge_selected()
        else:
            self.acknowledge_all()

    def _refresh_action_states(self) -> None:
        busy = self.recovery_in_progress or self.check_in_progress or self.update_in_progress
        now_utc = datetime.now(timezone.utc)
        pending_list = pending_alerts(self.state, now_utc)
        selected_keys = self.selected_model_keys()
        if selected_keys or self._has_row_selection():
            scope = [self._alerts_by_key.get(k, {}) for k in selected_keys]
            updatable = [
                a for a in scope if a.get("check_status") == "update-available"
            ]
            update_text = "Update" if len(updatable) <= 1 else f"Update {len(updatable)}"
            snooze_text = f"Snooze {self.snooze_hours} h"
            dismiss_text = "Dismiss"
            triage_enabled = bool(selected_keys)
        else:
            # An update can only be offered for models whose source still exists
            # upstream. A removed-remote model has nothing to download.
            updatable = [
                a for a in pending_list if a.get("check_status") == "update-available"
            ]
            update_text = f"Update all ({len(updatable)})" if updatable else "Update all"
            snooze_text = f"Snooze all {self.snooze_hours} h"
            dismiss_text = "Dismiss all"
            triage_enabled = bool(pending_list)

        controls = (
            ("check_button", not busy, "Checking…" if self.check_in_progress else "Check now"),
            ("update_button", bool(updatable) and not busy, update_text),
            ("snooze_button", triage_enabled, snooze_text),
            ("dismiss_button", triage_enabled, dismiss_text),
            (
                "cancel_update_button",
                self.update_in_progress
                and self.update_cancellable
                and not self.update_commit_started.is_set(),
                None,
            ),
        )
        for name, enabled, label in controls:
            control = getattr(self, name, None)
            if control is None:
                continue
            control.configure(state=tk.NORMAL if enabled else tk.DISABLED)
            if label is not None:
                try:
                    control.configure(text=label)
                except (tk.TclError, AttributeError):
                    pass
        self._refresh_busy_bar()

    def _refresh_busy_bar(self) -> None:
        bar = getattr(self, "busy_bar", None)
        if bar is None or self.window is None:
            return
        busy = self.recovery_in_progress or self.check_in_progress
        try:
            if busy and not self._busy_shown:
                bar.place(relx=0, rely=0, relwidth=1, relheight=1)
                bar.start(12)
                self._busy_shown = True
            elif not busy and self._busy_shown:
                bar.stop()
                bar.place_forget()
                self._busy_shown = False
        except tk.TclError:
            pass

    def _on_filter_change(self) -> None:
        self._filter = self.filter_var.get()
        if self.tree is not None:
            self.tree.selection_set(())
        self.refresh_tree()

    # ----- Refresh -----

    def refresh_tree(self) -> None:
        if self.tree is None:
            return
        tree = self.tree
        previous_selection = set(tree.selection())
        previous_focus = tree.focus()
        try:
            previous_view = tree.yview()[0]
        except (tk.TclError, TypeError, IndexError):
            previous_view = 0.0

        for item in tree.get_children():
            tree.delete(item)

        now_utc = datetime.now(timezone.utc)
        alerts = list(all_alerts(self.state, now_utc))
        alerts.sort(key=alert_sort_key)
        self._alerts_by_key = {alert["model_key"]: alert for alert in alerts}
        unresolved = [
            item for item in self.state.get("unresolved", []) if isinstance(item, dict)
        ]
        self._unresolved_by_iid = {
            f"{UNRESOLVED_IID_PREFIX}{item.get('model_key') or index}": item
            for index, item in enumerate(unresolved)
        }

        counts = {
            "all": len(alerts) + len(unresolved),
            "attention": sum(1 for a in alerts if a.get("status") == "pending"),
            "snoozed": sum(1 for a in alerts if a.get("status") == "snoozed"),
            "dismissed": sum(1 for a in alerts if a.get("status") == "acknowledged"),
            "unchecked": len(unresolved),
        }
        self._refresh_chips(counts)
        active = self._filter

        images = getattr(self, "_status_images", {})
        self._row_text = {}
        for alert in alerts:
            if not filter_matches(active, alert):
                continue
            status_raw = alert.get("status", "pending")
            update_status = self.update_model_status.get(alert.get("model_key"))
            kind = alert_kind(alert, update_status)
            changed = alert.get("remote_modified_utc") or alert.get("first_detected_utc")
            item_options = {
                "text": f"  {alert.get('display_name') or alert['model_key']}",
                "values": (
                    self._format_status(alert, status_raw, now_utc),
                    alert.get("remote_repo") or "—",
                    format_ago(changed, now=now_utc) if changed else "—",
                ),
                "tags": ("muted",) if status_raw == "acknowledged" else ("attention",),
            }
            if kind in images:
                item_options["image"] = images[kind]
            tree.insert("", tk.END, iid=alert["model_key"], **item_options)
            self._row_text[alert["model_key"]] = (
                str(alert.get("display_name") or alert["model_key"]),
                str(alert.get("remote_repo") or "\u2014"),
            )

        if active in ("all", "unchecked"):
            for iid, item in self._unresolved_by_iid.items():
                item_options = {
                    "text": f"  {item.get('display_name') or item.get('model_key') or 'Unknown model'}",
                    "values": ("Can't check", item.get("remote_repo") or "—", "—"),
                    "tags": ("muted",),
                }
                if "unchecked" in images:
                    item_options["image"] = images["unchecked"]
                tree.insert("", tk.END, iid=iid, **item_options)
                self._row_text[iid] = (
                    str(item.get("display_name") or item.get("model_key") or "Unknown model"),
                    str(item.get("remote_repo") or "\u2014"),
                )

        rows = tree.get_children()
        self._apply_ellipsis()
        restored = [iid for iid in rows if iid in previous_selection]
        if restored:
            tree.selection_set(restored)
            if previous_focus in rows:
                tree.focus(previous_focus)
        try:
            tree.yview_moveto(previous_view)
        except (tk.TclError, TypeError):
            pass

        self._refresh_empty_state(bool(rows), counts)
        self._refresh_banners()
        self._refresh_header_metrics()
        self._on_tree_select()

    def _refresh_chips(self, counts: dict[str, int]) -> None:
        chips = getattr(self, "_chip_buttons", None)
        if not chips:
            return
        if self._filter == "unchecked" and not counts["unchecked"]:
            self._filter = "all"
            self.filter_var.set("all")
        for key, label in FILTERS:
            chip = chips[key]
            count = counts.get(key, 0)
            chip.configure(text=f"{label}  {count}")
            if key == "unchecked":
                if count:
                    chip.pack(side=tk.LEFT, padx=(self.theme.px(4), 0))
                else:
                    chip.pack_forget()

    def _refresh_empty_state(self, has_rows: bool, counts: dict[str, int]) -> None:
        overlay = getattr(self, "empty_state", None)
        if overlay is None:
            return
        p = self.theme.palette
        if has_rows:
            overlay.grid_remove()
            self._show_detail_pane(True)
            return
        total = counts["all"]
        last_checked = self.state.get("last_checked_utc")
        if total == 0 and self.check_in_progress and not last_checked:
            icon, color = self.theme.glyph("refresh"), p.accent
            title = "Checking your models…"
            body = ("The first check hashes your local files, so it can take a few "
                    "minutes. Later checks are much faster.")
        elif total == 0 and self.state.get("last_error"):
            icon, color = self.theme.glyph("clock"), p.subtle
            title = "No results yet"
            body = "Models that need attention appear here after a successful check."
        elif total == 0 and not last_checked:
            icon, color = self.theme.glyph("clock"), p.subtle
            title = "No check yet"
            body = "Weight Watcher checks automatically. Press Check now to start right away."
        elif total == 0:
            tracked = self.state.get("last_summary", {}).get("checked", 0)
            icon, color = self.theme.glyph("done"), p.ok
            title = "Everything is up to date"
            body = (
                f"All {plural(tracked, 'model')} match Hugging Face."
                if tracked else "No model needs your attention."
            )
        else:
            icon, color = self.theme.glyph("done"), p.subtle
            title, body = EMPTY_FILTER_TEXT.get(self._filter, ("Nothing here", ""))
            body = body.format(hours=plural(self.snooze_hours, "hour"))
        self.empty_icon.configure(text=icon, fg=color)
        self.empty_title.configure(text=title)
        self.empty_body.configure(text=body)
        overlay.grid(row=0, column=0, columnspan=2, sticky="nsew")
        overlay.tkraise()
        self._show_detail_pane(total > 0)

    def _show_detail_pane(self, show: bool) -> None:
        card = getattr(self, "_detail_card", None)
        if card is None or show == self._detail_card_shown:
            return
        try:
            if show:
                self.paned.add(card, weight=2)
                self.paned.after_idle(
                    lambda: self._set_initial_sash(self.paned.winfo_width())
                )
            else:
                self.paned.forget(card)
            self._detail_card_shown = show
        except tk.TclError:
            pass

    def _refresh_banners(self) -> None:
        frame = getattr(self, "banner_frame", None)
        if frame is None or self.window is None:
            return
        specs = self._banner_specs()
        signature = (bool(self.update_in_progress), tuple((s["key"], s["title"], s["message"]) for s in specs))
        if signature == getattr(self, "_banner_signature", None):
            self._refresh_progress_banner()
            return
        self._banner_signature = signature
        for child in frame.winfo_children():
            if child is not getattr(self, "_progress_banner", None):
                child.destroy()
        px = self.theme.px
        if self.update_in_progress:
            if self._progress_banner is None:
                self._progress_banner = self._build_progress_banner(frame)
            self._progress_banner.pack(fill=tk.X, pady=(px(4), px(4)))
        elif self._progress_banner is not None:
            self._progress_banner.pack_forget()
        for spec in specs:
            banner = build_banner(
                frame,
                self.theme,
                kind=spec["kind"],
                title=spec["title"],
                message=spec["message"],
                icon=spec["icon"],
                actions=spec.get("actions"),
                on_close=spec.get("on_close"),
            )
            banner.pack(fill=tk.X, pady=(px(4), px(4)))
        self._refresh_progress_banner()

    def _banner_specs(self) -> list[dict]:
        specs: list[dict] = []
        notice = self.state.get("recovery_notice")
        if isinstance(notice, dict) and notice.get("message"):
            attention = bool(notice.get("attention"))
            actions = []
            if attention and self.last_models_root:
                actions.append(BannerAction(
                    "Open models folder",
                    lambda: reveal_in_file_manager(self.last_models_root),
                ))
            specs.append({
                "key": "recovery",
                "kind": "danger" if attention else "info",
                "icon": "warning" if attention else "info",
                "title": (
                    "An interrupted update needs your attention"
                    if attention else "Recovered from an interrupted update"
                ),
                "message": str(notice["message"]),
                "actions": actions,
                "on_close": self.dismiss_recovery_notice,
            })
        last_error = self.state.get("last_error")
        if last_error and not self.check_in_progress:
            message = redact_sensitive(str(last_error))
            if len(message) > 320:
                message = message[:317] + "…"
            specs.append({
                "key": "check-error",
                "kind": "danger",
                "icon": "error",
                "title": message,
                "message": "Weight Watcher tries again at the next scheduled check.",
                "actions": [BannerAction(
                    "Try again", lambda: self.run_check_async(reschedule=True)
                )],
            })
        plan_error = getattr(self, "_plan_error", None)
        if plan_error and not self.update_in_progress:
            specs.append({
                "key": "plan-error",
                "kind": "danger",
                "icon": "error",
                "title": "The update didn't start",
                "message": str(plan_error),
                "on_close": self.dismiss_plan_error,
            })
        last_update = self.state.get("last_update")
        if (
            isinstance(last_update, dict)
            and not self.update_in_progress
            and last_update.get("job_id") != getattr(self, "_dismissed_update_job", None)
        ):
            spec = self._last_update_banner(last_update)
            if spec:
                specs.append(spec)
        return specs

    def _last_update_banner(self, last_update: dict) -> dict | None:
        job_id = last_update.get("job_id")
        names = [str(n) for n in last_update.get("model_names") or []]
        who = names[0] if len(names) == 1 else plural(len(names), "model")
        this_session = job_id in getattr(self, "_session_update_jobs", set())
        completed = to_local(last_update.get("completed_utc"))
        recent = (
            completed is not None
            and (datetime.now(timezone.utc) - completed).total_seconds() < 86400
        )
        close = lambda: self.dismiss_update_banner(job_id)  # noqa: E731
        message = redact_sensitive(str(last_update.get("message") or ""))
        if last_update.get("success"):
            if not this_session:
                return None
            return {
                "key": f"update-{job_id}", "kind": "ok", "icon": "done",
                "title": f"Updated {who}",
                "message": "Every file was downloaded, verified against Hugging Face and installed.",
                "on_close": close,
            }
        if last_update.get("cancelled"):
            if not this_session:
                return None
            return {
                "key": f"update-{job_id}", "kind": "info", "icon": "info",
                "title": "Update cancelled",
                "message": "Installed files were not changed.",
                "on_close": close,
            }
        if not (this_session or recent):
            return None
        unload = "Unload the model" in message
        retry_keys = [
            key for key in last_update.get("model_keys") or []
            if self.state.get("alerts", {}).get(key, {}).get("check_status") == "update-available"
        ]
        actions = []
        if retry_keys:
            actions.append(BannerAction(
                "Try again", lambda keys=tuple(retry_keys): self.prepare_update_async(list(keys))
            ))
        return {
            "key": f"update-{job_id}", "kind": "danger" if not unload else "warn",
            "icon": "error" if not unload else "warning",
            "title": (
                f"Unload {who} in LM Studio, then try again" if unload
                else f"Updating {who} didn't finish"
            ),
            "message": message,
            "actions": actions,
            "on_close": close,
        }

    def _build_progress_banner(self, parent) -> tk.Frame:
        theme = self.theme
        p = theme.palette
        px = theme.px
        soft = p.info_soft
        outer = tk.Frame(parent, bg=soft, highlightthickness=1,
                         highlightbackground=p.border if not p.dark else p.border_strong)
        tk.Frame(outer, bg=p.accent, width=px(3)).pack(side=tk.LEFT, fill=tk.Y)
        body = tk.Frame(outer, bg=soft, padx=px(12), pady=px(10))
        body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        tk.Label(body, text=theme.glyph("download"), bg=soft, fg=p.accent,
                 font=theme.icon_font(13)).grid(row=0, column=0, rowspan=3, sticky="n",
                                                padx=(0, px(10)), pady=(px(1), 0))
        tk.Label(body, textvariable=self.progress_title_var, bg=soft, fg=p.text,
                 font=theme.fonts["body_strong"], anchor="w").grid(row=0, column=1, sticky="ew")
        tk.Label(body, textvariable=self.progress_detail_var, bg=soft, fg=p.muted if not p.dark else p.text,
                 font=theme.fonts["small"], anchor="w").grid(row=1, column=1, sticky="ew",
                                                             pady=(px(1), px(6)))
        self.update_progressbar = ttk.Progressbar(
            body, variable=self.update_progress_var, maximum=100.0,
            mode="determinate", style="info.Banner.Horizontal.TProgressbar",
        )
        self.update_progressbar.grid(row=2, column=1, sticky="ew")
        self.cancel_update_button = ttk.Button(
            body, text="Cancel", style="info.Banner.TButton", command=self.cancel_update
        )
        self.cancel_update_button.grid(row=0, column=2, rowspan=3, sticky="e", padx=(px(14), 0))
        Tooltip(self.cancel_update_button,
                "Stop and keep the installed files. Not possible once installation starts.",
                theme)
        self._progress_indeterminate = False
        return outer

    def _refresh_progress_banner(self) -> None:
        if not self.update_in_progress or getattr(self, "_progress_banner", None) is None:
            return
        plan = self.active_update_plan
        bar = getattr(self, "update_progressbar", None)
        planning = plan is None
        if bar is not None:
            try:
                if planning and not self._progress_indeterminate:
                    bar.configure(mode="indeterminate")
                    bar.start(12)
                    self._progress_indeterminate = True
                elif not planning and self._progress_indeterminate:
                    bar.stop()
                    bar.configure(mode="determinate")
                    self._progress_indeterminate = False
            except tk.TclError:
                pass
        if planning:
            count = len(self.update_model_status)
            self.progress_title_var.set(f"Preparing to update {plural(count, 'model')}…")
            self.progress_detail_var.set("Checking which files changed and how much space is free.")
        else:
            names = list(plan.selected_model_names)
            who = names[0] if len(names) == 1 else plural(len(names), "model")
            if getattr(self, "_awaiting_confirmation", False):
                self.progress_title_var.set(f"Ready to update {who}")
                self.progress_detail_var.set("Waiting for you to confirm the download.")
            else:
                self.progress_title_var.set(f"Updating {who}")
                self.progress_detail_var.set(self._progress_detail or "Starting\u2026")

    # ----- Header -----

    def _refresh_header_metrics(self) -> None:
        now_utc = datetime.now(timezone.utc)
        alerts = list(all_alerts(self.state, now_utc))
        pending = [a for a in alerts if a.get("status") == "pending"]
        removed_count = sum(1 for a in pending if a.get("check_status") == "removed-remote")
        updatable_count = len(pending) - removed_count
        snoozed_count = sum(1 for a in alerts if a.get("status") == "snoozed")
        summary = self.state.get("last_summary", {})
        unresolved_count = len(self.state.get("unresolved", []) or [])
        tracked = summary.get("checked", 0)
        notice = self.state.get("recovery_notice")
        attention = isinstance(notice, dict) and bool(notice.get("attention"))
        theme = getattr(self, "theme", None)

        if self.update_in_progress:
            headline, icon, tone = "Updating models…", "download", "accent"
        elif self.recovery_in_progress:
            headline, icon, tone = "Checking an interrupted update…", "refresh", "accent"
        elif self.check_in_progress:
            headline = f"Checking {plural(tracked, 'model')}…" if tracked else "Checking your models…"
            icon, tone = "refresh", "accent"
        elif attention:
            headline, icon, tone = "An interrupted update needs attention", "warning", "danger"
        elif updatable_count and removed_count:
            headline = (
                f"{plural(updatable_count, 'update')} available · "
                f"{removed_count} removed upstream"
            )
            icon, tone = "download", "accent"
        elif updatable_count:
            headline = f"{plural(updatable_count, 'update')} available"
            icon, tone = "download", "accent"
        elif removed_count:
            noun = "model was" if removed_count == 1 else "models were"
            headline = f"{removed_count} {noun} removed upstream"
            icon, tone = "warning", "warn"
        elif self.state.get("last_error"):
            headline, icon, tone = "Couldn't check for updates", "error", "danger"
        elif not self.state.get("last_checked_utc"):
            headline, icon, tone = "Waiting for the first check", "clock", "muted"
        elif snoozed_count:
            headline, icon, tone = "Nothing needs attention right now", "done", "ok"
        elif unresolved_count:
            headline = f"Up to date · {unresolved_count} can't be checked"
            icon, tone = "done", "ok"
        else:
            headline = (
                f"All {plural(tracked, 'model')} up to date" if tracked else "All models up to date"
            )
            icon, tone = "done", "ok"
        self.headline_var.set(headline)

        parts = []
        last_checked = self.state.get("last_checked_utc")
        if self.check_in_progress:
            parts.append("Comparing local files with Hugging Face")
        elif last_checked:
            parts.append(f"Checked {format_ago(last_checked, now=now_utc)}")
        due = getattr(self, "next_check_due", None)
        if due is not None and not self.check_in_progress:
            remaining = (due - now_utc).total_seconds()
            if remaining > 0:
                parts.append(f"next check in {format_duration(remaining)}")
        if tracked and not self.check_in_progress:
            parts.append(f"{plural(tracked, 'model')} tracked")
        if snoozed_count and not pending:
            parts.append(f"{snoozed_count} snoozed")
        self.subline_var.set("  ·  ".join(parts))

        icon_label = getattr(self, "header_icon", None)
        if icon_label is not None and theme is not None:
            p = theme.palette
            color = {
                "accent": p.accent, "danger": p.danger, "warn": p.warn,
                "ok": p.ok, "muted": p.subtle,
            }[tone]
            try:
                icon_label.configure(text=theme.glyph(icon), fg=color)
            except tk.TclError:
                pass

    def _format_status(self, alert: dict, status_raw: str, now_utc: datetime) -> str:
        update_status = self.update_model_status.get(alert.get("model_key"))
        if update_status:
            return update_status
        check_status = alert.get("check_status")
        if check_status == "removed-remote":
            if status_raw == "snoozed":
                until = alert.get("snoozed_until_utc")
                if until:
                    return f"Removed upstream · snoozed until {format_clock(until, now_utc)}"
                return "Removed upstream · snoozed"
            if status_raw == "acknowledged":
                return "Removed upstream · dismissed"
            return "Removed upstream"
        if status_raw == "pending":
            if any(
                a.get("status") == "missing-local" for a in alert.get("artifacts") or []
            ):
                return "File missing locally"
            return "Update available"
        if status_raw == "snoozed":
            until = alert.get("snoozed_until_utc")
            if until:
                return f"Snoozed until {format_clock(until, now_utc)}"
            return "Snoozed"
        if status_raw == "acknowledged":
            return "Update available · dismissed"
        return status_raw

    # ----- Actions -----

    def selected_model_keys(self) -> list[str]:
        if self.tree is None:
            return []
        return [iid for iid in self.tree.selection() if iid in self._alerts_by_key]

    def _remember_for_undo(self, keys: list[str], verb: str) -> None:
        alerts = self.state.get("alerts", {})
        snapshot = {
            key: (alerts[key].get("status"), alerts[key].get("snoozed_until_utc"))
            for key in keys
            if isinstance(alerts.get(key), dict)
        }
        self._undo_snapshot = snapshot or None
        if snapshot:
            self.status_var.set(f"{verb} {plural(len(snapshot), 'model')}.")
        button = getattr(self, "undo_button", None)
        if button is not None:
            try:
                if snapshot:
                    button.pack(side=tk.LEFT, padx=(self.theme.px(6), 0))
                else:
                    button.pack_forget()
            except tk.TclError:
                pass

    def undo_last_triage(self) -> None:
        snapshot = getattr(self, "_undo_snapshot", None)
        self._undo_snapshot = None
        button = getattr(self, "undo_button", None)
        if button is not None:
            try:
                button.pack_forget()
            except tk.TclError:
                pass
        if not snapshot:
            return
        self.state = set_alert_statuses(self.state, snapshot)
        save_state(self.state_path, self.state)
        self.status_var.set("Undone.")
        self.refresh_ui()

    def _triage_keys(self, keys: list[str] | None) -> list[str]:
        alerts = self.state.get("alerts", {})
        return [key for key in (keys or []) if isinstance(alerts.get(key), dict)]

    def acknowledge_selected(self) -> None:
        selected = self.selected_model_keys()
        if not selected:
            return
        self._remember_for_undo(selected, "Dismissed")
        self.state = acknowledge_alerts(self.state, selected)
        save_state(self.state_path, self.state)
        self.refresh_ui()

    def snooze_selected(self) -> None:
        selected = self.selected_model_keys()
        if not selected:
            return
        self._remember_for_undo(selected, f"Snoozed for {plural(self.snooze_hours, 'hour')}:")
        self.state = snooze_alerts(
            self.state,
            now_utc=datetime.now(timezone.utc),
            hours=self.snooze_hours,
            model_keys=selected,
        )
        save_state(self.state_path, self.state)
        self.refresh_ui()

    def restore_selected(self) -> None:
        selected = self.selected_model_keys()
        if not selected:
            return
        self._remember_for_undo(selected, "Moved back")
        self.state = restore_alerts(self.state, selected)
        save_state(self.state_path, self.state)
        self.refresh_ui()

    def acknowledge_all(self) -> None:
        keys = self._triage_keys(
            [a.get("model_key") for a in pending_alerts(self.state, datetime.now(timezone.utc))]
        )
        if self.window is not None and keys:
            self._remember_for_undo(keys, "Dismissed")
        self.state = acknowledge_alerts(self.state, keys or None) if keys else self.state
        save_state(self.state_path, self.state)
        self.refresh_ui()

    def snooze_all(self) -> None:
        keys = self._triage_keys(
            [a.get("model_key") for a in pending_alerts(self.state, datetime.now(timezone.utc))]
        )
        if not keys:
            return
        if self.window is not None:
            self._remember_for_undo(keys, f"Snoozed for {plural(self.snooze_hours, 'hour')}:")
        self.state = snooze_alerts(
            self.state,
            now_utc=datetime.now(timezone.utc),
            hours=self.snooze_hours,
            model_keys=keys,
        )
        save_state(self.state_path, self.state)
        self.refresh_ui()

    def dismiss_recovery_notice(self) -> None:
        if "recovery_notice" in self.state:
            self.state.pop("recovery_notice", None)
            save_state(self.state_path, self.state)
        self.refresh_ui()

    def dismiss_plan_error(self) -> None:
        self._plan_error = None
        self.refresh_ui()

    def dismiss_update_banner(self, job_id) -> None:
        self._dismissed_update_job = job_id
        self.refresh_ui()

    def refresh_ui(self) -> None:
        self.refresh_tree()
        self.refresh_tray_icon()
        self._refresh_action_states()

    # ----- Tray icon -----

    def refresh_tray_icon(self) -> None:
        now_utc = datetime.now(timezone.utc)
        pending = pending_alerts(self.state, now_utc)
        pending_count = len(pending)
        notice = self.state.get("recovery_notice")
        recovery_attention = isinstance(notice, dict) and bool(notice.get("attention"))
        busy = self.recovery_in_progress or self.check_in_progress or self.update_in_progress
        removed_only = bool(pending) and all(
            a.get("check_status") == "removed-remote" for a in pending
        )
        self.icon.icon = self.make_icon_image(
            max(pending_count, int(recovery_attention)),
            busy=busy,
            removed_only=removed_only and not recovery_attention,
        )
        self.icon.title = self.tray_tooltip_text()
        self.icon.update_menu()

    def tray_tooltip_text(self) -> str:
        """Tray hover text. Windows truncates at 127 characters."""
        now_utc = datetime.now(timezone.utc)
        pending = pending_alerts(self.state, now_utc)
        notice = self.state.get("recovery_notice")
        updates = sum(1 for a in pending if a.get("check_status") == "update-available")
        removed = len(pending) - updates
        if self.update_in_progress:
            status = "Updating models…"
        elif self.recovery_in_progress:
            status = "Checking an interrupted update…"
        elif self.check_in_progress:
            status = "Checking for updates…"
        elif isinstance(notice, dict) and notice.get("attention"):
            status = "Interrupted update needs attention"
        elif updates or removed:
            parts = []
            if updates:
                parts.append(f"{plural(updates, 'update')} available")
            if removed:
                parts.append(f"{removed} removed upstream")
            status = ", ".join(parts)
        elif self.state.get("last_error"):
            status = "Last check failed"
        else:
            status = "All models up to date"
        last_checked = self.state.get("last_checked_utc")
        when = f"\nChecked {format_ago(last_checked, now=now_utc)}" if last_checked else ""
        return f"Weight Watcher — {status}{when}"[:127]

    def make_icon_image(self, pending_count: int, *, busy: bool, removed_only: bool = False):
        if busy:
            kind = "busy"
        elif pending_count <= 0:
            kind = "ok"
        elif removed_only:
            kind = "removed"
        else:
            kind = "pending"
        return tray_icon_image(kind, pending_count)

    def quit(self) -> None:
        if self.shutting_down:
            return
        if getattr(self, "recovery_in_progress", False):
            self.quit_after_update = True
            if hasattr(self, "status_var"):
                self.status_var.set(
                    "Finishing recovery safely; the app will close afterward."
                )
            return
        if getattr(self, "update_in_progress", False):
            commit_started = getattr(self, "update_commit_started", None)
            if (
                not getattr(self, "update_cancellable", False)
                or (commit_started is not None and commit_started.is_set())
            ):
                self.quit_after_update = True
                if hasattr(self, "status_var"):
                    self.status_var.set(
                        "Finishing installation safely; the app will close afterward."
                    )
                return
            cancellation = getattr(self, "update_cancellation", None)
            if cancellation is not None:
                cancellation.cancel()
        self.shutting_down = True
        if self.next_check_token is not None:
            try:
                self.root.after_cancel(self.next_check_token)
            except tk.TclError:
                pass
            self.next_check_token = None
        self.icon.stop()
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass


# ----- Helpers -----


def alert_kind(alert: dict, update_status: str | None = None) -> str:
    """Visual kind for status dots and colors."""
    if update_status:
        if update_status.startswith(("Failed", "Waiting")):
            return "failed"
        if update_status == "Updated":
            return "ok"
        if update_status != "Cancelled":
            return "busy"
    status = alert.get("status", "pending")
    if status == "snoozed":
        return "snoozed"
    if status == "acknowledged":
        return "dismissed"
    if alert.get("check_status") == "removed-remote":
        return "removed"
    return "update"


def alert_sort_key(alert: dict) -> tuple:
    """Updates first, then removed upstream, then snoozed, then dismissed."""
    status = alert.get("status", "pending")
    if status == "pending":
        group = 1 if alert.get("check_status") == "removed-remote" else 0
    else:
        group = {"snoozed": 2, "acknowledged": 3}.get(status, 4)
    return (group, str(alert.get("display_name") or alert.get("model_key") or "").lower())


def filter_matches(filter_key: str, alert: dict) -> bool:
    status = alert.get("status", "pending")
    return {
        "all": True,
        "attention": status == "pending",
        "snoozed": status == "snoozed",
        "dismissed": status == "acknowledged",
    }.get(filter_key, False)


def fit_text(text: str, font, width: int) -> str:
    """Shorten ``text`` with a trailing ellipsis so it fits ``width`` pixels."""
    if width <= 0 or font.measure(text) <= width:
        return text
    ellipsis = "\u2026"
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if font.measure(text[:mid].rstrip() + ellipsis) <= width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + ellipsis if low else ellipsis


def format_clock(value, now_utc: datetime) -> str:
    """'21:43' today, otherwise '13 Nov, 21:43'."""
    local = to_local(value)
    if local is None:
        return str(value)
    if local.date() == now_utc.astimezone().date():
        return local.strftime("%H:%M")
    return f"{local.day} {local.strftime('%b, %H:%M')}"


def split_remote_repo(remote_repo: str | None) -> tuple[str, str]:
    """Split org/repo into (uploader, repository tail)."""
    if not remote_repo:
        return ("—", "—")
    parts = remote_repo.split("/", 1)
    if len(parts) == 1:
        return (parts[0], "—")
    return (parts[0], parts[1])


def remote_file_basename(remote_file: str | None) -> str:
    if not remote_file:
        return "—"
    name = Path(remote_file).name
    return name or remote_file


def hf_repo_url(repo: str | None) -> str:
    """Stable repo URL for a Hugging Face org/repo, e.g. unsloth/Foo-GGUF."""
    if not repo:
        return ""
    return f"https://huggingface.co/{repo.strip().strip('/')}"


def hf_file_url(repo: str | None, remote_file: str | None) -> str:
    """Best-effort direct file URL. May 404 for files nested in subdirs."""
    base = hf_repo_url(repo)
    if not base or not remote_file:
        return base
    return f"{base}/blob/main/{remote_file.lstrip('/')}"


def open_url(url: str) -> None:
    if not url:
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass


def format_alert_detail(alert: dict | None) -> str:
    if not alert:
        return "Selection details unavailable."

    lines: list[str] = []
    uploader, repo_tail = split_remote_repo(alert.get("remote_repo"))
    remote_repo = alert.get("remote_repo")
    remote_file = alert.get("remote_file")

    if uploader != "—":
        lines.append(f"Uploader: {uploader}")
    if remote_repo:
        lines.append(f"Repository: {remote_repo}")
    elif repo_tail != "—":
        lines.append(f"Repository: {repo_tail}")
    if remote_file:
        lines.append(f"Remote file: {remote_file}")
    if alert.get("local_path"):
        lines.append(f"Local file: {alert['local_path']}")
    publisher = alert.get("publisher")
    if publisher:
        lines.append(f"LM Studio publisher: {publisher}")
    remote_blob = alert.get("remote_sha256")
    if remote_blob:
        method = alert.get("hash_method") or "lfs-oid"
        lines.append(f"Remote blob: {remote_blob[:12]}…  (verified by {method})")

    display = alert.get("display_name") or alert.get("model_key")
    if display:
        lines.insert(0, f"Model: {display}")

    return "\n".join(lines) if lines else "No source metadata for this alert."


def humanize_delta(delta_seconds: float) -> str:
    if delta_seconds is None:
        return "—"
    direction = "newer remote" if delta_seconds >= 0 else "newer local"
    seconds = abs(int(delta_seconds))
    if seconds < 60:
        value = f"{seconds}s"
    elif seconds < 3600:
        value = f"{seconds // 60}m"
    elif seconds < 86400:
        value = f"{seconds // 3600}h"
    else:
        value = f"{seconds // 86400}d"
    return f"{value} · {direction}"


def format_iso_friendly(value: str | datetime | None) -> str:
    """Render UTC ISO-ish strings / datetimes in local time, concisely."""
    if value is None:
        return "—"
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(value)


def format_local(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    raise SystemExit(main())
