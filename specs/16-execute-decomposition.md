# 16. `execute` has outgrown one method

Priority: low. Touches `lmstudio_weight_updater.py`. Readability only. Do this
last, after specs 1, 2, 4 and 7 have settled the behavior it wraps.

## Observed

`UpdateExecutor.execute` (`lmstudio_weight_updater.py:654-856`) is one method of
roughly 200 lines and five nesting levels. In one `try` it does:

- download and verify each artifact, updating `owned_staging_files` and writing
  a manifest after each download (lines 662-695),
- publish the non-cancellable boundary and write the `installing` manifest
  (lines 697-710),
- install each artifact with backup, with two exception clauses that attempt a
  restore (lines 711-741),
- write `validating`, seed the hash cache, run the post-install validator
  (lines 743-759),
- delete backups with cleanup warnings (lines 761-774),
- write a terminal manifest and clean staging, with a nested `try` that retries
  the manifest write on `OSError` (lines 776-806),
- and in the two handlers, write one of four different terminal phases, roll
  back, restore the hash cache snapshot, and possibly re-raise as
  `InstallError` (lines 819-856).

The ordering invariant that makes recovery work — the manifest records a phase
*before* the mutation that phase describes — is real but only visible by reading
all 200 lines and noticing the pattern. Six separate `write_manifest` call sites
encode it. A future edit that moves one of them breaks recovery with no failing
test, because the tests assert end state, not manifest ordering.

## Required

1. `execute` MUST be split so that each phase is one method with a name that
   states the phase: stage, install, validate, finalize. Extraction, not
   redesign: no behavior change.
2. Manifest phase writes MUST move behind one helper that takes the phase and
   the optional error, so the call sites cannot drift in what they record.
3. The phase-before-mutation ordering MUST be stated in one place and covered by
   a test that asserts the manifest's phase observed *during* a mutation. The
   cleanest seam: a `replace_file` injector that reads the manifest file when it
   is called, and asserts the expected phase has already been written.
4. Rollback MUST stay in the failure path of one method, so a reader can see
   that install failure leads to rollback without crossing three methods. Do not
   bury the rollback call inside the new `install` method's own exception
   handling in a way that also catches rollback's own errors.
5. `execute`'s public signature, return type, and raised exceptions MUST NOT
   change. `tests/test_lmstudio_weight_updater.py` calls
   `UpdateExecutor(...).execute(post_install_validator=...)` and matches on
   `InstallError`, `IntegrityError`, and `UpdateCancelled`.
6. The `replace_file` injection point MUST stay a constructor parameter with the
   same name and signature. Every existing fault-injection test depends on it.
7. The hash cache snapshot/restore behavior (lines 747-751 and 837-839) MUST be
   preserved exactly. `test_validator_rollback_restores_hash_cache_snapshot`
   pins it.
8. No method introduces a new place where progress events are emitted with a
   different phase name than today. `handle_update_progress` in the watcher maps
   a fixed set of phase strings (`lmstudio_weight_watcher.py:761-770`).

## Out of scope

- Changing any phase name, manifest field, or on-disk layout.
- Merging download and verify. They stay separate phases because the UI shows
  them separately.
- Spec 15's shared-run extraction. Do that first; it changes `execute`'s caller,
  not its body.

## Acceptance tests

No new behavior, so the acceptance evidence is that the existing suite passes
plus the ordering test that did not exist before.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_manifest_records_phase_before_mutation` | `replace_file` injector reads the manifest on each call | first install move sees phase `installing`; the validating write happens before the validator runs |
| `test_failed_install_records_recovery_phase_before_rollback` | inject an install failure | manifest observed inside the rollback path already names the recovery/rolling-back phase |
| `test_rollback_failure_is_not_caught_by_install_handler` | inject a rollback failure | `InstallError` raised, not swallowed, and the manifest is `recovery-required` |
| existing suite | unchanged | all pass |

Add a source-level guard alongside the ordering test: assert the module has no
direct `write_manifest(` call inside `execute` other than through the helper, so
the invariant cannot be re-scattered. Parse the file with `ast`.

## Verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Diff review rule for this spec: the diff to `lmstudio_weight_updater.py` should
be almost entirely moved lines. Any changed string literal, phase name, or
manifest key in the diff is out of scope and must be reverted.
