from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 30
HASH_CHUNK_SIZE = 8 * 1024 * 1024
HF_API_ROOT = "https://huggingface.co/api/models"

# Matches a sharded GGUF, e.g. "Model-Q4_K-00001-of-00002.gguf".
SHARD_RE = re.compile(r"^(?P<stem>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)


@dataclass
class RemoteReference:
    repo: str
    remote_file: str
    local_relative_path: str


@dataclass
class Artifact:
    """One local file that belongs to an installed model.

    A model is treated as a SET of artifacts (its weight shards plus, for a
    vision model, any projector present on disk). Each artifact is compared
    against its Hugging Face counterpart independently, because uploaders do not
    always move shards or projectors as an atomic set.
    """

    kind: str            # "weight" | "projector"
    label: str           # human label, e.g. "weights (shard 1/2)" or "projector"
    local_path: Path
    remote_repo: str
    remote_file: str     # path within the repo (basename for root-level files)


@dataclass
class ArtifactResult:
    kind: str
    label: str
    status: str          # up-to-date | update-available | removed-remote | missing-local | unresolved
    local_path: str | None
    remote_file: str | None
    local_size: int | None
    remote_size: int | None
    local_oid: str | None
    remote_oid: str | None
    last_commit_title: str | None
    last_commit_date_utc: str | None
    message: str


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
    remote_sha256: str | None = None
    local_sha256: str | None = None
    hash_method: str | None = None
    last_commit_title: str | None = None
    artifacts: list = field(default_factory=list)


class CheckerError(RuntimeError):
    """Raised when the checker cannot continue."""


class RemoteFileMissing(CheckerError):
    """Raised when a file is confirmed absent from Hugging Face.

    Distinct from a transient ``CheckerError`` (timeout, auth, network): only a
    confirmed absence (HTTP 404, or the file is absent from a tree we
    successfully listed) qualifies, so the caller can safely classify it as
    ``removed-remote`` instead of a transient ``unresolved``.
    """


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
        "--hash-cache",
        type=Path,
        default=None,
        help=(
            "Path to the local-file sha256 cache (speeds up repeat checks). "
            "Default: next to the Weight Watcher state file in APPDATA."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    hash_cache_path = args.hash_cache or default_hash_cache_path()
    hash_cache = load_hash_cache(hash_cache_path)
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
            hash_cache=hash_cache,
        )
    except CheckerError as exc:
        save_hash_cache(hash_cache_path, hash_cache)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    save_hash_cache(hash_cache_path, hash_cache)

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
    lms_binary = "lms"
    # Fallback for Windows if lms is not on PATH
    if os.name == "nt":
        if not shutil.which("lms"):
            appdata = os.environ.get("APPDATA")
            user_profile = os.environ.get("USERPROFILE")
            candidates = []
            if user_profile:
                candidates.append(Path(user_profile) / ".cache" / "lm-studio" / "bin" / "lms.exe")
            if appdata:
                candidates.append(Path(appdata).parent / "Local" / ".cache" / "lm-studio" / "bin" / "lms.exe")

            for candidate in candidates:
                if candidate.is_file():
                    lms_binary = str(candidate)
                    break

    command = [lms_binary, *arguments]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except FileNotFoundError as exc:
        raise CheckerError(f"The `lms` CLI is not installed or not on PATH (tried '{lms_binary}').") from exc
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
    hash_cache: dict[str, dict[str, Any]] | None = None,
) -> list[CheckResult]:
    tree_cache: dict[tuple[str, str], dict[str, Any]] = {}
    if hash_cache is None:
        hash_cache = {}
    if models_root is not None:
        prune_hash_cache(hash_cache, models_root)
    results: list[CheckResult] = []

    for entry in inventory:
        try:
            artifacts = resolve_artifacts(entry, models_root, variant_lookup)
            artifact_results = [
                compare_artifact(
                    artifact,
                    timeout_seconds=timeout_seconds,
                    tree_cache=tree_cache,
                    hash_cache=hash_cache,
                    models_root=models_root,
                )
                for artifact in artifacts
            ]
            status, message = rollup_model_status(artifact_results)
            result = build_model_result(
                entry, artifacts, artifact_results, status, message
            )
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


