from __future__ import annotations  
  
import argparse
from copy import deepcopy
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
from tkinter import ttk, font as tkfont, messagebox
  
import pystray  
from PIL import Image, ImageDraw, ImageFont  
  
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
    save_state,
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
    InstallError,
    ProgressEvent,
    UpdateCancelled,
    UpdateExecutor,
    UpdatePlan,
    build_update_plan,
    collect_check_results,
    format_bytes,
    recover_interrupted_jobs,
    redact_sensitive,
)
  
DEFAULT_CHECK_INTERVAL_HOURS = 6  
DEFAULT_REMINDER_INTERVAL_MINUTES = 60  
DEFAULT_SNOOZE_HOURS = 4  
DEFAULT_TIMEOUT_SECONDS = 30
APP_NAME = "LM Studio Weight Watcher"
  
# Color palette (light, neutral, desktop-friendly)  
COLOR_BG = "#f4f5f7"  
COLOR_CARD = "#ffffff"  
COLOR_BORDER = "#dfe2e6"  
COLOR_TEXT = "#1f2328"  
COLOR_MUTED = "#656d76"  
COLOR_ACCENT = "#0969da"  
COLOR_DANGER = "#cf222e"  
COLOR_WARN = "#bf8700"  
COLOR_OK = "#1a7f37"  
COLOR_SNOOZE = "#8250df"  
COLOR_DETAIL_BG = "#eef6ff"
COLOR_DETAIL_BORDER = "#c8dff8"
  
