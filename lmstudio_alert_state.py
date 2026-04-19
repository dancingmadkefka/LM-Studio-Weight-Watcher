from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lmstudio_weight_checker import CheckResult, format_utc, parse_utc

STATE_VERSION = 1
DEFAULT_SNOOZE_HOURS = 4
APP_NAME = "LM Studio Weight Watcher"
LEGACY_APP_NAME = "LM Studio Weight Updater"


def default_state_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.cwd() / "lmstudio-weight-watcher-state.json"
    preferred = Path(appdata) / APP_NAME / "state.json"
    legacy = Path(appdata) / LEGACY_APP_NAME / "state.json"
    if not preferred.is_file() and legacy.is_file():
        return legacy
    return preferred


def blank_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_checked_utc": None,
        "last_error": None,
        "last_summary": {
            "checked": 0,
            "update_available": 0,
            "up_to_date": 0,
            "unresolved": 0,
        },
        "last_reminder_utc": None,
        "alerts": {},
        "unresolved": [],
    }


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
    if not isinstance(state.get("unresolved"), list):
        state["unresolved"] = []
    if not isinstance(state.get("last_summary"), dict):
        state["last_summary"] = blank_state()["last_summary"]
    return state


def migrate_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = blank_state()
    version = payload.get("version")
    if version in (None, 1):
        state.update(payload)
        state["version"] = STATE_VERSION
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
        if result.status == "update-available":
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
        "fingerprint": fingerprint_for_result(result),
        "first_detected_utc": first_detected_utc or format_utc(now_utc),
        "last_detected_utc": format_utc(now_utc),
        "status": status,
        "snoozed_until_utc": snoozed_until_utc if status == "snoozed" else None,
    }


def fingerprint_for_result(result: CheckResult) -> str:
    return "|".join(
        [
            result.remote_repo or "",
            result.remote_file or "",
            result.remote_modified_utc or "",
        ]
    )


def refresh_expired_snoozes(state: dict[str, Any], now_utc: datetime) -> None:
    for alert in state.get("alerts", {}).values():
        if alert.get("status") != "snoozed":
            continue
        snoozed_until = alert.get("snoozed_until_utc")
        if snoozed_until and parse_utc(snoozed_until) <= now_utc:
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
