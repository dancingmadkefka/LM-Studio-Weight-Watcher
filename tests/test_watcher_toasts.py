"""Tests for toast-based notifications and the no-focus-steal policy."""
from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher_toasts import WatchToaster

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHER_SOURCE = (REPO_ROOT / "lmstudio_weight_watcher.py").read_text(encoding="utf-8")


class NoFocusStealSourceTests(unittest.TestCase):
    """The watcher source must not contain any focus-steal primitives."""

    def test_no_focus_force(self) -> None:
        self.assertNotIn("focus_force", WATCHER_SOURCE)

    def test_no_force_topmost(self) -> None:
        self.assertNotIn("force_topmost", WATCHER_SOURCE)

    def test_no_topmost_attribute_writes(self) -> None:
        self.assertNotIn('attributes("-topmost"', WATCHER_SOURCE)

    def test_no_topmost_reset_machinery(self) -> None:
        self.assertNotIn("_schedule_topmost_reset", WATCHER_SOURCE)
        self.assertNotIn("_clear_topmost", WATCHER_SOURCE)

    def test_show_window_is_user_action_only(self) -> None:
        # The only call sites of show_window must be the tray handler and the
        # definition itself — no background/reminder/startup path may call it.
        calls = [
            line.strip()
            for line in WATCHER_SOURCE.splitlines()
            if "show_window" in line
        ]
        self.assertEqual(
            calls,
            [
                'self.root.after(0, self.show_window)',
                'def show_window(self) -> None:',
            ],
        )

    def test_reminder_path_uses_toast_not_window(self) -> None:
        self.assertIn("self.toaster.notify", WATCHER_SOURCE)
        self.assertNotIn("self.root.bell()", WATCHER_SOURCE)


class WatchToasterTests(unittest.TestCase):
    def make_toaster(self) -> WatchToaster:
        toaster = WatchToaster()
        return toaster

    def test_first_state_change_sends_toast(self) -> None:
        toaster = self.make_toaster()
        with patch.object(WatchToaster, "_show") as show:
            sent = toaster.notify(("m1", "update-available"), "Model One has an update")
        self.assertTrue(sent)
        self.assertEqual(show.call_count, 1)
        body_arg = show.call_args.args[0]
        self.assertIn("Model One", body_arg)

    def test_unchanged_state_sends_no_toast(self) -> None:
        toaster = self.make_toaster()
        with patch.object(WatchToaster, "_show") as show:
            first = toaster.notify(("m1", "update-available"), "body")
            second = toaster.notify(("m1", "update-available"), "body")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(show.call_count, 1)

    def test_new_state_change_sends_toast_again(self) -> None:
        toaster = self.make_toaster()
        with patch.object(WatchToaster, "_show") as show:
            toaster.notify(("m1", "update-available"), "body")
            toaster.notify(("m2", "update-available"), "body2")
        self.assertEqual(show.call_count, 2)

    def test_clear_resets_dedupe(self) -> None:
        toaster = self.make_toaster()
        with patch.object(WatchToaster, "_show") as show:
            toaster.notify(("m1", "update-available"), "body")
            toaster.clear()
            toaster.notify(("m1", "update-available"), "body")
        self.assertEqual(show.call_count, 2)

    def test_toast_failure_is_logged_and_disabled_not_raised(self) -> None:
        toaster = self.make_toaster()
        stderr = io.StringIO()
        with (
            patch.object(
                WatchToaster, "_show", side_effect=RuntimeError("winrt exploded")
            ),
            patch("sys.stderr", stderr),
        ):
            sent = toaster.notify(("m1", "update-available"), "body")
            sent_again = toaster.notify(("m1", "update-available"), "body")
        self.assertFalse(sent)
        self.assertFalse(sent_again)
        self.assertIn("winrt exploded", stderr.getvalue())
        self.assertIn("[toast]", stderr.getvalue())

    def test_import_failure_is_swallowed_not_raised(self) -> None:
        toaster = self.make_toaster()
        stderr = io.StringIO()

        def fake_show(body: str) -> None:
            raise ImportError("No module named 'windows_toasts'")

        with (
            patch.object(WatchToaster, "_show", side_effect=fake_show),
            patch("sys.stderr", stderr),
        ):
            sent = toaster.notify(("m1", "update-available"), "body")
        self.assertFalse(sent)
        self.assertIn("[toast]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
