from __future__ import annotations

import ast
import builtins
import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from lmstudio_weight_checker import ArtifactResult, CheckResult
from lmstudio_weight_updater import (
    CancellationToken,
    HuggingFaceDownloader,
    InstallError,
    IntegrityError,
    MANIFEST_TEMP_RE,
    PlanError,
    UpdateCancelled,
    UpdateError,
    UpdateExecutor,
    UpdateResult,
    _select_results,
    _verification_marker_path,
    build_update_plan,
    cleanup_pending_backups,
    main,
    parse_args,
    recover_interrupted_jobs,
    run_update_plan,
    verify_staged_artifact,
    summarize_recovery_actions,
    write_manifest,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def artifact_result(
    *,
    destination: Path,
    remote_file: str,
    content: bytes,
    label: str = "weights",
    kind: str = "weight",
    status: str = "update-available",
) -> ArtifactResult:
    return ArtifactResult(
        kind=kind,
        label=label,
        status=status,
        local_path=str(destination),
        remote_file=remote_file,
        local_size=destination.stat().st_size if destination.exists() else None,
        remote_size=len(content),
        local_oid=None,
        remote_oid=digest(content),
        last_commit_title="test update",
        last_commit_date_utc="2026-07-17T12:00:00Z",
        message="changed",
    )


def check_result(
    *,
    key: str,
    name: str,
    repo: str,
    artifacts: list[ArtifactResult],
) -> CheckResult:
    primary = artifacts[0]
    return CheckResult(
        model_key=key,
        display_name=name,
        status="update-available",
        publisher=repo.split("/", 1)[0],
        local_path=primary.local_path,
        local_modified_utc=None,
        remote_repo=repo,
        remote_file=primary.remote_file,
        remote_modified_utc="2026-07-17T12:00:00Z",
        delta_seconds=1,
        message="update",
        remote_sha256=primary.remote_oid,
        local_sha256=None,
        hash_method="lfs-oid",
        last_commit_title="test update",
        artifacts=artifacts,
    )


class FakeDownloader:
    def __init__(self, content_by_remote: dict[str, bytes]) -> None:
        self.content_by_remote = content_by_remote
        self.calls: list[str] = []

    def download(self, artifact, *, cancellation, progress):
        cancellation.raise_if_cancelled()
        self.calls.append(artifact.remote_file)
        data = self.content_by_remote[artifact.remote_file]
        artifact.staged_path.parent.mkdir(parents=True, exist_ok=True)
        artifact.staged_path.write_bytes(data)
        progress(len(data), len(data))
        return artifact.staged_path


class PlanTests(unittest.TestCase):
    def _space_result(self, base: Path, *, old: bytes = b"o" * 100, new: bytes = b"n" * 100):
        root = base / "models"
        destination = root / "org" / "repo" / "model.gguf"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(old)
        result = check_result(
            key="model", name="Model", repo="org/repo",
            artifacts=[artifact_result(destination=destination, remote_file="model.gguf", content=new)],
        )
        return root, result

    def test_plan_requires_space_for_backup_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, result = self._space_result(Path(tmp))
            for free in (150, 200, 299):
                with self.subTest(available_bytes=free):
                    with self.assertRaisesRegex(PlanError, "Insufficient disk space"):
                        build_update_plan(
                            [result], models_root=root, safety_reserve_bytes=0,
                            available_bytes=free,
                        )
            plan = build_update_plan(
                [result], models_root=root, safety_reserve_bytes=0,
                available_bytes=300,
            )
            self.assertEqual(plan.required_bytes, 300)
            self.assertEqual(plan.backup_bytes, 100)

    def test_plan_space_error_names_backup_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, result = self._space_result(Path(tmp))
            with self.assertRaises(PlanError) as raised:
                build_update_plan([result], models_root=root, safety_reserve_bytes=0, available_bytes=150)
            self.assertIn("download 100", str(raised.exception))
            self.assertIn("backup 100", str(raised.exception))
            self.assertIn("need 300", str(raised.exception))
            self.assertIn("have 150", str(raised.exception))

    def test_plan_rejects_cross_volume_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, result = self._space_result(Path(tmp))
            with patch("lmstudio_weight_updater._volume_identity", side_effect=["destination", "staging"]):
                with self.assertRaises(PlanError) as raised:
                    build_update_plan([result], models_root=root, safety_reserve_bytes=0, available_bytes=1000)
            self.assertIn(str(root), str(raised.exception))
            self.assertIn(str(root.parent / ".weight-watcher-staging"), str(raised.exception))

    def test_plan_accepts_same_volume_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, result = self._space_result(Path(tmp))
            with patch("lmstudio_weight_updater._volume_identity", return_value="same") as identity:
                plan = build_update_plan([result], models_root=root, safety_reserve_bytes=0, available_bytes=1000)
            self.assertEqual(identity.call_count, 2)
            self.assertEqual(plan.models_root, root)

    def test_plan_reserve_covers_largest_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, result = self._space_result(Path(tmp), old=b"old", new=b"n" * 4096)
            plan = build_update_plan([result], models_root=root, safety_reserve_bytes=1, available_bytes=8200)
            self.assertEqual(plan.effective_reserve_bytes, 4096)
            self.assertEqual(plan.required_bytes, 4096 + 3 + 4096)

    def test_plan_summary_reports_both_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, result = self._space_result(Path(tmp))
            plan = build_update_plan([result], models_root=root, safety_reserve_bytes=0, available_bytes=1000)
            summary = plan.summary_dict()
            self.assertEqual(summary["destination_available_bytes"], 1000)
            self.assertEqual(summary["staging_available_bytes"], 1000)
            self.assertEqual(summary["backup_bytes"], 100)

    def test_plan_preserves_nested_file_and_stages_outside_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            destination = root / "org" / "repo" / "sub" / "model.gguf"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"old")
            result = check_result(
                key="model",
                name="Model",
                repo="org/repo",
                artifacts=[
                    artifact_result(
                        destination=destination,
                        remote_file="sub/model.gguf",
                        content=b"new",
                    )
                ],
            )

            plan = build_update_plan(
                [result], models_root=root, safety_reserve_bytes=0
            )

            self.assertEqual(plan.artifacts[0].remote_file, "sub/model.gguf")
            self.assertEqual(plan.artifacts[0].staged_path.name, "model.gguf")
            with self.assertRaises(ValueError):
                plan.staging_root.relative_to(root)
            self.assertEqual(plan.staging_root.parent, root.parent)

    def test_plan_deduplicates_shared_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            target = root / "org" / "repo" / "shared.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            changed = artifact_result(
                destination=target, remote_file="shared.gguf", content=b"new"
            )
            first = check_result(key="one", name="One", repo="org/repo", artifacts=[changed])
            second = check_result(key="two", name="Two", repo="org/repo", artifacts=[changed])

            plan = build_update_plan(
                [first, second], models_root=root, safety_reserve_bytes=0
            )

            self.assertEqual(len(plan.artifacts), 1)
            self.assertEqual(plan.artifacts[0].model_keys, ("one", "two"))

    def test_plan_rejects_destination_outside_models_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "models"
            root.mkdir()
            outside = base / "outside.gguf"
            outside.write_bytes(b"old")
            result = check_result(
                key="model",
                name="Model",
                repo="org/repo",
                artifacts=[artifact_result(destination=outside, remote_file="model.gguf", content=b"new")],
            )
            with self.assertRaisesRegex(PlanError, "outside"):
                build_update_plan([result], models_root=root, safety_reserve_bytes=0)

    def test_plan_rejects_invalid_oid_and_insufficient_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            artifact = artifact_result(destination=target, remote_file="model.gguf", content=b"new")
            artifact.remote_oid = "not-a-hash"
            result = check_result(key="model", name="Model", repo="org/repo", artifacts=[artifact])
            with self.assertRaisesRegex(PlanError, "SHA-256"):
                build_update_plan([result], models_root=root, safety_reserve_bytes=0)

            artifact.remote_oid = digest(b"new")
            with self.assertRaisesRegex(PlanError, "Insufficient"):
                build_update_plan(
                    [result],
                    models_root=root,
                    safety_reserve_bytes=100,
                    available_bytes=1,
                )

    def test_plan_rejects_remote_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model",
                name="Model",
                repo="org/repo",
                artifacts=[
                    artifact_result(
                        destination=target,
                        remote_file="../../../models/org/repo/model.gguf",
                        content=b"new",
                    )
                ],
            )
            with self.assertRaisesRegex(PlanError, "unsafe"):
                build_update_plan([result], models_root=root, safety_reserve_bytes=0)

    def test_plan_rejects_credential_bearing_remote_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model",
                name="Model",
                repo="org/repo",
                artifacts=[
                    artifact_result(
                        destination=target,
                        remote_file="model.gguf?token=PERSISTED_SECRET",
                        content=b"new",
                    )
                ],
            )
            with self.assertRaisesRegex(PlanError, "unsafe"):
                build_update_plan([result], models_root=root, safety_reserve_bytes=0)

    def test_unrelated_staging_file_is_not_counted_as_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model",
                name="Model",
                repo="org/repo",
                artifacts=[artifact_result(destination=target, remote_file="model.gguf", content=b"new")],
            )
            first = build_update_plan([result], models_root=root, safety_reserve_bytes=0)
            unrelated = first.artifacts[0].download_dir / "unrelated.bin"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(b"xxx")
            with self.assertRaisesRegex(PlanError, "Insufficient"):
                build_update_plan(
                    [result],
                    models_root=root,
                    safety_reserve_bytes=0,
                    available_bytes=0,
                )

    def test_plan_rejects_model_with_mixed_changed_and_unresolved_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            first = root / "org" / "repo" / "one.gguf"
            second = root / "org" / "repo" / "two.gguf"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"old-one")
            second.write_bytes(b"old-two")
            changed = artifact_result(destination=first, remote_file="one.gguf", content=b"new-one")
            unresolved = artifact_result(
                destination=second,
                remote_file="two.gguf",
                content=b"new-two",
                status="unresolved",
            )
            result = check_result(
                key="model", name="Model", repo="org/repo", artifacts=[changed, unresolved]
            )
            with self.assertRaisesRegex(PlanError, "unresolved"):
                build_update_plan([result], models_root=root, safety_reserve_bytes=0)


