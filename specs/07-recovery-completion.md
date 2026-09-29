# 7. Recovery cannot finish an interrupted cleanup

Priority: high. Touches `lmstudio_weight_updater.py`. Serves `SPEC.md`
section 4: interrupted installations are inspected, and ambiguous state is
reported rather than guessed at.

## Observed

`recover_interrupted_jobs` (`lmstudio_weight_updater.py:938-971`) walks
manifests in `staging/jobs`, and for each artifact decides:

- destination missing and backup present: restore from backup, action
  `restored`,
- backup present at all: action `reported`, "Destination and rollback both
  exist; manual inspection required",
- otherwise: nothing.

Walk a crash during the cleanup loop (lines 761-774, and after spec 4 the
cleanup pass):

1. Install completes, all destinations hold verified bytes, validator passed.
2. Cleanup deletes backups for artifacts 1 and 2, then the process dies.
3. Next start: artifact 1 has no backup, so recovery is silent about it. That is
   correct and should stay silent.
4. Artifact 3 still has its backup. Destination and backup both exist, so
   recovery reports "manual inspection required" and the user is asked to
   investigate a file that is already correct and already verified.

The user is told to do manual work for a clean state, and there is no path that
finishes the interrupted cleanup. The tray surfaces the report once (see spec 8)
and then the manifest sits in the jobs directory forever, because the only code
that deletes manifests is `_cleanup_staging` with `remove_manifest=True` during
a successful run of the same job (line 928-929) — and a new run computes a new
`job_id` from the artifact identity, so it never re-enters the old job.

Two further gaps:

- A crash after the install loop but before `write_manifest(..., "validating")`
  leaves the manifest in the `installing` phase even though all files are in
  place. Recovery cannot tell "install finished" from "install interrupted
  mid-loop".
- Nothing bounds the jobs directory. Abandoned manifests and their staging data
  accumulate with no cleanup surface.

## Required

1. A manifest MUST be able to express "all artifacts installed and validated,
   backup deletion in progress". That is the phase spec 4 introduces; recovery
   MUST recognize it.
2. In that phase, recovery MUST classify a destination-plus-backup pair as
   "cleanup pending", not as ambiguous. The destination holds verified bytes;
   the backup is deletable.
3. Recovery MUST NOT itself delete a backup unless it can confirm the
   destination is correct. Confirmation means: the artifact is recorded as
   installed in a manifest that reached the cleanup phase. A manifest that never
   reached that phase MUST NOT authorize deletion.
4. Recovery MUST report counts, not a wall of paths: how many artifacts were
   restored, how many are cleanup-pending, how many need attention. Per-path
   detail belongs in the return value, which the CLI already prints.
5. Every terminal manifest MUST be removable. Add a cleanup path that deletes
   manifests and staging data for jobs in a terminal phase (completed,
   completed-cleanup-pending after cleanup, cancelled, failed with no
   outstanding backup). Recovery MUST call it, or the CLI MUST, once per run.
6. Abandoned staging data MUST be reported before it is deleted if its job
   cannot be identified. Files with no manifest MUST NOT be deleted
   automatically, per `SPEC.md` section 2.
7. The staging root MUST NOT be left in a state where `downloads/` or `jobs/`
   hold entries for jobs whose manifests were deleted. If a manifest is
   removed, its staging files are removed in the same pass, or both are kept.
8. Recovery MUST remain idempotent. Running it twice MUST produce the same
   end state and MUST NOT report a restored action twice for one artifact.

## Out of scope

- Which phases exist during a normal run. That is spec 4's additive phase.
- Surfacing recovery results in the tray. That is spec 8.
- Automatic cleanup of staging data for a job that is still in flight. Never do
  that.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_recovery_finishes_interrupted_cleanup` | manifest in cleanup phase; two artifacts with backups deleted, one still present, destinations all present | one cleanup-pending action; no "manual inspection" action; manifest and staging removed |
| `test_recovery_does_not_delete_without_cleanup_phase` | manifest in `installing` phase, destination and backup both present | backup still present after recovery |
| `test_recovery_restores_missing_destination` | existing test, kept | action `restored`; bytes correct |
| `test_recovery_reports_counts` | mixed state | summary counts match the per-path actions |
| `test_recovery_is_idempotent` | run twice | second run makes no changes and reports no new restores |
| `test_recovery_preserves_data_without_manifest` | orphan staging file, no manifest | file still present; reported |
| `test_terminal_manifest_is_pruned` | `completed` phase manifest | manifest and its staging files both gone after recovery |

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe .\lmstudio_weight_updater.py --all --dry-run
```

The dry run must print no `recovery:` line on a clean tree, and must print one
summary line with action counts when a fixture manifest is present. Per-path
actions remain available in the recovery return value. Manifests live in
`<models-root-parent>/.weight-watcher-staging/jobs`.
