from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lmstudio_weight_checker import CheckResult, format_utc, parse_utc

STATE_VERSION = 2
DEFAULT_SNOOZE_HOURS = 4
APP_NAME = "LM Studio Weight Watcher"
LEGACY_APP_NAME = "LM Studio Weight Updater"

ALERT_STRING_FIELDS = (
    "publisher",
    "local_path",
    "local_modified_utc",
    "remote_repo",
    "remote_file",
    "remote_modified_utc",
    "message",
    "remote_sha256",
    "hash_method",
    "last_commit_title",
    "check_status",
    "fingerprint",
    "first_detected_utc",
    "last_detected_utc",
    "snoozed_until_utc",
)
ARTIFACT_STRING_FIELDS = (
    "kind",
    "label",
    "status",
    "local_path",
    "remote_file",
    "local_oid",
    "remote_oid",
    "last_commit_title",
    "last_commit_date_utc",
    "message",
)


def default_state_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.cwd() / "lmstudio-weight-watcher-state.json"
    return Path(appdata) / APP_NAME / "state.json"


def blank_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_checked_utc": None,
        "last_error": None,
        "last_summary": {
            "checked": 0,
            "update_available": 0,
            "removed_remote": 0,
            "up_to_date": 0,
            "unresolved": 0,
        },
        "last_reminder_utc": None,
        "alerts": {},
        "unresolved": [],
        "active_update": None,
        "last_update": None,
    }


def _sanitize_alert(key: object, value: dict[str, Any]) -> dict[str, Any]:
    """Return an alert safe for sorting, rendering, and update planning."""
    alert = deepcopy(value)
    fallback_key = str(key)
    model_key = alert.get("model_key")
    alert["model_key"] = model_key if isinstance(model_key, str) and model_key else fallback_key
    display_name = alert.get("display_name")
    alert["display_name"] = (
        display_name
        if isinstance(display_name, str) and display_name
        else alert["model_key"]
    )
    for field in ALERT_STRING_FIELDS:
        field_value = alert.get(field)
        if field_value is not None and not isinstance(field_value, str):
            alert[field] = None
    status = alert.get("status")
    if not isinstance(status, str) or status not in {
        "pending",
        "snoozed",
        "acknowledged",
    }:
        alert["status"] = "pending"
        alert["snoozed_until_utc"] = None
    delta = alert.get("delta_seconds")
    if isinstance(delta, bool) or not isinstance(delta, (int, float)):
        alert["delta_seconds"] = None

    artifacts = alert.get("artifacts")
    clean_artifacts: list[dict[str, Any]] = []
    if isinstance(artifacts, list):
        for artifact_value in artifacts:
            if not isinstance(artifact_value, dict):
                continue
            artifact = deepcopy(artifact_value)
            for field in ARTIFACT_STRING_FIELDS:
                field_value = artifact.get(field)
                if field_value is not None and not isinstance(field_value, str):
                    artifact[field] = None
            for field in ("local_size", "remote_size"):
                field_value = artifact.get(field)
                if isinstance(field_value, bool) or not isinstance(field_value, int):
                    artifact[field] = None
            sugg = artifact.get("suggestions")
            if isinstance(sugg, list):
                artifact["suggestions"] = [s for s in sugg if isinstance(s, str)][:24]
            else:
                artifact["suggestions"] = []
            clean_artifacts.append(artifact)
    alert["artifacts"] = clean_artifacts

    suggestions = alert.get("suggestions")
    if isinstance(suggestions, list):
        alert["suggestions"] = [s for s in suggestions if isinstance(s, str)][:24]
    else:
        alert["suggestions"] = []
    return alert


