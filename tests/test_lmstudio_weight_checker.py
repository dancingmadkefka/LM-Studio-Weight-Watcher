from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from lmstudio_weight_checker import (
    Artifact,
    CheckerError,
    RemoteFileMissing,
    _quant_family,
    build_variant_lookup,
    candidate_references,
    compare_artifact,
    find_weight_alternatives,
    is_projector_filename,
    parse_remote_reference,
    prune_hash_cache,
    resolve_artifacts,
    rollup_model_status,
)


def remote_entry(
    *,
    oid: str,
    size: int,
    title: str = "Upload folder using huggingface_hub",
    date: str = "2026-06-04T21:22:27.000Z",
) -> dict:
    return {
        "type": "file",
        "path": "ignored.gguf",
        "lfs": {"oid": oid, "size": size},
        "lastCommit": {"id": "commit-id", "title": title, "date": date},
    }


class ParseRemoteReferenceTests(unittest.TestCase):
    def test_parses_direct_path(self) -> None:
        parsed = parse_remote_reference(
            "unsloth/Qwen3.5-35B-A3B-GGUF/Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf"
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.repo, "unsloth/Qwen3.5-35B-A3B-GGUF")
        self.assertEqual(parsed.remote_file, "Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf")

    def test_parses_variant_indexed_identifier(self) -> None:
        parsed = parse_remote_reference(
            "liquid/lfm2-24b-a2b@lmstudio-community/LFM2-24B-A2B-GGUF/LFM2-24B-A2B-Q4_K_M.gguf"
        )

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.repo, "lmstudio-community/LFM2-24B-A2B-GGUF")
        self.assertEqual(parsed.remote_file, "LFM2-24B-A2B-Q4_K_M.gguf")


class CandidateReferenceTests(unittest.TestCase):
    def test_deduplicates_candidate_paths(self) -> None:
        entry = {
            "modelKey": "unsloth/qwen3.5-35b-a3b",
            "path": "unsloth/Qwen3.5-35B-A3B-GGUF/Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf",
            "indexedModelIdentifier": "unsloth/Qwen3.5-35B-A3B-GGUF/Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf",
        }

        candidates = candidate_references(entry, {})

        self.assertEqual(len(candidates), 1)


class ProjectorDetectionTests(unittest.TestCase):
    def test_is_projector_filename_flags_common_names(self) -> None:
        for name in (
            "mmproj-F32.gguf",
            "mmproj-Qwen3.6-35B-A3B-BF16.gguf",
            "model.projector.gguf",
        ):
            self.assertTrue(is_projector_filename(name), name)

    def test_is_projector_filename_ignores_main_weights(self) -> None:
        for name in (
            "Qwen3.5-9B-UD-Q4_K_XL.gguf",
            "gemma-4-12b-it-UD-Q3_K_XL.gguf",
            "nomic-embed-text-v1.5.Q4_K_M.gguf",
        ):
            self.assertFalse(is_projector_filename(name), name)