class ExecutionTests(unittest.TestCase):
    def make_plan(self, base: Path, items: list[tuple[str, bytes, bytes]]):
        root = base / "models"
        artifacts = []
        contents = {}
        for filename, old, new in items:
            destination = root / "org" / "repo" / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(old)
            artifacts.append(
                artifact_result(
                    destination=destination,
                    remote_file=filename,
                    content=new,
                    label=filename,
                )
            )
            contents[filename] = new
        result = check_result(key="model", name="Model", repo="org/repo", artifacts=artifacts)
        plan = build_update_plan([result], models_root=root, safety_reserve_bytes=0)
        return root, plan, contents

    def rebuild_staged_plan(self, root: Path, plan, content: bytes):
        artifact = plan.artifacts[0]
        result = check_result(
            key="model", name="Model", repo="org/repo",
            artifacts=[artifact_result(
                destination=artifact.destination, remote_file=artifact.remote_file,
                content=content,
            )],
        )
        return build_update_plan([result], models_root=root, safety_reserve_bytes=0)

    def test_progress_callback_cancellation_stops_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            downloader = FakeDownloader(contents)

            def cancel_on_download(event):
                if event.phase == "downloading":
                    raise UpdateCancelled("cancelled by observer")

            with (patch("lmstudio_weight_updater.check_xet_acceleration",
                        return_value=(True, "")),
                  self.assertRaisesRegex(UpdateCancelled, "cancelled by observer")):
                UpdateExecutor(
                    plan, downloader=downloader, progress=cancel_on_download
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(downloader.calls, [])
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old")
            self.assertFalse(plan.artifacts[0].backup_path.exists())

    def test_late_observer_cancellation_cannot_mark_installed_job_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])

            def cancel_after_install(event):
                if event.message.startswith("Installed ") or event.phase in {"validating", "completed"}:
                    raise UpdateCancelled("too late")

            result = UpdateExecutor(
                plan, downloader=FakeDownloader(contents), progress=cancel_after_install
            ).execute(post_install_validator=lambda _plan: None)
            self.assertTrue(result.success)
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"new")
            manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "completed-cleanup-pending")

    def test_manifest_records_phase_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            observed = []

            def observe_move(source, destination):
                phase = json.loads(plan.manifest_path.read_text(encoding="utf-8"))["phase"]
                observed.append((Path(source), Path(destination), phase))
                os.replace(source, destination)

            def validate(_plan):
                phase = json.loads(plan.manifest_path.read_text(encoding="utf-8"))["phase"]
                self.assertEqual(phase, "validating")

            UpdateExecutor(
                plan, downloader=FakeDownloader(contents), replace_file=observe_move
            ).execute(post_install_validator=validate)
            self.assertEqual(observed[0],
                             (artifact.destination, artifact.backup_path, "installing"))
            self.assertEqual(observed[1],
                             (artifact.staged_path, artifact.destination, "installing"))

    def test_failed_install_records_recovery_phase_before_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("one.gguf", b"old-one", b"new-one"),
                            ("two.gguf", b"old-two", b"new-two")]
            )
            first, second = plan.artifacts
            observed_rollback = []

            def fail_second_install(source, destination):
                source, destination = Path(source), Path(destination)
                if source == second.staged_path and destination == second.destination:
                    raise OSError("install failed")
                if source == first.destination and destination == first.staged_path.with_name(
                    first.staged_path.name + ".rollback"
                ):
                    observed_rollback.append(
                        json.loads(plan.manifest_path.read_text(encoding="utf-8"))["phase"]
                    )
                os.replace(source, destination)

            with self.assertRaisesRegex(InstallError, "install failed"):
                UpdateExecutor(
                    plan, downloader=FakeDownloader(contents), replace_file=fail_second_install
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(observed_rollback, ["rolling-back"])

    def test_rollback_failure_is_not_caught_by_install_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("one.gguf", b"old-one", b"new-one"),
                            ("two.gguf", b"old-two", b"new-two")]
            )
            first, second = plan.artifacts

            def fail_install_and_rollback(source, destination):
                source, destination = Path(source), Path(destination)
                if source == second.staged_path and destination == second.destination:
                    raise OSError("install failed")
                if source == first.backup_path and destination == first.destination:
                    raise OSError("rollback failed")
                os.replace(source, destination)

            with self.assertRaisesRegex(InstallError, "rollback failed"):
                UpdateExecutor(
                    plan, downloader=FakeDownloader(contents),
                    replace_file=fail_install_and_rollback,
                ).execute(post_install_validator=lambda _plan: None)
            manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "recovery-required")

    def test_execute_writes_manifest_only_through_helper(self) -> None:
        import lmstudio_weight_updater as updater

        tree = ast.parse(Path(updater.__file__).read_text(encoding="utf-8"))
        executor = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                        and node.name == "UpdateExecutor")
        execute = next(node for node in executor.body if isinstance(node, ast.FunctionDef)
                       and node.name == "execute")
        helper = next(node for node in executor.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_write_phase_manifest")
        self.assertFalse(any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "write_manifest" for node in ast.walk(execute)
        ))
        self.assertEqual(sum(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "write_manifest" for node in ast.walk(helper)
        ), 1)

    def test_shared_run_returns_result_and_fresh_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            fresh = check_result(
                key="model", name="Model", repo="org/repo",
                artifacts=[artifact_result(
                    destination=plan.artifacts[0].destination,
                    remote_file="model.gguf", content=b"new",
                )],
            )
            fresh.status = "up-to-date"
            with (patch("lmstudio_weight_updater.collect_check_results", return_value=[fresh]),
                  patch("lmstudio_weight_updater.save_hash_cache")):
                result, results = run_update_plan(
                    plan, timeout_seconds=5, hash_cache={},
                    hash_cache_path=root / "hash-cache.json",
                    downloader=FakeDownloader(contents),
                )
            self.assertTrue(result.success)
            self.assertEqual(result.job_id, plan.job_id)
            self.assertEqual(results, [fresh])

    def test_shared_run_validator_rejects_non_up_to_date_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            stale = check_result(
                key="model", name="Model", repo="org/repo",
                artifacts=[artifact_result(
                    destination=plan.artifacts[0].destination,
                    remote_file="model.gguf", content=b"new",
                )],
            )
            with (patch("lmstudio_weight_updater.collect_check_results", return_value=[stale]),
                  patch("lmstudio_weight_updater.save_hash_cache")):
                with self.assertRaisesRegex(
                    InstallError, "Post-install checker did not confirm success: model: update-available"
                ):
                    run_update_plan(
                        plan, timeout_seconds=5, hash_cache={},
                        hash_cache_path=root / "hash-cache.json",
                        downloader=FakeDownloader(contents),
                    )

    def test_shared_run_saves_hash_cache_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            fresh = check_result(
                key="model", name="Model", repo="org/repo",
                artifacts=[artifact_result(
                    destination=plan.artifacts[0].destination,
                    remote_file="model.gguf", content=b"new",
                )],
            )
            fresh.status = "up-to-date"
            cache = {}
            with (patch("lmstudio_weight_updater.collect_check_results", return_value=[fresh]),
                  patch("lmstudio_weight_updater.save_hash_cache") as save):
                run_update_plan(
                    plan, timeout_seconds=5, hash_cache=cache,
                    hash_cache_path=root / "hash-cache.json",
                    downloader=FakeDownloader(contents),
                )
            save.assert_called_once_with(root / "hash-cache.json", cache)

    def test_shared_run_saves_hash_cache_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            cache = {}
            with (patch("lmstudio_weight_updater.UpdateExecutor.execute",
                        side_effect=InstallError("install failed")),
                  patch("lmstudio_weight_updater.save_hash_cache") as save):
                with self.assertRaisesRegex(InstallError, "install failed"):
                    run_update_plan(
                        plan, timeout_seconds=5, hash_cache=cache,
                        hash_cache_path=root / "hash-cache.json",
                    )
            save.assert_called_once_with(root / "hash-cache.json", cache)

    def test_shared_run_saves_failure_refresh_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            cache = {}
            failure_results = []
            saved_snapshots = []
            stale = check_result(
                key="model", name="Model", repo="org/repo",
                artifacts=[artifact_result(
                    destination=plan.artifacts[0].destination,
                    remote_file="model.gguf", content=b"new",
                )],
            )

            def refresh(**_kwargs):
                cache["refreshed"] = {"sha256": "abc"}
                return [stale]

            with (patch("lmstudio_weight_updater.UpdateExecutor.execute",
                        side_effect=InstallError("install failed")),
                  patch("lmstudio_weight_updater.collect_check_results", side_effect=refresh),
                  patch("lmstudio_weight_updater.save_hash_cache",
                        side_effect=lambda _path, data: saved_snapshots.append(dict(data))) as save):
                with self.assertRaisesRegex(InstallError, "install failed"):
                    run_update_plan(
                        plan, timeout_seconds=5, hash_cache=cache,
                        hash_cache_path=root / "hash-cache.json",
                        failure_results_out=failure_results,
                    )
            self.assertEqual(failure_results, [stale])
            save.assert_called_once_with(root / "hash-cache.json", cache)
            self.assertIn("refreshed", saved_snapshots[0])

    def test_cli_and_tray_share_the_run_function(self) -> None:
        import lmstudio_weight_updater as updater
        import lmstudio_weight_watcher as watcher

        self.assertIs(updater.run_update_plan, watcher.run_update_plan)
        tree = ast.parse(Path(watcher.__file__).read_text(encoding="utf-8"))
        for function in (node for node in ast.walk(tree)
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            self.assertFalse(any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "Post-install checker did not confirm success" in node.value
                for node in ast.walk(function)
            ), function.name)

    def test_missing_xet_emits_one_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("one.gguf", b"old", b"one"), ("two.gguf", b"old", b"two")]
            )
            events = []
            with patch("lmstudio_weight_updater.check_xet_acceleration",
                       return_value=(False, "hf_xet is missing")) as check:
                result = UpdateExecutor(
                    plan, downloader=FakeDownloader(contents), progress=events.append
                ).execute(post_install_validator=lambda _plan: None)
            warnings = [event for event in events if "slower HTTP path" in event.message]
            self.assertTrue(result.success)
            self.assertEqual(len(warnings), 1)
            self.assertIn("hf_xet is missing", warnings[0].message)
            check.assert_called_once()

    def test_present_xet_emits_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            events = []
            with patch("lmstudio_weight_updater.check_xet_acceleration", return_value=(True, "")):
                UpdateExecutor(plan, downloader=FakeDownloader(contents), progress=events.append).execute(
                    post_install_validator=lambda _plan: None
                )
            self.assertFalse(any("Xet" in event.message or "transfers" in event.message
                                 for event in events))

    def test_missing_xet_still_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            downloader = FakeDownloader(contents)
            with patch("lmstudio_weight_updater.check_xet_acceleration",
                       return_value=(False, "hf_xet is missing")):
                result = UpdateExecutor(plan, downloader=downloader).execute(
                    post_install_validator=lambda _plan: None
                )
            self.assertTrue(result.success)
            self.assertEqual(downloader.calls, ["model.gguf"])

    def test_availability_check_tolerates_missing_hub(self) -> None:
        from lmstudio_weight_updater import check_xet_acceleration
        imports = []
        def missing_hub(name):
            imports.append(name)
            raise ImportError("missing hub")
        with patch("lmstudio_weight_updater.importlib.import_module", side_effect=missing_hub):
            available, reason = check_xet_acceleration(Path("scratch") / ".hf-xet")
        self.assertFalse(available)
        self.assertIn("huggingface_hub", reason)
        self.assertEqual(imports, ["huggingface_hub"])

    def test_import_error_message_no_longer_requires_xet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            original_import = builtins.__import__
            def missing_hub(name, *args, **kwargs):
                if name == "huggingface_hub":
                    raise ImportError("missing hub")
                return original_import(name, *args, **kwargs)
            with patch("builtins.__import__", side_effect=missing_hub):
                with self.assertRaises(UpdateError) as raised:
                    HuggingFaceDownloader().download(
                        plan.artifacts[0], cancellation=CancellationToken(),
                        progress=lambda _done, _total: None,
                    )
            self.assertIn("requires huggingface_hub", str(raised.exception))
            self.assertIn("hf_xet is optional", str(raised.exception))

    def test_plan_does_not_reread_verified_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            artifact.staged_path.parent.mkdir(parents=True)
            artifact.staged_path.write_bytes(contents[artifact.remote_file])
            verify_staged_artifact(artifact, cancellation=CancellationToken())
            staged_bytes_read = 0
            original_open = Path.open
            class CountedReader:
                def __init__(self, handle):
                    self.handle = handle
                def __enter__(self):
                    self.handle.__enter__()
                    return self
                def __exit__(self, *args):
                    return self.handle.__exit__(*args)
                def read(self, *args, **kwargs):
                    nonlocal staged_bytes_read
                    data = self.handle.read(*args, **kwargs)
                    staged_bytes_read += len(data)
                    return data
                def __getattr__(self, name):
                    return getattr(self.handle, name)
            def count_open(path, *args, **kwargs):
                handle = original_open(path, *args, **kwargs)
                return CountedReader(handle) if path == artifact.staged_path else handle
            with patch.object(Path, "open", count_open):
                rebuilt = self.rebuild_staged_plan(root, plan, contents[artifact.remote_file])
            self.assertEqual(staged_bytes_read, 0)
            self.assertEqual(rebuilt.remaining_download_bytes, 0)

    def test_marker_written_after_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            marker = _verification_marker_path(artifact.staged_path)
            observed = []
            def observe_move(source, destination):
                if Path(source) == artifact.staged_path and Path(destination) == artifact.destination:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
                    observed.append((payload, manifest))
                os.replace(source, destination)
            UpdateExecutor(
                plan, downloader=FakeDownloader(contents), replace_file=observe_move
            ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(len(observed), 1)
            payload, manifest = observed[0]
            self.assertEqual(payload["expected_size"], artifact.expected_size)
            self.assertEqual(payload["expected_sha256"], artifact.expected_sha256)
            self.assertEqual(payload["staged_size"], artifact.expected_size)
            self.assertIsInstance(payload["st_mtime_ns"], int)
            self.assertIn(str(marker), manifest["owned_staging_files"])

    def test_stale_marker_mtime_invalidates_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            artifact.staged_path.parent.mkdir(parents=True)
            artifact.staged_path.write_bytes(contents[artifact.remote_file])
            verify_staged_artifact(artifact, cancellation=CancellationToken())
            stat = artifact.staged_path.stat()
            os.utime(artifact.staged_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            rebuilt = self.rebuild_staged_plan(root, plan, contents[artifact.remote_file])
            self.assertEqual(rebuilt.remaining_download_bytes, artifact.expected_size)

    def test_marker_hash_mismatch_invalidates_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            artifact.staged_path.parent.mkdir(parents=True)
            artifact.staged_path.write_bytes(contents[artifact.remote_file])
            verify_staged_artifact(artifact, cancellation=CancellationToken())
            marker = _verification_marker_path(artifact.staged_path)
            payload = json.loads(marker.read_text(encoding="utf-8"))
            payload["expected_sha256"] = "0" * 64
            marker.write_text(json.dumps(payload), encoding="utf-8")
            rebuilt = self.rebuild_staged_plan(root, plan, contents[artifact.remote_file])
            self.assertEqual(rebuilt.remaining_download_bytes, artifact.expected_size)

    def test_marker_size_mismatch_invalidates_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            artifact.staged_path.parent.mkdir(parents=True)
            artifact.staged_path.write_bytes(contents[artifact.remote_file])
            verify_staged_artifact(artifact, cancellation=CancellationToken())
            marker = _verification_marker_path(artifact.staged_path)
            for field in ("expected_size", "staged_size"):
                with self.subTest(field=field):
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    payload[field] += 1
                    marker.write_text(json.dumps(payload), encoding="utf-8")
                    rebuilt = self.rebuild_staged_plan(root, plan, contents[artifact.remote_file])
                    self.assertEqual(rebuilt.remaining_download_bytes, artifact.expected_size)
                    payload[field] -= 1
                    marker.write_text(json.dumps(payload), encoding="utf-8")

    def test_corrupt_marker_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            artifact.staged_path.parent.mkdir(parents=True)
            artifact.staged_path.write_bytes(contents[artifact.remote_file])
            _verification_marker_path(artifact.staged_path).write_text("{broken", encoding="utf-8")
            rebuilt = self.rebuild_staged_plan(root, plan, contents[artifact.remote_file])
            self.assertEqual(rebuilt.remaining_download_bytes, artifact.expected_size)

    def test_marker_is_cleaned_up_with_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            marker = _verification_marker_path(plan.artifacts[0].staged_path)
            result = UpdateExecutor(plan, downloader=FakeDownloader(contents)).execute(
                post_install_validator=lambda _plan: None
            )
            self.assertTrue(result.success)
            self.assertFalse(marker.exists())

    def test_marker_ownership_survives_interrupted_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            marker = _verification_marker_path(artifact.staged_path)
            def fail_install(source, destination):
                if Path(source) == artifact.staged_path and Path(destination) == artifact.destination:
                    raise OSError("install interrupted")
                os.replace(source, destination)
            with self.assertRaisesRegex(InstallError, "install interrupted"):
                UpdateExecutor(
                    plan, downloader=FakeDownloader(contents), replace_file=fail_install
                ).execute(post_install_validator=lambda _plan: None)
            manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(marker.exists())
            self.assertIn(str(marker), manifest["owned_staging_files"])

    def test_marker_is_not_written_after_failed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            artifact.staged_path.parent.mkdir(parents=True)
            artifact.staged_path.write_bytes(b"bad")
            with self.assertRaises(IntegrityError):
                verify_staged_artifact(artifact, cancellation=CancellationToken())
            self.assertFalse(_verification_marker_path(artifact.staged_path).exists())

    def test_downloader_passes_cache_dir_in_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            seen = {}
            def fake_download(**kwargs):
                seen.update(kwargs)
                artifact.staged_path.write_bytes(contents[artifact.remote_file])
                return str(artifact.staged_path)
            with patch("huggingface_hub.hf_hub_download", side_effect=fake_download):
                HuggingFaceDownloader().download(
                    artifact, cancellation=CancellationToken(), progress=lambda _done, _total: None
                )
            cache = Path(seen["cache_dir"])
            self.assertTrue(cache.is_relative_to(plan.staging_root))
            self.assertTrue(cache.is_relative_to(artifact.download_dir))
            self.assertNotEqual(cache, Path.home() / ".cache" / "huggingface" / "hub")

    def test_downloader_uses_local_dir_and_no_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            seen = {}
            def fake_download(**kwargs):
                seen.update(kwargs)
                artifact.staged_path.write_bytes(contents[artifact.remote_file])
                return str(artifact.staged_path)
            cancellation = CancellationToken()
            with patch("huggingface_hub.hf_hub_download", side_effect=fake_download):
                HuggingFaceDownloader().download(
                    artifact, cancellation=cancellation, progress=lambda _done, _total: None
                )
            self.assertEqual(seen["local_dir"], str(artifact.download_dir))
            self.assertFalse(seen["force_download"])
            tracker = seen["tqdm_class"](total=3, disable=True)
            cancellation.cancel()
            with self.assertRaises(UpdateCancelled):
                tracker.update(1)
            tracker.close()

    def test_download_path_must_match_staged_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            wrong = artifact.download_dir / "wrong.gguf"
            wrong.parent.mkdir(parents=True)
            wrong.write_bytes(b"new")
            with patch("huggingface_hub.hf_hub_download", return_value=str(wrong)):
                with self.assertRaisesRegex(UpdateError, "unexpected staging path"):
                    HuggingFaceDownloader().download(
                        artifact, cancellation=CancellationToken(), progress=lambda _done, _total: None
                    )

    def test_downloader_final_stat_error_names_artifact_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            expected = artifact.staged_path.resolve(strict=False)
            original_stat = Path.stat
            progress_calls = []

            def fake_download(**_kwargs):
                artifact.staged_path.write_bytes(contents[artifact.remote_file])
                return str(artifact.staged_path)

            def fail_final_stat(path, *args, **kwargs):
                if path == expected:
                    raise OSError("file vanished")
                return original_stat(path, *args, **kwargs)

            with (patch("huggingface_hub.hf_hub_download", side_effect=fake_download),
                  patch.object(Path, "stat", autospec=True, side_effect=fail_final_stat)):
                with self.assertRaises(UpdateError) as raised:
                    HuggingFaceDownloader().download(
                        artifact, cancellation=CancellationToken(),
                        progress=lambda done, total: progress_calls.append((done, total)),
                    )
            self.assertIn(artifact.label, str(raised.exception))
            self.assertIn(str(expected), str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertEqual(progress_calls, [])

    def test_successful_update_reports_no_cleanup_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            metadata = artifact.download_dir / ".cache" / "huggingface" / "metadata"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("resume", encoding="utf-8")
            xet_log = artifact.download_dir / ".hf-xet" / "logs" / "xet.log"
            class CachingDownloader(FakeDownloader):
                def download(self, current, *, cancellation, progress):
                    result = super().download(current, cancellation=cancellation, progress=progress)
                    (current.download_dir / ".cache" / "huggingface" / "new-meta").write_text(
                        "owned", encoding="utf-8"
                    )
                    xet_log.parent.mkdir(parents=True)
                    xet_log.write_text("local", encoding="utf-8")
                    return result
            result = UpdateExecutor(plan, downloader=CachingDownloader(contents)).execute(
                post_install_validator=lambda _plan: None
            )
            manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertIn(str(metadata), manifest["owned_staging_files"])
            self.assertIn(str(xet_log), manifest["owned_staging_files"])
            self.assertNotIn("Preserved", result.message)
            self.assertNotIn("Cleanup remains", result.message)
            self.assertFalse(artifact.download_dir.exists())

    def test_downloader_does_not_touch_global_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _root, plan, contents = self.make_plan(base, [("model.gguf", b"old", b"new")])
            profile = base / "profile"
            profile.mkdir()
            artifact = plan.artifacts[0]
            artifact.staged_path.parent.mkdir(parents=True)
            artifact.staged_path.write_bytes(contents[artifact.remote_file])
            resume_metadata = artifact.download_dir / ".cache" / "huggingface" / "resume"
            resume_metadata.parent.mkdir(parents=True)
            resume_metadata.write_text("complete", encoding="utf-8")
            downloader = FakeDownloader(contents)
            with patch("pathlib.Path.home", return_value=profile):
                result = UpdateExecutor(plan, downloader=downloader).execute(
                    post_install_validator=lambda _plan: None
                )
            self.assertTrue(result.success)
            self.assertEqual(downloader.calls, [])
            self.assertFalse(resume_metadata.exists())
            self.assertEqual(list(profile.rglob("*")), [])

    def test_downloader_scopes_xet_cache_and_restores_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _root, plan, contents = self.make_plan(base, [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            profile = base / "profile"
            profile.mkdir()
            previous = str(profile / "existing-xet-cache")
            seen = []
            def fake_download(**kwargs):
                xet_cache = Path(os.environ["HF_XET_CACHE"])
                seen.append(xet_cache)
                log = xet_cache / "logs" / "xet.log"
                log.parent.mkdir(parents=True)
                log.write_text("scoped", encoding="utf-8")
                artifact.staged_path.write_bytes(contents[artifact.remote_file])
                return str(artifact.staged_path)
            with (patch.dict(os.environ, {"HF_XET_CACHE": previous}),
                  patch("pathlib.Path.home", return_value=profile),
                  patch("huggingface_hub.hf_hub_download", side_effect=fake_download),
                  patch("huggingface_hub.utils._xet.abort_xet_session", create=True) as abort):
                HuggingFaceDownloader().download(
                    artifact, cancellation=CancellationToken(), progress=lambda _done, _total: None
                )
                self.assertEqual(os.environ["HF_XET_CACHE"], previous)
            self.assertEqual(seen, [artifact.download_dir / ".hf-xet"])
            self.assertTrue((seen[0] / "logs" / "xet.log").exists())
            self.assertEqual(list(profile.rglob("*")), [])
            self.assertEqual(abort.call_count, 2)

    def test_downloader_restores_xet_environment_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("one.gguf", b"old", b"one"), ("two.gguf", b"old", b"two")]
            )
            first, second = plan.artifacts
            seen = []
            def fake_download(**kwargs):
                seen.append(Path(os.environ["HF_XET_CACHE"]))
                if kwargs["filename"] == first.remote_file:
                    raise OSError("network failed")
                second.staged_path.write_bytes(contents[second.remote_file])
                return str(second.staged_path)
            with (patch.dict(os.environ, {"HF_XET_CACHE": "previous-cache"}),
                  patch("huggingface_hub.hf_hub_download", side_effect=fake_download),
                  patch("huggingface_hub.utils._xet.abort_xet_session", create=True) as abort):
                with self.assertRaisesRegex(OSError, "network failed"):
                    HuggingFaceDownloader().download(
                        first, cancellation=CancellationToken(), progress=lambda _done, _total: None
                    )
                self.assertEqual(os.environ["HF_XET_CACHE"], "previous-cache")
                HuggingFaceDownloader().download(
                    second, cancellation=CancellationToken(), progress=lambda _done, _total: None
                )
                self.assertEqual(os.environ["HF_XET_CACHE"], "previous-cache")
            self.assertEqual(seen, [first.download_dir / ".hf-xet", second.download_dir / ".hf-xet"])
            self.assertEqual(abort.call_count, 4)

    def test_concurrent_downloads_keep_xet_caches_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("one.gguf", b"old", b"one"), ("two.gguf", b"old", b"two")]
            )
            by_name = {artifact.remote_file: artifact for artifact in plan.artifacts}
            seen = {}
            active = 0
            highest_active = 0
            guard = threading.Lock()
            def fake_download(**kwargs):
                nonlocal active, highest_active
                artifact = by_name[kwargs["filename"]]
                with guard:
                    active += 1
                    highest_active = max(highest_active, active)
                seen[artifact.remote_file] = Path(os.environ["HF_XET_CACHE"])
                time.sleep(0.02)
                artifact.staged_path.write_bytes(contents[artifact.remote_file])
                with guard:
                    active -= 1
                return str(artifact.staged_path)
            with (patch.dict(os.environ, {"HF_XET_CACHE": "previous-cache"}),
                  patch("huggingface_hub.hf_hub_download", side_effect=fake_download),
                  patch("huggingface_hub.utils._xet.abort_xet_session", create=True)):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(HuggingFaceDownloader().download, artifact,
                                           cancellation=CancellationToken(),
                                           progress=lambda _done, _total: None)
                               for artifact in plan.artifacts]
                    for future in futures:
                        future.result()
                self.assertEqual(os.environ["HF_XET_CACHE"], "previous-cache")
            self.assertEqual(highest_active, 1)
            self.assertEqual(
                seen, {artifact.remote_file: artifact.download_dir / ".hf-xet"
                       for artifact in plan.artifacts}
            )

    def test_success_verifies_installs_seeds_cache_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new bytes")])
            cache = {}
            result = UpdateExecutor(
                plan,
                downloader=FakeDownloader(contents),
                hash_cache=cache,
            ).execute(post_install_validator=lambda _plan: None)

            self.assertTrue(result.success)
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"new bytes")
            self.assertEqual(plan.artifacts[0].backup_path.read_bytes(), b"old")
            self.assertTrue(plan.manifest_path.exists())
            self.assertFalse(plan.artifacts[0].staged_path.exists())
            self.assertEqual(cache["org/repo/model.gguf"]["sha256"], digest(b"new bytes"))

    def test_success_retains_backups_until_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            result = UpdateExecutor(plan, downloader=FakeDownloader(contents)).execute(
                post_install_validator=lambda _plan: None
            )
            manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(result.success)
            self.assertEqual(result.backup_paths, ())
            self.assertEqual(result.message, "All selected artifacts were updated and verified.")
            self.assertEqual(plan.artifacts[0].backup_path.read_bytes(), b"old")
            self.assertEqual(manifest["phase"], "completed-cleanup-pending")
            self.assertEqual(manifest["retained_backups"], [str(plan.artifacts[0].backup_path)])

    def test_cleanup_pass_removes_only_manifested_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            UpdateExecutor(plan, downloader=FakeDownloader(contents)).execute(
                post_install_validator=lambda _plan: None
            )
            unrelated = plan.artifacts[0].destination.with_name("unrelated.txt")
            unrelated.write_text("keep", encoding="utf-8")
            actions = cleanup_pending_backups(root, model_is_current=lambda _keys: True)
            self.assertTrue(any(action.action == "cleaned" for action in actions))
            self.assertFalse(plan.artifacts[0].backup_path.exists())
            self.assertFalse(plan.manifest_path.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_recovery_restores_missing_destination_from_pending_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            UpdateExecutor(plan, downloader=FakeDownloader(contents)).execute(
                post_install_validator=lambda _plan: None
            )
            plan.artifacts[0].destination.unlink()
            actions = recover_interrupted_jobs(root)
            self.assertTrue(any(action.action == "restored" for action in actions))
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old")
            self.assertEqual(plan.artifacts[0].backup_path.read_bytes(), b"old")

    def test_cleanup_pass_keeps_backups_when_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            UpdateExecutor(plan, downloader=FakeDownloader(contents)).execute(
                post_install_validator=lambda _plan: None
            )
            first = cleanup_pending_backups(root, model_is_current=lambda _keys: False)
            second = cleanup_pending_backups(root, model_is_current=lambda _keys: False)
            self.assertEqual([action.action for action in first], ["reported"])
            self.assertEqual(second, [])
            self.assertEqual(plan.artifacts[0].backup_path.read_bytes(), b"old")
            manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["cleanup_reported"])

    def test_keep_backups_survives_cleanup_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            result = UpdateExecutor(
                plan, downloader=FakeDownloader(contents), keep_backups=True
            ).execute(post_install_validator=lambda _plan: None)
            actions = cleanup_pending_backups(root, model_is_current=lambda _keys: True)
            manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(actions, [])
            self.assertEqual(result.backup_paths, (str(plan.artifacts[0].backup_path),))
            self.assertEqual(plan.artifacts[0].backup_path.read_bytes(), b"old")
            self.assertEqual(manifest["phase"], "completed-backups-kept")
            self.assertTrue(manifest["keep_backups"])

    def test_cleanup_ignores_unmanifested_backup_looking_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            lookalike = plan.artifacts[0].backup_path
            lookalike.write_bytes(b"user data")
            actions = cleanup_pending_backups(root, model_is_current=lambda _keys: True)
            self.assertEqual(actions, [])
            self.assertEqual(lookalike.read_bytes(), b"user data")

    def test_next_execute_cleans_previous_validated_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _root, first, contents = self.make_plan(base, [("one.gguf", b"old", b"new")])
            UpdateExecutor(first, downloader=FakeDownloader(contents)).execute(
                post_install_validator=lambda _plan: None
            )
            _root, second, contents = self.make_plan(base, [("two.gguf", b"old2", b"new2")])
            UpdateExecutor(second, downloader=FakeDownloader(contents)).execute(
                post_install_validator=lambda _plan: None
            )
            self.assertFalse(first.artifacts[0].backup_path.exists())
            self.assertTrue(second.artifacts[0].backup_path.exists())

    def test_hash_mismatch_leaves_installed_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"right")])
            downloader = FakeDownloader({"model.gguf": b"wrong"})
            with self.assertRaises(IntegrityError):
                UpdateExecutor(plan, downloader=downloader).execute(
                    post_install_validator=lambda _plan: None
                )
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old")
            self.assertFalse(plan.artifacts[0].backup_path.exists())

    def test_size_mismatch_leaves_installed_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, _contents = self.make_plan(
                Path(tmp), [("model.gguf", b"old", b"right")]
            )
            downloader = FakeDownloader({"model.gguf": b"wrong-size"})
            with self.assertRaisesRegex(IntegrityError, "Size mismatch"):
                UpdateExecutor(plan, downloader=downloader).execute(
                    post_install_validator=lambda _plan: None
                )
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old")
            self.assertFalse(plan.artifacts[0].backup_path.exists())

    def test_partial_staging_download_resumes_and_installs_verified_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("model.gguf", b"old", b"new-complete-bytes")]
            )
            artifact = plan.artifacts[0]
            prefix = b"new-com"
            artifact.staged_path.parent.mkdir(parents=True, exist_ok=True)
            artifact.staged_path.write_bytes(prefix)

            class ResumingDownloader:
                def __init__(self) -> None:
                    self.saw_prefix = False

                def download(self, current, *, cancellation, progress):
                    cancellation.raise_if_cancelled()
                    existing = current.staged_path.read_bytes()
                    self.saw_prefix = existing == prefix
                    complete = contents[current.remote_file]
                    current.staged_path.write_bytes(existing + complete[len(existing):])
                    progress(len(complete), len(complete))
                    return current.staged_path

            downloader = ResumingDownloader()
            result = UpdateExecutor(plan, downloader=downloader).execute(
                post_install_validator=lambda _plan: None
            )

            self.assertTrue(result.success)
            self.assertTrue(downloader.saw_prefix)
            self.assertEqual(artifact.destination.read_bytes(), b"new-complete-bytes")

    def test_post_install_failure_rolls_back_all_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp),
                [("one.gguf", b"old-one", b"new-one"), ("two.gguf", b"old-two", b"new-two")],
            )
            with self.assertRaisesRegex(RuntimeError, "validator failed"):
                UpdateExecutor(plan, downloader=FakeDownloader(contents)).execute(
                    post_install_validator=lambda _plan: (_ for _ in ()).throw(RuntimeError("validator failed"))
                )
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old-one")
            self.assertEqual(plan.artifacts[1].destination.read_bytes(), b"old-two")

    def test_rollback_attempts_backup_restore_after_move_back_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("one.gguf", b"old-one", b"new-one"), ("two.gguf", b"old-two", b"new-two")]
            )
            first = plan.artifacts[0]
            parked = first.staged_path.with_name(first.staged_path.name + ".rollback")
            failed_moves = 0

            def fail_move_back(source, destination):
                nonlocal failed_moves
                if Path(source) == parked and Path(destination) == first.staged_path:
                    failed_moves += 1
                    raise OSError("stage move failed")
                os.replace(source, destination)

            with self.assertRaisesRegex(InstallError, "stage move failed"):
                UpdateExecutor(
                    plan, downloader=FakeDownloader(contents), replace_file=fail_move_back
                ).execute(post_install_validator=lambda _plan: (_ for _ in ()).throw(RuntimeError("invalid")))
            self.assertEqual(failed_moves, 1)
            self.assertEqual(first.destination.read_bytes(), b"old-one")
            self.assertEqual(plan.artifacts[1].destination.read_bytes(), b"old-two")

    def _run_partial_rollback(self, base: Path):
        _root, plan, contents = self.make_plan(
            base, [("one.gguf", b"old-one", b"new-one"), ("two.gguf", b"old-two", b"new-two")]
        )
        failed = plan.artifacts[1]
        restore_attempts = 0

        def fail_one_restore(source, destination):
            nonlocal restore_attempts
            if Path(source) == failed.backup_path and Path(destination) == failed.destination:
                restore_attempts += 1
                raise OSError("restore failed")
            os.replace(source, destination)

        with self.assertRaises(InstallError) as raised:
            UpdateExecutor(
                plan, downloader=FakeDownloader(contents), replace_file=fail_one_restore
            ).execute(post_install_validator=lambda _plan: (_ for _ in ()).throw(RuntimeError("invalid")))
        self.assertEqual(restore_attempts, 1)
        return plan, raised.exception

    def test_rollback_reports_artifact_level_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, error = self._run_partial_rollback(Path(tmp))
            self.assertIn("restore failed", str(error))
            manifest = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "recovery-required")
            statuses = {entry["destination"]: entry for entry in manifest["rollback_status"]}
            first, second = plan.artifacts
            self.assertEqual(statuses[str(first.destination)]["backup_path"], str(first.backup_path))
            self.assertTrue(statuses[str(first.destination)]["original_restored"])
            self.assertEqual(statuses[str(second.destination)]["backup_path"], str(second.backup_path))
            self.assertFalse(statuses[str(second.destination)]["original_restored"])
            self.assertEqual(first.destination.read_bytes(), b"old-one")
            self.assertEqual(second.backup_path.read_bytes(), b"old-two")

    def test_partial_rollback_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan, error = self._run_partial_rollback(Path(tmp))
            self.assertIsInstance(error, InstallError)
            self.assertTrue(plan.manifest_path.exists())

    def test_rollback_restores_every_artifact_on_full_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("one.gguf", b"old-one", b"new-one"), ("two.gguf", b"old-two", b"new-two")]
            )
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                UpdateExecutor(plan, downloader=FakeDownloader(contents)).execute(
                    post_install_validator=lambda _plan: (_ for _ in ()).throw(RuntimeError("invalid"))
                )
            self.assertEqual([a.destination.read_bytes() for a in plan.artifacts], [b"old-one", b"old-two"])

    def test_partial_install_move_is_included_in_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("one.gguf", b"old-one", b"new-one"), ("two.gguf", b"old-two", b"new-two")]
            )
            second = plan.artifacts[1]
            injected = 0

            def move_then_fail(source, destination):
                nonlocal injected
                os.replace(source, destination)
                if Path(source) == second.staged_path and Path(destination) == second.destination:
                    injected += 1
                    raise OSError("failed after move")

            with self.assertRaisesRegex(InstallError, "failed after move"):
                UpdateExecutor(
                    plan, downloader=FakeDownloader(contents), replace_file=move_then_fail
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(injected, 1)
            self.assertEqual([a.destination.read_bytes() for a in plan.artifacts], [b"old-one", b"old-two"])

    def test_recovery_restores_from_partial_rollback_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            plan, _error = self._run_partial_rollback(Path(tmp))
            failed = plan.artifacts[1]
            if failed.destination.exists():
                failed.destination.unlink()
            actions = recover_interrupted_jobs(root)
            self.assertEqual(failed.destination.read_bytes(), b"old-two")
            self.assertEqual(failed.backup_path.read_bytes(), b"old-two")
            self.assertTrue(any(action.action == "restored" and action.path == str(failed.destination) for action in actions))

    def test_partial_install_failure_rolls_back_prior_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp),
                [("one.gguf", b"old-one", b"new-one"), ("two.gguf", b"old-two", b"new-two")],
            )
            calls = 0

            def fail_fourth(source, destination):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError("injected replacement failure")
                os.replace(source, destination)

            with self.assertRaisesRegex(InstallError, "injected replacement failure"):
                UpdateExecutor(
                    plan,
                    downloader=FakeDownloader(contents),
                    replace_file=fail_fourth,
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old-one")
            self.assertEqual(plan.artifacts[1].destination.read_bytes(), b"old-two")

    def test_permission_error_restores_original_and_mentions_unload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            calls = 0

            def locked_second(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PermissionError("locked")
                os.replace(source, destination)

            with self.assertRaisesRegex(InstallError, "Unload the model"):
                UpdateExecutor(
                    plan,
                    downloader=FakeDownloader(contents),
                    replace_file=locked_second,
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old")

    def _failed_restore_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            moves: dict[str, int] = {"backup": 0, "install": 0, "restore": 0}

            def fail_install_and_restore(source, destination):
                source, destination = Path(source), Path(destination)
                if source == artifact.destination and destination == artifact.backup_path:
                    moves["backup"] += 1
                elif source == artifact.staged_path and destination == artifact.destination:
                    moves["install"] += 1
                    raise PermissionError("install locked")
                elif source == artifact.backup_path and destination == artifact.destination:
                    moves["restore"] += 1
                    raise PermissionError("restore locked")
                os.replace(source, destination)

            with self.assertRaises(InstallError) as raised:
                UpdateExecutor(
                    plan,
                    downloader=FakeDownloader(contents),
                    replace_file=fail_install_and_restore,
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(moves, {"backup": 1, "install": 1, "restore": 1})
            return (
                raised.exception,
                str(artifact.destination),
                str(artifact.backup_path),
                artifact.backup_path.read_bytes(),
                json.loads(plan.manifest_path.read_text(encoding="utf-8")),
            )

    def test_failed_restore_keeps_original_reachable(self) -> None:
        error, destination, backup, original, _manifest = self._failed_restore_case()
        self.assertEqual(original, b"old")
        self.assertIn(destination, str(error))
        self.assertIn(backup, str(error))
        self.assertIn("restore locked", str(error))

    def test_failed_restore_names_required_recovery(self) -> None:
        _error, destination, backup, _original, manifest = self._failed_restore_case()
        self.assertEqual(manifest["phase"], "recovery-required")
        self.assertIn(destination, manifest["error"])
        self.assertIn(backup, manifest["error"])
        self.assertEqual(manifest["artifacts"][0]["destination"], destination)

    def test_restore_failure_is_not_swallowed(self) -> None:
        error, _destination, _backup, _original, _manifest = self._failed_restore_case()
        self.assertIsInstance(error, InstallError)

    def test_locked_file_guidance_survives_restore_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            artifact = plan.artifacts[0]
            install_moves = 0

            def fail_install(source, destination):
                nonlocal install_moves
                if Path(source) == artifact.staged_path and Path(destination) == artifact.destination:
                    install_moves += 1
                    raise PermissionError("locked")
                os.replace(source, destination)

            with self.assertRaises(InstallError) as raised:
                UpdateExecutor(
                    plan,
                    downloader=FakeDownloader(contents),
                    replace_file=fail_install,
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(install_moves, 1)
            self.assertIn("Unload the model in LM Studio", str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_cancellation_before_commit_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            token = CancellationToken()
            token.cancel()
            with self.assertRaises(UpdateCancelled):
                UpdateExecutor(
                    plan,
                    downloader=FakeDownloader(contents),
                    cancellation=token,
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old")

    def test_cancellation_wins_at_non_cancellable_handoff_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(
                Path(tmp), [("model.gguf", b"old", b"new")]
            )
            token = CancellationToken()

            def cancel_at_handoff(event):
                if event.phase == "installing":
                    token.cancel()

            with self.assertRaises(UpdateCancelled):
                UpdateExecutor(
                    plan,
                    downloader=FakeDownloader(contents),
                    cancellation=token,
                    progress=cancel_at_handoff,
                ).execute(post_install_validator=lambda _plan: None)
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old")

    def test_manifest_contains_recovery_metadata_but_no_signed_urls_or_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            write_manifest(plan, "downloading")
            text = plan.manifest_path.read_text(encoding="utf-8")
            payload = json.loads(text)
            self.assertEqual(payload["job_id"], plan.job_id)
            self.assertNotIn("HF_TOKEN", text)
            self.assertNotIn("resolve/main", text)
            self.assertNotIn("X-Amz-Signature", text)

    def test_failure_manifest_redacts_tokens_and_signed_query_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, _contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])

            class FailingDownloader:
                def download(self, artifact, *, cancellation, progress):
                    raise RuntimeError(
                        "hf_secret=very-secret Bearer abcdefghijk "
                        "https://example.test/file?X-Amz-Signature=signed-value&token=token-value"
                    )

            with self.assertRaises(RuntimeError):
                UpdateExecutor(plan, downloader=FailingDownloader()).execute(
                    post_install_validator=lambda _plan: None
                )
            text = plan.manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("very-secret", text)
            self.assertNotIn("abcdefghijk", text)
            self.assertNotIn("signed-value", text)
            self.assertNotIn("token-value", text)
            self.assertIn("[REDACTED]", text)

    def test_validator_rollback_restores_hash_cache_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            cache = {"unrelated": {"size": 1, "mtime_ns": 2, "sha256": "abc"}}
            with self.assertRaisesRegex(RuntimeError, "validator"):
                UpdateExecutor(
                    plan,
                    downloader=FakeDownloader(contents),
                    hash_cache=cache,
                ).execute(
                    post_install_validator=lambda _plan: (_ for _ in ()).throw(RuntimeError("validator"))
                )
            self.assertEqual(plan.artifacts[0].destination.read_bytes(), b"old")
            self.assertEqual(
                cache,
                {"unrelated": {"size": 1, "mtime_ns": 2, "sha256": "abc"}},
            )

    def test_cleanup_preserves_and_reports_unknown_staging_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan, contents = self.make_plan(Path(tmp), [("model.gguf", b"old", b"new")])
            marker = plan.staging_root / "downloads" / "unrelated" / "user-note.txt"

            def validate(_plan):
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("keep me", encoding="utf-8")

            result = UpdateExecutor(plan, downloader=FakeDownloader(contents)).execute(
                post_install_validator=validate
            )
            self.assertTrue(result.success)
            self.assertTrue(marker.is_file())
            self.assertIn("Preserved 1 unknown staging file", result.message)
            self.assertTrue(plan.manifest_path.is_file())


class RecoveryTests(unittest.TestCase):
    def make_plan(self, base: Path, names: tuple[str, ...] = ("one.gguf",)):
        root = base / "models"
        root.mkdir(exist_ok=True)
        artifacts = []
        for name in names:
            target = root / "org" / "repo" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"old")
            artifacts.append(artifact_result(destination=target, remote_file=name, content=b"new"))
        plan = build_update_plan(
            [check_result(key="model", name="Model", repo="org/repo", artifacts=artifacts)],
            models_root=root, safety_reserve_bytes=0,
        )
        return root, plan

    def test_write_manifest_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan = self.make_plan(Path(tmp))
            write_manifest(plan, "downloading")
            self.assertEqual(list(plan.manifest_path.parent.iterdir()), [plan.manifest_path])

    def test_write_manifest_temp_names_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan = self.make_plan(Path(tmp))
            original = tempfile.NamedTemporaryFile
            names = []
            def capture(*args, **kwargs):
                handle = original(*args, **kwargs)
                names.append(handle.name)
                return handle
            with patch("lmstudio_weight_updater.tempfile.NamedTemporaryFile", side_effect=capture):
                write_manifest(plan, "downloading")
                write_manifest(plan, "installing")
            self.assertEqual(len(set(names)), 2)
            self.assertTrue(all(Path(name).parent == plan.manifest_path.parent for name in names))
            self.assertTrue(all(MANIFEST_TEMP_RE.fullmatch(Path(name).name) for name in names))

    def test_write_manifest_cleans_up_after_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan = self.make_plan(Path(tmp))
            write_manifest(plan, "downloading")
            before = plan.manifest_path.read_bytes()
            with patch("lmstudio_weight_updater.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_manifest(plan, "installing")
            self.assertEqual(plan.manifest_path.read_bytes(), before)
            self.assertEqual(list(plan.manifest_path.parent.iterdir()), [plan.manifest_path])

    def test_write_manifest_cleans_up_after_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan = self.make_plan(Path(tmp))
            with patch("lmstudio_weight_updater.json.dump", side_effect=OSError("write failed")):
                with self.assertRaisesRegex(OSError, "write failed"):
                    write_manifest(plan, "downloading")
            self.assertEqual(list(plan.manifest_path.parent.iterdir()), [])

    def test_write_manifest_survives_concurrent_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, plan = self.make_plan(Path(tmp))
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(write_manifest, plan, phase)
                           for phase in ("installing", "validating")]
                for future in futures:
                    future.result()
            payload = json.loads(plan.manifest_path.read_text(encoding="utf-8"))
            self.assertIn(payload["phase"], {"installing", "validating"})
            self.assertEqual(list(plan.manifest_path.parent.iterdir()), [plan.manifest_path])

    def test_recovery_prunes_stale_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp))
            jobs = plan.manifest_path.parent
            jobs.mkdir(parents=True)
            old = jobs / f".{plan.manifest_path.name}.abcdef12.tmp"
            old.write_text("partial", encoding="utf-8")
            stale_time = time.time() - 7200
            os.utime(old, (stale_time, stale_time))
            unrelated = jobs / f".{plan.manifest_path.name}.abcdef12.tmp.keep"
            unrelated.write_text("keep", encoding="utf-8")
            actions = recover_interrupted_jobs(root)
            self.assertFalse(old.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(any(action.action == "temp-cleaned" and action.path == str(old)
                                for action in actions))

    def test_recovery_keeps_recent_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp))
            jobs = plan.manifest_path.parent
            jobs.mkdir(parents=True)
            recent = jobs / f".{plan.manifest_path.name}.abcdef12.tmp"
            recent.write_text("in progress", encoding="utf-8")
            self.assertEqual(recover_interrupted_jobs(root), [])
            self.assertTrue(recent.exists())

    def test_recovery_reports_invalid_manifest_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp))
            plan.manifest_path.parent.mkdir(parents=True)
            plan.manifest_path.write_text("{broken", encoding="utf-8")
            actions = recover_interrupted_jobs(root)
            self.assertEqual([action.action for action in actions], ["reported"])
            self.assertTrue(plan.manifest_path.exists())

    def test_recovery_finishes_interrupted_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp), ("one.gguf", "two.gguf", "three.gguf"))
            for artifact in plan.artifacts:
                artifact.destination.write_bytes(b"new")
                artifact.backup_path.write_bytes(b"old")
            write_manifest(plan, "completed-cleanup-pending",
                           retained_backups=[artifact.backup_path for artifact in plan.artifacts])
            for artifact in plan.artifacts[:2]:
                artifact.backup_path.unlink()
            pending = recover_interrupted_jobs(root)
            self.assertEqual([action.action for action in pending], ["cleanup-pending"])
            self.assertTrue(plan.artifacts[2].backup_path.exists())
            cleaned = cleanup_pending_backups(root, model_is_current=lambda _keys: True)
            self.assertEqual([action.action for action in cleaned], ["cleaned"])
            self.assertFalse(plan.manifest_path.exists())
            self.assertFalse(plan.staging_root.exists())

    def test_recovery_does_not_delete_without_cleanup_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp))
            artifact = plan.artifacts[0]
            artifact.backup_path.write_bytes(b"old")
            write_manifest(plan, "installing")
            actions = recover_interrupted_jobs(root)
            self.assertEqual([action.action for action in actions], ["reported"])
            self.assertTrue(artifact.backup_path.exists())

    def test_recovery_reports_counts_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp), ("one.gguf", "two.gguf", "three.gguf"))
            write_manifest(plan, "installing")
            first, second, third = plan.artifacts
            first.backup_path.write_bytes(b"old")
            first.destination.unlink()
            second.backup_path.write_bytes(b"old")
            orphan = plan.staging_root / "downloads" / "orphan" / "stray.bin"
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(b"stray")
            actions = recover_interrupted_jobs(root)
            self.assertEqual(summarize_recovery_actions(actions),
                             {"restored": 1, "cleanup_pending": 0, "attention": 2})
            self.assertEqual(first.destination.read_bytes(), b"old")
            again = recover_interrupted_jobs(root)
            self.assertFalse(any(action.action == "restored" for action in again))
            self.assertEqual(orphan.read_bytes(), b"stray")
            self.assertTrue(third.destination.exists())

    def test_terminal_manifest_is_pruned_with_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp))
            staged = plan.artifacts[0].staged_path
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b"new")
            write_manifest(plan, "completed", owned_staging_files=[staged])
            self.assertEqual(recover_interrupted_jobs(root), [])
            self.assertFalse(plan.manifest_path.exists())
            self.assertFalse(staged.exists())

    def test_orphan_staging_is_reported_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            root.mkdir()
            orphan = root.parent / ".weight-watcher-staging" / "downloads" / "orphan.bin"
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(b"keep")
            actions = recover_interrupted_jobs(root)
            self.assertEqual([(action.action, action.path) for action in actions],
                             [("reported", str(orphan))])
            self.assertEqual(orphan.read_bytes(), b"keep")

    def test_marker_is_not_deleted_when_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp))
            marker = plan.staging_root / "downloads" / "orphan" / "model.gguf.verified.json"
            marker.parent.mkdir(parents=True)
            marker.write_text("unknown", encoding="utf-8")
            actions = recover_interrupted_jobs(root)
            self.assertTrue(marker.exists())
            self.assertTrue(any(action.action == "reported" and action.path == str(marker)
                                for action in actions))

    def test_cancelled_manifest_is_pruned_but_unknown_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, plan = self.make_plan(Path(tmp))
            directory = plan.artifacts[0].download_dir
            directory.mkdir(parents=True)
            unknown = directory / "unknown.txt"
            unknown.write_text("keep", encoding="utf-8")
            write_manifest(plan, "cancelled")
            actions = recover_interrupted_jobs(root)
            self.assertEqual([action.action for action in actions], ["reported"])
            self.assertTrue(plan.manifest_path.exists())
            self.assertTrue(unknown.exists())
            unknown.unlink()
            self.assertEqual(recover_interrupted_jobs(root), [])
            self.assertFalse(plan.manifest_path.exists())

    def test_recovery_restores_only_manifested_missing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model",
                name="Model",
                repo="org/repo",
                artifacts=[artifact_result(destination=target, remote_file="model.gguf", content=b"new")],
            )
            plan = build_update_plan([result], models_root=root, safety_reserve_bytes=0)
            write_manifest(plan, "installing")
            os.replace(target, plan.artifacts[0].backup_path)

            actions = recover_interrupted_jobs(root)

            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(plan.artifacts[0].backup_path.read_bytes(), b"old")
            self.assertEqual(actions[0].action, "restored")