def _safe_string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _sanitize_update_summary(value: object, *, active: bool) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary = deepcopy(value)
    for field in ("job_id", "phase", "message", "started_utc", "updated_utc", "completed_utc"):
        field_value = summary.get(field)
        if field_value is not None and not isinstance(field_value, str):
            summary[field] = None
    summary["model_keys"] = _safe_string_list(summary.get("model_keys"))
    summary["model_names"] = _safe_string_list(summary.get("model_names"))
    for field in ("bytes_completed", "bytes_total", "total_bytes"):
        field_value = summary.get(field)
        summary[field] = (
            field_value
            if isinstance(field_value, int) and not isinstance(field_value, bool) and field_value >= 0
            else 0
        )
    if active:
        summary["cancellable"] = bool(summary.get("cancellable"))
    else:
        summary["success"] = bool(summary.get("success"))
        summary["cancelled"] = bool(summary.get("cancelled"))
    return summary


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return blank_state()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return blank_state()

    state = migrate_state(payload if isinstance(payload, dict) else {})
    if not isinstance(state.get("alerts"), dict):
        state["alerts"] = {}
    else:
        state["alerts"] = {
            str(key): _sanitize_alert(key, alert)
            for key, alert in state["alerts"].items()
            if isinstance(alert, dict)
        }
    if not isinstance(state.get("unresolved"), list):
        state["unresolved"] = []
    else:
        clean_unresolved = []
        for raw_item in state["unresolved"]:
            if not isinstance(raw_item, dict):
                continue
            item = deepcopy(raw_item)
            for field in ("model_key", "display_name", "publisher", "message", "remote_repo"):
                if not isinstance(item.get(field), str):
                    item[field] = ""
            clean_unresolved.append(item)
        state["unresolved"] = clean_unresolved
    if not isinstance(state.get("last_summary"), dict):
        state["last_summary"] = blank_state()["last_summary"]
    else:
        summary = blank_state()["last_summary"]
        for field in summary:
            value = state["last_summary"].get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                summary[field] = value
        state["last_summary"] = summary
    state["active_update"] = _sanitize_update_summary(
        state.get("active_update"), active=True
    )
    state["last_update"] = _sanitize_update_summary(
        state.get("last_update"), active=False
    )
    reminder = state.get("last_reminder_utc")
    if reminder is not None:
        if not isinstance(reminder, str):
            state["last_reminder_utc"] = None
        else:
            try:
                parse_utc(reminder)
            except (TypeError, ValueError):
                state["last_reminder_utc"] = None
    return state


def migrate_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = blank_state()
    version = payload.get("version")
    if version in (None, 1, 2):
        state.update(payload)
        state["version"] = STATE_VERSION
        state.setdefault("active_update", None)
        state.setdefault("last_update", None)
        return state
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def apply_results(
    state: dict[str, Any],
    results: list[CheckResult],
    *,
    now_utc: datetime,
    last_error: str | None = None,
) -> dict[str, Any]:
    next_state = blank_state()
    if isinstance(state, dict):
        next_state.update(deepcopy(state))
    if not isinstance(next_state.get("alerts"), dict):
        next_state["alerts"] = {}
    if not isinstance(next_state.get("unresolved"), list):
        next_state["unresolved"] = []
    if not isinstance(next_state.get("last_summary"), dict):
        next_state["last_summary"] = blank_state()["last_summary"]
    refresh_expired_snoozes(next_state, now_utc)

    if last_error and not results:
        next_state["last_checked_utc"] = format_utc(now_utc)
        next_state["last_error"] = last_error
        return next_state

    alerts = next_state["alerts"]
    active_alert_keys: set[str] = set()

    for result in results:
        if result.status in {"update-available", "removed-remote"}:
            active_alert_keys.add(result.model_key)
            fingerprint = fingerprint_for_result(result)
            current = alerts.get(result.model_key)

            if current and current.get("fingerprint") == fingerprint:
                status = current.get("status", "pending")
                snoozed_until_utc = current.get("snoozed_until_utc")
                if status == "snoozed":
                    snoozed_until = current.get("snoozed_until_utc")
                    if snoozed_until and parse_utc(snoozed_until) <= now_utc:
                        status = "pending"
                        snoozed_until_utc = None
                current.update(
                    alert_payload(
                        result,
                        now_utc,
                        current.get("first_detected_utc"),
                        status,
                        snoozed_until_utc=snoozed_until_utc,
                    )
                )
            else:
                alerts[result.model_key] = alert_payload(
                    result,
                    now_utc,
                    first_detected_utc=format_utc(now_utc),
                    status="pending",
                    snoozed_until_utc=None,
                )

    for model_key in list(alerts):
        if model_key not in active_alert_keys:
            del alerts[model_key]

    next_state["last_checked_utc"] = format_utc(now_utc)
    next_state["last_error"] = last_error
    next_state["unresolved"] = [
        {
            "model_key": result.model_key,
            "display_name": result.display_name,
            "publisher": result.publisher,
            "message": result.message,
        }
        for result in results
        if result.status == "unresolved"
    ]
    next_state["last_summary"] = {
        "checked": len(results),
        "update_available": sum(result.status == "update-available" for result in results),
        "removed_remote": sum(result.status == "removed-remote" for result in results),
        "up_to_date": sum(result.status == "up-to-date" for result in results),
        "unresolved": sum(result.status == "unresolved" for result in results),
    }
    return next_state