class ResolveArtifactsTests(unittest.TestCase):
    def _entry(self, *, path: str, vision: bool = False) -> dict:
        return {
            "modelKey": "unsloth/qwen3.5-9b",
            "displayName": "Qwen3.5 9B",
            "publisher": "unsloth",
            "path": path,
            "indexedModelIdentifier": path,
            "vision": vision,
            "quantization": {"name": "Q4_K_XL"},
        }

    def _touch(self, path: Path, content: bytes = b"x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_single_file_yields_one_weight_artifact(self) -> None:
        entry = self._entry(
            path="unsloth/Qwen3.5-9B-GGUF/Qwen3.5-9B-UD-Q4_K_XL.gguf"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "unsloth" / "Qwen3.5-9B-GGUF"
            self._touch(repo / "Qwen3.5-9B-UD-Q4_K_XL.gguf")

            artifacts = resolve_artifacts(entry, Path(tmpdir), {})

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].kind, "weight")
        self.assertEqual(artifacts[0].remote_file, "Qwen3.5-9B-UD-Q4_K_XL.gguf")

    def test_sharded_quant_yields_one_artifact_per_local_shard(self) -> None:
        entry = self._entry(
            path="unsloth/Big-GGUF/Big-Q4_K-00001-of-00002.gguf"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "unsloth" / "Big-GGUF"
            self._touch(repo / "Big-Q4_K-00001-of-00002.gguf")
            self._touch(repo / "Big-Q4_K-00002-of-00002.gguf")
            # An unrelated shard total must NOT be pulled in.
            self._touch(repo / "Big-Q4_K-00001-of-00003.gguf")

            artifacts = resolve_artifacts(entry, Path(tmpdir), {})

        labels = sorted(a.label for a in artifacts)
        self.assertEqual(
            labels, ["weights (shard 1/2)", "weights (shard 2/2)"]
        )
        for a in artifacts:
            self.assertEqual(a.kind, "weight")

    def test_vision_model_includes_present_projector(self) -> None:
        entry = self._entry(
            path="unsloth/Qwen3.5-9B-GGUF/Qwen3.5-9B-UD-Q4_K_XL.gguf",
            vision=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "unsloth" / "Qwen3.5-9B-GGUF"
            self._touch(repo / "Qwen3.5-9B-UD-Q4_K_XL.gguf")
            self._touch(repo / "mmproj-F32.gguf")

            artifacts = resolve_artifacts(entry, Path(tmpdir), {})

        kinds = sorted(a.kind for a in artifacts)
        self.assertEqual(kinds, ["projector", "weight"])
        proj = next(a for a in artifacts if a.kind == "projector")
        self.assertEqual(proj.remote_file, "mmproj-F32.gguf")

    def test_vision_model_without_projector_is_not_an_error(self) -> None:
        # Generic handling: a missing projector (deleted, renamed, never
        # downloaded) is simply not an installed artifact. No alert, no error,
        # and crucially no knowledge of *why* it is missing.
        entry = self._entry(
            path="unsloth/Qwen3.5-9B-GGUF/Qwen3.5-9B-UD-Q4_K_XL.gguf",
            vision=True,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "unsloth" / "Qwen3.5-9B-GGUF"
            self._touch(repo / "Qwen3.5-9B-UD-Q4_K_XL.gguf")
            # No mmproj present (and no .BAK either -- the app does not look).

            artifacts = resolve_artifacts(entry, Path(tmpdir), {})

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].kind, "weight")

    def test_missing_main_weight_raises_instead_of_guessing(self) -> None:
        entry = self._entry(
            path="unsloth/Qwen3.5-9B-GGUF/Qwen3.5-9B-UD-Q4_K_XL.gguf"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "unsloth" / "Qwen3.5-9B-GGUF"
            # Only a projector is present; the referenced weight is absent.
            self._touch(repo / "mmproj-F32.gguf")

            with self.assertRaises(CheckerError):
                resolve_artifacts(entry, Path(tmpdir), {})

    def test_resolves_via_selected_variant_when_base_path_has_no_file(self) -> None:
        entry = {
            "modelKey": "liquid/lfm2-24b-a2b",
            "displayName": "Lfm2 24B A2B",
            "publisher": "liquid",
            "path": "liquid/lfm2-24b-a2b",
            "indexedModelIdentifier": "liquid/lfm2-24b-a2b",
            "selectedVariant": "liquid/lfm2-24b-a2b@q4_k_m",
            "quantization": {"name": "Q4_K_M"},
        }
        variant_groups = [
            {
                "model": {"modelKey": "liquid/lfm2-24b-a2b"},
                "variants": [
                    {
                        "modelKey": "liquid/lfm2-24b-a2b@q4_k_m",
                        "path": "liquid/lfm2-24b-a2b",
                        "indexedModelIdentifier": (
                            "liquid/lfm2-24b-a2b@"
                            "lmstudio-community/LFM2-24B-A2B-GGUF/LFM2-24B-A2B-Q4_K_M.gguf"
                        ),
                    }
                ],
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            model_file = (
                Path(tmpdir)
                / "lmstudio-community"
                / "LFM2-24B-A2B-GGUF"
                / "LFM2-24B-A2B-Q4_K_M.gguf"
            )
            self._touch(model_file)

            artifacts = resolve_artifacts(
                entry, Path(tmpdir), build_variant_lookup(variant_groups)
            )

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(
            artifacts[0].remote_repo, "lmstudio-community/LFM2-24B-A2B-GGUF"
        )


class CompareArtifactTests(unittest.TestCase):
    REPO = "tester/test-model"

    def _artifact(self, path: Path, *, remote_file: str = "test.gguf",
                  kind: str = "weight", label: str = "weights") -> Artifact:
        return Artifact(
            kind=kind, label=label, local_path=path,
            remote_repo=self.REPO, remote_file=remote_file,
        )

    def _tree(self, tmpdir: str, remote_file: str, entry: dict | None) -> dict:
        # Pre-populate the tree cache so compare_artifact never hits the network.
        # entry=None means "remote no longer has this file".
        return {(self.REPO, ""): {} if entry is None else {remote_file: entry}}

    def test_matching_blob_is_up_to_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.gguf"
            f.write_bytes(b"weights-bytes")
            digest = hashlib.sha256(b"weights-bytes").hexdigest()
            tree = self._tree(tmpdir, "test.gguf", remote_entry(oid=digest, size=len(b"weights-bytes")))

            result = compare_artifact(
                self._artifact(f), timeout_seconds=5, tree_cache=tree, hash_cache={}
            )

        self.assertEqual(result.status, "up-to-date")
        self.assertEqual(result.remote_oid, digest)
        self.assertEqual(result.local_oid, digest)

    def test_mismatched_blob_flags_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.gguf"
            f.write_bytes(b"weights-bytes")
            tree = self._tree(
                tmpdir, "test.gguf",
                remote_entry(oid="0" * 64, size=len(b"weights-bytes")),
            )

            result = compare_artifact(
                self._artifact(f), timeout_seconds=5, tree_cache=tree, hash_cache={}
            )

        self.assertEqual(result.status, "update-available")
        # Size matched, so the file had to be hashed to know it differs.
        self.assertIsNotNone(result.local_oid)

    def test_size_difference_flags_update_without_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.gguf"
            f.write_bytes(b"short")
            tree = self._tree(
                tmpdir, "test.gguf",
                remote_entry(oid="0" * 64, size=999),
            )
            hash_cache: dict = {}

            result = compare_artifact(
                self._artifact(f), timeout_seconds=5, tree_cache=tree,
                hash_cache=hash_cache,
            )

        self.assertEqual(result.status, "update-available")
        # Size already settled it; the file must NOT have been hashed.
        self.assertIsNone(result.local_oid)
        self.assertEqual(hash_cache, {})

    def test_remote_file_removed_with_local_copy_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.gguf"
            f.write_bytes(b"local-only")
            tree = self._tree(tmpdir, "test.gguf", None)

            result = compare_artifact(
                self._artifact(f), timeout_seconds=5, tree_cache=tree, hash_cache={}
            )

        self.assertEqual(result.status, "removed-remote")

    def test_removed_weight_suggests_same_family_quants(self) -> None:
        # When the exact quant is deleted upstream but the repo still publishes
        # sibling quants of the same base model, surface those so the user knows
        # what to switch to instead of being offered a doomed download.
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "Test-Model-Q4_K_M.gguf"
            f.write_bytes(b"local-only")
            tree = {
                (self.REPO, ""): {
                    "Test-Model-BF16.gguf": remote_entry(oid="b" * 64, size=10),
                    "Test-Model-Q4_0.gguf": remote_entry(oid="c" * 64, size=10),
                    "Test-Model-Q8_0.gguf": remote_entry(oid="d" * 64, size=10),
                    "mmproj-F32.gguf": remote_entry(oid="e" * 64, size=10),
                    "OtherModel-Q4_0.gguf": remote_entry(oid="f" * 64, size=10),
                }
            }

            result = compare_artifact(
                self._artifact(f, remote_file="Test-Model-Q4_K_M.gguf"),
                timeout_seconds=5, tree_cache=tree, hash_cache={},
            )

        self.assertEqual(result.status, "removed-remote")
        self.assertEqual(
            result.suggestions,
            ["Test-Model-BF16.gguf", "Test-Model-Q4_0.gguf", "Test-Model-Q8_0.gguf"],
        )

    def test_local_shard_missing_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist.gguf"
            digest = hashlib.sha256(b"x").hexdigest()
            tree = self._tree(
                tmpdir, "test.gguf", remote_entry(oid=digest, size=1),
            )

            result = compare_artifact(
                self._artifact(missing), timeout_seconds=5, tree_cache=tree,
                hash_cache={},
            )

        self.assertEqual(result.status, "missing-local")

    def test_carries_last_commit_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.gguf"
            f.write_bytes(b"x")
            digest = hashlib.sha256(b"x").hexdigest()
            tree = self._tree(
                tmpdir, "test.gguf",
                remote_entry(oid=digest, size=1, title="Re-quantize Q4_K_XL"),
            )

            result = compare_artifact(
                self._artifact(f), timeout_seconds=5, tree_cache=tree, hash_cache={}
            )

        self.assertEqual(result.last_commit_title, "Re-quantize Q4_K_XL")


class RollupModelStatusTests(unittest.TestCase):
    def _art(self, status: str, *, kind: str = "weight", label: str = "weights") -> object:
        from lmstudio_weight_checker import ArtifactResult
        return ArtifactResult(
            kind=kind, label=label, status=status, local_path=None,
            remote_file="x.gguf", local_size=None, remote_size=None,
            local_oid=None, remote_oid=None, last_commit_title=None,
            last_commit_date_utc=None, message="",
        )

    def test_all_up_to_date(self) -> None:
        status, _ = rollup_model_status([self._art("up-to-date"), self._art("up-to-date")])
        self.assertEqual(status, "up-to-date")

    def test_any_update_available(self) -> None:
        status, message = rollup_model_status(
            [self._art("up-to-date"), self._art("update-available", label="weights (shard 2/2)")]
        )
        self.assertEqual(status, "update-available")
        self.assertIn("shard 2/2", message)

    def test_missing_local_weight_is_unresolved(self) -> None:
        status, _ = rollup_model_status([self._art("missing-local", label="weights (shard 2/2)")])
        self.assertEqual(status, "unresolved")

    def test_removed_remote_is_its_own_model_status(self) -> None:
        status, message = rollup_model_status([self._art("removed-remote", label="weights")])
        # A removed-remote file is tracked distinctly from a genuine weight
        # update: the UI shows "Removed upstream" and does not offer to
        # download (there is nothing to fetch), so it must not collapse into
        # "update-available".
        self.assertEqual(status, "removed-remote")
        self.assertIn("Hugging Face", message)

    def test_removed_remote_outranks_update_available(self) -> None:
        # If one artifact is removed upstream and another has new weights, the
        # removed-remote verdict wins so we never offer a partial, doomed update.
        status, _ = rollup_model_status(
            [self._art("removed-remote", label="weights"), self._art("update-available", label="projector")]
        )
        self.assertEqual(status, "removed-remote")

    def test_projector_missing_local_alone_does_not_break_model(self) -> None:
        # A projector is optional; even if one were reported missing-local it
        # must not elevate the whole model to unresolved.
        status, _ = rollup_model_status(
            [self._art("missing-local", kind="projector", label="projector")]
        )
        self.assertNotEqual(status, "unresolved")


class RunCheckIntegrationTests(unittest.TestCase):
    """End-to-end test of run_check (the entry point the watcher calls), with
    the network layer mocked so no real Hugging Face calls are made."""

    def test_run_check_assembles_artifacts_into_a_model_result(self) -> None:
        from unittest.mock import patch
        from lmstudio_weight_checker import run_check

        repo = "unsloth/Smol-GGUF"
        remote_file = "Smol-Q4_K_XL.gguf"
        content = b"weights"
        oid = hashlib.sha256(content).hexdigest()

        entry = {
            "modelKey": "unsloth/smol",
            "displayName": "Smol",
            "publisher": "unsloth",
            "path": f"{repo}/{remote_file}",
            "indexedModelIdentifier": f"{repo}/{remote_file}",
            "vision": True,
        }

        def fake_fetch_tree(repo_arg, parent, timeout):
            # Only the repo root is populated; everything else is empty.
            if parent == "":
                return {
                    remote_file: remote_entry(oid=oid, size=len(content)),
                    "mmproj-F32.gguf": remote_entry(
                        oid=hashlib.sha256(b"proj").hexdigest(), size=4
                    ),
                }
            return {}

        with tempfile.TemporaryDirectory() as tmpdir:
            models_root = Path(tmpdir)
            repo_dir = models_root / "unsloth" / "Smol-GGUF"
            repo_dir.mkdir(parents=True)
            (repo_dir / remote_file).write_bytes(content)
            (repo_dir / "mmproj-F32.gguf").write_bytes(b"proj")

            with patch("lmstudio_weight_checker.fetch_tree", side_effect=fake_fetch_tree):
                results = run_check(
                    models_root=models_root,
                    inventory=[entry],
                    variant_lookup={},
                    timeout_seconds=5,
                    hash_cache={},
                )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.status, "up-to-date")
        self.assertEqual(result.remote_repo, repo)
        # Both the weight and the projector were checked as separate artifacts.
        kinds = sorted(a.kind for a in result.artifacts)
        self.assertEqual(kinds, ["projector", "weight"])
        self.assertEqual(len(result.artifacts), 2)


class RemoteFailureClassificationTests(unittest.TestCase):
    """Regression tests for the P1 bug: transient HF failures must not be
    reported as removed-remote (a false update alert)."""

    REPO = "tester/test-model"

    def _artifact(self, path: Path) -> Artifact:
        return Artifact(
            kind="weight", label="weights", local_path=path,
            remote_repo=self.REPO, remote_file="test.gguf",
        )

    def test_confirmed_missing_tree_is_removed_remote(self) -> None:
        # Tree fetched successfully but file absent -> genuine removal.
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.gguf"
            f.write_bytes(b"local")
            with patch(
                "lmstudio_weight_checker.fetch_tree", return_value={}
            ):
                result = compare_artifact(
                    self._artifact(f), timeout_seconds=5,
                    tree_cache={}, hash_cache={},
                )
        self.assertEqual(result.status, "removed-remote")

    def test_repo_404_is_removed_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.gguf"
            f.write_bytes(b"local")
            with patch(
                "lmstudio_weight_checker.fetch_tree",
                side_effect=RemoteFileMissing("repo gone"),
            ):
                result = compare_artifact(
                    self._artifact(f), timeout_seconds=5,
                    tree_cache={}, hash_cache={},
                )
        self.assertEqual(result.status, "removed-remote")

    def test_transient_network_error_is_unresolved_not_removed(self) -> None:
        # Timeout / auth / 5xx must never look like a removal.
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.gguf"
            f.write_bytes(b"local")
            with patch(
                "lmstudio_weight_checker.fetch_tree",
                side_effect=CheckerError("Network error: timed out"),
            ):
                result = compare_artifact(
                    self._artifact(f), timeout_seconds=5,
                    tree_cache={}, hash_cache={},
                )
        self.assertEqual(result.status, "unresolved")
        self.assertIn("could not reach Hugging Face", result.message)


class ShardAndNestedPathTests(unittest.TestCase):
    """P2: missing shards must be detected; nested remote paths preserved."""

    def _entry(self, path: str, *, vision: bool = False) -> dict:
        return {
            "modelKey": "org/big",
            "displayName": "Big",
            "publisher": "org",
            "path": path,
            "indexedModelIdentifier": path,
            "vision": vision,
            "quantization": {"name": "Q4_K"},
        }

    def _touch(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    def test_missing_local_shard_is_still_emitted_as_an_artifact(self) -> None:
        entry = self._entry(path="org/big-gguf/Big-Q4_K-00001-of-00003.gguf")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "org" / "big-gguf"
            self._touch(repo_dir / "Big-Q4_K-00001-of-00003.gguf")
            # Shards 2 and 3 are absent on disk.
            artifacts = resolve_artifacts(entry, Path(tmpdir), {})

            labels = sorted(a.label for a in artifacts)
            self.assertEqual(
                labels,
                ["weights (shard 1/3)", "weights (shard 2/3)", "weights (shard 3/3)"],
            )
            present = [a for a in artifacts if a.local_path.is_file()]
            missing = [a for a in artifacts if not a.local_path.is_file()]
            self.assertEqual(len(present), 1)
            self.assertEqual(len(missing), 2)

    def test_nested_remote_path_is_preserved_on_shards(self) -> None:
        # A model living under a repo subdir must keep that prefix in remote_file.
        entry = self._entry(path="org/big-gguf/sub/Big-Q4_K-00001-of-00002.gguf")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "org" / "big-gguf" / "sub"
            self._touch(repo_dir / "Big-Q4_K-00001-of-00002.gguf")
            self._touch(repo_dir / "Big-Q4_K-00002-of-00002.gguf")
            artifacts = resolve_artifacts(entry, Path(tmpdir), {})

        remotes = sorted(a.remote_file for a in artifacts)
        self.assertEqual(
            remotes,
            ["sub/Big-Q4_K-00001-of-00002.gguf", "sub/Big-Q4_K-00002-of-00002.gguf"],
        )

    def test_nested_remote_path_preserved_for_single_file(self) -> None:
        entry = self._entry(path="org/big-gguf/sub/Big-Q4_K_M.gguf")
        with tempfile.TemporaryDirectory() as tmpdir:
            self._touch(Path(tmpdir) / "org" / "big-gguf" / "sub" / "Big-Q4_K_M.gguf")
            artifacts = resolve_artifacts(entry, Path(tmpdir), {})

        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].remote_file, "sub/Big-Q4_K_M.gguf")


class HashCachePersistenceTests(unittest.TestCase):
    """P2: run_check must persist newly computed hashes to the caller's cache,
    and prune stale entries in place."""

    def test_run_check_writes_new_hashes_into_caller_cache(self) -> None:
        from unittest.mock import patch
        from lmstudio_weight_checker import run_check

        content = b"weights"
        oid = hashlib.sha256(content).hexdigest()
        repo = "org/repo-gguf"
        fname = "model.gguf"
        entry = {
            "modelKey": "org/model",
            "displayName": "Model",
            "publisher": "org",
            "path": f"{repo}/{fname}",
            "indexedModelIdentifier": f"{repo}/{fname}",
        }

        def fake_fetch_tree(_repo, _parent, _timeout):
            return {fname: remote_entry(oid=oid, size=len(content))}

        caller_cache: dict = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            models_root = Path(tmpdir)
            (models_root / "org" / "repo-gguf").mkdir(parents=True)
            (models_root / "org" / "repo-gguf" / fname).write_bytes(content)
            with patch("lmstudio_weight_checker.fetch_tree", side_effect=fake_fetch_tree):
                run_check(
                    models_root=models_root,
                    inventory=[entry],
                    variant_lookup={},
                    timeout_seconds=5,
                    hash_cache=caller_cache,
                )

        # The caller's dict (which perform_check later saves) must now hold the
        # freshly computed hash. Before the fix this was empty.
        self.assertEqual(len(caller_cache), 1)
        entry_cache = next(iter(caller_cache.values()))
        self.assertEqual(entry_cache["sha256"], oid)

    def test_prune_hash_cache_mutates_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            kept = root / "org" / "repo" / "present.gguf"
            kept.parent.mkdir(parents=True)
            kept.write_bytes(b"x")

            cache = {
                "org/repo/present.gguf": {"sha256": "abc"},
                "org/repo/gone.gguf": {"sha256": "def"},
            }
            result = prune_hash_cache(cache, root)
            # Returns None (in place) and the stale entry is gone from cache.
            self.assertIsNone(result)
            self.assertIn("org/repo/present.gguf", cache)
            self.assertNotIn("org/repo/gone.gguf", cache)


class LocalOidCacheTests(unittest.TestCase):
    def test_get_local_oid_serves_cached_value_when_size_and_mtime_match(self) -> None:
        from lmstudio_weight_checker import get_local_oid

        with tempfile.TemporaryDirectory() as tmpdir:
            model_file = Path(tmpdir) / "test.gguf"
            model_file.write_bytes(b"abc")
            cache: dict = {}

            oid_first = get_local_oid(model_file, cache)
            # Tamper with the cached sha256; since size+mtime are unchanged the
            # cached (wrong) value must be returned rather than recomputing.
            key = next(iter(cache))
            cache[key]["sha256"] = "deadbeef"
            oid_second = get_local_oid(model_file, cache)

        self.assertEqual(oid_first, hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(oid_second, "deadbeef")


class QuantFamilyTests(unittest.TestCase):
    def test_family_strips_trailing_quant_token(self) -> None:
        # The trailing quantization token is stripped; a leading "UD" imatrix
        # marker is a separate dash-token and stays part of the family key, so
        # UD-Q4_K_XL and UD-Q8_K_XL still share a family.
        self.assertEqual(_quant_family("Qwen3.5-9B-UD-Q4_K_XL.gguf"), ("Qwen3.5-9B-UD", "Q4_K_XL"))
        self.assertEqual(_quant_family("Model-Q4_0.gguf"), ("Model", "Q4_0"))
        self.assertEqual(_quant_family("Model-Q8_0.gguf"), ("Model", "Q8_0"))
        self.assertEqual(_quant_family("Model-BF16.gguf"), ("Model", "BF16"))

    def test_sharded_name_keeps_family(self) -> None:
        self.assertEqual(
            _quant_family("Big-Q4_K-00001-of-00002.gguf"),
            ("Big", "Q4_K"),
        )

    def test_trie_quant_tokens_are_recognized(self) -> None:
        self.assertEqual(_quant_family("Model-TQ1_0.gguf"), ("Model", "TQ1_0"))
        self.assertEqual(_quant_family("Model-IQ2_XXS.gguf"), ("Model", "IQ2_XXS"))

    def test_non_quant_name_has_no_family(self) -> None:
        self.assertEqual(_quant_family("README.md"), (None, None))

    def test_find_weight_alternatives_excludes_projectors_and_other_models(self) -> None:
        tree_cache = {
            ("org/repo", ""): {
                "Model-BF16.gguf": {"type": "file"},
                "Model-Q4_0.gguf": {"type": "file"},
                "mmproj-F32.gguf": {"type": "file"},
                "Other-Q4_0.gguf": {"type": "file"},
            }
        }
        self.assertEqual(
            find_weight_alternatives("org/repo", "Model-Q4_K_M.gguf", tree_cache),
            ["Model-BF16.gguf", "Model-Q4_0.gguf"],
        )


if __name__ == "__main__":
    unittest.main()
