# 11. Plan building re-hashes multi-GB staged files

Priority: medium. Touches `lmstudio_weight_updater.py`. Perf, not correctness:
this one makes the confirmation dialog take minutes on a real model.

## Observed

`_reusable_staged_bytes` (`lmstudio_weight_updater.py:260-271`) streams a full
SHA-256 of each staged artifact to decide how many bytes still need downloading.
`build_update_plan` calls it for every artifact at line 436-439.

`build_update_plan` runs:

- in the CLI before the dry-run output, and again before the confirmation,
- in the tray app in `prepare_update_worker`
  (`lmstudio_weight_watcher.py:569-572`), which is what the user waits on before
  the confirmation dialog appears.

So a 17 GB quant in staging is read end to end before the user can see the
dialog, and read again by `verify_staged_artifact` during `execute`. The
purpose of the hash is only to produce `remaining_download_bytes`, a number
shown in the dialog and used for the space check in spec 3.

The data is not user data. It lives in staging, it was written by this
updater's own download, and the worst case of trusting a stale marker is one
wasted re-download, never a wrong install: `execute` verifies every artifact
before installing regardless (line 684-695).

Existing test `test_unrelated_staging_file_is_not_counted_as_resumable` pins the
important part, that an unrelated file in the download directory does not count
as resumable bytes. Keep it passing.

## Required

1. Building a plan MUST NOT read the full contents of an already-verified
   staged artifact. Requirement is on bytes read, not on wall time: a test that
   counts bytes read through a patched open is the proof.
2. Reuse MUST be proven by a sidecar marker written after a successful
   verification, recording at minimum the artifact's expected size, expected
   SHA-256, the staged file's size, and its `st_mtime_ns`.
3. A marker MUST be treated as invalid, and the file counted as not-reusable,
   when any recorded value disagrees with the file on disk or with the plan.
   Reuse is an optimization; a false positive here only costs a re-verification,
   so bias toward invalidating.
4. The marker MUST be written only after `verify_staged_artifact` succeeds.
   Never after a download, and never speculatively.
5. Markers MUST be owned staging files: tracked in the manifest's
   `owned_staging_files`, removed by cleanup, and preserved as unknown when
   cleanup is unsure. The "never delete unknown files" rule covers them.
6. A missing, unreadable, or corrupt marker MUST NOT raise. Fall back to
   counting the bytes as not reusable and let `execute` verify normally.
7. `verify_staged_artifact` MUST remain the sole authority on whether bytes are
   correct. Nothing in this spec may let an unverified file be installed.
8. The plan summary MUST still report `remaining_download_bytes` and
   `total_bytes` with the same meanings.

## Out of scope

- Skipping the verify pass in `execute`. It stays mandatory.
- Downloading with resumable ranges. That is `huggingface_hub`'s job and spec 12
  affects where it writes.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_plan_does_not_reread_verified_staged_file` | stage and verify a file before installation, then build a plan for the same artifact with a patched read counter | zero bytes read for that artifact; `remaining_download_bytes` is 0 |
| `test_marker_written_after_verification` | successful staged-file verification before installation | marker exists next to the staged file with the expected fields |
| `test_stale_marker_mtime_invalidates_reuse` | verify, then touch the staged file | plan counts the artifact's bytes as remaining |
| `test_marker_hash_mismatch_invalidates_reuse` | rewrite the marker with a wrong expected SHA-256 | counted as remaining |
| `test_corrupt_marker_does_not_raise` | write garbage into the marker | plan builds; artifact counted as remaining |
| `test_unrelated_file_still_not_reusable` | existing test, kept | unchanged behavior |
| `test_marker_is_cleaned_up_with_staging` | successful execute plus cleanup | marker gone |
| `test_marker_is_not_deleted_when_unknown` | place a marker in an abandoned job's directory with no manifest | preserved and reported |

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Do spec 12 first. Once the downloader writes its cache inside `download_dir`,
the marker logic must not treat cache files as markers or as staged artifacts.