def alert_payload(
    result: CheckResult,
    now_utc: datetime,
    first_detected_utc: str | None,
    status: str,
    *,
    snoozed_until_utc: str | None,
) -> dict[str, Any]:
    return {
        "model_key": result.model_key,
        "display_name": result.display_name,
        "publisher": result.publisher,
        "local_path": result.local_path,
        "local_modified_utc": result.local_modified_utc,
        "remote_repo": result.remote_repo,
        "remote_file": result.remote_file,
        "remote_modified_utc": result.remote_modified_utc,
        "delta_seconds": result.delta_seconds,
        "message": result.message,
        "remote_sha256": getattr(result, "remote_sha256", None),
        "hash_method": getattr(result, "hash_method", None),
        "last_commit_title": getattr(result, "last_commit_title", None),
        "check_status": getattr(result, "status", None),
        "suggestions": getattr(result, "suggestions", None),
        "artifacts": [asdict(a) for a in getattr(result, "artifacts", [])],
        "fingerprint": fingerprint_for_result(result),
        "first_detected_utc": first_detected_utc or format_utc(now_utc),
        "last_detected_utc": format_utc(now_utc),
        "status": status,
        "snoozed_until_utc": snoozed_until_utc if status == "snoozed" else None,
    }


def fingerprint_for_result(result: CheckResult) -> str:
    """Identity of a model that is stable across cosmetic remote churn.

    Built from the per-artifact blob identities (file path + remote LFS oid, or
    the artifact status when there is no oid, e.g. removed-remote). This means a
    Hugging Face rename / upload-folder commit that leaves the bytes untouched
    cannot resurrect an acknowledged alert, while ANY genuine content change in
    ANY artifact (weight shard or projector) flips the fingerprint and re-raises
    the alert. Falls back to the legacy single-file identity for results that
    carry no artifact breakdown.
    """
    artifacts = getattr(result, "artifacts", [])
    if artifacts:
        parts: list[str] = [result.remote_repo or ""]
        for art in artifacts:
            oid = art.remote_oid or art.status or ""
            parts.append(f"{art.remote_file}={oid}")
        return "|".join(parts)

    identity = getattr(result, "remote_sha256", None) or result.remote_modified_utc
    return "|".join(
        [
            result.remote_repo or "",
            result.remote_file or "",
            identity or "",
        ]
    )


def refresh_expired_snoozes(state: dict[str, Any], now_utc: datetime) -> None:
    alerts = state.get("alerts", {})
    if not isinstance(alerts, dict):
        state["alerts"] = {}
        return
    for key, alert in list(alerts.items()):
        if not isinstance(alert, dict):
            del alerts[key]
            continue
        if alert.get("status") != "snoozed":
            continue
        snoozed_until = alert.get("snoozed_until_utc")
        try:
            expired = bool(snoozed_until) and parse_utc(str(snoozed_until)) <= now_utc
        except (TypeError, ValueError):
            expired = True
        if expired:
            alert["status"] = "pending"
            alert["snoozed_until_utc"] = None


def pending_alerts(state: dict[str, Any], now_utc: datetime) -> list[dict[str, Any]]:
    refresh_expired_snoozes(state, now_utc)
    return sorted(
        [
            alert
            for alert in state.get("alerts", {}).values()
            if alert.get("status") == "pending"
        ],
        key=lambda alert: (
            alert.get("remote_modified_utc") or "",
            alert.get("display_name") or "",
        ),
        reverse=True,
    )


