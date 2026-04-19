from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from lmstudio_weight_checker import (
    build_variant_lookup,
    candidate_references,
    compare_model,
    parse_remote_reference,
    resolve_model_entry,
)
from lmstudio_weight_checker import ResolvedModel


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


class ResolveModelEntryTests(unittest.TestCase):
    def test_uses_selected_variant_when_base_entry_has_no_file(self) -> None:
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
            model_file.parent.mkdir(parents=True, exist_ok=True)
            model_file.write_bytes(b"test")

            resolved = resolve_model_entry(
                entry,
                Path(tmpdir),
                build_variant_lookup(variant_groups),
            )

        self.assertEqual(resolved.remote_repo, "lmstudio-community/LFM2-24B-A2B-GGUF")
        self.assertTrue(resolved.local_path.name.endswith(".gguf"))


class CompareModelTests(unittest.TestCase):
    def test_flags_update_when_remote_is_outside_tolerance(self) -> None:
        resolved = ResolvedModel(
            model_key="test-model",
            display_name="Test Model",
            publisher="tester",
            local_path=Path("C:/models/test.gguf"),
            local_modified_utc=datetime.fromisoformat("2026-04-18T00:00:00+00:00"),
            local_size_bytes=100,
            remote_repo="tester/test-model",
            remote_file="test.gguf",
            quantization="Q4_K_M",
        )
        remote_entry = {"lastCommit": {"date": "2026-04-18T00:02:00Z"}}

        result = compare_model(resolved, remote_entry, timedelta(seconds=60))

        self.assertEqual(result.status, "update-available")

    def test_respects_tolerance_window(self) -> None:
        resolved = ResolvedModel(
            model_key="test-model",
            display_name="Test Model",
            publisher="tester",
            local_path=Path("C:/models/test.gguf"),
            local_modified_utc=datetime.fromisoformat("2026-04-18T00:00:00+00:00"),
            local_size_bytes=100,
            remote_repo="tester/test-model",
            remote_file="test.gguf",
            quantization="Q4_K_M",
        )
        remote_entry = {"lastCommit": {"date": "2026-04-18T00:00:30Z"}}

        result = compare_model(resolved, remote_entry, timedelta(seconds=60))

        self.assertEqual(result.status, "up-to-date")


class CandidateReferenceTests(unittest.TestCase):
    def test_deduplicates_candidate_paths(self) -> None:
        entry = {
            "modelKey": "unsloth/qwen3.5-35b-a3b",
            "path": "unsloth/Qwen3.5-35B-A3B-GGUF/Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf",
            "indexedModelIdentifier": "unsloth/Qwen3.5-35B-A3B-GGUF/Qwen3.5-35B-A3B-UD-Q4_K_XL.gguf",
        }

        candidates = candidate_references(entry, {})

        self.assertEqual(len(candidates), 1)


if __name__ == "__main__":
    unittest.main()