class CliTests(unittest.TestCase):
    def test_dry_run_prints_recovery_counts_without_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model", name="Model", repo="org/repo",
                artifacts=[artifact_result(destination=target, remote_file="model.gguf", content=b"new")],
            )
            plan = build_update_plan([result], models_root=root, safety_reserve_bytes=0)
            write_manifest(plan, "installing")
            plan.artifacts[0].backup_path.write_bytes(b"old")
            errors = io.StringIO()
            with (patch("lmstudio_weight_updater.discover_models_root", return_value=root),
                  patch("lmstudio_weight_updater.collect_check_results", return_value=[result]),
                  contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors)):
                self.assertEqual(main(["--all", "--dry-run", "--models-root", str(root),
                                       "--state-file", str(base / "state.json")]), 0)
            self.assertEqual(errors.getvalue().strip(),
                             "recovery: 0 restored, 0 cleanup pending, 1 need attention")
            self.assertTrue(plan.artifacts[0].backup_path.exists())

    def test_second_cli_run_cleans_backup_in_scratch_models_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model", name="Model", repo="org/repo",
                artifacts=[artifact_result(destination=target, remote_file="model.gguf", content=b"new")],
            )
            plan = build_update_plan([result], models_root=root, safety_reserve_bytes=0)
            UpdateExecutor(plan, downloader=FakeDownloader({"model.gguf": b"new"})).execute(
                post_install_validator=lambda _plan: None
            )
            self.assertEqual(plan.artifacts[0].backup_path.read_bytes(), b"old")
            result.status = "up-to-date"
            result.artifacts[0].status = "up-to-date"
            with (
                patch("lmstudio_weight_updater.discover_models_root", return_value=root),
                patch("lmstudio_weight_updater.collect_check_results", return_value=[result]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = main([
                    "--all", "--yes", "--models-root", str(root),
                    "--state-file", str(base / "state.json"),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertFalse(plan.artifacts[0].backup_path.exists())

    def test_parser_exposes_required_cli_options(self) -> None:
        args = parse_args(
            [
                "--model-key",
                "one",
                "--model-key",
                "two",
                "--dry-run",
                "--yes",
                "--keep-backups",
                "--models-root",
                "models",
                "--state-file",
                "state.json",
                "--timeout-seconds",
                "45",
            ]
        )
        self.assertEqual(args.model_key, ["one", "two"])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.timeout_seconds, 45)

    def test_dry_run_does_not_construct_or_call_downloader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model",
                name="Model",
                repo="org/repo",
                artifacts=[artifact_result(destination=target, remote_file="model.gguf", content=b"new")],
            )
            output = io.StringIO()
            with (
                patch("lmstudio_weight_updater.discover_models_root", return_value=root),
                patch("lmstudio_weight_updater.collect_check_results", return_value=[result]),
                patch("lmstudio_weight_updater.HuggingFaceDownloader.download") as download,
                contextlib.redirect_stdout(output),
            ):
                code = main(
                    [
                        "--all",
                        "--dry-run",
                        "--models-root",
                        str(root),
                        "--state-file",
                        str(base / "state.json"),
                    ]
                )
            self.assertEqual(code, 0)
            download.assert_not_called()
            self.assertTrue(output.getvalue().lstrip().startswith("{"))
            self.assertIn("job_id", json.loads(output.getvalue()))

    def test_dry_run_json_also_prints_plan_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model", name="Model", repo="org/repo",
                artifacts=[artifact_result(
                    destination=target, remote_file="model.gguf", content=b"new"
                )],
            )
            output = io.StringIO()
            with (patch("lmstudio_weight_updater.discover_models_root", return_value=root),
                  patch("lmstudio_weight_updater.collect_check_results", return_value=[result]),
                  contextlib.redirect_stdout(output)):
                code = main([
                    "--all", "--dry-run", "--json", "--models-root", str(root),
                    "--state-file", str(base / "state.json"),
                ])
            self.assertEqual(code, 0)
            self.assertTrue(output.getvalue().lstrip().startswith("{"))
            self.assertIn("artifacts", json.loads(output.getvalue()))

    def test_normal_update_json_prints_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model", name="Model", repo="org/repo",
                artifacts=[artifact_result(
                    destination=target, remote_file="model.gguf", content=b"new"
                )],
            )
            update_result = UpdateResult(
                job_id="job-test", success=True, updated_paths=(str(target),),
                backup_paths=(), completed_at_utc="2026-09-29T00:00:00Z", message="Done",
            )
            output = io.StringIO()
            with (patch("lmstudio_weight_updater.discover_models_root", return_value=root),
                  patch("lmstudio_weight_updater.collect_check_results", return_value=[result]),
                  patch("lmstudio_weight_updater.run_update_plan", return_value=(update_result, [])),
                  contextlib.redirect_stdout(output)):
                code = main([
                    "--all", "--yes", "--json", "--models-root", str(root),
                    "--state-file", str(base / "state.json"),
                ])
            self.assertEqual(code, 0)
            self.assertTrue(output.getvalue().lstrip().startswith("{"))
            parsed = json.loads(output.getvalue())
            self.assertEqual(parsed["job_id"], "job-test")
            self.assertTrue(parsed["success"])

    def test_all_selects_repairable_missing_local_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "models"
            root.mkdir()
            target = root / "org" / "repo" / "missing.gguf"
            missing = artifact_result(
                destination=target,
                remote_file="missing.gguf",
                content=b"replacement",
                status="missing-local",
            )
            result = check_result(
                key="model", name="Model", repo="org/repo", artifacts=[missing]
            )
            result.status = "unresolved"
            selected = _select_results([result], parse_args(["--all"]))
            self.assertEqual(selected, [result])

    def test_unexpected_downloader_error_is_redacted_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "models"
            target = root / "org" / "repo" / "model.gguf"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")
            result = check_result(
                key="model",
                name="Model",
                repo="org/repo",
                artifacts=[artifact_result(destination=target, remote_file="model.gguf", content=b"new")],
            )
            stderr = io.StringIO()
            stdout = io.StringIO()
            with (
                patch("lmstudio_weight_updater.discover_models_root", return_value=root),
                patch("lmstudio_weight_updater.collect_check_results", return_value=[result]),
                patch(
                    "lmstudio_weight_updater.UpdateExecutor.execute",
                    side_effect=RuntimeError(
                        "https://example.test/file?X-Amz-Signature=PRINTED_SECRET&token=PRINTED_TOKEN"
                    ),
                ),
                contextlib.redirect_stderr(stderr),
                contextlib.redirect_stdout(stdout),
            ):
                code = main(
                    [
                        "--all",
                        "--yes",
                        "--models-root",
                        str(root),
                        "--state-file",
                        str(base / "state.json"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertNotIn("PRINTED_SECRET", stderr.getvalue())
            self.assertNotIn("PRINTED_TOKEN", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertIn("[REDACTED]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