ROW_PENDING_BG = "#fff5f5"  
ROW_SNOOZED_BG = "#f7f4ff"  
ROW_OK_BG = "#ffffff"  
  
  
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
        self.topmost_reset_token = None  
        self.window = None  
        self.tree = None  
  
        self.root = tk.Tk()  
        self.root.withdraw()  
  
        # Tk variables for header/status  
        self.headline_var = tk.StringVar(master=self.root, value="Starting...")  
        self.subline_var = tk.StringVar(master=self.root, value="")  
        self.pending_count_var = tk.StringVar(master=self.root, value="0")  
        self.snoozed_count_var = tk.StringVar(master=self.root, value="0")  
        self.unresolved_count_var = tk.StringVar(master=self.root, value="0")  
        self.checked_count_var = tk.StringVar(master=self.root, value="0")  
        self.status_var = tk.StringVar(master=self.root, value="Starting...")  
        self.selection_var = tk.StringVar(master=self.root, value="No selection")
        self.update_progress_var = tk.DoubleVar(master=self.root, value=0.0)
        self._alerts_by_key: dict[str, dict] = {}  
  
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
            pystray.MenuItem("Open Alerts", self.on_open_alerts, default=True),  
            pystray.MenuItem("Check Now", self.on_check_now),  
            pystray.MenuItem("Update All Pending", self.on_update_all),
            pystray.MenuItem("Cancel Update", self.on_cancel_update),
            pystray.MenuItem("Acknowledge All", self.on_acknowledge_all),  
            pystray.MenuItem(  
                lambda item: f"Snooze All ({self.snooze_hours}h)",  
                self.on_snooze_all,  
            ),  
            pystray.Menu.SEPARATOR,  
            pystray.MenuItem("Quit", self.on_quit),  
        )  
  
    def menu_status_text(self) -> str:  
        now_utc = datetime.now(timezone.utc)  
        pending_count = len(pending_alerts(self.state, now_utc))  
        summary = self.state.get("last_summary", {})  
        checked = summary.get("checked", 0)  
        if self.recovery_in_progress:
            return "Checking interrupted update recovery..."
        if self.update_in_progress:
            return f"Updating models... ({pending_count} pending)"
        if self.check_in_progress:  
            return f"Checking models... (last total {checked})"  
        return f"{pending_count} pending · {checked} models tracked"  
  
    def on_open_alerts(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:  
        self.root.after(0, lambda: self.show_window(force_topmost=True))  
  
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
        self.status_var.set("Checking for remote updates...")  
        self.refresh_tray_icon()  
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
  
        self.status_var.set(  
            f"Last checked {format_local(outcome.generated_at_utc)}"  
            + (f"  ·  Error: {outcome.error}" if outcome.error else "")  
        )  
        self.refresh_ui()  
        self.maybe_raise_pending_window(force=False)  
        if reschedule:  
            self.schedule_next_check()  
  
    def schedule_next_check(self) -> None:  
        if self.shutting_down:  
            return  
        if self.next_check_token is not None:  
            self.root.after_cancel(self.next_check_token)  
            self.next_check_token = None  
        delay_ms = int(self.check_interval.total_seconds() * 1000)  
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
        if outcome.error:
            self.status_var.set(f"Recovery check failed: {outcome.error}")
        active = self.state.get("active_update")
        if not outcome.error and isinstance(active, dict):
            restored = sum(action.action == "restored" for action in outcome.actions)
            message = "Previous update was interrupted."
            if restored:
                message += f" Restored {restored} file(s) from rollback."
            elif outcome.actions:
                message += " Recovery details require attention."
            self.state = record_update_finished(
                self.state,
                success=False,
                message=message,
                now_utc=datetime.now(timezone.utc),
            )
            save_state(self.state_path, self.state)
            self.status_var.set(message)
        self.refresh_ui()
        if self.quit_after_update:
            self.quit_after_update = False
            self.quit()
            return
        self.run_check_async(reschedule=True)
        self.maybe_raise_pending_window(force=False)

    def update_selected(self) -> None:
        self.prepare_update_async(self.selected_model_keys())

    def update_all_pending(self) -> None:
        keys = [
            alert.get("model_key")
            for alert in pending_alerts(self.state, datetime.now(timezone.utc))
            if alert.get("model_key")
        ]
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
        self.status_var.set("Preparing update plan...")
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
                key: (
                    "Cancelled"
                    if cancelled
                    else f"Failed · {error or 'Could not build update plan'}"
                )
                for key in self.update_model_status
            }
            self.status_var.set(
                "Update cancelled before download."
                if cancelled
                else f"Update not started: {error or 'unknown planning error'}"
            )
            self.refresh_ui()
            return
        self.active_update_plan = plan
        self.last_models_root = plan.models_root
        details = [
            f"Models: {len(plan.selected_model_keys)}",
            f"Files: {len(plan.artifacts)}",
            f"Download remaining: {format_bytes(plan.remaining_download_bytes)}",
            f"Total verified install: {format_bytes(plan.total_bytes)}",
            f"Available space: {format_bytes(plan.available_bytes)}",
            "",
        ]
        details.extend(f"• {name}" for name in plan.selected_model_names)
        commit_titles = sorted(
            {
                str(self.state.get("alerts", {}).get(key, {}).get("last_commit_title"))
                for key in plan.selected_model_keys
                if self.state.get("alerts", {}).get(key, {}).get("last_commit_title")
            }
        )
        if commit_titles:
            details.extend(["", "Upstream changes:"])
            details.extend(f"• {title}" for title in commit_titles)
        try:
            confirmed = messagebox.askyesno(
                "Confirm model update",
                "\n".join(details),
                parent=self.window if self.window and self.window.winfo_exists() else None,
            )
        except tk.TclError as exc:
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
        self.status_var.set("Update queued...")
        self.update_thread = threading.Thread(
            target=self.update_worker,
            args=(plan, self.update_cancellation),
            daemon=True,
        )
        self.update_thread.start()
        self.refresh_ui()

    def update_worker(self, plan: UpdatePlan, cancellation: CancellationToken) -> None:
        hash_cache = load_hash_cache(self.hash_cache_path)
        latest_results: list = []

        def validate(completed_plan: UpdatePlan) -> None:
            nonlocal latest_results
            latest_results = collect_check_results(
                models_root=completed_plan.models_root,
                timeout_seconds=self.timeout_seconds,
                hash_cache=hash_cache,
            )
            by_key = {result.model_key: result for result in latest_results}
            failures = [
                f"{key}: {by_key.get(key).status if by_key.get(key) else 'missing'}"
                for key in completed_plan.selected_model_keys
                if key not in by_key or by_key[key].status != "up-to-date"
            ]
            if failures:
                raise InstallError(
                    "Post-install checker did not confirm success: " + ", ".join(failures)
                )

        try:
            result = UpdateExecutor(
                plan,
                cancellation=cancellation,
                progress=self.post_update_progress,
                hash_cache=hash_cache,
            ).execute(post_install_validator=validate)
            outcome = UpdateWorkerOutcome(True, False, result.message, latest_results)
        except UpdateCancelled as exc:
            outcome = UpdateWorkerOutcome(False, True, redact_sensitive(exc), [])
        except Exception as exc:
            try:
                latest_results = collect_check_results(
                    models_root=plan.models_root,
                    timeout_seconds=self.timeout_seconds,
                    hash_cache=hash_cache,
                )
            except Exception:
                latest_results = []
            outcome = UpdateWorkerOutcome(False, False, redact_sensitive(exc), latest_results)
        finally:
            save_hash_cache(self.hash_cache_path, hash_cache)

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
        status_text = {
            "queued": "Queued",
            "downloading": "Downloading",
            "verifying": "Verifying",
            "installing": "Installing",
            "validating": "Validating",
            "completed": "Updated",
            "cancelled": "Cancelled",
            "failed": "Failed",
        }.get(event.phase, event.phase.replace("-", " ").title())
        for key in event.model_keys or tuple(self.update_model_status):
            self.update_model_status[key] = status_text
        if event.bytes_total:
            self.update_progress_var.set(
                min(100.0, 100.0 * event.bytes_completed / event.bytes_total)
            )
            self.status_var.set(
                f"{event.message} · {format_bytes(event.bytes_completed)} / "
                f"{format_bytes(event.bytes_total)}"
            )
        else:
            self.status_var.set(event.message)
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
            self._refresh_action_states()

    def finish_update(self, outcome: UpdateWorkerOutcome) -> None:
        if self.shutting_down:
            return
        self.update_in_progress = False
        self.update_cancellable = False
        self.update_cancellation = None
        self.update_thread = None
        self.update_commit_started.clear()
        self.active_update_plan = None
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
            self.status_var.set(outcome.message)
            self.update_model_status = {
                key: "Updated" for key in self.update_model_status
            }
            self.update_progress_var.set(100.0)
        elif outcome.cancelled:
            self.status_var.set("Update cancelled; installed files were not changed.")
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
        self.status_var.set("Cancelling update safely...")
        self.refresh_ui()
  
    def maybe_raise_pending_window(self, *, force: bool) -> None:  
        now_utc = datetime.now(timezone.utc)  
        if force or reminder_due(  
            self.state,  
            now_utc=now_utc,  
            reminder_interval=self.reminder_interval,  
        ):  
            self.show_window(force_topmost=True)  
            self.root.bell()  
            self.state = record_reminder(self.state, now_utc)  
            save_state(self.state_path, self.state)  
  
    # ----- Window -----  
  
    def show_window(self, *, force_topmost: bool) -> None:  
        if self.shutting_down:  
            return  
        if self.window is None or not self.window.winfo_exists():  
            self.create_window()  
  
        assert self.window is not None  
        self.refresh_tree()  
        self.window.deiconify()  
        self.window.lift()  
        if force_topmost:  
            self.window.attributes("-topmost", True)  
            self._schedule_topmost_reset()  
        self.window.focus_force()  
  
    def hide_window(self) -> None:  
        if self.window and self.window.winfo_exists():  
            self.window.withdraw()  

    def _schedule_topmost_reset(self) -> None:  
        if self.window is None or not self.window.winfo_exists():  
            return  
        if self.topmost_reset_token is not None:  
            try:  
                self.window.after_cancel(self.topmost_reset_token)  
            except tk.TclError:  
                pass  
            self.topmost_reset_token = None  
        self.topmost_reset_token = self.window.after(1500, self._clear_topmost)  

    def _clear_topmost(self) -> None:  
        self.topmost_reset_token = None  
        if self.shutting_down or self.window is None or not self.window.winfo_exists():  
            return  
        self.window.attributes("-topmost", False)  
  
    def _configure_styles(self) -> None:  
        style = ttk.Style(self.window)  
        try:  
            style.theme_use("clam")  
        except tk.TclError:  
            pass  
  
        default_family = tkfont.nametofont("TkDefaultFont").actual("family")  
  
        self._fonts = {  
            "headline": tkfont.Font(family=default_family, size=14, weight="bold"),  
            "subline": tkfont.Font(family=default_family, size=9),  
            "metric_value": tkfont.Font(family=default_family, size=18, weight="bold"),  
            "metric_label": tkfont.Font(family=default_family, size=9),  
            "section": tkfont.Font(family=default_family, size=10, weight="bold"),  
            "mono": tkfont.Font(family="Consolas", size=9),  
        }  
  
        style.configure("App.TFrame", background=COLOR_BG)  
        style.configure("Card.TFrame", background=COLOR_CARD, relief="flat")  
        style.configure("Toolbar.TFrame", background=COLOR_BG)  
  
        style.configure("Headline.TLabel", background=COLOR_CARD,  
                        foreground=COLOR_TEXT, font=self._fonts["headline"])  
        style.configure("Subline.TLabel", background=COLOR_CARD,  
                        foreground=COLOR_MUTED, font=self._fonts["subline"])  
        style.configure("MetricValue.TLabel", background=COLOR_CARD,  
                        foreground=COLOR_TEXT, font=self._fonts["metric_value"])  
        style.configure("MetricValuePending.TLabel", background=COLOR_CARD,  
                        foreground=COLOR_DANGER, font=self._fonts["metric_value"])  
        style.configure("MetricValueSnoozed.TLabel", background=COLOR_CARD,  
                        foreground=COLOR_SNOOZE, font=self._fonts["metric_value"])  
        style.configure("MetricValueUnresolved.TLabel", background=COLOR_CARD,  
                        foreground=COLOR_WARN, font=self._fonts["metric_value"])  
        style.configure("MetricValueOk.TLabel", background=COLOR_CARD,  
                        foreground=COLOR_OK, font=self._fonts["metric_value"])  
        style.configure("MetricLabel.TLabel", background=COLOR_CARD,  
                        foreground=COLOR_MUTED, font=self._fonts["metric_label"])  
        style.configure("Section.TLabel", background=COLOR_BG,  
                        foreground=COLOR_TEXT, font=self._fonts["section"])  
        style.configure("Status.TLabel", background=COLOR_BG,  
                        foreground=COLOR_MUTED, font=self._fonts["subline"])  
        style.configure("Selection.TLabel", background=COLOR_BG,  
                        foreground=COLOR_MUTED, font=self._fonts["subline"])  
        style.configure(
            "Hint.TLabel",
            background=COLOR_BG,
            foreground=COLOR_MUTED,
            font=self._fonts["subline"],
        )
  
        # Treeview  
        style.configure(  
            "Alerts.Treeview",  
            background=COLOR_CARD,  
            fieldbackground=COLOR_CARD,  
            foreground=COLOR_TEXT,  
            rowheight=32,  
            borderwidth=0,  
        )  
        style.configure(  
            "Alerts.Treeview.Heading",  
            background=COLOR_BG,  
            foreground=COLOR_MUTED,  
            relief="flat",  
            font=(default_family, 9, "bold"),  
            padding=(8, 6),  
        )  
        style.map("Alerts.Treeview.Heading", background=[("active", COLOR_BORDER)])  
        style.map(  
            "Alerts.Treeview",  
            background=[("selected", "#d0e4ff")],  
            foreground=[("selected", COLOR_TEXT)],  
        )  
  
        style.configure("Primary.TButton", padding=(10, 6))  
        style.configure("TButton", padding=(10, 6))  
  
    def create_window(self) -> None:  
        self.window = tk.Toplevel(self.root)  
        self.window.title(APP_NAME)  
        self.window.geometry("1180x680")  
        self.window.minsize(960, 540)  
        self.window.configure(bg=COLOR_BG)  
        self.window.protocol("WM_DELETE_WINDOW", self.hide_window)  
  
        self._configure_styles()  
  
        outer = ttk.Frame(self.window, style="App.TFrame", padding=14)  
        outer.pack(fill=tk.BOTH, expand=True)  
  
        # --- Header card ---  
        header = ttk.Frame(outer, style="Card.TFrame", padding=16)  
        header.pack(fill=tk.X)  
        self._add_card_border(header)  
  
        header.columnconfigure(0, weight=1)  
        header.columnconfigure(1, weight=0)  
  
        title_block = ttk.Frame(header, style="Card.TFrame")  
        title_block.grid(row=0, column=0, sticky="nsew")  
        ttk.Label(title_block, textvariable=self.headline_var,  
                  style="Headline.TLabel").pack(anchor="w")  
        ttk.Label(title_block, textvariable=self.subline_var,  
                  style="Subline.TLabel").pack(anchor="w", pady=(2, 0))  
  
        metrics = ttk.Frame(header, style="Card.TFrame")  
        metrics.grid(row=0, column=1, sticky="e")  
  
        self._make_metric(metrics, "Pending", self.pending_count_var,  
                          "MetricValuePending.TLabel", col=0)  
        self._make_metric(metrics, "Snoozed", self.snoozed_count_var,  
                          "MetricValueSnoozed.TLabel", col=1)  
        self._make_metric(metrics, "Unresolved", self.unresolved_count_var,  
                          "MetricValueUnresolved.TLabel", col=2)  
        self._make_metric(metrics, "Tracked", self.checked_count_var,  
                          "MetricValue.TLabel", col=3)  
  
        # --- Alerts section ---
        alerts_card = ttk.Frame(outer, style="Card.TFrame", padding=1)  
        alerts_card.pack(fill=tk.BOTH, expand=True, pady=(14, 0))  
        self._add_card_border(alerts_card)  
  
        tree_wrap = ttk.Frame(alerts_card, style="Card.TFrame")  
        tree_wrap.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)  
  
        columns = (
            "model",
            "uploader",
            "repository",
            "remote_file",
            "status",
            "remote_modified",
            "delta",
        )
        self.tree = ttk.Treeview(  
            tree_wrap,  
            columns=columns,  
            show="headings",  
            style="Alerts.Treeview",  
            selectmode="extended",  
        )  
        self.tree.heading("model", text="  Model")  
        self.tree.heading("uploader", text="Uploader")  
        self.tree.heading("repository", text="HF Repository")  
        self.tree.heading("remote_file", text="File")  
        self.tree.heading("status", text="Status")  
        self.tree.heading("remote_modified", text="Remote Updated")  
        self.tree.heading("delta", text="Time Delta")  
        self.tree.column("model", width=220, anchor="w", stretch=True)  
        self.tree.column("uploader", width=130, anchor="w", stretch=False)  
        self.tree.column("repository", width=240, anchor="w", stretch=True)  
        self.tree.column("remote_file", width=200, anchor="w", stretch=True)  
        self.tree.column("status", width=150, anchor="w", stretch=False)  
        self.tree.column("remote_modified", width=130, anchor="w", stretch=False)  
        self.tree.column("delta", width=120, anchor="w", stretch=False)  
  
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)  
        hsb = ttk.Scrollbar(tree_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_wrap.rowconfigure(0, weight=1)
        tree_wrap.columnconfigure(0, weight=1)  
  
        # Row tags for color coding  
        self.tree.tag_configure("pending", background=ROW_PENDING_BG, foreground=COLOR_TEXT)  
        self.tree.tag_configure("snoozed", background=ROW_SNOOZED_BG, foreground=COLOR_TEXT)  
        self.tree.tag_configure("acknowledged", background=ROW_OK_BG, foreground=COLOR_MUTED)  
        self.tree.tag_configure("empty", foreground=COLOR_MUTED)  
  
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)  
        self.tree.bind("<Double-1>", lambda e: self.update_selected())

        detail_card = tk.Frame(
            alerts_card,
            bg=COLOR_DETAIL_BG,
            highlightbackground=COLOR_DETAIL_BORDER,
            highlightthickness=1,
        )
        detail_card.pack(fill=tk.X, padx=10, pady=(0, 10))
        detail_inner = tk.Frame(detail_card, bg=COLOR_DETAIL_BG, padx=12, pady=10)
        detail_inner.pack(fill=tk.X)
        tk.Label(
            detail_inner,
            text="Selected",
            bg=COLOR_DETAIL_BG,
            fg=COLOR_MUTED,
            font=(self._fonts["subline"].actual("family"), 9, "bold"),
        ).pack(anchor="w")
        self.detail_text = tk.Text(
            detail_inner,
            wrap="word",
            relief="flat",
            borderwidth=0,
            height=10,
            bg=COLOR_DETAIL_BG,
            fg=COLOR_TEXT,
            font=self._fonts["mono"],
            padx=0,
            pady=0,
            highlightthickness=0,
            cursor="xterm",
        )
        self.detail_text.pack(anchor="w", fill=tk.X, pady=(4, 0))
        self.detail_text.tag_configure("muted", foreground=COLOR_MUTED)
        self.detail_text.tag_configure(
            "link", foreground=COLOR_ACCENT, underline=True
        )
        self.detail_text.bind("<Button-1>", self._on_detail_click)
        self.detail_text.bind("<Motion>", self._on_detail_motion)
        self.detail_text.bind("<Leave>", self._on_detail_leave)
        self.detail_text.configure(state=tk.DISABLED)
  
        # --- Toolbar ---  
        toolbar = ttk.Frame(outer, style="Toolbar.TFrame")  
        toolbar.pack(fill=tk.X, pady=(10, 0))  
  
        # Left group: primary  
        left = ttk.Frame(toolbar, style="Toolbar.TFrame")  
        left.pack(side=tk.LEFT)  
        self.check_button = ttk.Button(
            left,
            text="↻  Check Now",
            command=lambda: self.run_check_async(reschedule=True),
        )
        self.check_button.pack(side=tk.LEFT)
        self.update_selected_button = ttk.Button(
            left,
            text="⇩  Update Selected",
            style="Primary.TButton",
            command=self.update_selected,
        )
        self.update_selected_button.pack(side=tk.LEFT, padx=(6, 0))
        self.update_all_button = ttk.Button(
            left,
            text="⇩  Update All Pending",
            command=self.update_all_pending,
        )
        self.update_all_button.pack(side=tk.LEFT, padx=(6, 0))
        self.cancel_update_button = ttk.Button(
            left,
            text="Cancel Update",
            command=self.cancel_update,
        )
        self.cancel_update_button.pack(side=tk.LEFT, padx=(6, 0))
  
        ttk.Separator(toolbar, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)  
  
        # Middle group: per-selection  
        mid = ttk.Frame(toolbar, style="Toolbar.TFrame")  
        mid.pack(side=tk.LEFT)  
        ttk.Button(mid, text="✓  Acknowledge Selected",  
                   command=self.acknowledge_selected).pack(side=tk.LEFT, padx=(0, 6))  
        ttk.Button(mid, text=f"⏾  Snooze Selected ({self.snooze_hours}h)",  
                   command=self.snooze_selected).pack(side=tk.LEFT)  
  
        ttk.Separator(toolbar, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)  
  
        ttk.Button(mid, text="Open on Hugging Face",
                   command=self.open_selected_on_hf).pack(side=tk.LEFT, padx=(6, 0))

        # Right-of-middle: bulk  
        bulk = ttk.Frame(toolbar, style="Toolbar.TFrame")  
        bulk.pack(side=tk.LEFT)  
        ttk.Button(bulk, text="Acknowledge All",  
                   command=self.acknowledge_all).pack(side=tk.LEFT, padx=(0, 6))  
        ttk.Button(bulk, text=f"Snooze All ({self.snooze_hours}h)",  
                   command=self.snooze_all).pack(side=tk.LEFT)  
  
        # Far right: close  
        ttk.Button(toolbar, text="Close",  
                   command=self.hide_window).pack(side=tk.RIGHT)  
  
        ttk.Label(toolbar, textvariable=self.selection_var,  
                  style="Selection.TLabel").pack(side=tk.RIGHT, padx=(0, 12))  
  
        # --- Unresolved section ---  
        ttk.Label(outer, text="DIAGNOSTICS",  
                  style="Section.TLabel").pack(anchor="w", pady=(14, 6))  
  
        unresolved_card = ttk.Frame(outer, style="Card.TFrame", padding=12)  
        unresolved_card.pack(fill=tk.X)  
        self._add_card_border(unresolved_card)  
  
        self.unresolved_text = tk.Text(  
            unresolved_card,  
            height=5,  
            wrap="word",  
            relief="flat",  
            borderwidth=0,  
            background=COLOR_CARD,  
            foreground=COLOR_TEXT,  
            font=self._fonts["mono"],  
            padx=4,  
            pady=4,  
        )  
        self.unresolved_text.pack(fill=tk.BOTH, expand=True)  
        self.unresolved_text.tag_configure("error_heading",  
                                           foreground=COLOR_DANGER,  
                                           font=(self._fonts["mono"].actual("family"), 9, "bold"))  
        self.unresolved_text.tag_configure("error_body", foreground=COLOR_DANGER)  
        self.unresolved_text.tag_configure("unresolved_heading",  
                                           foreground=COLOR_WARN,  
                                           font=(self._fonts["mono"].actual("family"), 9, "bold"))  
        self.unresolved_text.tag_configure("muted", foreground=COLOR_MUTED)  
        self.unresolved_text.tag_configure("name", foreground=COLOR_TEXT,  
                                           font=(self._fonts["mono"].actual("family"), 9, "bold"))  
        self.unresolved_text.configure(state=tk.DISABLED)  
  
        # --- Status bar ---  
        statusbar = ttk.Frame(outer, style="Toolbar.TFrame")  
        statusbar.pack(fill=tk.X, pady=(10, 0))  
        ttk.Label(statusbar, textvariable=self.status_var,  
                  style="Status.TLabel").pack(side=tk.LEFT)  
        self.update_progressbar = ttk.Progressbar(
            statusbar,
            variable=self.update_progress_var,
            maximum=100.0,
            length=220,
            mode="determinate",
        )
        self.update_progressbar.pack(side=tk.RIGHT)
  
    def _add_card_border(self, frame: ttk.Frame) -> None:  
        """Simulate a 1px card border using a tk.Frame highlight."""  
        try:  
            frame.configure(borderwidth=1, relief="solid")  
        except tk.TclError:  
            pass  
  
    def _make_metric(self, parent, label: str, var: tk.StringVar,  
                     value_style: str, *, col: int) -> None:  
        cell = ttk.Frame(parent, style="Card.TFrame", padding=(18, 0))  
        cell.grid(row=0, column=col, sticky="ns")  
        ttk.Label(cell, textvariable=var, style=value_style).pack(anchor="e")  
        ttk.Label(cell, text=label, style="MetricLabel.TLabel").pack(anchor="e")  
  
    def _on_tree_select(self, _event=None) -> None:  
        selected = self.selected_model_keys()  
        count = len(selected)  
        if count == 0:  
            self.selection_var.set("No selection")  
            self._render_detail(None, selection_text="Select a row to see the source and the upstream commit.")
        elif count == 1:  
            self.selection_var.set("1 model selected")  
            alert = self._alerts_by_key.get(selected[0])
            self._render_detail(alert)
        else:  
            self.selection_var.set(f"{count} models selected")
            uploaders = sorted(
                {
                    uploader
                    for key in selected
                    if key in self._alerts_by_key
                    for uploader in [
                        split_remote_repo(self._alerts_by_key[key].get("remote_repo"))[0]
                    ]
                    if uploader != "—"
                }
            )
            if uploaders:
                self._render_detail(None, selection_text=f"{count} models selected · uploaders: {', '.join(uploaders)}")
            else:
                self._render_detail(None, selection_text=f"{count} models selected")
        self._refresh_action_states()

    def _refresh_action_states(self) -> None:
        busy = self.recovery_in_progress or self.check_in_progress or self.update_in_progress
        selected = bool(self.selected_model_keys())
        pending = bool(pending_alerts(self.state, datetime.now(timezone.utc)))
        controls = (
            ("check_button", not busy),
            ("update_selected_button", selected and not busy),
            ("update_all_button", pending and not busy),
            (
                "cancel_update_button",
                self.update_in_progress
                and self.update_cancellable
                and not self.update_commit_started.is_set(),
            ),
        )
        for name, enabled in controls:
            control = getattr(self, name, None)
            if control is not None:
                control.configure(state=tk.NORMAL if enabled else tk.DISABLED)
  
    def _render_detail(self, alert: dict | None, *, selection_text: str | None = None) -> None:
        if not hasattr(self, "detail_text"):
            return
        text = self.detail_text
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        if alert is None:
            text.insert("1.0", selection_text or "Select a row to see details.")
            text.configure(state=tk.DISABLED)
            return

        text.insert(tk.END, format_alert_detail(alert) + "\n")

        commit_title = alert.get("last_commit_title")
        remote_mod = alert.get("remote_modified_utc")
        if commit_title or remote_mod:
            text.insert(tk.END, "\n", "muted")
            text.insert(tk.END, "Last upstream commit: ", "muted")
            if commit_title:
                text.insert(tk.END, commit_title)
            if remote_mod:
                text.insert(tk.END, f"  ({format_iso_friendly(remote_mod)})", "muted")
            text.insert(tk.END, "\n")

        artifacts = alert.get("artifacts") or []
        if artifacts:
            text.insert(tk.END, "\n", "muted")
            text.insert(tk.END, f"Files checked ({len(artifacts)}):\n", "muted")
            for a in artifacts:
                status = a.get("status", "?")
                label = a.get("label") or a.get("remote_file") or "?"
                line = f"  - [{status}] {label}"
                ct = a.get("last_commit_title")
                if ct:
                    line += f"  (commit: {ct})"
                text.insert(tk.END, line + "\n")

        repo = alert.get("remote_repo")
        if repo:
            url = hf_repo_url(repo)
            text.insert(tk.END, "\n", "muted")
            text.insert(tk.END, "Open on Hugging Face: ", "muted")
            text.insert(tk.END, url + "\n", "link")

        text.configure(state=tk.DISABLED)

    def _on_detail_click(self, event) -> None:
        index = self.detail_text.index(f"@{event.x},{event.y}")
        if "link" not in self.detail_text.tag_names(index):
            return
        ranges = self.detail_text.tag_ranges("link")
        for i in range(0, len(ranges), 2):
            start, end = ranges[i], ranges[i + 1]
            if (self.detail_text.compare(start, "<=", index)
                    and self.detail_text.compare(index, "<", end)):
                open_url(self.detail_text.get(start, end).strip())
                return

    def _on_detail_motion(self, event) -> None:
        index = self.detail_text.index(f"@{event.x},{event.y}")
        over_link = "link" in self.detail_text.tag_names(index)
        self.detail_text.configure(cursor="hand2" if over_link else "xterm")

    def _on_detail_leave(self, _event=None) -> None:
        self.detail_text.configure(cursor="xterm")

    def open_selected_on_hf(self) -> None:
        selected = self.selected_model_keys()
        if not selected:
            return
        alert = self._alerts_by_key.get(selected[0])
        if not alert or not alert.get("remote_repo"):
            return
        open_url(hf_repo_url(alert["remote_repo"]))

    # ----- Refresh -----  
  
    def refresh_tree(self) -> None:  
        if self.tree is None:  
            return  
  
        for item in self.tree.get_children():  
            self.tree.delete(item)  
  
        now_utc = datetime.now(timezone.utc)  
        alerts = list(all_alerts(self.state, now_utc))  
  
        # Sort: pending first, then snoozed, then acknowledged; within each, by name  
        def sort_key(a):  
            status = a.get("status", "pending")  
            order = {"pending": 0, "snoozed": 1, "acknowledged": 2}.get(status, 3)  
            return (order, (a.get("display_name") or a.get("model_key", "")).lower())  
  
        alerts.sort(key=sort_key)  
        self._alerts_by_key = {alert["model_key"]: alert for alert in alerts}
  
        if not alerts:  
            self.tree.insert(  
                "", tk.END,  
                values=(
                    "  No model alerts — everything looks up to date.",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ),
                tags=("empty",),  
            )  
        else:  
            for alert in alerts:  
                status_raw = alert.get("status", "pending")  
                status_display = self._format_status(alert, status_raw, now_utc)  
                delta = alert.get("delta_seconds")  
                delta_text = humanize_delta(delta) if delta is not None else "—"  
                remote_mod = alert.get("remote_modified_utc")  
                remote_display = format_iso_friendly(remote_mod) if remote_mod else "—"  
                tag = status_raw if status_raw in ("pending", "snoozed", "acknowledged") else "pending"  
  
                name = alert.get("display_name") or alert["model_key"]  
                uploader, repo_tail = split_remote_repo(alert.get("remote_repo"))  
                remote_file = remote_file_basename(alert.get("remote_file"))  
                # Prefix bullet only for pending  
                prefix = "● " if tag == "pending" else ""  
  
                self.tree.insert(  
                    "",  
                    tk.END,  
                    iid=alert["model_key"],  
                    values=(  
                        f"  {prefix}{name}",  
                        uploader,  
                        repo_tail,  
                        remote_file,  
                        status_display,  
                        remote_display,  
                        delta_text,  
                    ),  
                    tags=(tag,),  
                )  
  
        self._refresh_unresolved()  
        self._refresh_header_metrics()  
        self._on_tree_select()  
  
    def _format_status(self, alert: dict, status_raw: str, now_utc: datetime) -> str:  
        update_status = self.update_model_status.get(alert.get("model_key"))
        if update_status:
            return update_status
        if status_raw == "pending":  
            return "Update available"  
        if status_raw == "snoozed":  
            until = alert.get("snoozed_until_utc")  
            if until:  
                return f"Snoozed · until {format_iso_friendly(until)}"  
            return "Snoozed"  
        if status_raw == "acknowledged":  
            return "Acknowledged"  
        return status_raw  
  
    def _refresh_unresolved(self) -> None:  
        if not hasattr(self, "unresolved_text"):  
            return  
        self.unresolved_text.configure(state=tk.NORMAL)  
        self.unresolved_text.delete("1.0", tk.END)  
  
        last_error = self.state.get("last_error")  
        unresolved = self.state.get("unresolved", [])  
  
        if last_error:  
            self.unresolved_text.insert(tk.END, "Last error\n", "error_heading")  
            self.unresolved_text.insert(tk.END, f"{last_error}\n", "error_body")  
            if unresolved:  
                self.unresolved_text.insert(tk.END, "\n")  
  
        if unresolved:  
            self.unresolved_text.insert(  
                tk.END,  
                f"Unresolved items ({len(unresolved)})\n",  
                "unresolved_heading",  
            )  
            for item in unresolved:  
                name = item.get("display_name") or item.get("model_key") or "(unknown)"  
                key = item.get("model_key", "")  
                msg = item.get("message", "")  
                remote_repo = item.get("remote_repo")
                self.unresolved_text.insert(tk.END, "  • ", "muted")  
                self.unresolved_text.insert(tk.END, f"{name}", "name")  
                if key and key != name:  
                    self.unresolved_text.insert(tk.END, f"  ({key})", "muted")
                if remote_repo:
                    uploader, _ = split_remote_repo(remote_repo)
                    self.unresolved_text.insert(
                        tk.END,
                        f"\n      source: {uploader} / {remote_repo}",
                        "muted",
                    )
                self.unresolved_text.insert(tk.END, f"\n      {msg}\n")  
  
        if not last_error and not unresolved:  
            self.unresolved_text.insert(  
                tk.END,  
                "All clear. No errors and no unresolved items.",  
                "muted",  
            )  
  
        self.unresolved_text.configure(state=tk.DISABLED)  
  
    def _refresh_header_metrics(self) -> None:  
        now_utc = datetime.now(timezone.utc)  
        alerts = list(all_alerts(self.state, now_utc))  
        pending_count = sum(1 for a in alerts if a.get("status") == "pending")  
        snoozed_count = sum(1 for a in alerts if a.get("status") == "snoozed")  
  
        summary = self.state.get("last_summary", {})  
        unresolved_count = summary.get("unresolved", len(self.state.get("unresolved", [])))  
        checked_count = summary.get("checked", 0)  
  
        self.pending_count_var.set(str(pending_count))  
        self.snoozed_count_var.set(str(snoozed_count))  
        self.unresolved_count_var.set(str(unresolved_count))  
        self.checked_count_var.set(str(checked_count))  
  
        # Headline + subline  
        if self.update_in_progress:
            self.headline_var.set("Updating model weights...")
        elif self.check_in_progress:
            self.headline_var.set("Checking for updates...")  
        elif pending_count > 0:  
            noun = "update" if pending_count == 1 else "updates"  
            self.headline_var.set(f"{pending_count} model {noun} available")  
        elif snoozed_count > 0:  
            self.headline_var.set("All caught up · snoozed items remain")  
        elif unresolved_count > 0 or self.state.get("last_error"):  
            self.headline_var.set("No pending updates · see diagnostics")  
        else:  
            self.headline_var.set("All models up to date")  
  
        last_checked = self.state.get("last_checked_utc")  
        last_checked_disp = format_iso_friendly(last_checked) if last_checked else "never"  
        subline = f"Last checked: {last_checked_disp}"  
        if self.last_models_root:  
            subline += f"   ·   Root: {self.last_models_root}"  
        self.subline_var.set(subline)  
  
    # ----- Actions -----  
  
    def selected_model_keys(self) -> list[str]:  
        if self.tree is None:  
            return []  
        return [iid for iid in self.tree.selection() if iid in self._alerts_by_key]
  
    def acknowledge_selected(self) -> None:  
        selected = self.selected_model_keys()  
        if not selected:  
            return  
        self.state = acknowledge_alerts(self.state, selected)  
        save_state(self.state_path, self.state)  
        self.refresh_ui()  
  
    def snooze_selected(self) -> None:  
        selected = self.selected_model_keys()  
        if not selected:  
            return  
        self.state = snooze_alerts(  
            self.state,  
            now_utc=datetime.now(timezone.utc),  
            hours=self.snooze_hours,  
            model_keys=selected,  
        )  
        save_state(self.state_path, self.state)  
        self.refresh_ui()  
  
    def acknowledge_all(self) -> None:  
        self.state = acknowledge_alerts(self.state)  
        save_state(self.state_path, self.state)  
        self.refresh_ui()  
  
    def snooze_all(self) -> None:  
        self.state = snooze_alerts(  
            self.state,  
            now_utc=datetime.now(timezone.utc),  
            hours=self.snooze_hours,  
        )  
        save_state(self.state_path, self.state)  
        self.refresh_ui()  
  
    def refresh_ui(self) -> None:  
        self.refresh_tree()  
        self.refresh_tray_icon()  
        self._refresh_action_states()
  
    # ----- Tray icon -----  
  
    def refresh_tray_icon(self) -> None:  
        now_utc = datetime.now(timezone.utc)  
        pending_count = len(pending_alerts(self.state, now_utc))  
        busy = self.recovery_in_progress or self.check_in_progress or self.update_in_progress
        self.icon.icon = self.make_icon_image(pending_count, busy=busy)  
        if self.update_in_progress:
            status_text = "updating models"
        elif self.check_in_progress:
            status_text = "checking"
        else:
            status_text = f"{pending_count} pending alerts"
        last_checked = self.state.get("last_checked_utc") or "never"  
        self.icon.title = f"{APP_NAME}: {status_text} (last checked {last_checked})"  
        self.icon.update_menu()  
  
    def make_icon_image(self, pending_count: int, *, busy: bool) -> Image.Image:  
        base = Image.new("RGBA", (64, 64), (0, 0, 0, 0))  
        draw = ImageDraw.Draw(base)  
  
        if busy:  
            color = (230, 150, 20, 255)  
        elif pending_count > 0:  
            color = (210, 60, 60, 255)  
        else:  
            color = (45, 150, 80, 255)  
  
        draw.rounded_rectangle((8, 8, 56, 56), radius=14, fill=color)  
        text = "!" if pending_count > 9 else str(pending_count)  
        if pending_count == 0:  
            text = "OK"  
        if busy:  
            text = "..."  
  
        font = ImageFont.load_default()  
        bbox = draw.textbbox((0, 0), text, font=font)  
        width = bbox[2] - bbox[0]  
        height = bbox[3] - bbox[1]  
        draw.text(  
            ((64 - width) / 2, (64 - height) / 2 - 1),  
            text,  
            fill=(255, 255, 255, 255),  
            font=font,  
        )  
        return base  
  
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
        if self.topmost_reset_token is not None and self.window and self.window.winfo_exists():  
            try:  
                self.window.after_cancel(self.topmost_reset_token)  
            except tk.TclError:  
                pass  
            self.topmost_reset_token = None  
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
