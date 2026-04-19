from __future__ import annotations  
  
import argparse  
import threading  
from dataclasses import dataclass  
from datetime import datetime, timedelta, timezone  
from pathlib import Path  
import tkinter as tk  
from tkinter import ttk, font as tkfont  
  
import pystray  
from PIL import Image, ImageDraw, ImageFont  
  
from lmstudio_alert_state import (  
    acknowledge_alerts,  
    all_alerts,  
    apply_results,  
    default_state_path,  
    load_state,  
    pending_alerts,  
    record_reminder,  
    reminder_due,  
    save_state,  
    snooze_alerts,  
)  
from lmstudio_weight_checker import (  
    CheckerError,  
    discover_models_root,  
    filter_inventory,  
    format_utc,  
    load_lms_json,  
    load_variant_lookup,  
    run_check,  
)  
  
DEFAULT_CHECK_INTERVAL_HOURS = 6  
DEFAULT_REMINDER_INTERVAL_MINUTES = 60  
DEFAULT_SNOOZE_HOURS = 4  
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_TOLERANCE_SECONDS = 60
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
  
ROW_PENDING_BG = "#fff5f5"  
ROW_SNOOZED_BG = "#f7f4ff"  
ROW_OK_BG = "#ffffff"  
  
  
@dataclass  
class CheckOutcome:  
    models_root: Path | None
    results: list  
    error: str | None  
    generated_at_utc: datetime  
  
  
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
    parser.add_argument(
        "--tolerance-seconds",
        type=int,
        default=DEFAULT_TOLERANCE_SECONDS,
        help=f"How much newer a remote file must be before it counts as an update. Default: {DEFAULT_TOLERANCE_SECONDS}.",
    )
    return parser.parse_args()  
  
  
def main() -> int:  
    args = parse_args()  
    state_path = (args.state_file or default_state_path()).expanduser()  
  
    if args.once:  
        return run_once(  
            state_path=state_path,  
            models_root_override=args.models_root,  
            timeout_seconds=args.timeout_seconds,
            tolerance_seconds=args.tolerance_seconds,
        )  
  
    app = WatcherApp(  
        state_path=state_path,  
        models_root_override=args.models_root,  
        check_interval=timedelta(hours=args.check_interval_hours),  
        reminder_interval=timedelta(minutes=args.reminder_interval_minutes),  
        snooze_hours=args.snooze_hours,  
        timeout_seconds=args.timeout_seconds,
        tolerance_seconds=args.tolerance_seconds,
    )  
    app.start()  
    return 0  
  
  
