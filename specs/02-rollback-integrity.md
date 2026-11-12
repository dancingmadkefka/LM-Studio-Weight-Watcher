# 2. Rollback is best-effort and fails silently

Priority: critical. Touches `lmstudio_weight_updater.py`. Serves `SPEC.md`
sections 2 and 5.

## Observed

`_rollback` (`lmstudio_weight_updater.py:858-869`) restores each already
installed artifact in reverse order:

```python
if artifact.destination.exists():
    self._replace_file(artifact.destination, artifact.staged_path)
if had_original and artifact.backup_path.exists():
    self._replace_file(artifact.backup_path, artifact.destination)
```

Both moves share one `try`. If the first raises, the second never runs, so the
backup is not restored even though it is the only remaining original. The
artifact is left holding the new bytes with the old bytes parked at
`backup_path`, and the returned error only mentions the destination.

Three further gaps:

1. When `rollback_errors` is non-empty the executor raises `InstallError` and
   writes a `recovery-required` manifest (lines 840-848), but the manifest
   records only a joined error string. Nothing states which artifact is in
   which state, so recovery (`recover_interrupted_jobs`, line 938) cannot act on
   it and a human cannot either without guessing.
2. When `rollback_errors` is empty the job is marked `failed` (line 849). A
   rollback that failed to restore one artifact but succeeded on the others
   lands in the same phase as a rollback that fully succeeded, as long as no
   exception was appended. Verify this when implementing: a restore that
   silently does nothing because `destination` is missing appends nothing.
3. `installed.append` happens only after both moves succeed (line 733). An
   artifact whose install failed mid-sequence may still have been moved to
   backup by the injector path, and `_rollback` will not consider it.

## Required

1. Each restoration step MUST be attempted independently. A failure of one step
   MUST NOT prevent any other step from being attempted.
2. Per artifact, the executor MUST attempt, in this order: restore the backup to
   `destination`, and move the installed bytes back to staging.
3. A rollback that completes every step MUST leave every affected artifact
   byte-identical to its pre-update content. This is the existing guarantee and
   is already tested; keep it.
4. A rollback that cannot restore one or more artifacts MUST raise
   `InstallError` and MUST persist a manifest whose phase marks that recovery is
   required.
5. The manifest for a failed rollback MUST record, per affected artifact, the
   destination, the backup path, and whether the original was restored.
   Redaction rules apply as everywhere else; these are local paths, so they pass
   through unchanged.
6. Recovery MUST treat an entry marked "original not restored" as a restoration
   candidate when `destination` is missing and `backup_path` is present. It MUST
   NOT delete the backup in any case.
7. The overall transaction MUST still report failure to the caller. A partial
   rollback MUST NOT be reported as success.

Additive manifest field: add a per-artifact rollback status list. Existing
readers ignore unknown keys, and `recover_interrupted_jobs` reads
`payload["artifacts"]`, so the new field MUST be additive, not a restructuring.

## Out of scope

- The initial install moves and the file-lock message. That is spec 1.
- Whether backups should exist at cleanup time. That is spec 4.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_rollback_attempts_backup_restore_after_move_back_fails` | 2 artifacts; inject failure so that rollback's staged-move-back raises for the first artifact and the backup restore would succeed | the original bytes are back at that destination; no data loss |
| `test_rollback_reports_artifact_level_state` | inject a rollback restore failure for one of two artifacts | `InstallError` raised; manifest records the failed artifact with destination and backup path, and the other as restored |
| `test_partial_rollback_is_not_success` | as above | `execute` raises; no `UpdateResult` returned |
| `test_rollback_restores_every_artifact_on_full_success` | existing scenario, kept | all destinations byte-identical to pre-update |
| `test_recovery_restores_from_partial_rollback_manifest` | write the manifest produced above, delete the destination, run `recover_interrupted_jobs` | destination restored from backup; action reported as `restored` |

Distinguish injected failure by destination path, not call ordinal, so the test
does not break when the number of moves changes.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