def resolve_artifacts(
    entry: dict[str, Any],
    models_root: Path,
    variant_lookup: dict[str, dict[str, Any]],
) -> list[Artifact]:
    """Build the set of local artifacts that belong to one model entry.

    The main weight is whatever LM Studio points at. If it is sharded
    (``-00001-of-00002``), every sibling shard present on disk for the same
    quant stem is included, because shards are not always moved upstream as a
    set. For a vision model, any projector present on disk under the
    conventional ``mmproj``/``projector`` name is included too; a projector that
    is absent (deleted, renamed, never downloaded) is simply not an installed
    artifact and is not alerted on -- no special-casing of why it is absent.

    This never guesses: if the main weight LM Studio references is not on disk,
    it raises ``CheckerError`` so the model is reported unresolved rather than
    silently re-pointed at some neighboring file.
    """
    candidates = candidate_references(entry, variant_lookup)

    active: RemoteReference | None = None
    for candidate in candidates:
        local_path = models_root.joinpath(*candidate.local_relative_path.split("/"))
        if local_path.is_file():
            active = candidate
            break

    if active is None:
        raise CheckerError(
            f"Could not resolve a local file for {require_string(entry, 'modelKey')}. "
            "LM Studio returned metadata, but none of the candidate paths exists on disk."
        )

    repo = active.repo
    # Preserve any repository subdirectory in the remote path (e.g. an entry
    # like org/repo/sub/file.gguf has remote_file = "sub/file.gguf"). Stripping
    # to the basename would point at the wrong (or missing) remote file.
    remote_file = active.remote_file
    main_basename = remote_file.rsplit("/", 1)[-1]
    remote_prefix = (
        remote_file[: -len(main_basename)] if remote_file != main_basename else ""
    )
    local_dir = models_root.joinpath(*active.local_relative_path.split("/")[:-1])
    main_local = models_root.joinpath(*active.local_relative_path.split("/"))

    artifacts: list[Artifact] = []
    seen: set[tuple[str, str]] = set()

    shard_match = SHARD_RE.match(main_basename)
    if shard_match:
        stem = shard_match.group("stem")
        total = int(shard_match.group("total"))
        # Generate EVERY shard declared by `total`, not just the ones present on
        # disk. A locally missing shard yields an artifact whose local_path does
        # not exist, which compare_artifact reports as missing-local (and the
        # rollup surfaces the model as unresolved). Without this, an incomplete
        # shard set would be silently reported as up to date.
        for idx in range(1, total + 1):
            shard_name = f"{stem}-{idx:05d}-of-{total:05d}.gguf"
            shard_remote = f"{remote_prefix}{shard_name}"
            shard_local = local_dir / shard_name
            key = (repo, shard_remote)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(
                Artifact(
                    kind="weight",
                    label=_shard_label(idx, total),
                    local_path=shard_local,
                    remote_repo=repo,
                    remote_file=shard_remote,
                )
            )
    else:
        key = (repo, remote_file)
        seen.add(key)
        artifacts.append(
            Artifact(
                kind="weight",
                label="weights",
                local_path=main_local,
                remote_repo=repo,
                remote_file=remote_file,
            )
        )

    # Optional projectors: tracked only when one is present on disk.
    if entry.get("vision") is True and local_dir.is_dir():
        for sibling in sorted(local_dir.glob("*.gguf")):
            if sibling.suffix.lower() != ".gguf":
                continue
            if not is_projector_filename(sibling.name):
                continue
            key = (repo, sibling.name)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(
                Artifact(
                    kind="projector",
                    label=f"projector ({sibling.name})",
                    local_path=sibling,
                    remote_repo=repo,
                    remote_file=sibling.name,
                )
            )

    return artifacts


def _shard_label(idx: int, total: int) -> str:
    return f"weights (shard {idx}/{total})"


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


