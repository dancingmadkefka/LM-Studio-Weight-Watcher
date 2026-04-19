from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from lmstudio_alert_state import (
    acknowledge_alerts,
    apply_results,
    load_state,
    pending_alerts,
    reminder_due,
    snooze_alerts,
)
from lmstudio_weight_checker import CheckResult


def make_result(
    *,
    model_key: str = "test-model",
    remote_modified_utc: str = "2026-04-18T01:00:00Z",
    status: str = "update-available",
) -> CheckResult:
    return CheckResult(
        model_key=model_key,
        display_name="Test Model",
        status=status,
        publisher="tester",
        local_path="C:/models/test.gguf",
        local_modified_utc="2026-04-18T00:00:00Z",
        remote_repo="tester/test-model",
        remote_file="test.gguf",
        remote_modified_utc=remote_modified_utc,
        delta_seconds=3600,
        message="Remote file is newer than the installed LM Studio file.",
    )


class ApplyResultsTests(unittest.TestCase):
    def test_acknowledged_alert_stays_acknowledged_for_same_remote_file(self) -> None:
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        state = apply_results({}, [make_result()], now_utc=now)
        state = acknowledge_alerts(state, ["test-model"])

        refreshed = apply_results(state, [make_result()], now_utc=now + timedelta(hours=1))

        self.assertEqual(refreshed["alerts"]["test-model"]["status"], "acknowledged")

    def test_new_remote_timestamp_reactivates_alert(self) -> None:
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        state = apply_results({}, [make_result()], now_utc=now)
        state = acknowledge_alerts(state, ["test-model"])

        refreshed = apply_results(
            state,
            [make_result(remote_modified_utc="2026-04-19T05:00:00Z")],
            now_utc=now + timedelta(hours=1),
        )

        self.assertEqual(refreshed["alerts"]["test-model"]["status"], "pending")

    def test_up_to_date_result_clears_existing_alert(self) -> None:
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        state = apply_results({}, [make_result()], now_utc=now)

        refreshed = apply_results(
            state,
            [make_result(status="up-to-date")],
            now_utc=now + timedelta(hours=1),
        )

        self.assertNotIn("test-model", refreshed["alerts"])


class ReminderTests(unittest.TestCase):
    def test_snoozed_alert_is_not_pending_until_expired(self) -> None:
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        state = apply_results({}, [make_result()], now_utc=now)
        state = snooze_alerts(state, now_utc=now, hours=4)

        self.assertEqual(len(pending_alerts(state, now + timedelta(hours=1))), 0)
        self.assertEqual(len(pending_alerts(state, now + timedelta(hours=5))), 1)

    def test_reminder_due_requires_pending_alert(self) -> None:
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        state = apply_results({}, [make_result()], now_utc=now)

        self.assertTrue(reminder_due(state, now_utc=now, reminder_interval=timedelta(minutes=30)))


class StateMigrationTests(unittest.TestCase):
    def test_unknown_state_version_falls_back_to_blank_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text('{"version": 999, "alerts": {"x": {"status": "pending"}}}', encoding="utf-8")

            state = load_state(path)

        self.assertEqual(state["version"], 1)
        self.assertEqual(state["alerts"], {})


if __name__ == "__main__":
    unittest.main()
