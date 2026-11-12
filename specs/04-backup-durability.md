# 4. Backups deleted before the swap is durable

Priority: high. Touches `lmstudio_weight_updater.py`. Serves `SPEC.md`
section 2: "Rollback copies MUST remain until a post-install checker pass
confirms the model is up to date."

## Observed

After the post-install validator returns, `execute` deletes each backup
immediately (`lmstudio_weight_updater.py:761-774`). The validator re-runs the
checker, which reads the destination, so the requirement is arguably met. The
window is still real:

- The checker's verdict comes from a hash cache seeded by `cache_verified_oid`
  at lines 752-758, so for a freshly installed file the validator may confirm
  identity without reading the new bytes at all. The comment on
  `cache_verified_oid` says this is deliberate.
- Between the seed and the unlink, nothing flushes the destination's directory
  entry. A crash can leave the destination's rename not yet durable while the
  backup's unlink is, on a filesystem that does not order these.
- Once the unlink loop starts, a crash partway leaves an ambiguous state that
  recovery reports rather than resolves (see spec 7).

The result is a period where the only copy of the new file is unflushed and the
old copy is gone.

## Required

1. Backups MUST NOT be deleted in the same synchronous block that installs
   them. Retain them past the post-install validation.
2. Retention MUST be recorded. The manifest MUST list the retained backup paths
   and the job MUST carry a phase that a later run recognizes as "installed and
   validated, cleanup pending".
3. A later successful checker pass confirming the model up-to-date MUST be what
   authorizes deletion. In practice: the next `execute` or CLI run performs a
   cleanup pass over manifests in the cleanup-pending phase and deletes the
   recorded backups only when the model check is clean.
4. Deletion MUST be durable-assisted: before unlinking a backup, flush the
   containing directory where the platform supports it (`os.fsync` on the
   directory handle on POSIX). On Windows, where directory fsync is not
   available, document that the ordering guarantee is provided by NTFS
   metadata ordering and skip the call.
5. `--keep-backups` MUST continue to mean "never delete". Retained-then-cleaned
   backups are a different thing from kept backups; naming and state must keep
   them distinguishable.
6. The user-visible result of a successful update MUST NOT change. The update
   still reports success; the leftover backup is an internal detail except when
   cleanup is pending, which MUST be reported once, not on every run.
7. Cleanup MUST never delete a path that is not recorded in a manifest as a
   backup this updater created. The "never delete unknown files" invariant is
   not negotiable.

Note on requirement 6's scope: the post-install validator already runs a full
checker pass, so a successful update has a fresh up-to-date result in hand.
When the same run can see that, it may treat the cleanup as already authorized
and not report anything pending. "Reported once" is for the case where cleanup
genuinely defers to a later run, which is the failure case in the test table
below. Do not make the common path noisier than it is today.

## Out of scope

- Finishing an interrupted cleanup after a crash. That is spec 7.
- Disk accounting for backups during planning. That is spec 3.

## Acceptance tests

Add to `tests/test_lmstudio_weight_updater.py`.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_success_retains_backups_until_cleanup` | normal successful update | `backup_path` still exists after `execute` returns; manifest lists it and the phase is cleanup-pending |
| `test_cleanup_pass_removes_only_manifested_backups` | run cleanup with a healthy model and an unrelated file next to the model | backup removed; unrelated file untouched |
| `test_cleanup_pass_keeps_backups_when_check_fails` | cleanup with a validator reporting a failed model | backup still present; cleanup deferred and reported once |
| `test_keep_backups_survives_cleanup_pass` | `keep_backups=True`, then cleanup | backup still present; not described as pending |
| `test_cleanup_ignores_unmanifested_backup_looking_file` | create a file named like a backup with no manifest | not deleted |

The existing `test_success_verifies_installs_seeds_cache_and_cleans` asserts
cleanup happens in the same pass. That expectation changes. Update the test and
note the change in the commit message; do not delete the test.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Also confirm by hand once, on a scratch models directory, that a successful
update leaves exactly one `.lmww-backup-*` file and that a second run of the CLI
removes it.
