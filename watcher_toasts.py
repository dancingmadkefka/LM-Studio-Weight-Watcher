"""Fallback-safe Windows toast notifications for the Weight Watcher tray app.

Wrap ``windows_toasts`` so background reminders never steal focus: the tray
app raises Windows toasts instead of showing/raising its Tk window. Every
failure path (missing package, WinRT error, rejected toast) is logged to
stderr — which ``run_watcher.bat`` redirects to ``watcher-error.log`` — and
swallowed. A notification must never crash the tray app.

Dedupe contract: one toast per alert-state change. ``notify`` returns True
only when a toast was actually attempted for a *new* ``alert_key``; an
identical key is silently skipped until ``clear`` resets it.
"""
from __future__ import annotations

import sys

TOAST_TITLE = "LM Studio Weight Watcher"
TOAST_AUMID = "LM Studio Weight Watcher"


def _log_toast_error(message: str) -> None:
    # stderr is redirected to watcher-error.log by run_watcher.bat (hidden launch).
    print(f"[toast] {message}", file=sys.stderr, flush=True)


class WatchToaster:
    """Lazily-constructed, failure-safe toast sender with alert-state dedupe."""

    def __init__(self) -> None:
        self._toaster = None  # Built on first use; import kept lazy.
        self._disabled = False
        self._last_alert_key: tuple | None = None

    @property
    def last_alert_key(self) -> tuple | None:
        return self._last_alert_key

    def clear(self) -> None:
        """Reset dedupe memory (call when no alerts remain pending)."""
        self._last_alert_key = None

    def notify(self, alert_key: tuple, body: str) -> bool:
        """Show one toast for a new alert state. Never raises.

        Returns True if a toast was attempted, False if deduped, disabled,
        or failed (failure also disables further attempts this session).
        """
        if self._disabled:
            return False
        if alert_key == self._last_alert_key:
            return False
        try:
            self._show(body)
        except Exception as exc:  # noqa: BLE001 - notification must never crash the app
            self._disabled = True
            _log_toast_error(
                f"toast failed and is now disabled: {type(exc).__name__}: {exc}"
            )
            return False
        self._last_alert_key = alert_key
        return True

    def _show(self, body: str) -> None:
        if self._toaster is None:
            from windows_toasts import WindowsToaster

            self._toaster = WindowsToaster(TOAST_AUMID)
        from windows_toasts import Toast, ToastDuration

        toast = Toast()
        toast.text_fields = [TOAST_TITLE, body]
        toast.duration = ToastDuration.Long
        self._toaster.show_toast(toast)
