# 10. Manifest temp file collides and leaks

Priority: medium. Touches `lmstudio_weight_updater.py`. Serves `SPEC.md`
section 4 (deterministic job manifests).

## Observed

`_write_json_atomic` (`lmstudio_weight_updater.py:481-485`) writes to a fixed
sibling path:

```python
temporary = path.with_suffix(path.suffix + ".tmp")
temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
os.replace(temporary, path)
```

The job id in `manifest_path` is derived from artifact identity, which is the
point: the same selection produces the same `job_id` (line 408-417). So two
processes working the same selection — a CLI run and the tray app, or two
watched invocations — write the same `.tmp` path. One of them replaces a
half-written file into the manifest. The manifest is the only record recovery
trusts, so a torn manifest reads as a corrupt job and the recovery path takes
the "Unreadable manifest" branch at line 949-951.

The leak is separate and easier to hit: if the process dies between
`write_text` and `os.replace`, the `.tmp` file stays. Nothing ever removes
files matching `*.tmp` in the jobs directory. `recover_interrupted_jobs` globs
`job-*.json` (line 946), so the orphan is invisible to recovery and to
`_cleanup_staging`.

## Required

1. The temporary file MUST have a unique name, created in the manifest's own
   directory so the replace stays on one volume.
2. The temporary file MUST be removed if the write or the replace fails.
3. `write_text` followed by `os.replace` MUST be replaced by a write that
   flushes and closes before the replace. `json.dump` to an opened handle is
   acceptable; `write_text` plus a separate open is not.
4. Recovery MUST delete `*.tmp` files in the jobs directory that are not
   currently in use by a live job. Since a live job in this process is not a
   thing recovery can see, restrict this to files older than a short threshold
   (one hour is enough) and report what was deleted.
5. A manifest MUST NOT be considered valid if it is not valid JSON, and that
   MUST stay a `reported` action, not a crash. Existing behavior at line 950
   stands.
6. Only files created by this mechanism may be deleted. Match on the exact
   temp-file name pattern used in requirement 1, nothing broader.

## Out of scope

- Locking the manifest against concurrent writers. Unique temp names plus
  atomic replace is enough; a lock file adds a stale-lock problem.
- The manifest's `owned_staging_files` bookkeeping.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_write_manifest_leaves_no_temp_file` | write a manifest | directory contains only the manifest |
| `test_write_manifest_temp_names_are_unique` | capture the temp path via a patched `tempfile` helper, write twice | names differ |
| `test_write_manifest_cleans_up_after_replace_failure` | patch `os.replace` to raise | no temp file remains; exception propagates |
| `test_write_manifest_survives_concurrent_writers` | two threads writing the same manifest path | final file parses as JSON and contains one complete payload |
| `test_recovery_prunes_stale_temp_files` | create a matching temp file with an old mtime | deleted and reported |
| `test_recovery_keeps_recent_temp_files` | matching temp file, fresh mtime | not deleted, not reported |

For the concurrent test, both threads must write the same `manifest_path` from
the same plan. Assert the file parses; do not assert which writer won.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