def is_projector_filename(name: str) -> bool:
    """True if a local file is a multimodal projector, not the main weights.

    Projectors share the .gguf extension but must never be substituted for the
    model itself (they are tiny, model-specific, and their bytes have nothing to
    do with the weights we are asked to check).
    """
    lowered = name.lower()
    return "mmproj" in lowered or "projector" in lowered


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
    """Resolve a file's remote metadata, distinguishing absence from errors.

    Raises ``RemoteFileMissing`` only when the file is confirmed absent (the
    repo 404s, or the file is missing from trees we successfully listed).
    Raises ``CheckerError`` when a request failed transiently, so the caller
    can avoid mistaking a network/auth failure for a removal.
    """
    search_paths = [""]
    if "/" in remote_file:
        search_paths.append(remote_file.rsplit("/", 1)[0])

    had_error = False
    repo_confirmed_gone = False

    def fetch_into_cache(key: tuple[str, str], parent: str) -> None:
        nonlocal had_error, repo_confirmed_gone
        if key in tree_cache:
            return
        try:
            tree_cache[key] = fetch_tree(repo, parent, timeout_seconds)
        except RemoteFileMissing:
            # 404 on a tree endpoint. The repo root being gone means every
            # file in it is gone; a missing subdir is not conclusive on its own.
            if parent == "":
                repo_confirmed_gone = True
            tree_cache[key] = {}
        except CheckerError:
            # Transient failure. Record an empty entry so subsequent lookups in
            # this run don't KeyError, but keep the flag so the final verdict is
            # "unresolved" rather than a false "removed".
            had_error = True
            tree_cache[key] = {}

    for parent in search_paths:
        cache_key = (repo, parent)
        fetch_into_cache(cache_key, parent)
        file_entry = tree_cache[cache_key].get(remote_file)
        if file_entry:
            return file_entry

    # If still not found, try a flat search of the entire repo (first level folders)
    # This helps when the file is in a folder like 'IQ4_XS' but the path is flat.
    bare_name = remote_file.rsplit("/", 1)[-1] if "/" in remote_file else remote_file
    root_key = (repo, "")
    fetch_into_cache(root_key, "")

    for entry in tree_cache[root_key].values():
        if entry.get("type") == "directory":
            dir_name = entry["path"]
            dir_key = (repo, dir_name)
            fetch_into_cache(dir_key, dir_name)
            file_entry = tree_cache[dir_key].get(f"{dir_name}/{bare_name}")
            if file_entry:
                return file_entry

    if repo_confirmed_gone:
        raise RemoteFileMissing(f"Repository removed on Hugging Face: {repo}")
    if had_error:
        raise CheckerError(
            f"Could not confirm remote status of {repo}/{remote_file} "
            "(a Hugging Face request failed)."
        )
    raise RemoteFileMissing(
        f"Could not find remote file metadata for {repo}/{remote_file}."
    )


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
            raise RemoteFileMissing(f"Hugging Face resource not found: {url}") from exc
        raise CheckerError(f"Hugging Face request failed with HTTP {exc.code}: {url}") from exc
    except urllib.error.URLError as exc:
        raise CheckerError(f"Network error while calling Hugging Face: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise CheckerError(f"Invalid JSON received from Hugging Face: {url}") from exc


def compare_artifact(
    artifact: Artifact,
    *,
    timeout_seconds: int,
    tree_cache: dict[tuple[str, str], dict[str, Any]],
    hash_cache: dict[str, dict[str, Any]],
    models_root: Path | None = None,
) -> ArtifactResult:
    """Compare one artifact against its Hugging Face counterpart by blob.

    Decision order (cheapest first):
      1. remote unreachable (transient)  -> unresolved
      2. remote file confirmed missing   -> removed-remote
      3. local file missing              -> missing-local
      4. sizes differ                    -> update-available (no hashing)
      5. local sha256 != remote lfs.oid  -> update-available
      6. otherwise                       -> up-to-date
    """
    last_commit_title: str | None = None
    last_commit_date: str | None = None
    remote_size: int | None = None
    remote_oid: str | None = None

    def result(status: str, *, local_size: int | None, local_oid: str | None,
              local_path: str | None, message: str) -> ArtifactResult:
        return ArtifactResult(
            kind=artifact.kind,
            label=artifact.label,
            status=status,
            local_path=local_path,
            remote_file=artifact.remote_file,
            local_size=local_size,
            remote_size=remote_size,
            local_oid=local_oid,
            remote_oid=remote_oid,
            last_commit_title=last_commit_title,
            last_commit_date_utc=last_commit_date,
            message=message,
        )

    local_exists = artifact.local_path.is_file()
    local_size = artifact.local_path.stat().st_size if local_exists else None
    local_path_str = str(artifact.local_path) if local_exists else None

    try:
        remote = get_remote_file_metadata(
            artifact.remote_repo,
            artifact.remote_file,
            timeout_seconds=timeout_seconds,
            tree_cache=tree_cache,
        )
    except RemoteFileMissing:
        remote = None
    except CheckerError as exc:
        # Transient failure (timeout, auth, network). Must NOT be treated as a
        # removal -- report unresolved so the user is not misled into acting.
        return result(
            "unresolved",
            local_size=local_size,
            local_oid=None,
            local_path=local_path_str,
            message=f"{artifact.label}: could not reach Hugging Face ({exc}).",
        )

    if remote:
        commit = remote.get("lastCommit") or {}
        last_commit_title = (
            commit.get("title") if isinstance(commit.get("title"), str) else None
        )
        last_commit_date = (
            commit.get("date") if isinstance(commit.get("date"), str) else None
        )
        lfs = remote.get("lfs")
        remote_size = lfs.get("size") if isinstance(lfs, dict) else None
        remote_oid = lfs.get("oid") if isinstance(lfs, dict) else None

    if remote is None:
        if artifact.local_path.is_file():
            return result(
                "removed-remote",
                local_size=artifact.local_path.stat().st_size,
                local_oid=None,
                local_path=str(artifact.local_path),
                message=(
                    f"{artifact.label}: removed from Hugging Face "
                    "(a local copy is still on disk)."
                ),
            )
        return result(
            "removed-remote",
            local_size=None,
            local_oid=None,
            local_path=None,
            message=(
                f"{artifact.label}: no longer on Hugging Face and not present locally."
            ),
        )

    if not artifact.local_path.is_file():
        return result(
            "missing-local",
            local_size=None,
            local_oid=None,
            # Preserve the expected destination so recovery/update tooling can
            # repair an incomplete shard set without guessing a filename.
            local_path=str(artifact.local_path),
            message=f"{artifact.label}: expected file not found on disk.",
        )

    stat = artifact.local_path.stat()

    if isinstance(remote_size, int) and stat.st_size != remote_size:
        return result(
            "update-available",
            local_size=stat.st_size,
            local_oid=None,
            local_path=str(artifact.local_path),
            message=(
                f"{artifact.label}: size differs "
                f"(local {stat.st_size} vs remote {remote_size})."
            ),
        )

    if not (isinstance(remote_oid, str) and remote_oid):
        return result(
            "unresolved",
            local_size=stat.st_size,
            local_oid=None,
            local_path=str(artifact.local_path),
            message=(
                f"{artifact.label}: remote file has no LFS oid; "
                "cannot verify content."
            ),
        )

    try:
        local_oid = get_local_oid(artifact.local_path, hash_cache, models_root=models_root)
    except OSError as exc:
        return result(
            "unresolved",
            local_size=stat.st_size,
            local_oid=None,
            local_path=str(artifact.local_path),
            message=f"{artifact.label}: could not hash local file ({exc}).",
        )

    if local_oid == remote_oid:
        return result(
            "up-to-date",
            local_size=stat.st_size,
            local_oid=local_oid,
            local_path=str(artifact.local_path),
            message=f"{artifact.label}: bytes match the remote.",
        )

    return result(
        "update-available",
        local_size=stat.st_size,
        local_oid=local_oid,
        local_path=str(artifact.local_path),
        message=f"{artifact.label}: content differs from the remote.",
    )


def rollup_model_status(
    artifact_results: list[ArtifactResult],
) -> tuple[str, str]:
    """Roll per-artifact verdicts into one model-level status + message.

    Severity (highest wins): missing-local weight > removed-remote >
    update-available > unresolved > up-to-date.
    """
    if not artifact_results:
        return "unresolved", "No artifacts resolved for this model."

    missing_weight = [
        r.label for r in artifact_results
        if r.status == "missing-local" and r.kind == "weight"
    ]
    if missing_weight:
        return (
            "unresolved",
            "Missing local file(s): " + ", ".join(missing_weight) + ".",
        )

    removed = [r.label for r in artifact_results if r.status == "removed-remote"]
    if removed:
        return (
            "update-available",
            "No longer on Hugging Face: " + ", ".join(removed)
            + " (upstream removed these files).",
        )

    updates = [r.label for r in artifact_results if r.status == "update-available"]
    if updates:
        return (
            "update-available",
            "Update available for: " + ", ".join(updates) + ".",
        )

    unresolved = [r.label for r in artifact_results if r.status == "unresolved"]
    if unresolved:
        return (
            "unresolved",
            "Could not verify: " + ", ".join(unresolved) + ".",
        )

    return "up-to-date", "All files match the remote."


def build_model_result(
    entry: dict[str, Any],
    artifacts: list[Artifact],
    artifact_results: list[ArtifactResult],
    status: str,
    message: str,
) -> CheckResult:
    """Assemble a CheckResult, deriving legacy top-level fields from artifacts."""
    primary = next(
        (a for a in artifacts if a.kind == "weight"),
        artifacts[0] if artifacts else None,
    )
    primary_result = next(
        (r for r in artifact_results if primary and r.label == primary.label),
        None,
    )

    local_path = str(primary.local_path) if primary and primary.local_path.is_file() else None
    local_modified_utc: str | None = None
    if primary and primary.local_path.is_file():
        local_modified_utc = format_utc(
            datetime.fromtimestamp(primary.local_path.stat().st_mtime, tz=timezone.utc)
        )

    remote_repo = primary.remote_repo if primary else None
    remote_file = primary.remote_file if primary else None

    commit_dates = [
        parse_utc(r.last_commit_date_utc)
        for r in artifact_results
        if r.last_commit_date_utc
    ]
    remote_modified_utc = format_utc(max(commit_dates)) if commit_dates else None

    delta_seconds: float | None = None
    if remote_modified_utc and local_modified_utc:
        delta_seconds = (
            parse_utc(remote_modified_utc) - parse_utc(local_modified_utc)
        ).total_seconds()

    headline = next(
        (r for r in artifact_results if r.status in {"update-available", "removed-remote"}),
        primary_result,
    )

    return CheckResult(
        model_key=require_string(entry, "modelKey"),
        display_name=require_string(entry, "displayName"),
        status=status,
        publisher=require_string(entry, "publisher"),
        local_path=local_path,
        local_modified_utc=local_modified_utc,
        remote_repo=remote_repo,
        remote_file=remote_file,
        remote_modified_utc=remote_modified_utc,
        delta_seconds=delta_seconds,
        message=message,
        remote_sha256=(headline.remote_oid if headline else None),
        local_sha256=(headline.local_oid if headline else None),
        hash_method="lfs-oid",
        last_commit_title=(headline.last_commit_title if headline else None),
        artifacts=artifact_results,
    )


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def compute_sha256(path: Path) -> str:
    """Stream-hash a (potentially large) file and return its hex sha256."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_local_oid(
    path: Path,
    hash_cache: dict[str, dict[str, Any]],
    *,
    models_root: Path | None = None,
) -> str:
    """Return the local file's sha256, cached by (size, mtime_ns).

    Hugging Face LFS oids are the sha256 of the raw file content, so a local
    sha256 can be compared directly to the remote ``lfs.oid``. The cache key is
    the path relative to ``models_root`` when provided (survives moving the
    models directory), otherwise the resolved absolute path.
    """
    stat = path.stat()
    key = _hash_cache_key(path, models_root)
    entry = hash_cache.get(key)
    if (
        isinstance(entry, dict)
        and entry.get("size") == stat.st_size
        and entry.get("mtime_ns") == stat.st_mtime_ns
        and isinstance(entry.get("sha256"), str)
    ):
        return entry["sha256"]

    sha256 = compute_sha256(path)
    hash_cache[key] = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256,
    }
    return sha256


def cache_verified_oid(
    path: Path,
    sha256: str,
    hash_cache: dict[str, dict[str, Any]],
    *,
    models_root: Path | None = None,
) -> str:
    """Seed the local hash cache after an external full-file verification.

    The updater hashes staged bytes before installation. Recording that verified
    identity against the destination's final stat avoids immediately reading a
    multi-gigabyte model a second time during the post-install checker pass.
    """
    stat = path.stat()
    key = _hash_cache_key(path, models_root)
    hash_cache[key] = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256,
    }
    return key


def _hash_cache_key(path: Path, models_root: Path | None) -> str:
    try:
        if models_root is not None:
            rel = path.resolve().relative_to(models_root.resolve())
            return rel.as_posix()
    except ValueError:
        pass
    return str(path.resolve())


def prune_hash_cache(
    cache: dict[str, dict[str, Any]], models_root: Path
) -> None:
    """Drop cache entries whose file no longer exists under models_root.

    Mutates ``cache`` in place so callers that retain their original reference
    (and later save it) persist the surviving entries along with any new hashes
    computed during the check. Entries keyed by paths outside models_root are
    left untouched.
    """
    for key in list(cache.keys()):
        if (models_root / key).is_file():
            continue
        del cache[key]


def default_hash_cache_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.cwd() / "lmstudio-weight-local-hash-cache.json"
    return Path(appdata) / "LM Studio Weight Watcher" / "local_hash_cache.json"


def load_hash_cache(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_hash_cache(path: Path | None, cache: dict[str, dict[str, Any]]) -> None:
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass


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
        if result.remote_repo:
            print(f"  repository:      {result.remote_repo}")
        if result.local_path:
            print(f"  local path:      {result.local_path}")
        if result.remote_modified_utc:
            print(f"  remote modified: {result.remote_modified_utc}")
        if result.delta_seconds is not None:
            print(f"  delta:           {humanize_delta(result.delta_seconds)}")
        if result.last_commit_title:
            print(f"  last commit:     {result.last_commit_title}")
        if result.message:
            print(f"  note:            {result.message}")
        for art in result.artifacts:
            print(
                f"    - [{art.status}] {art.label}  "
                f"({art.remote_file})"
            )
            if art.last_commit_title:
                print(f"        commit: {art.last_commit_title}")


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
