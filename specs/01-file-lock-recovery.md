# 1. File-lock recovery can lose the original

Priority: critical. Touches `lmstudio_weight_updater.py`. Serves `SPEC.md`
sections 2 and 5.

## Observed

`UpdateExecutor.execute` installs each artifact by moving the original to
`backup_path`, then moving the staged file to `destination`
(`lmstudio_weight_updater.py:717-733`). If the second move raises
`PermissionError`, the handler at lines 722-728 tries to put the backup back.
That restore is itself an `os.replace` that can fail, and if it does:

- the `OSError` from the restore replaces the original exception,
- the handler never raises `InstallError`,
- `_rollback` (`lmstudio_weight_updater.py:858-869`) refuses to restore a
  backup when `destination` does not exist, because the restore is guarded by
  `if artifact.destination.exists()`,
- the artifact is left with no file at `destination` and its only copy at
  `backup_path`, and the error text does not say so.

A second, smaller defect in the same block: the two `except` clauses at lines
722 and 729 call the restore and then raise. If the restore raises, the
`InstallError` with the "Unload the model in LM Studio and retry" guidance never
reaches the user.

`execute`'s catch-all at line 828 writes a manifest for whatever exception
arrives, so today the failure path is reachable but the manifest phase is
`failed` or `rolling-back`, which tells recovery nothing about the artifact that
needs attention.

## Required

1. A failed install of one artifact MUST leave that artifact's original bytes
   reachable at either `destination` or `backup_path`. Never neither.
2. When both the install move and the restore move fail, the executor MUST
   raise `InstallError`. The message MUST contain the destination path, the
   backup path, and the underlying error text.
3. When the restore succeeds, the raised error MUST be the existing
   "Model file is in use ... Unload the model in LM Studio and retry" guidance,
   chained from the original `PermissionError`.
4. A failed restore MUST NOT be swallowed by the generic handler and MUST NOT
   be reported as a plain `bad`/`failed` job. It MUST leave the manifest in a
   phase that names the affected artifact and both paths, per requirement 2.
5. No requirement here may weaken the existing guarantee that a locked file
   leaves an unchanged model readable: whatever happens, the pre-update bytes
   MUST remain on disk somewhere the recovery pass can find.
6. LM Studio MUST NOT be terminated or asked to unload anything by the updater.

Design note, not a requirement: proving the destination is writable before
moving the original aside removes this failure mode instead of recovering from
it. If you take that route, requirement 1 still holds and the tests below still
apply, with the "restore path" being the proof-of-writability path.

## Out of scope

- `_rollback`'s internal ordering and reporting. That is spec 2.
- Disk accounting for backups. That is spec 3.
- Deleting backups. That is spec 4.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`. Use the existing `make_plan`
and `replace_file` injection point. The existing
`test_permission_error_restores_original_and_mentions_unload` stays as is; it
covers the restore-succeeds path.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_failed_restore_keeps_original_reachable` | `replace_file` raises `PermissionError` on the staged-to-destination move AND on the backup-to-destination restore | `InstallError` raised; message contains both paths and the cause; the original bytes are readable at `backup_path` |
| `test_failed_restore_names_required_recovery` | as above | persisted manifest `phase` is the recovery phase for a failed restore, and the manifest names the artifact |
| `test_restore_failure_is_not_swallowed` | as above | no bare `OSError` escapes; the raised type is `InstallError` |
| `test_locked_file_guidance_survives_restore_success` | restore succeeds | message contains "Unload the model in LM Studio" |

The fault injector must count calls, because the executor makes a variable
number of moves per artifact. Pin the count to the specific move under test
rather than a magic ordinal if the implementation changes the number of moves.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Both must pass with no network access. The second must still report 136 tests
plus the new ones.
