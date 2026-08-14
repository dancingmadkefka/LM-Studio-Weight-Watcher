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
    record_update_finished,
    record_update_progress,
    record_update_started,
    snooze_alerts,
)
from lmstudio_weight_checker import ArtifactResult, CheckResult
from lmstudio_weight_updater import PlanError, build_update_plan


def make_result(
    *,
    model_key: str = "test-model",
    remote_modified_utc: str = "2026-04-18T01:00:00Z",
    status: str = "update-available",
    remote_sha256: str | None = None,
    message: str = "Remote file is newer than the installed LM Studio file.",
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
        message=message,
        remote_sha256=remote_sha256,
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

    def test_stable_blob_is_not_reactivated_by_date_only_churn(self) -> None:
        # When a remote blob identity is known, a mere commit-date bump (e.g. a
        # Hugging Face rename / upload-folder commit that doesn't change bytes)
        # must not resurrect an acknowledged alert.
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        state = apply_results(
            {}, [make_result(remote_sha256="abc123")], now_utc=now
        )
        state = acknowledge_alerts(state, ["test-model"])

        refreshed = apply_results(
            state,
            [
                make_result(
                    remote_modified_utc="2026-04-19T05:00:00Z",
                    remote_sha256="abc123",
                )
            ],
            now_utc=now + timedelta(hours=1),
        )

        self.assertEqual(refreshed["alerts"]["test-model"]["status"], "acknowledged")

    def test_up_to_date_result_clears_existing_alert(self) -> None:
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        state = apply_results({}, [make_result()], now_utc=now)

        refreshed = apply_results(
            state,
            [make_result(status="up-to-date")],
            now_utc=now + timedelta(hours=1),
        )

        self.assertNotIn("test-model", refreshed["alerts"])

    def test_removed_remote_is_tracked_and_counted(self) -> None:
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        removed = make_result(
            status="removed-remote",
            message="No longer on Hugging Face: weights (upstream removed these files).",
        )
        removed.suggestions = ["model-Q4_0.gguf", "model-Q8_0.gguf"]
        state = apply_results({}, [removed], now_utc=now)

        alert = state["alerts"]["test-model"]
        self.assertEqual(alert["check_status"], "removed-remote")
        self.assertEqual(alert["suggestions"], ["model-Q4_0.gguf", "model-Q8_0.gguf"])
        # It is still an actionable alert (pending acknowledgement) so the user
        # is made aware, but it must never be offered as a downloadable update.
        self.assertEqual(alert["status"], "pending")
        self.assertEqual(state["last_summary"]["removed_remote"], 1)
        self.assertEqual(state["last_summary"]["update_available"], 0)

    def test_removed_remote_does_not_offer_update_in_plan(self) -> None:
        # The alert vocabulary the UI filters on must match what the updater
        # refuses to plan: a removed-remote alert is never a downloadable update.
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        removed = make_result(
            status="removed-remote",
            message="No longer on Hugging Face: weights (upstream removed these files).",
        )
        removed.artifacts = [
            ArtifactResult(
                kind="weight",
                label="weights",
                status="removed-remote",
                local_path=None,
                remote_file="test.gguf",
                local_size=None,
                remote_size=None,
                local_oid=None,
                remote_oid=None,
                last_commit_title=None,
                last_commit_date_utc=None,
                message="removed from Hugging Face",
            )
        ]
        state = apply_results({}, [removed], now_utc=now)
        alert = state["alerts"]["test-model"]
        self.assertEqual(alert["check_status"], "removed-remote")
        with TemporaryDirectory() as tmpdir:
            with self.assertRaises(PlanError) as ctx:
                build_update_plan([alert], models_root=Path(tmpdir))
        self.assertIn("removed remote artifact", str(ctx.exception))


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
    def test_corrupt_state_falls_back_to_blank_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text('{"version": 2, broken', encoding="utf-8")

            state = load_state(path)

        self.assertEqual(state["version"], 2)
        self.assertEqual(state["alerts"], {})
        self.assertIsNone(state["active_update"])

    def test_structurally_corrupt_alert_entries_are_discarded(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                '{"version": 1, "alerts": {"broken": null, "also-broken": 7}}',
                encoding="utf-8",
            )

            state = load_state(path)
            pending = pending_alerts(state, datetime.now(timezone.utc))

        self.assertEqual(state["alerts"], {})
        self.assertEqual(pending, [])

    def test_invalid_snooze_timestamp_reactivates_safely(self) -> None:
        state = {
            "alerts": {
                "model": {
                    "status": "snoozed",
                    "snoozed_until_utc": "not-a-time",
                }
            }
        }
        alerts = pending_alerts(state, datetime.now(timezone.utc))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["status"], "pending")

    def test_dictionary_alert_fields_are_sanitized_for_sorting_and_ui(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                '{"version": 1, "alerts": {'
                '"one": {"model_key": [], "display_name": {}, '
                '"remote_modified_utc": ["bad"], "status": "pending"}, '
                '"two": {"remote_modified_utc": "2026-07-17T12:00:00Z", '
                '"status": "pending", "artifacts": [null, {"remote_size": []}]}'
                '}, "unresolved": [null, {"message": "kept"}], '
                '"last_summary": {"checked": "many"}}',
                encoding="utf-8",
            )

            state = load_state(path)
            alerts = pending_alerts(state, datetime.now(timezone.utc))

        self.assertEqual({alert["model_key"] for alert in alerts}, {"one", "two"})
        self.assertTrue(all(isinstance(alert["display_name"], str) for alert in alerts))
        self.assertIsNone(state["alerts"]["one"]["remote_modified_utc"])
        self.assertEqual(state["alerts"]["two"]["artifacts"][0]["remote_size"], None)
        self.assertEqual(state["unresolved"][0]["message"], "kept")
        self.assertEqual(state["last_summary"]["checked"], 0)

    def test_container_status_unresolved_and_active_update_are_sanitized(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                '{"version": 2, "alerts": {"model": {"status": []}}, '
                '"unresolved": [{"remote_repo": ["bad"], "message": {}}], '
                '"active_update": {"job_id": 4, "model_keys": 7, '
                '"model_names": ["Model", null]}, "last_reminder_utc": []}',
                encoding="utf-8",
            )
            state = load_state(path)
            finished = record_update_finished(
                state,
                success=False,
                message="Recovered",
                now_utc=datetime.now(timezone.utc),
            )

        self.assertEqual(state["alerts"]["model"]["status"], "pending")
        self.assertEqual(state["unresolved"][0]["remote_repo"], "")
        self.assertEqual(state["unresolved"][0]["message"], "")
        self.assertEqual(state["active_update"]["model_keys"], [])
        self.assertEqual(state["active_update"]["model_names"], ["Model"])
        self.assertIsNone(state["last_reminder_utc"])
        self.assertEqual(finished["last_update"]["model_keys"], [])

    def test_unknown_state_version_falls_back_to_blank_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text('{"version": 999, "alerts": {"x": {"status": "pending"}}}', encoding="utf-8")

            state = load_state(path)

        self.assertEqual(state["version"], 2)
        self.assertEqual(state["alerts"], {})

    def test_v1_state_migrates_without_losing_snoozed_alert(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                '{"version": 1, "alerts": {"x": {"status": "snoozed", '
                '"snoozed_until_utc": "2026-07-18T00:00:00Z"}}}',
                encoding="utf-8",
            )
            state = load_state(path)
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["alerts"]["x"]["status"], "snoozed")
        self.assertIsNone(state["active_update"])
        self.assertIsNone(state["last_update"])

    def test_update_lifecycle_is_bounded_and_clears_active_state(self) -> None:
        now = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)
        state = record_update_started(
            {},
            job_id="job-1",
            model_keys=["model"],
            model_names=["Model"],
            total_bytes=100,
            now_utc=now,
        )
        state = record_update_progress(
            state,
            phase="downloading",
            message="Downloading",
            bytes_completed=25,
            bytes_total=100,
            cancellable=True,
            now_utc=now + timedelta(minutes=1),
        )
        self.assertEqual(state["active_update"]["bytes_completed"], 25)
        state = record_update_finished(
            state,
            success=True,
            message="Done",
            now_utc=now + timedelta(minutes=2),
        )
        self.assertIsNone(state["active_update"])
        self.assertTrue(state["last_update"]["success"])
        self.assertEqual(state["last_update"]["model_keys"], ["model"])


if __name__ == "__main__":
    unittest.main()
