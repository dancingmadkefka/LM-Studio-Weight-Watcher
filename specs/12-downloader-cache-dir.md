# 12. Downloader fills the shared HF cache

Priority: medium. Touches `lmstudio_weight_updater.py`. Serves `SPEC.md`
section 2: staging is outside the models directory and on the same volume as the
destination.

## Observed

`HuggingFaceDownloader.download` calls `hf_hub_download` with `local_dir` set
but no `cache_dir` (`lmstudio_weight_updater.py:591-597`):

```python
downloaded = hf_hub_download(
    repo_id=artifact.remote_repo,
    filename=artifact.remote_file,
    local_dir=str(artifact.download_dir),
    force_download=False,
    tqdm_class=ProgressTqdm,
)
```

With `local_dir` set and no `cache_dir`, `huggingface_hub` still uses the
default cache (`%USERPROFILE%\.cache\huggingface\hub` on Windows) and links or
copies into `local_dir`. Consequences:

1. Every artifact occupies its bytes twice. The repo default cache is on the
   system drive; a models root on `D:` now needs system-drive space that the
   plan in spec 3 never accounts for.
2. If the cache and `local_dir` are on different volumes, the link degrades to a
   copy, so a 100 GB update needs 200 GB of staging traffic.
3. `artifact.download_dir` contains a `.cache` subtree the updater did not
   record in `owned_staging_files`. `_cleanup_staging` (lines 904-924) only
   removes files it owns, then reports "Preserved N unknown staging file(s)".
   Every successful update ends in a cleanup warning, which trains the user to
   ignore cleanup warnings. The existing test
   `test_cleanup_preserves_and_reports_unknown_staging_file` asserts that
   message for a deliberately unrelated file at line 525. That behavior stays
   correct for genuinely unknown files; it must not be the case that hides this
   noise. Requirement 3 is about a normal update having no unknown files to
   begin with.

## Required

1. The downloader MUST pass a `cache_dir` inside the job's staging tree. The
   cache MUST be under `staging_root`, not under the user's profile.
2. Cache files MUST be recorded as owned staging files and removed by cleanup.
   The whole `download_dir` subtree for a completed job is disposable by
   definition, because `staged_path` was moved out of it during install.
3. A completed update MUST NOT produce a cleanup warning on a normal run. Add a
   test asserting the success message carries no "Preserved" or "Cleanup
   remains" text.
4. The downloaded path MUST still equal `artifact.staged_path`. The existing
   comparison at lines 598-603 stays, and stays a hard error.
5. `force_download=False` MUST stay, so an already-complete staged file is
   reused rather than re-fetched. Resumability of a partial file comes from
   `huggingface_hub`'s own behavior and MUST NOT be broken.
6. Progress reporting MUST keep working through the `tqdm_class` hook,
   including cancellation checks on each `update` call.
7. The cleanup MUST still preserve and report files that cannot be attributed to
   the job. Requirement 2 covers files under the job's own download directory;
   anything outside it keeps the current conservative behavior.
8. On a run where the artifact is already fully staged, the downloader MUST NOT
   need to write to a global cache to satisfy the call.

## Out of scope

- The shared cache's existing contents. Do not delete `%USERPROFILE%\.cache\
  huggingface`. If it is worth reporting, report it once; do not touch it.
- Symlink versus copy semantics inside the local directory. Whatever
  `huggingface_hub` does, requirement 4 still holds.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`. The real `hf_hub_download`
cannot be called in tests, so patch it and assert on the arguments the
downloader passes, plus assert on the manifest and cleanup behavior with a fake
that writes both the artifact and a `.cache` file.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_downloader_passes_cache_dir_in_staging` | patch `hf_hub_download`, capture kwargs | `cache_dir` is under the artifact's staging root and not under the user profile |
| `test_downloader_uses_local_dir_and_no_force` | as above | `local_dir` equals `download_dir`; `force_download` is false |
| `test_successful_update_reports_no_cleanup_warning` | fake downloader writes a `.cache` subtree | result message contains no cleanup warning; `.cache` removed |
| `test_download_path_must_match_staged_path` | fake returns a different path | `UpdateError` raised |
| `test_unknown_file_outside_download_dir_still_preserved` | place a file elsewhere in staging | preserved and reported (existing behavior) |
| `test_downloader_does_not_touch_global_cache` | capture every path written under a patched `Path.home()` | none |

Assert on `cache_dir` through the captured kwargs. Do not assert on
`huggingface_hub`'s internal layout, which changes between versions.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Manual check, once, with the real downloader and a small public GGUF: confirm
the artifact lands in staging, that no new bytes appear under
`%USERPROFILE%\.cache\huggingface`, and that the update reports no cleanup
warning. This is the only way to see the real cache interaction; note the model
used in the commit message.
