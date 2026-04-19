from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_TOLERANCE_SECONDS = 60
HF_API_ROOT = "https://huggingface.co/api/models"


@dataclass
class RemoteReference:
    repo: str
    remote_file: str
    local_relative_path: str


@dataclass
class ResolvedModel:
    model_key: str
    display_name: str
    publisher: str
    local_path: Path
    local_modified_utc: datetime
    local_size_bytes: int
    remote_repo: str
    remote_file: str
    quantization: str | None


@dataclass
class CheckResult:
    model_key: str
    display_name: str
    status: str
    publisher: str
    local_path: str | None
    local_modified_utc: str | None
    remote_repo: str | None
    remote_file: str | None
    remote_modified_utc: str | None
    delta_seconds: float | None
    message: str | None = None


class CheckerError(RuntimeError):
    """Raised when the checker cannot continue."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check installed LM Studio models against Hugging Face file timestamps "
            "and report models with newer remote weights."
        )
    )
    parser.add_argument(
        "--models-root",
        type=Path,
        help="Override the LM Studio models root folder.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Show up-to-date models in addition to updates and unresolved entries.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    parser.add_argument(
        "--include-embeddings",
        action="store_true",
        help="Include embedding models. By default only LLM models are checked.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--tolerance-seconds",
        type=int,
        default=DEFAULT_TOLERANCE_SECONDS,
        help=(
            "Tolerance window before a remote file counts as newer. "
            f"Default: {DEFAULT_TOLERANCE_SECONDS}."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        models_root = discover_models_root(args.models_root)
        inventory = filter_inventory(
            load_lms_json(["ls", "--json"]),
            include_embeddings=args.include_embeddings,
        )
        variant_lookup = load_variant_lookup(inventory)
        results = run_check(
            models_root=models_root,
            inventory=inventory,
            variant_lookup=variant_lookup,
            timeout_seconds=args.timeout,
            tolerance=timedelta(seconds=args.tolerance_seconds),
        )
    except CheckerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "modelsRoot": str(models_root),
            "generatedAtUtc": format_utc(datetime.now(timezone.utc)),
            "summary": summarize_results(results),
            "results": [asdict(result) for result in results],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print_human_report(results, models_root, show_all=args.all)
    return 0


def discover_models_root(override: Path | None) -> Path:
    if override:
        return ensure_directory(override.expanduser())

    env_override = os.environ.get("LMSTUDIO_MODELS_ROOT")
    if env_override:
        return ensure_directory(Path(env_override).expanduser())

    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise CheckerError("APPDATA is not set and --models-root was not provided.")

    settings_path = Path(appdata) / "LM Studio" / "settings.json"
    if not settings_path.is_file():
        raise CheckerError(
            f"Could not find LM Studio settings at {settings_path}. "
            "Pass --models-root to continue."
        )

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckerError(f"Could not read LM Studio settings: {exc}") from exc

    downloads_folder = settings.get("downloadsFolder")
    if not isinstance(downloads_folder, str) or not downloads_folder.strip():
        raise CheckerError(
            "LM Studio settings do not contain a usable downloadsFolder. "
            "Pass --models-root to continue."
        )

    return ensure_directory(Path(downloads_folder).expanduser())


def ensure_directory(path: Path) -> Path:
    resolved = path.expanduser()
    if not resolved.is_dir():
        raise CheckerError(f"Models root does not exist: {resolved}")
    return resolved


def load_lms_json(arguments: list[str]) -> Any:
    command = ["lms", *arguments]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise CheckerError("The `lms` CLI is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else "unknown error"
        raise CheckerError(f"`{' '.join(command)}` failed: {stderr}") from exc

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CheckerError(f"`{' '.join(command)}` did not return valid JSON.") from exc


def run_check(
    *,
    models_root: Path,
    inventory: list[dict[str, Any]],
    variant_lookup: dict[str, dict[str, Any]],
    timeout_seconds: int,
    tolerance: timedelta,
) -> list[CheckResult]:
    tree_cache: dict[tuple[str, str], dict[str, Any]] = {}
    results: list[CheckResult] = []

    for entry in inventory:
        try:
            resolved = resolve_model_entry(entry, models_root, variant_lookup)
            remote_entry = get_remote_file_metadata(
                resolved.remote_repo,
                resolved.remote_file,
                timeout_seconds=timeout_seconds,
                tree_cache=tree_cache,
            )
            result = compare_model(resolved, remote_entry, tolerance)
        except CheckerError as exc:
            result = unresolved_result(entry, str(exc))
        results.append(result)

    return sorted(
        results,
        key=lambda item: (
            status_sort_key(item.status),
            item.display_name.lower(),
            item.model_key.lower(),
        ),
    )


def build_variant_lookup(variant_groups: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for group in variant_groups:
        model = group.get("model") or {}
        model_key = model.get("modelKey")
        if isinstance(model_key, str):
            lookup[model_key] = group
    return lookup


def filter_inventory(
    inventory: list[dict[str, Any]],
    *,
    include_embeddings: bool,
) -> list[dict[str, Any]]:
    allowed_types = {"llm"}
    if include_embeddings:
        allowed_types.add("embedding")
    return [
        entry
        for entry in inventory
        if isinstance(entry, dict) and entry.get("type") in allowed_types
    ]


def load_variant_lookup(inventory: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for entry in inventory:
        if not isinstance(entry.get("selectedVariant"), str):
            continue
        model_key = entry.get("modelKey")
        if not isinstance(model_key, str):
            continue
        try:
            payload = load_lms_json(["ls", model_key, "--json"])
        except CheckerError:
            continue
        if isinstance(payload, list):
            group = {"model": entry, "variants": payload}
        else:
            continue
        lookup[model_key] = group
    return lookup


def resolve_model_entry(
    entry: dict[str, Any],
    models_root: Path,
    variant_lookup: dict[str, dict[str, Any]],
) -> ResolvedModel:
    candidates = candidate_references(entry, variant_lookup)
    for candidate in candidates:
        local_path = models_root.joinpath(*candidate.local_relative_path.split("/"))
        if local_path.is_file():
            stat = local_path.stat()
            return ResolvedModel(
                model_key=require_string(entry, "modelKey"),
                display_name=require_string(entry, "displayName"),
                publisher=require_string(entry, "publisher"),
                local_path=local_path,
                local_modified_utc=datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ),
                local_size_bytes=stat.st_size,
                remote_repo=candidate.repo,
                remote_file=candidate.remote_file,
                quantization=((entry.get("quantization") or {}).get("name")),
            )

    model_key = require_string(entry, "modelKey")
    raise CheckerError(
        f"Could not resolve a local file for {model_key}. "
        "LM Studio returned metadata, but none of the candidate paths exists on disk."
    )


def candidate_references(
    entry: dict[str, Any], variant_lookup: dict[str, dict[str, Any]]
) -> list[RemoteReference]:
    raw_candidates: list[str] = []

    path_value = entry.get("path")
    if isinstance(path_value, str):
        raw_candidates.append(path_value)

    indexed_value = entry.get("indexedModelIdentifier")
    if isinstance(indexed_value, str):
        raw_candidates.append(indexed_value)

    selected_variant_key = entry.get("selectedVariant")
    if isinstance(selected_variant_key, str):
        group = variant_lookup.get(entry.get("modelKey"))
        if group:
            for variant in group.get("variants", []):
                if variant.get("modelKey") == selected_variant_key:
                    variant_path = variant.get("path")
                    if isinstance(variant_path, str):
                        raw_candidates.append(variant_path)
                    variant_identifier = variant.get("indexedModelIdentifier")
                    if isinstance(variant_identifier, str):
                        raw_candidates.append(variant_identifier)
                    break

    seen: set[tuple[str, str, str]] = set()
    references: list[RemoteReference] = []
    for candidate in raw_candidates:
        parsed = parse_remote_reference(candidate)
        if not parsed:
            continue
        key = (parsed.repo, parsed.remote_file, parsed.local_relative_path)
        if key in seen:
            continue
        seen.add(key)
        references.append(parsed)

    return references


def parse_remote_reference(candidate: str) -> RemoteReference | None:
    cleaned = candidate.split("@", 1)[1] if "@" in candidate else candidate
    parts = [segment for segment in cleaned.split("/") if segment]
    if len(parts) < 3:
        return None

    repo = f"{parts[0]}/{parts[1]}"
    remote_file = "/".join(parts[2:])
    local_relative_path = "/".join(parts)
    return RemoteReference(
        repo=repo,
        remote_file=remote_file,
        local_relative_path=local_relative_path,
    )


def get_remote_file_metadata(
    repo: str,
    remote_file: str,
    *,
    timeout_seconds: int,
    tree_cache: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    parent = ""
    if "/" in remote_file:
        parent = remote_file.rsplit("/", 1)[0]

    cache_key = (repo, parent)
    if cache_key not in tree_cache:
        tree_cache[cache_key] = fetch_tree(repo, parent, timeout_seconds)

    file_entry = tree_cache[cache_key].get(remote_file)
    if not file_entry:
        raise CheckerError(
            f"Could not find remote file metadata for {repo}/{remote_file}."
        )

    return file_entry


def fetch_tree(repo: str, parent: str, timeout_seconds: int) -> dict[str, Any]:
    repo_part = urllib.parse.quote(repo, safe="/")
    if parent:
        parent_part = urllib.parse.quote(parent, safe="/")
        url = f"{HF_API_ROOT}/{repo_part}/tree/main/{parent_part}?expand=true"
    else:
        url = f"{HF_API_ROOT}/{repo_part}/tree/main?expand=true"

    payload = fetch_json(url, timeout_seconds)
    if not isinstance(payload, list):
        raise CheckerError(f"Unexpected response while fetching Hugging Face tree for {repo}.")

    indexed: dict[str, Any] = {}
    for item in payload:
        path_value = item.get("path")
        if isinstance(path_value, str):
            indexed[path_value] = item
    return indexed


def fetch_json(url: str, timeout_seconds: int) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "lmstudio-weight-updater/0.1",
    }

    token = (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGINGFACE_TOKEN")
        or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CheckerError(f"Hugging Face resource not found: {url}") from exc
        raise CheckerError(f"Hugging Face request failed with HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise CheckerError(f"Network error while calling Hugging Face: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise CheckerError(f"Invalid JSON received from Hugging Face: {url}") from exc


def compare_model(
    model: ResolvedModel, remote_entry: dict[str, Any], tolerance: timedelta
) -> CheckResult:
    remote_modified_value = ((remote_entry.get("lastCommit") or {}).get("date"))
    if not isinstance(remote_modified_value, str):
        raise CheckerError(
            f"Remote file metadata for {model.remote_repo}/{model.remote_file} "
            "does not include lastCommit.date."
        )

    remote_modified_utc = parse_utc(remote_modified_value)
    delta = (remote_modified_utc - model.local_modified_utc).total_seconds()

    if delta > tolerance.total_seconds():
        status = "update-available"
        message = "Remote file is newer than the installed LM Studio file."
    else:
        status = "up-to-date"
        message = "Installed LM Studio file is as new as the remote file within tolerance."

    return CheckResult(
        model_key=model.model_key,
        display_name=model.display_name,
        status=status,
        publisher=model.publisher,
        local_path=str(model.local_path),
        local_modified_utc=format_utc(model.local_modified_utc),
        remote_repo=model.remote_repo,
        remote_file=model.remote_file,
        remote_modified_utc=format_utc(remote_modified_utc),
        delta_seconds=delta,
        message=message,
    )


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def unresolved_result(entry: dict[str, Any], message: str) -> CheckResult:
    return CheckResult(
        model_key=require_string(entry, "modelKey"),
        display_name=require_string(entry, "displayName"),
        status="unresolved",
        publisher=require_string(entry, "publisher"),
        local_path=None,
        local_modified_utc=None,
        remote_repo=None,
        remote_file=None,
        remote_modified_utc=None,
        delta_seconds=None,
        message=message,
    )


def require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CheckerError(f"Expected `{key}` to be a non-empty string.")
    return value


def summarize_results(results: list[CheckResult]) -> dict[str, int]:
    summary = {
        "update-available": 0,
        "up-to-date": 0,
        "unresolved": 0,
    }
    for result in results:
        summary[result.status] = summary.get(result.status, 0) + 1
    summary["checked"] = len(results)
    return summary


def status_sort_key(status: str) -> int:
    return {
        "update-available": 0,
        "unresolved": 1,
        "up-to-date": 2,
    }.get(status, 9)


def print_human_report(
    results: list[CheckResult], models_root: Path, *, show_all: bool
) -> None:
    summary = summarize_results(results)
    print(f"Models root: {models_root}")
    print(
        "Summary: "
        f"{summary['checked']} checked, "
        f"{summary['update-available']} updates, "
        f"{summary['unresolved']} unresolved, "
        f"{summary['up-to-date']} up to date"
    )

    visible = [
        result
        for result in results
        if show_all or result.status in {"update-available", "unresolved"}
    ]
    if not visible:
        print("No updates detected.")
        return

    for result in visible:
        print()
        print(f"[{result.status}] {result.display_name} ({result.model_key})")
        if result.local_path:
            print(f"  local path: {result.local_path}")
        if result.local_modified_utc:
            print(f"  local modified:  {result.local_modified_utc}")
        if result.remote_repo and result.remote_file:
            print(f"  remote file:     {result.remote_repo}/{result.remote_file}")
        if result.remote_modified_utc:
            print(f"  remote modified: {result.remote_modified_utc}")
        if result.delta_seconds is not None:
            print(f"  delta:           {humanize_delta(result.delta_seconds)}")
        if result.message:
            print(f"  note:            {result.message}")


def humanize_delta(delta_seconds: float) -> str:
    direction = "newer" if delta_seconds >= 0 else "older"
    seconds = abs(int(delta_seconds))
    if seconds < 60:
        return f"{seconds}s {direction}"
    if seconds < 3600:
        return f"{seconds // 60}m {direction}"
    if seconds < 86400:
        return f"{seconds // 3600}h {direction}"
    return f"{seconds // 86400}d {direction}"


if __name__ == "__main__":
    raise SystemExit(main())