def all_alerts(state: dict[str, Any], now_utc: datetime) -> list[dict[str, Any]]:
    refresh_expired_snoozes(state, now_utc)
    return sorted(
        list(state.get("alerts", {}).values()),
        key=lambda alert: (
            alert.get("status") != "pending",
            alert.get("display_name") or "",
        ),
    )


def acknowledge_alerts(
    state: dict[str, Any],
    model_keys: list[str] | None = None,
) -> dict[str, Any]:
    next_state = deepcopy(state)
    targets = set(model_keys or next_state.get("alerts", {}).keys())
    for key, alert in next_state.get("alerts", {}).items():
        if key in targets:
            alert["status"] = "acknowledged"
            alert["snoozed_until_utc"] = None
    return next_state


def snooze_alerts(
    state: dict[str, Any],
    *,
    now_utc: datetime,
    hours: int = DEFAULT_SNOOZE_HOURS,
    model_keys: list[str] | None = None,
) -> dict[str, Any]:
    next_state = deepcopy(state)
    targets = set(model_keys or next_state.get("alerts", {}).keys())
    snoozed_until = format_utc(now_utc + timedelta(hours=hours))
    for key, alert in next_state.get("alerts", {}).items():
        if key in targets:
            alert["status"] = "snoozed"
            alert["snoozed_until_utc"] = snoozed_until
    return next_state


def record_reminder(state: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    next_state = deepcopy(state)
    next_state["last_reminder_utc"] = format_utc(now_utc)
    return next_state


def record_update_started(
    state: dict[str, Any],
    *,
    job_id: str,
    model_keys: list[str] | tuple[str, ...],
    model_names: list[str] | tuple[str, ...],
    total_bytes: int,
    now_utc: datetime,
) -> dict[str, Any]:
    next_state = deepcopy(state)
    next_state["version"] = STATE_VERSION
    next_state["active_update"] = {
        "job_id": job_id,
        "model_keys": list(model_keys),
        "model_names": list(model_names),
        "phase": "queued",
        "message": "Update queued",
        "bytes_completed": 0,
        "bytes_total": total_bytes,
        "cancellable": True,
        "started_utc": format_utc(now_utc),
        "updated_utc": format_utc(now_utc),
    }
    return next_state


def record_update_progress(
    state: dict[str, Any],
    *,
    phase: str,
    message: str,
    bytes_completed: int,
    bytes_total: int,
    cancellable: bool,
    now_utc: datetime,
) -> dict[str, Any]:
    next_state = deepcopy(state)
    active = next_state.get("active_update")
    if not isinstance(active, dict):
        return next_state
    active.update(
        {
            "phase": phase,
            "message": message,
            "bytes_completed": max(0, int(bytes_completed)),
            "bytes_total": max(0, int(bytes_total)),
            "cancellable": bool(cancellable),
            "updated_utc": format_utc(now_utc),
        }
    )
    return next_state


def record_update_finished(
    state: dict[str, Any],
    *,
    success: bool,
    message: str,
    now_utc: datetime,
    cancelled: bool = False,
) -> dict[str, Any]:
    next_state = deepcopy(state)
    active = next_state.get("active_update")
    summary = active if isinstance(active, dict) else {}
    next_state["last_update"] = {
        "job_id": summary.get("job_id"),
        "model_keys": _safe_string_list(summary.get("model_keys")),
        "model_names": _safe_string_list(summary.get("model_names")),
        "success": bool(success),
        "cancelled": bool(cancelled),
        "message": message,
        "completed_utc": format_utc(now_utc),
    }
    next_state["active_update"] = None
    return next_state


def reminder_due(
    state: dict[str, Any],
    *,
    now_utc: datetime,
    reminder_interval: timedelta,
) -> bool:
    if not pending_alerts(state, now_utc):
        return False

    last_reminder = state.get("last_reminder_utc")
    if not last_reminder:
        return True
    return parse_utc(last_reminder) + reminder_interval <= now_utc
