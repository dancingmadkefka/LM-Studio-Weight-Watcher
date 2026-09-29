from __future__ import annotations

import argparse
import errno
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Protocol, Sequence

from lmstudio_alert_state import apply_results, default_state_path, load_state, save_state
from lmstudio_weight_checker import (
    ArtifactResult,
    CheckerError,
    CheckResult,
    cache_verified_oid,
    discover_models_root,
    filter_inventory,
    format_utc,
    load_hash_cache,
    load_lms_json,
    load_variant_lookup,
    run_check,
    save_hash_cache,
)


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_SAFETY_RESERVE_BYTES = 1024 * 1024 * 1024
HASH_CHUNK_SIZE = 8 * 1024 * 1024
OID_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MANIFEST_VERSION = 1
MANIFEST_TEMP_MAX_AGE_SECONDS = 3600
MANIFEST_TEMP_RE = re.compile(r"^\.job-[0-9a-f]{20}\.json\.[a-z0-9_]{8}\.tmp$")
STAGING_DIRECTORY_NAME = ".weight-watcher-staging"
_XET_DOWNLOAD_LOCK = threading.RLock()
SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:x-amz-(?:signature|credential|security-token)|signature|token|access_token|auth)=)[^&\s]+"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:hf|huggingface|hugging_face)_(?:token|secret)\s*[:=]\s*)[^\s,;]+"
)
BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+\-/=]+")
HF_TOKEN_RE = re.compile(r"\bhf_[A-Za-z0-9]{8,}\b")


class UpdateError(RuntimeError):
    """Base error for a safe update failure."""


class PlanError(UpdateError):
    """The requested checker results cannot form a safe update plan."""


class IntegrityError(UpdateError):
    """Downloaded bytes do not match the remote identity."""


class InstallError(UpdateError):
    """Verified bytes could not be transactionally installed."""


class _RestoreFailure(InstallError):
    """The original remains in its rollback path and needs recovery."""


class UpdateCancelled(UpdateError):
    """The caller cancelled before installation began."""


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise UpdateCancelled("Update cancelled before installation.")


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    message: str
    artifact: str | None = None
    bytes_completed: int = 0
    bytes_total: int = 0
    cancellable: bool = True
    model_keys: tuple[str, ...] = ()


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class UpdateArtifact:
    model_keys: tuple[str, ...]
    model_names: tuple[str, ...]
    kind: str
    label: str
    remote_repo: str
    remote_file: str
    destination: Path
    expected_size: int
    expected_sha256: str
    download_dir: Path
    staged_path: Path
    backup_path: Path

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "model_keys": list(self.model_keys),
            "model_names": list(self.model_names),
            "kind": self.kind,
            "label": self.label,
            "remote_repo": redact_sensitive(self.remote_repo),
            "remote_file": redact_sensitive(self.remote_file),
            "destination": str(self.destination),
            "expected_size": self.expected_size,
            "expected_sha256": self.expected_sha256,
            "download_dir": str(self.download_dir),
            "staged_path": str(self.staged_path),
            "backup_path": str(self.backup_path),
        }


@dataclass(frozen=True)
class UpdatePlan:
    job_id: str
    models_root: Path
    staging_root: Path
    manifest_path: Path
    artifacts: tuple[UpdateArtifact, ...]
    selected_model_keys: tuple[str, ...]
    selected_model_names: tuple[str, ...]
    total_bytes: int
    remaining_download_bytes: int
    available_bytes: int
    safety_reserve_bytes: int
    backup_bytes: int = 0
    required_bytes: int = 0
    effective_reserve_bytes: int = 0
    destination_available_bytes: int = 0
    staging_available_bytes: int = 0

    def summary_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "models_root": str(self.models_root),
            "staging_root": str(self.staging_root),
            "selected_model_keys": list(self.selected_model_keys),
            "selected_model_names": list(self.selected_model_names),
            "total_bytes": self.total_bytes,
            "remaining_download_bytes": self.remaining_download_bytes,
            "available_bytes": self.available_bytes,
            "safety_reserve_bytes": self.safety_reserve_bytes,
            "backup_bytes": self.backup_bytes,
            "required_bytes": self.required_bytes,
            "effective_reserve_bytes": self.effective_reserve_bytes,
            "destination_available_bytes": self.destination_available_bytes,
            "staging_available_bytes": self.staging_available_bytes,
            "artifacts": [artifact.manifest_dict() for artifact in self.artifacts],
        }


@dataclass(frozen=True)
class UpdateResult:
    job_id: str
    success: bool
    updated_paths: tuple[str, ...]
    backup_paths: tuple[str, ...]
    completed_at_utc: str
    message: str


@dataclass(frozen=True)
class RecoveryAction:
    job_id: str
    action: str
    path: str
    message: str


