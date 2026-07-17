from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lmstudio_weight_checker import ArtifactResult, CheckResult
from lmstudio_weight_updater import (
    CancellationToken,
    InstallError,
    IntegrityError,
    PlanError,
    UpdateCancelled,
    UpdateExecutor,
    _select_results,
    build_update_plan,
    main,
    parse_args,
    recover_interrupted_jobs,
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
            self.assertFalse(plan.artifacts[0].backup_path.exists())
            self.assertFalse(plan.staging_root.exists())
            self.assertEqual(cache["org/repo/model.gguf"]["sha256"], digest(b"new bytes"))

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
            marker = plan.artifacts[0].download_dir / "user-note.txt"

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
            self.assertEqual(actions[0].action, "restored")


class CliTests(unittest.TestCase):
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
            self.assertIn('"job_id"', output.getvalue())

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