def run_once(
    *,
    state_path: Path,
    models_root_override: Path | None,
    timeout_seconds: int,
    tolerance_seconds: int,
) -> int:
    state = load_state(state_path)  
    outcome = perform_check(
        models_root_override,
        timeout_seconds=timeout_seconds,
        tolerance_seconds=tolerance_seconds,
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
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> CheckOutcome:
    generated_at_utc = datetime.now(timezone.utc)  
    models_root = None
    error = None  
  
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
            tolerance=timedelta(seconds=tolerance_seconds),  
        )  
    except CheckerError as exc:  
        error = str(exc)  
        results = []  
  
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
        tolerance_seconds: int,
    ) -> None:  
        self.state_path = state_path  
        self.models_root_override = models_root_override  
        self.check_interval = check_interval  
        self.reminder_interval = reminder_interval  
        self.snooze_hours = snooze_hours  
        self.timeout_seconds = timeout_seconds
        self.tolerance_seconds = tolerance_seconds
        self.state = load_state(state_path)  
        self.last_models_root = None  
        self.check_in_progress = False  
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
        self.refresh_ui()  
        self.run_check_async(reschedule=True)  
        self.maybe_raise_pending_window(force=False)  
  
    # ----- Tray menu -----  
  
    def build_menu(self) -> pystray.Menu:  
        return pystray.Menu(  
            pystray.MenuItem(lambda item: self.menu_status_text(), None, enabled=False),  
            pystray.MenuItem("Open Alerts", self.on_open_alerts, default=True),  
            pystray.MenuItem("Check Now", self.on_check_now),  
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
        if self.check_in_progress:  
            return f"Checking models... (last total {checked})"  
        return f"{pending_count} pending · {checked} models tracked"  
  
    def on_open_alerts(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:  
        self.root.after(0, lambda: self.show_window(force_topmost=True))  
  
    def on_check_now(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:  
        self.root.after(0, lambda: self.run_check_async(reschedule=True))  
  
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
            tolerance_seconds=self.tolerance_seconds,
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
  
        # Treeview  
        style.configure(  
            "Alerts.Treeview",  
            background=COLOR_CARD,  
            fieldbackground=COLOR_CARD,  
            foreground=COLOR_TEXT,  
            rowheight=28,  
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
        self.window.geometry("1020x620")  
        self.window.minsize(860, 500)  
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
        ttk.Label(outer, text="MODEL ALERTS",  
                  style="Section.TLabel").pack(anchor="w", pady=(14, 6))  
  
        alerts_card = ttk.Frame(outer, style="Card.TFrame", padding=1)  
        alerts_card.pack(fill=tk.BOTH, expand=True)  
        self._add_card_border(alerts_card)  
  
        tree_wrap = ttk.Frame(alerts_card, style="Card.TFrame")  
        tree_wrap.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)  
  
        columns = ("model", "status", "remote_modified", "delta")  
        self.tree = ttk.Treeview(  
            tree_wrap,  
            columns=columns,  
            show="headings",  
            style="Alerts.Treeview",  
            selectmode="extended",  
        )  
        self.tree.heading("model", text="  Model")  
        self.tree.heading("status", text="Status")  
        self.tree.heading("remote_modified", text="Remote Modified")  
        self.tree.heading("delta", text="Time Delta")  
        self.tree.column("model", width=460, anchor="w")  
        self.tree.column("status", width=180, anchor="w")  
        self.tree.column("remote_modified", width=200, anchor="w")  
        self.tree.column("delta", width=160, anchor="w")  
  
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)  
        self.tree.configure(yscrollcommand=vsb.set)  
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)  
        vsb.pack(side=tk.RIGHT, fill=tk.Y)  
  
        # Row tags for color coding  
        self.tree.tag_configure("pending", background=ROW_PENDING_BG, foreground=COLOR_TEXT)  
        self.tree.tag_configure("snoozed", background=ROW_SNOOZED_BG, foreground=COLOR_TEXT)  
        self.tree.tag_configure("acknowledged", background=ROW_OK_BG, foreground=COLOR_MUTED)  
        self.tree.tag_configure("empty", foreground=COLOR_MUTED)  
  
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)  
        self.tree.bind("<Double-1>", lambda e: self.acknowledge_selected())  
  
        # --- Toolbar ---  
        toolbar = ttk.Frame(outer, style="Toolbar.TFrame")  
        toolbar.pack(fill=tk.X, pady=(10, 0))  
  
        # Left group: primary  
        left = ttk.Frame(toolbar, style="Toolbar.TFrame")  
        left.pack(side=tk.LEFT)  
        ttk.Button(left, text="↻  Check Now",  
                   command=lambda: self.run_check_async(reschedule=True)).pack(side=tk.LEFT)  
  
        ttk.Separator(toolbar, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)  
  
        # Middle group: per-selection  
        mid = ttk.Frame(toolbar, style="Toolbar.TFrame")  
        mid.pack(side=tk.LEFT)  
        ttk.Button(mid, text="✓  Acknowledge Selected",  
                   command=self.acknowledge_selected).pack(side=tk.LEFT, padx=(0, 6))  
        ttk.Button(mid, text=f"⏾  Snooze Selected ({self.snooze_hours}h)",  
                   command=self.snooze_selected).pack(side=tk.LEFT)  
  
        ttk.Separator(toolbar, orient="vertical").pack(side=tk.LEFT, fill=tk.Y, padx=10)  
  
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
            height=7,  
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
        count = len(self.selected_model_keys())  
        if count == 0:  
            self.selection_var.set("No selection")  
        elif count == 1:  
            self.selection_var.set("1 model selected")  
        else:  
            self.selection_var.set(f"{count} models selected")  
  
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
  
        if not alerts:  
            self.tree.insert(  
                "", tk.END,  
                values=("  No model alerts — everything looks up to date.", "", "", ""),  
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
                # Prefix bullet only for pending  
                prefix = "  ● " if tag == "pending" else "    "  
  
                self.tree.insert(  
                    "",  
                    tk.END,  
                    iid=alert["model_key"],  
                    values=(  
                        f"{prefix}{name}",  
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
                self.unresolved_text.insert(tk.END, "  • ", "muted")  
                self.unresolved_text.insert(tk.END, f"{name}", "name")  
                if key and key != name:  
                    self.unresolved_text.insert(tk.END, f"  ({key})", "muted")  
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
        if self.check_in_progress:  
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
        # Filter out the synthetic "empty" row (no iid given)  
        return [iid for iid in self.tree.selection() if iid]  
  
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
  
    # ----- Tray icon -----  
  
    def refresh_tray_icon(self) -> None:  
        now_utc = datetime.now(timezone.utc)  
        pending_count = len(pending_alerts(self.state, now_utc))  
        busy = self.check_in_progress  
        self.icon.icon = self.make_icon_image(pending_count, busy=busy)  
        status_text = "checking" if busy else f"{pending_count} pending alerts"  
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