class ArtifactDownloader(Protocol):
    def download(
        self,
        artifact: UpdateArtifact,
        *,
        cancellation: CancellationToken,
        progress: Callable[[int, int], None],
    ) -> Path: ...


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _safe_resolve(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _ensure_under(path: Path, root: Path, *, label: str) -> Path:
    resolved = _safe_resolve(path)
    resolved_root = _safe_resolve(root)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PlanError(f"{label} is outside the configured models root: {resolved}") from exc
    if resolved == resolved_root:
        raise PlanError(f"{label} cannot be the models root itself: {resolved}")
    return resolved


def redact_sensitive(value: Any) -> str:
    text = str(value)
    text = SENSITIVE_QUERY_RE.sub(r"\1[REDACTED]", text)
    text = SENSITIVE_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = BEARER_RE.sub(r"\1[REDACTED]", text)
    return HF_TOKEN_RE.sub("hf_[REDACTED]", text)


def _validate_remote_file(value: str, *, label: str) -> str:
    if (
        not value
        or value.startswith(("/", "\\"))
        or "\\" in value
        or any(marker in value for marker in ("?", "#", "&"))
    ):
        raise PlanError(f"{label}: remote file path is unsafe: {value!r}")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise PlanError(f"{label}: remote file path is unsafe: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise PlanError(f"{label}: remote file path is unsafe: {value!r}")
    if path.parts and ":" in path.parts[0]:
        raise PlanError(f"{label}: remote file path is unsafe: {value!r}")
    return path.as_posix()


def _validate_remote_repo(value: str, *, label: str) -> str:
    parts = value.split("/")
    if (
        len(parts) != 2
        or any(not part or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) for part in parts)
    ):
        raise PlanError(f"{label}: remote repository is unsafe: {value!r}")
    return value


def _ensure_within(path: Path, root: Path, *, label: str) -> Path:
    resolved = _safe_resolve(path)
    resolved_root = _safe_resolve(root)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PlanError(f"{label} escapes its staging directory: {resolved}") from exc
    if resolved == resolved_root:
        raise PlanError(f"{label} cannot be the staging directory itself: {resolved}")
    return resolved


def _verification_marker_path(path: Path) -> Path:
    return path.with_name(path.name + ".verified.json")


def _reusable_staged_bytes(path: Path, expected_size: int, expected_sha256: str) -> int:
    """Use verification metadata to estimate reusable staged bytes without hashing."""
    try:
        if not path.is_file():
            return 0
        stat = path.stat()
        if stat.st_size != expected_size:
            return 0
        marker = json.loads(_verification_marker_path(path).read_text(encoding="utf-8"))
        if not isinstance(marker, dict):
            return 0
        if (type(marker.get("expected_size")) is not int
                or type(marker.get("staged_size")) is not int
                or type(marker.get("st_mtime_ns")) is not int):
            return 0
        if (marker.get("version") != 1
                or marker["expected_size"] != expected_size
                or marker.get("expected_sha256") != expected_sha256
                or marker["staged_size"] != stat.st_size
                or marker["st_mtime_ns"] != stat.st_mtime_ns):
            return 0
        return expected_size
    except (OSError, ValueError, TypeError):
        return 0


def _files_under(path: Path) -> set[Path]:
    if not path.is_dir():
        return set()
    files: set[Path] = set()
    for child in path.rglob("*"):
        try:
            if child.is_file():
                files.add(child.resolve())
        except OSError:
            continue
    return files


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def staging_root_for(models_root: Path) -> Path:
    return _safe_resolve(models_root).parent / STAGING_DIRECTORY_NAME


def _volume_identity(path: Path) -> str | int:
    """Identify the volume containing an existing path or its nearest parent."""
    existing = _nearest_existing(path).resolve(strict=True)
    if os.name == "nt":
        return existing.drive.casefold()
    return existing.stat().st_dev


def build_update_plan(
    results: Sequence[CheckResult | dict[str, Any]],
    *,
    models_root: Path,
    safety_reserve_bytes: int = DEFAULT_SAFETY_RESERVE_BYTES,
    available_bytes: int | None = None,
) -> UpdatePlan:
    """Build and validate a deduplicated update plan from checker results."""
    root = _safe_resolve(models_root)
    if not root.is_dir():
        raise PlanError(f"Models root does not exist: {root}")
    if safety_reserve_bytes < 0:
        raise PlanError("Safety reserve cannot be negative.")

    raw: dict[str, dict[str, Any]] = {}
    selected_keys: list[str] = []
    selected_names: list[str] = []
    reasons: list[str] = []

    for result in results:
        model_key = str(_value(result, "model_key", "")).strip()
        display_name = str(_value(result, "display_name", model_key)).strip() or model_key
        remote_repo_raw = str(_value(result, "remote_repo", "")).strip().strip("/")
        if not model_key:
            reasons.append("A selected result has no model key.")
            continue
        try:
            remote_repo = _validate_remote_repo(
                remote_repo_raw, label=f"{display_name}"
            )
        except PlanError as exc:
            reasons.append(str(exc))
            continue
        selected_keys.append(model_key)
        selected_names.append(display_name)
        artifacts = list(_value(result, "artifacts", []) or [])
        blocking = [
            str(_value(artifact, "label", _value(artifact, "remote_file", "artifact")))
            for artifact in artifacts
            if str(_value(artifact, "status", "")) in {"unresolved", "removed-remote"}
        ]
        if blocking:
            reasons.append(
                f"{display_name}: unresolved or removed remote artifact(s): {', '.join(blocking)}."
            )
            continue
        found = False
        for artifact in artifacts:
            status = str(_value(artifact, "status", ""))
            if status not in {"update-available", "missing-local"}:
                continue
            found = True
            local_path = _value(artifact, "local_path")
            remote_file_raw = str(_value(artifact, "remote_file", ""))
            remote_size = _value(artifact, "remote_size")
            remote_oid = str(_value(artifact, "remote_oid", "")).lower()
            label = str(_value(artifact, "label", remote_file_raw))
            kind = str(_value(artifact, "kind", "weight"))
            if not remote_repo or not remote_file_raw:
                reasons.append(f"{display_name} / {label}: missing remote repository or file.")
                continue
            try:
                remote_file = _validate_remote_file(remote_file_raw, label=f"{display_name} / {label}")
            except PlanError as exc:
                reasons.append(str(exc))
                continue
            if not local_path:
                reasons.append(f"{display_name} / {label}: checker did not provide a destination path.")
                continue
            if not isinstance(remote_size, int) or remote_size < 0:
                reasons.append(f"{display_name} / {label}: remote size is unavailable.")
                continue
            if not OID_RE.fullmatch(remote_oid):
                reasons.append(f"{display_name} / {label}: remote SHA-256 is unavailable or invalid.")
                continue
            destination = _ensure_under(Path(str(local_path)), root, label=f"Destination for {label}")
            key = os.path.normcase(str(destination))
            existing = raw.get(key)
            if existing:
                if (
                    existing["expected_sha256"] != remote_oid
                    or existing["expected_size"] != remote_size
                    or existing["remote_repo"] != remote_repo
                    or existing["remote_file"] != remote_file
                ):
                    raise PlanError(
                        f"Conflicting remote identities target the same file: {destination}"
                    )
                existing["model_keys"].add(model_key)
                existing["model_names"].add(display_name)
                continue
            raw[key] = {
                "model_keys": {model_key},
                "model_names": {display_name},
                "kind": kind,
                "label": label,
                "remote_repo": remote_repo,
                "remote_file": remote_file,
                "destination": destination,
                "expected_size": remote_size,
                "expected_sha256": remote_oid,
            }
        if not found:
            reasons.append(f"{display_name}: no downloadable changed artifacts were found.")

    if reasons:
        raise PlanError("Cannot safely plan this update:\n- " + "\n- ".join(reasons))
    if not raw:
        raise PlanError("No downloadable changed artifacts were selected.")

    identity = "\n".join(
        sorted(
            f"{item['destination']}|{item['remote_repo']}|{item['remote_file']}|"
            f"{item['expected_sha256']}|{item['expected_size']}"
            for item in raw.values()
        )
    )
    job_id = "job-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    staging_root = staging_root_for(root)
    if _volume_identity(root) != _volume_identity(staging_root):
        raise PlanError(
            f"Staging root {staging_root} is on a different volume from models root {root}."
        )
    manifest_path = staging_root / "jobs" / f"{job_id}.json"

    planned: list[UpdateArtifact] = []
    remaining = 0
    for item in sorted(raw.values(), key=lambda value: str(value["destination"]).lower()):
        source_key = hashlib.sha256(
            f"{item['remote_repo']}\n{item['remote_file']}\n{item['expected_sha256']}\n"
            f"{item['destination']}".encode("utf-8")
        ).hexdigest()[:20]
        download_dir = staging_root / "downloads" / source_key
        staged_path = _ensure_within(
            download_dir.joinpath(*PurePosixPath(item["remote_file"]).parts),
            download_dir,
            label=f"Staged path for {item['label']}",
        )
        _ensure_within(staged_path, staging_root, label=f"Staged path for {item['label']}")
        backup_path = item["destination"].with_name(
            item["destination"].name + f".lmww-backup-{job_id}"
        )
        present = _reusable_staged_bytes(
            staged_path, item["expected_size"], item["expected_sha256"]
        )
        remaining += max(0, item["expected_size"] - present)
        planned.append(
            UpdateArtifact(
                model_keys=tuple(sorted(item["model_keys"])),
                model_names=tuple(sorted(item["model_names"])),
                kind=item["kind"],
                label=item["label"],
                remote_repo=item["remote_repo"],
                remote_file=item["remote_file"],
                destination=item["destination"],
                expected_size=item["expected_size"],
                expected_sha256=item["expected_sha256"],
                download_dir=download_dir,
                staged_path=staged_path,
                backup_path=backup_path,
            )
        )

    backup_bytes = sum(
        artifact.destination.stat().st_size
        for artifact in planned
        if artifact.destination.is_file()
    )
    if available_bytes is None:
        available_bytes = shutil.disk_usage(_nearest_existing(root)).free
    destination_available_bytes = available_bytes
    staging_available_bytes = destination_available_bytes
    effective_reserve = max(safety_reserve_bytes, max(a.expected_size for a in planned))
    required = remaining + backup_bytes + effective_reserve
    if available_bytes < required:
        raise PlanError(
            "Insufficient disk space: "
            f"need {required} bytes (download {remaining}, backup {backup_bytes}, "
            f"reserve {effective_reserve}), have {available_bytes} bytes."
        )

    return UpdatePlan(
        job_id=job_id,
        models_root=root,
        staging_root=staging_root,
        manifest_path=manifest_path,
        artifacts=tuple(planned),
        selected_model_keys=tuple(dict.fromkeys(selected_keys)),
        selected_model_names=tuple(dict.fromkeys(selected_names)),
        total_bytes=sum(artifact.expected_size for artifact in planned),
        remaining_download_bytes=remaining,
        available_bytes=available_bytes,
        safety_reserve_bytes=safety_reserve_bytes,
        backup_bytes=backup_bytes,
        required_bytes=required,
        effective_reserve_bytes=effective_reserve,
        destination_available_bytes=destination_available_bytes,
        staging_available_bytes=staging_available_bytes,
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) != 5 or attempt == 4:
                    raise
                time.sleep(0.01)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_manifest(
    plan: UpdatePlan,
    phase: str,
    *,
    error: str | None = None,
    owned_staging_files: Iterable[Path] = (),
    rollback_status: Iterable[dict[str, Any]] = (),
    retained_backups: Iterable[Path] = (),
    keep_backups: bool = False,
) -> None:
    payload = {
        "version": MANIFEST_VERSION,
        "job_id": plan.job_id,
        "phase": phase,
        "updated_at_utc": format_utc(datetime.now(timezone.utc)),
        "models_root": str(plan.models_root),
        "error": redact_sensitive(error) if error else None,
        "owned_staging_files": sorted(str(Path(path).resolve()) for path in owned_staging_files),
        "artifacts": [artifact.manifest_dict() for artifact in plan.artifacts],
        "rollback_status": list(rollback_status),
        "selected_model_keys": list(plan.selected_model_keys),
        "retained_backups": sorted(str(path) for path in retained_backups),
        "keep_backups": keep_backups,
        "cleanup_reported": False,
    }
    _write_json_atomic(plan.manifest_path, payload)


def _flush_parent_directory(path: Path) -> None:
    if os.name == "nt":
        # NTFS orders metadata updates; Windows does not expose directory fsync.
        return
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EBADF}:
            raise


def cleanup_pending_backups(
    models_root: Path,
    *,
    model_is_current: Callable[[tuple[str, ...]], bool],
) -> list[RecoveryAction]:
    """Remove only recorded backups after a fresh clean model check."""
    root = _safe_resolve(models_root)
    staging_root = staging_root_for(root)
    jobs_dir = staging_root / "jobs"
    if not jobs_dir.is_dir():
        return []
    actions: list[RecoveryAction] = []
    for manifest_path in sorted(jobs_dir.glob("job-*.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("phase") != "completed-cleanup-pending" or payload.get("keep_backups"):
            continue
        job_id = str(payload.get("job_id", ""))
        try:
            if payload.get("version") != MANIFEST_VERSION or manifest_path.stem != job_id:
                raise PlanError("Manifest identity does not match its filename.")
            if _safe_resolve(Path(payload["models_root"])) != root:
                raise PlanError("Manifest models root does not match the current root.")
            allowed: set[Path] = set()
            for item in payload["artifacts"]:
                destination = _ensure_under(Path(item["destination"]), root, label="Cleanup destination")
                backup = Path(item["backup_path"]).resolve(strict=False)
                if backup != destination.with_name(destination.name + f".lmww-backup-{job_id}"):
                    raise PlanError(f"Unexpected rollback path: {backup}")
                allowed.add(backup)
            retained = [Path(value).resolve(strict=False) for value in payload["retained_backups"]]
            if len(set(retained)) != len(retained) or any(path not in allowed for path in retained):
                raise PlanError("Retained backup list does not match the manifest artifacts.")
            model_keys = tuple(str(key) for key in payload["selected_model_keys"])
            if not model_keys:
                raise PlanError("Manifest has no model keys for cleanup validation.")
        except (KeyError, TypeError, ValueError, PlanError) as exc:
            actions.append(RecoveryAction(job_id, "reported", str(manifest_path), redact_sensitive(exc)))
            continue

        try:
            clean = model_is_current(model_keys)
        except Exception as exc:
            clean = False
            check_error = redact_sensitive(exc)
        else:
            check_error = "Model check did not confirm an up-to-date model."
        if not clean:
            if not payload.get("cleanup_reported"):
                payload["cleanup_reported"] = True
                payload["updated_at_utc"] = format_utc(datetime.now(timezone.utc))
                _write_json_atomic(manifest_path, payload)
                actions.append(RecoveryAction(job_id, "reported", str(manifest_path), f"Backup cleanup deferred: {check_error}"))
            continue

        remaining = list(retained)
        for backup in retained:
            try:
                removed = False
                if backup.exists():
                    if not backup.is_file():
                        raise OSError(f"Recorded backup is not a file: {backup}")
                    _flush_parent_directory(backup)
                    backup.unlink()
                    removed = True
                remaining.remove(backup)
                payload["retained_backups"] = [str(path) for path in remaining]
                _write_json_atomic(manifest_path, payload)
                if removed:
                    actions.append(RecoveryAction(job_id, "cleaned", str(backup), "Removed validated rollback file."))
            except OSError as exc:
                if not payload.get("cleanup_reported"):
                    payload["cleanup_reported"] = True
                    payload["error"] = redact_sensitive(exc)
                    _write_json_atomic(manifest_path, payload)
                    actions.append(RecoveryAction(job_id, "reported", str(backup), f"Backup cleanup deferred: {redact_sensitive(exc)}"))
        if not remaining:
            actions.extend(_prune_terminal_manifest(staging_root, manifest_path, payload, root))
    return actions


def _prune_terminal_manifest(
    staging_root: Path, manifest_path: Path, payload: dict[str, Any], models_root: Path
) -> list[RecoveryAction]:
    """Remove recorded staging files before removing their terminal manifest."""
    job_id = str(payload.get("job_id") or "unknown")
    downloads_root = staging_root / "downloads"
    try:
        if manifest_path.stem != job_id or _safe_resolve(Path(payload["models_root"])) != models_root:
            raise PlanError("Manifest identity or models root does not match.")
        directories = [
            _ensure_within(Path(item["download_dir"]), downloads_root, label="Download directory")
            for item in payload["artifacts"]
        ]
        owned = [
            _ensure_within(Path(value), downloads_root, label="Owned staging file")
            for value in payload.get("owned_staging_files", [])
        ]
        if any(not any(path.is_relative_to(directory) for directory in directories) for path in owned):
            raise PlanError("Owned staging file is outside the manifest download directories.")
    except (KeyError, TypeError, ValueError, PlanError) as exc:
        return [RecoveryAction(job_id, "reported", str(manifest_path), f"Cannot prune manifest: {redact_sensitive(exc)}")]

    for other_path in (staging_root / "jobs").glob("job-*.json"):
        if other_path == manifest_path:
            continue
        try:
            other = json.loads(other_path.read_text(encoding="utf-8"))
            other_directories = {
                _ensure_within(Path(item["download_dir"]), downloads_root, label="Download directory")
                for item in other["artifacts"]
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError, PlanError):
            return [RecoveryAction(job_id, "reported", str(manifest_path),
                                   "Cannot prune staging while another manifest is unreadable.")]
        if set(directories) & other_directories:
            return [RecoveryAction(job_id, "reported", str(manifest_path),
                                   "Cannot prune staging shared with another job.")]

    # Unknown files are never inferred to belong to this job.
    unknown = set().union(*(_files_under(directory) for directory in directories)) - set(owned)
    if unknown:
        return [RecoveryAction(job_id, "reported", str(manifest_path),
                               f"Preserved {len(unknown)} unknown staging file(s).")]
    try:
        for path in owned:
            if path.is_file():
                path.unlink()
        for directory in sorted(set(directories), key=lambda path: len(path.parts), reverse=True):
            if directory.is_dir():
                for child in sorted(directory.rglob("*"), key=lambda path: len(path.parts), reverse=True):
                    if child.is_dir():
                        child.rmdir()
                directory.rmdir()
        manifest_path.unlink()
        for directory in (downloads_root, staging_root / "jobs", staging_root):
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
    except OSError as exc:
        return [RecoveryAction(job_id, "reported", str(manifest_path),
                               f"Terminal staging cleanup deferred: {redact_sensitive(exc)}")]
    return []


def compute_verified_sha256(
    path: Path,
    *,
    cancellation: CancellationToken,
    progress: Callable[[int, int], None] | None = None,
    cancellable: bool = True,
) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    completed = 0
    with path.open("rb") as handle:
        while True:
            if cancellable:
                cancellation.raise_if_cancelled()
            chunk = handle.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            completed += len(chunk)
            if progress:
                progress(completed, total)
    return digest.hexdigest()


def verify_staged_artifact(
    artifact: UpdateArtifact,
    *,
    cancellation: CancellationToken,
    progress: Callable[[int, int], None] | None = None,
) -> None:
    marker_path = _verification_marker_path(artifact.staged_path)
    try:
        marker_path.unlink(missing_ok=True)
    except OSError:
        pass
    if not artifact.staged_path.is_file():
        raise IntegrityError(f"Downloaded file is missing: {artifact.staged_path}")
    before = artifact.staged_path.stat()
    size = before.st_size
    if size != artifact.expected_size:
        raise IntegrityError(
            f"Size mismatch for {artifact.label}: expected {artifact.expected_size}, got {size}."
        )
    digest = compute_verified_sha256(
        artifact.staged_path,
        cancellation=cancellation,
        progress=progress,
    )
    if digest.lower() != artifact.expected_sha256:
        raise IntegrityError(
            f"SHA-256 mismatch for {artifact.label}: expected "
            f"{artifact.expected_sha256}, got {digest.lower()}."
        )
    after = artifact.staged_path.stat()
    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise IntegrityError(f"Staged file changed during verification: {artifact.staged_path}")
    try:
        _write_json_atomic(marker_path, {
            "version": 1,
            "expected_size": artifact.expected_size,
            "expected_sha256": artifact.expected_sha256,
            "staged_size": after.st_size,
            "st_mtime_ns": after.st_mtime_ns,
        })
    except OSError:
        # Reuse is optional; a missing marker makes the next plan conservative.
        pass


@contextmanager
def _scoped_xet_cache(cache_dir: Path):
    """Keep Xet's process-wide cache setting tied to one download at a time."""
    with _XET_DOWNLOAD_LOCK:
        previous = os.environ.get("HF_XET_CACHE")
        os.environ["HF_XET_CACHE"] = str(cache_dir)
        try:
            try:
                from huggingface_hub.utils import _xet
            except ImportError:
                abort_xet_session = None
            else:
                abort_xet_session = getattr(_xet, "abort_xet_session", None)

            # A session created by an earlier download keeps its original cache.
            if abort_xet_session is not None:
                abort_xet_session()
            try:
                yield
            finally:
                if abort_xet_session is not None:
                    abort_xet_session()
        finally:
            if previous is None:
                os.environ.pop("HF_XET_CACHE", None)
            else:
                os.environ["HF_XET_CACHE"] = previous


def check_xet_acceleration(cache_dir: Path) -> tuple[bool, str]:
    """Check Hub and Xet separately so a missing Hub never imports hf_xet."""
    try:
        importlib.import_module("huggingface_hub")
    except (ImportError, OSError):
        return False, "huggingface_hub is unavailable"
    with _scoped_xet_cache(cache_dir):
        try:
            importlib.import_module("hf_xet")
        except (ImportError, OSError):
            return False, "hf_xet is not installed or cannot load"
        try:
            runtime = importlib.import_module("huggingface_hub.utils._runtime")
            if not runtime.is_xet_available():
                return False, "Xet is disabled or unavailable to huggingface_hub"
        except (ImportError, AttributeError):
            return False, "this huggingface_hub version cannot use hf_xet"
    return True, ""


class HuggingFaceDownloader:
    """Lazy Hugging Face downloader with resumable local-dir semantics."""

    def download(
        self,
        artifact: UpdateArtifact,
        *,
        cancellation: CancellationToken,
        progress: Callable[[int, int], None],
    ) -> Path:
        cancellation.raise_if_cancelled()
        artifact.download_dir.mkdir(parents=True, exist_ok=True)
        with _scoped_xet_cache(artifact.download_dir / ".hf-xet"):
            try:
                from huggingface_hub import hf_hub_download
                from tqdm.auto import tqdm
            except ImportError as exc:
                raise UpdateError(
                    "Downloading requires huggingface_hub and tqdm; hf_xet is optional for speed. "
                    "Install the project's requirements and retry."
                ) from exc

            token = cancellation

            class ProgressTqdm(tqdm):
                def update(self, n: int = 1) -> bool | None:
                    token.raise_if_cancelled()
                    changed = super().update(n)
                    progress(int(self.n), int(self.total or artifact.expected_size))
                    return changed

                def close(self) -> None:
                    progress(int(self.n), int(self.total or artifact.expected_size))
                    super().close()

            downloaded = hf_hub_download(
                repo_id=artifact.remote_repo,
                filename=artifact.remote_file,
                local_dir=str(artifact.download_dir),
                cache_dir=str(artifact.download_dir / ".hf-cache"),
                force_download=False,
                tqdm_class=ProgressTqdm,
            )
        path = Path(downloaded).resolve()
        expected = artifact.staged_path.resolve(strict=False)
        if path != expected:
            raise UpdateError(
                f"Hugging Face returned an unexpected staging path: {path} (expected {expected})."
            )
        progress(path.stat().st_size, artifact.expected_size)
        return path


class UpdateExecutor:
    def __init__(
        self,
        plan: UpdatePlan,
        *,
        downloader: ArtifactDownloader | None = None,
        cancellation: CancellationToken | None = None,
        progress: ProgressCallback | None = None,
        keep_backups: bool = False,
        hash_cache: dict[str, dict[str, Any]] | None = None,
        replace_file: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
    ) -> None:
        self.plan = plan
        self.downloader = downloader or HuggingFaceDownloader()
        self.cancellation = cancellation or CancellationToken()
        self.progress = progress or (lambda _event: None)
        self.keep_backups = keep_backups
        self.hash_cache = hash_cache
        self._replace_file = replace_file

    def _emit(
        self,
        phase: str,
        message: str,
        *,
        artifact: UpdateArtifact | None = None,
        completed: int = 0,
        total: int = 0,
        cancellable: bool = True,
    ) -> None:
        try:
            self.progress(
                ProgressEvent(
                    phase=phase,
                    message=message,
                    artifact=artifact.label if artifact else None,
                    bytes_completed=completed,
                    bytes_total=total,
                    cancellable=cancellable,
                    model_keys=artifact.model_keys if artifact else (),
                )
            )
        except Exception:
            # UI/log observers must never compromise an update transaction.
            pass

    def execute(self, *, post_install_validator: Callable[[UpdatePlan], None]) -> UpdateResult:
        def validate_pending(model_keys: tuple[str, ...]) -> bool:
            verdict = post_install_validator(
                replace(self.plan, selected_model_keys=model_keys)
            )
            return verdict is not False

        for action in cleanup_pending_backups(
            self.plan.models_root, model_is_current=validate_pending
        ):
            if action.action == "reported":
                self._emit("validating", action.message, cancellable=False)
        if self.plan.manifest_path.is_file():
            try:
                existing_manifest = json.loads(self.plan.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing_manifest = {}
            if existing_manifest.get("phase") == "completed-cleanup-pending":
                raise InstallError(
                    f"Previous backup cleanup is still pending: {self.plan.manifest_path}"
                )
        installed: list[tuple[UpdateArtifact, bool]] = []
        pending_install: tuple[UpdateArtifact, bool] | None = None
        owned_staging_files = self._load_owned_staging_files()
        hash_cache_snapshot = None
        write_manifest(
            self.plan, "downloading", owned_staging_files=owned_staging_files
        )
        try:
            xet_checked = False
            for artifact in self.plan.artifacts:
                self.cancellation.raise_if_cancelled()
                download_root = artifact.download_dir.resolve(strict=False)
                owned_staging_files.update(
                    path for path in _files_under(artifact.download_dir)
                    if path.is_relative_to(download_root)
                )
                write_manifest(
                    self.plan, "downloading", owned_staging_files=owned_staging_files
                )
                if not artifact.staged_path.is_file() or artifact.staged_path.stat().st_size != artifact.expected_size:
                    if not xet_checked:
                        available, reason = check_xet_acceleration(
                            artifact.download_dir / ".hf-xet"
                        )
                        xet_checked = True
                        if not available:
                            self._emit(
                                "downloading",
                                f"Xet acceleration unavailable ({reason}); transfers will use the slower HTTP path.",
                            )
                    self._emit("downloading", f"Downloading {artifact.label}", artifact=artifact)
                    try:
                        self.downloader.download(
                            artifact,
                            cancellation=self.cancellation,
                            progress=lambda done, total, current=artifact: self._emit(
                                "downloading",
                                f"Downloading {current.label}",
                                artifact=current,
                                completed=done,
                                total=total,
                            ),
                        )
                    finally:
                        owned_staging_files.update(
                            path for path in _files_under(artifact.download_dir)
                            if path.is_relative_to(download_root)
                        )
                        write_manifest(
                            self.plan, "downloading", owned_staging_files=owned_staging_files
                        )
                self._emit("verifying", f"Verifying {artifact.label}", artifact=artifact)
                verify_staged_artifact(
                    artifact,
                    cancellation=self.cancellation,
                    progress=lambda done, total, current=artifact: self._emit(
                        "verifying",
                        f"Verifying {current.label}",
                        artifact=current,
                        completed=done,
                        total=total,
                    ),
                )
                marker = _verification_marker_path(artifact.staged_path)
                if marker.is_file():
                    owned_staging_files.add(marker)
                    write_manifest(
                        self.plan, "downloading", owned_staging_files=owned_staging_files
                    )

            self._emit(
                "installing",
                "Installing verified artifacts",
                total=len(self.plan.artifacts),
                cancellable=False,
            )
            # Publish the non-cancellable boundary synchronously before the
            # final cancellation check. A cancellation that already won still
            # aborts here; otherwise observers know shutdown must defer before
            # any manifest/install filesystem mutation begins.
            self.cancellation.raise_if_cancelled()
            write_manifest(
                self.plan, "installing", owned_staging_files=owned_staging_files
            )
            for index, artifact in enumerate(self.plan.artifacts, start=1):
                artifact.destination.parent.mkdir(parents=True, exist_ok=True)
                if artifact.backup_path.exists():
                    raise InstallError(
                        f"Rollback path already exists; recovery is required: {artifact.backup_path}"
                    )
                had_original = artifact.destination.exists()
                pending_install = (artifact, had_original)
                try:
                    if had_original:
                        self._replace_file(artifact.destination, artifact.backup_path)
                    self._replace_file(artifact.staged_path, artifact.destination)
                except OSError as exc:
                    if had_original and artifact.backup_path.exists() and not artifact.destination.exists():
                        try:
                            self._replace_file(artifact.backup_path, artifact.destination)
                        except OSError as restore_exc:
                            raise _RestoreFailure(
                                f"Could not restore {artifact.destination} from "
                                f"{artifact.backup_path} after install failed: {exc}; "
                                f"restore failed: {restore_exc}"
                            ) from restore_exc
                        pending_install = None
                    if isinstance(exc, PermissionError):
                        raise InstallError(
                            f"Model file is in use: {artifact.destination}. "
                            "Unload the model in LM Studio and retry."
                        ) from exc
                    raise InstallError(f"Could not install {artifact.destination}: {exc}") from exc
                installed.append((artifact, had_original))
                pending_install = None
                self._emit(
                    "installing",
                    f"Installed {artifact.label}",
                    artifact=artifact,
                    completed=index,
                    total=len(self.plan.artifacts),
                    cancellable=False,
                )

            write_manifest(
                self.plan, "validating", owned_staging_files=owned_staging_files
            )
            self._emit("validating", "Running post-install model check", cancellable=False)
            if self.hash_cache is not None:
                hash_cache_snapshot = {
                    key: dict(value) if isinstance(value, dict) else value
                    for key, value in self.hash_cache.items()
                }
                for artifact in self.plan.artifacts:
                    cache_verified_oid(
                        artifact.destination,
                        artifact.expected_sha256,
                        self.hash_cache,
                        models_root=self.plan.models_root,
                    )
            post_install_validator(self.plan)

            retained = [
                artifact.backup_path
                for artifact, had_original in installed
                if had_original and artifact.backup_path.is_file()
            ]
            backups = [str(path) for path in retained] if self.keep_backups else []
            cleanup_warnings: list[str] = []
            completion_phase = (
                "completed-backups-kept" if self.keep_backups and retained
                else "completed-cleanup-pending" if retained
                else "completed"
            )

            try:
                write_manifest(
                    self.plan,
                    completion_phase,
                    owned_staging_files=owned_staging_files,
                    retained_backups=retained,
                    keep_backups=self.keep_backups,
                )
                cleanup_warnings.extend(
                    self._cleanup_staging(
                        owned_staging_files,
                        remove_manifest=not retained,
                    )
                )
                if cleanup_warnings:
                    write_manifest(
                        self.plan,
                        completion_phase if retained else "completed-cleanup-pending",
                        error="; ".join(cleanup_warnings),
                        owned_staging_files=owned_staging_files,
                        retained_backups=retained,
                        keep_backups=self.keep_backups,
                    )
            except OSError as exc:
                cleanup_warnings.append(f"Could not fully clean staging data: {exc}")
                try:
                    write_manifest(
                        self.plan,
                        completion_phase if retained else "completed-cleanup-pending",
                        error="; ".join(cleanup_warnings),
                        owned_staging_files=owned_staging_files,
                        retained_backups=retained,
                        keep_backups=self.keep_backups,
                    )
                except OSError:
                    pass
            self._emit("completed", "Update completed", cancellable=False)
            message = "All selected artifacts were updated and verified."
            if cleanup_warnings:
                message += " Cleanup remains: " + "; ".join(cleanup_warnings)
            return UpdateResult(
                job_id=self.plan.job_id,
                success=True,
                updated_paths=tuple(str(a.destination) for a in self.plan.artifacts),
                backup_paths=tuple(backups),
                completed_at_utc=format_utc(datetime.now(timezone.utc)),
                message=message,
            )
        except UpdateCancelled as exc:
            write_manifest(
                self.plan,
                "cancelled",
                error=str(exc),
                owned_staging_files=owned_staging_files,
            )
            self._emit("cancelled", str(exc), cancellable=False)
            raise
        except Exception as exc:
            rollback_status: list[dict[str, Any]] = []
            if pending_install is not None:
                artifact, had_original = pending_install
                if isinstance(exc, _RestoreFailure):
                    rollback_status.append({
                        "destination": str(artifact.destination),
                        "backup_path": str(artifact.backup_path),
                        "original_restored": False,
                    })
                elif artifact.backup_path.exists() or (
                    not had_original and artifact.destination.exists() and not artifact.staged_path.exists()
                ):
                    installed.append(pending_install)
            if installed:
                write_manifest(
                    self.plan,
                    "rolling-back",
                    error=str(exc),
                    owned_staging_files=owned_staging_files,
                )
                rollback_errors, completed_status = self._rollback(installed)
                rollback_status.extend(completed_status)
                if hash_cache_snapshot is not None and self.hash_cache is not None:
                    self.hash_cache.clear()
                    self.hash_cache.update(hash_cache_snapshot)
                if rollback_errors:
                    message = f"{exc}; rollback also failed: {'; '.join(rollback_errors)}"
                    write_manifest(
                        self.plan,
                        "recovery-required",
                        error=message,
                        owned_staging_files=owned_staging_files,
                        rollback_status=rollback_status,
                    )
                    raise InstallError(message) from exc
            write_manifest(
                self.plan,
                "recovery-required" if isinstance(exc, _RestoreFailure) else "failed",
                error=str(exc),
                owned_staging_files=owned_staging_files,
                rollback_status=rollback_status,
            )
            self._emit("failed", redact_sensitive(exc), cancellable=False)
            raise

    def _rollback(
        self, installed: list[tuple[UpdateArtifact, bool]]
    ) -> tuple[list[str], list[dict[str, Any]]]:
        errors: list[str] = []
        statuses: list[dict[str, Any]] = []
        for artifact, had_original in reversed(installed):
            original_restored = False
            parked = artifact.staged_path.with_name(artifact.staged_path.name + ".rollback")
            if had_original:
                # Park the new bytes first, then restore the original before
                # moving the parked bytes back to staging.
                if artifact.destination.exists():
                    try:
                        artifact.staged_path.parent.mkdir(parents=True, exist_ok=True)
                        if parked.exists():
                            raise OSError(f"Rollback staging path already exists: {parked}")
                        self._replace_file(artifact.destination, parked)
                    except OSError as exc:
                        errors.append(f"Could not park {artifact.destination}: {exc}")
                if artifact.backup_path.exists():
                    try:
                        self._replace_file(artifact.backup_path, artifact.destination)
                        original_restored = True
                    except OSError as exc:
                        errors.append(f"Could not restore {artifact.destination} from {artifact.backup_path}: {exc}")
                else:
                    errors.append(f"Missing rollback file for {artifact.destination}: {artifact.backup_path}")
                if parked.exists():
                    try:
                        self._replace_file(parked, artifact.staged_path)
                    except OSError as exc:
                        errors.append(f"Could not return {parked} to staging: {exc}")
            elif artifact.destination.exists():
                try:
                    artifact.staged_path.parent.mkdir(parents=True, exist_ok=True)
                    self._replace_file(artifact.destination, artifact.staged_path)
                    original_restored = True
                except OSError as exc:
                    errors.append(f"Could not remove new file {artifact.destination}: {exc}")
            else:
                original_restored = True
            statuses.append({
                "destination": str(artifact.destination),
                "backup_path": str(artifact.backup_path),
                "original_restored": original_restored,
            })
        return errors, statuses

    def _load_owned_staging_files(self) -> set[Path]:
        if not self.plan.manifest_path.is_file():
            return set()
        try:
            payload = json.loads(self.plan.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        owned: set[Path] = set()
        for value in payload.get("owned_staging_files", []):
            try:
                owned.add(
                    _ensure_within(
                        Path(str(value)),
                        self.plan.staging_root / "downloads",
                        label="Owned staging file",
                    )
                )
            except PlanError:
                continue
        return owned

    def _cleanup_staging(
        self, owned_staging_files: set[Path], *, remove_manifest: bool
    ) -> list[str]:
        warnings: list[str] = []
        downloads_root = self.plan.staging_root / "downloads"
        for path in sorted(owned_staging_files, key=lambda item: len(item.parts), reverse=True):
            try:
                safe = _ensure_within(path, downloads_root, label="Owned staging file")
                if safe.is_file():
                    safe.unlink()
            except (OSError, PlanError) as exc:
                warnings.append(f"Could not remove owned staging file {path}: {exc}")
        for artifact in self.plan.artifacts:
            if artifact.download_dir.is_dir():
                directories = sorted(
                    (path for path in artifact.download_dir.rglob("*") if path.is_dir()),
                    key=lambda item: len(item.parts),
                    reverse=True,
                )
                for directory in directories:
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                try:
                    artifact.download_dir.rmdir()
                except OSError:
                    leftovers = _files_under(artifact.download_dir)
                    if leftovers:
                        warnings.append(
                            f"Preserved {len(leftovers)} unknown staging file(s) in "
                            f"{artifact.download_dir}."
                        )
        downloads = self.plan.staging_root / "downloads"
        unknown_outside_job = {
            path for path in _files_under(downloads)
            if not any(path.is_relative_to(artifact.download_dir.resolve(strict=False))
                       for artifact in self.plan.artifacts)
        }
        if unknown_outside_job:
            warnings.append(
                f"Preserved {len(unknown_outside_job)} unknown staging file(s) outside this job's download directories."
            )
        if downloads.is_dir() and not any(downloads.iterdir()):
            downloads.rmdir()
        if remove_manifest and not warnings and self.plan.manifest_path.is_file():
            self.plan.manifest_path.unlink()
        jobs = self.plan.staging_root / "jobs"
        if jobs.is_dir() and not any(jobs.iterdir()):
            jobs.rmdir()
        if self.plan.staging_root.is_dir() and not any(self.plan.staging_root.iterdir()):
            self.plan.staging_root.rmdir()
        return warnings


def recover_interrupted_jobs(models_root: Path) -> list[RecoveryAction]:
    """Restore missing destinations and report cleanup that needs a later clean check."""
    root = _safe_resolve(models_root)
    staging_root = staging_root_for(root)
    jobs_dir = staging_root / "jobs"
    actions: list[RecoveryAction] = []
    known_download_dirs: set[Path] = set()
    manifests = sorted(jobs_dir.glob("job-*.json")) if jobs_dir.is_dir() else []
    for manifest_path in manifests:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            actions.append(RecoveryAction("unknown", "reported", str(manifest_path), f"Unreadable manifest: {exc}"))
            continue
        if not isinstance(payload, dict):
            actions.append(RecoveryAction("unknown", "reported", str(manifest_path), "Unreadable manifest structure."))
            continue
        job_id = str(payload.get("job_id") or "unknown")
        if (payload.get("version") != MANIFEST_VERSION or manifest_path.stem != job_id
                or payload.get("models_root") != str(root)):
            actions.append(RecoveryAction(job_id, "reported", str(manifest_path), "Unsupported or mismatched manifest."))
            continue
        phase = payload.get("phase")
        cleanup_pending = phase == "completed-cleanup-pending" and not payload.get("keep_backups")
        outstanding_backup = False
        for item in payload.get("artifacts", []):
            try:
                destination = _ensure_under(Path(item["destination"]), root, label="Recovery destination")
                backup = Path(item["backup_path"]).resolve(strict=False)
                if backup != destination.with_name(destination.name + f".lmww-backup-{job_id}"):
                    raise PlanError(f"Unexpected rollback path: {backup}")
                known_download_dirs.add(_ensure_within(
                    Path(item["download_dir"]), staging_root / "downloads", label="Download directory"
                ))
            except (KeyError, TypeError, PlanError) as exc:
                actions.append(RecoveryAction(job_id, "reported", str(manifest_path), f"Unsafe manifest entry: {exc}"))
                continue
            if not destination.exists() and backup.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, destination)
                actions.append(RecoveryAction(job_id, "restored", str(destination), "Restored missing destination from rollback."))
                outstanding_backup = True
            elif backup.exists() and cleanup_pending and destination.is_file():
                outstanding_backup = True
                actions.append(RecoveryAction(job_id, "cleanup-pending", str(backup),
                                              "Validated rollback awaits a later clean model check."))
            elif backup.exists() and phase != "completed-backups-kept":
                outstanding_backup = True
                actions.append(RecoveryAction(job_id, "reported", str(backup), "Destination and rollback both exist; manual inspection required."))
            elif backup.exists():
                outstanding_backup = True
        rollback_status = payload.get("rollback_status", [])
        if (not outstanding_backup and phase in {"completed", "cancelled", "failed", "completed-cleanup-pending"}
                and isinstance(rollback_status, list)
                and all(isinstance(item, dict) and item.get("original_restored", True) for item in rollback_status)):
            actions.extend(_prune_terminal_manifest(staging_root, manifest_path, payload, root))

    downloads_root = staging_root / "downloads"
    for path in sorted(_files_under(downloads_root)):
        if not any(path.is_relative_to(directory) for directory in known_download_dirs):
            actions.append(RecoveryAction("unknown", "reported", str(path),
                                          "Staging file has no identifiable manifest; preserved."))
    if jobs_dir.is_dir():
        for path in sorted(jobs_dir.iterdir()):
            if path.is_file() and path not in manifests:
                if MANIFEST_TEMP_RE.fullmatch(path.name):
                    try:
                        if time.time() - path.stat().st_mtime >= MANIFEST_TEMP_MAX_AGE_SECONDS:
                            path.unlink()
                            actions.append(RecoveryAction("unknown", "temp-cleaned", str(path),
                                                          "Removed stale manifest temp file."))
                    except OSError as exc:
                        actions.append(RecoveryAction("unknown", "reported", str(path),
                                                      f"Could not remove stale manifest temp file: {redact_sensitive(exc)}"))
                    continue
                actions.append(RecoveryAction("unknown", "reported", str(path),
                                              "Job file has no identifiable manifest; preserved."))
    return actions


def summarize_recovery_actions(actions: Sequence[RecoveryAction]) -> dict[str, int]:
    return {
        "restored": sum(action.action == "restored" for action in actions),
        "cleanup_pending": sum(action.action == "cleanup-pending" for action in actions),
        "attention": sum(action.action == "reported" for action in actions),
    }


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely update LM Studio model artifacts from Hugging Face.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="Update every model with downloadable changed artifacts.")
    selection.add_argument("--model-key", action="append", help="Update one model key; repeat to select multiple models.")
    parser.add_argument("--dry-run", action="store_true", help="Print the validated plan without downloading or changing files.")
    parser.add_argument("--yes", action="store_true", help="Confirm replacement without an interactive prompt.")
    parser.add_argument("--keep-backups", action="store_true", help="Keep rollback files indefinitely after a successful update.")
    parser.add_argument("--models-root", type=Path, help="Override the LM Studio models root.")
    parser.add_argument("--state-file", type=Path, help="Override watcher state used for alert refresh and hash cache placement.")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true", help="Print plan/result as JSON.")
    return parser.parse_args(argv)


def collect_check_results(
    *, models_root: Path,
    timeout_seconds: int,
    hash_cache: dict[str, dict[str, Any]],
) -> list[CheckResult]:
    inventory = filter_inventory(load_lms_json(["ls", "--json"]), include_embeddings=False)
    variants = load_variant_lookup(inventory)
    return run_check(
        models_root=models_root,
        inventory=inventory,
        variant_lookup=variants,
        timeout_seconds=timeout_seconds,
        hash_cache=hash_cache,
    )


def _select_results(results: Sequence[CheckResult], args: argparse.Namespace) -> list[CheckResult]:
    if args.all:
        selected = [
            result
            for result in results
            if any(
                str(_value(artifact, "status", ""))
                in {"update-available", "missing-local"}
                for artifact in result.artifacts
            )
        ]
    else:
        requested = set(args.model_key or [])
        selected = [result for result in results if result.model_key in requested]
        missing = requested - {result.model_key for result in selected}
        if missing:
            raise PlanError("Unknown model key(s): " + ", ".join(sorted(missing)))
    if not selected:
        raise PlanError("No models with downloadable updates were selected.")
    return selected


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    state_path = (args.state_file or default_state_path()).expanduser()
    hash_cache_path = state_path.parent / "local_hash_cache.json"
    hash_cache = load_hash_cache(hash_cache_path)
    try:
        models_root = discover_models_root(args.models_root)
        recovery = recover_interrupted_jobs(models_root)
        if recovery:
            counts = summarize_recovery_actions(recovery)
            stale_temps_removed = sum(action.action == "temp-cleaned" for action in recovery)
            print(
                "recovery: "
                f"{counts['restored']} restored, "
                f"{counts['cleanup_pending']} cleanup pending, "
                f"{counts['attention']} need attention"
                + (f", {stale_temps_removed} stale manifest temp "
                   f"{'file' if stale_temps_removed == 1 else 'files'} removed"
                   if stale_temps_removed else ""),
                file=sys.stderr,
            )
        initial_results = collect_check_results(
            models_root=models_root,
            timeout_seconds=args.timeout_seconds,
            hash_cache=hash_cache,
        )
        cleanup_actions: list[RecoveryAction] = []
        if not args.dry_run:
            by_key = {result.model_key: result for result in initial_results}
            cleanup_actions = cleanup_pending_backups(
                models_root,
                model_is_current=lambda keys: all(
                    key in by_key and by_key[key].status == "up-to-date" for key in keys
                ),
            )
            for action in cleanup_actions:
                if action.action == "reported":
                    print(f"cleanup: {action.message} ({action.path})", file=sys.stderr)
        try:
            selected = _select_results(initial_results, args)
        except PlanError as exc:
            if cleanup_actions and str(exc) == "No models with downloadable updates were selected.":
                print("Pending backup cleanup checked.")
                return 0
            raise
        plan = build_update_plan(selected, models_root=models_root)
        if args.dry_run:
            print(json.dumps(plan.summary_dict(), indent=2))
        elif not args.json:
            print(
                f"{len(plan.selected_model_keys)} model(s), {len(plan.artifacts)} file(s), "
                f"{format_bytes(plan.total_bytes)} total."
            )
        if args.dry_run:
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                raise PlanError("Non-interactive updates require --yes.")
            answer = input("Download, verify, and replace these files? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Cancelled.")
                return 2

        latest_results: list[CheckResult] = []

        def validate(completed_plan: UpdatePlan) -> None:
            nonlocal latest_results
            latest_results = collect_check_results(
                models_root=models_root,
                timeout_seconds=args.timeout_seconds,
                hash_cache=hash_cache,
            )
            by_key = {result.model_key: result for result in latest_results}
            failures = [
                f"{key}: {by_key.get(key).status if by_key.get(key) else 'missing'}"
                for key in completed_plan.selected_model_keys
                if key not in by_key or by_key[key].status != "up-to-date"
            ]
            if failures:
                raise InstallError("Post-install checker did not confirm success: " + ", ".join(failures))

        def show_progress(event: ProgressEvent) -> None:
            suffix = ""
            if event.bytes_total:
                suffix = f" ({format_bytes(event.bytes_completed)} / {format_bytes(event.bytes_total)})"
            print(f"[{event.phase}] {event.message}{suffix}", file=sys.stderr)

        result = UpdateExecutor(
            plan,
            progress=show_progress,
            keep_backups=args.keep_backups,
            hash_cache=hash_cache,
        ).execute(post_install_validator=validate)
        save_hash_cache(hash_cache_path, hash_cache)
        if latest_results:
            state = apply_results(
                load_state(state_path),
                latest_results,
                now_utc=datetime.now(timezone.utc),
                last_error=None,
            )
            save_state(state_path, state)
        if args.json:
            print(json.dumps(asdict(result), indent=2))
        else:
            print(result.message)
        return 0
    except UpdateCancelled as exc:
        print(f"cancelled: {redact_sensitive(exc)}", file=sys.stderr)
        return 2
    except (UpdateError, CheckerError) as exc:
        print(f"error: {redact_sensitive(exc)}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Last-resort CLI boundary: third-party downloaders and filesystem
        # adapters must never leak credential-bearing exception text via a
        # traceback. Detailed recovery state is already persisted redacted.
        print(f"error: {redact_sensitive(exc)}", file=sys.stderr)
        return 1
    finally:
        save_hash_cache(hash_cache_path, hash_cache)


if __name__ == "__main__":
    raise SystemExit(main())
