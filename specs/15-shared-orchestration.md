# 15. Update orchestration is duplicated

Priority: medium. Touches `lmstudio_weight_updater.py` and
`lmstudio_weight_watcher.py`. This one prevents future bugs rather than fixing
one.

## Observed

The CLI and the tray app implement the same policy twice.

CLI, `lmstudio_weight_updater.py:1070-1113`:

- defines `validate(plan)` which re-runs the checker and requires every selected
  model key to report `up-to-date`,
- runs `UpdateExecutor(...).execute(post_install_validator=validate)`,
- saves the hash cache,
- applies the fresh results to state when there are any,
- prints the result.

Tray, `lmstudio_weight_watcher.py:683-733`:

- defines `validate(plan)` with the same body and the same failure message,
- runs `UpdateExecutor(...).execute(post_install_validator=validate)`,
- saves the hash cache in a `finally`,
- returns an `UpdateWorkerOutcome` carrying `latest_results`,
- applies results to state later, in `finish_update` (lines 813-827).

The duplication is not just lines. It is the definition of what "the update
succeeded" means. The `failures` list comprehension,
`"Post-install checker did not confirm success: "`, and the `by_key` mapping are
character-identical in both. Any change to the success criterion has to be made
twice, and nothing fails if it is made once. The two callers already diverge in
their handling of the surrounding state.

## Required

1. One function MUST own the update run: given a plan, a cancellation token, a
   progress callback, the timeout, and a hash cache, it MUST build the validator,
   execute the plan, save the hash cache, and return the result plus the fresh
   check results.
2. Both callers MUST use it. The CLI keeps argument parsing, confirmation, and
   printing. The tray keeps threading, `root.after` marshalling, and state
   application.
3. The success criterion MUST exist once. Its message text MUST NOT change; the
   existing tests match on parts of it.
4. Hash cache saving MUST happen exactly once per run and MUST happen on failure
   paths as well. Today the CLI saves twice (lines 1100 and 1127) and the tray
   saves in a `finally`. One place.
5. Caller-specific behavior MUST be parameters or return values, not branches on
   who is calling. The fresh results are returned so the tray can apply them to
   state on the Tk thread, and the CLI can do what it already does.
6. Public names the tests import MUST keep working: `collect_check_results`,
   `_select_results`, `main`, `UpdateExecutor`, `build_update_plan`,
   `recover_interrupted_jobs`, `write_manifest`, and the exception types.
   `tests/test_lmstudio_weight_updater.py` imports all of those, and
   `tests/test_lmstudio_weight_watcher.py` imports
   `CancellationToken`, `ProgressEvent`, and `UpdatePlan`.
7. `lmstudio_weight_checker.py` MUST NOT change in this spec. It is shared and
   the changes above belong to the orchestration layer.
8. Redaction MUST stay at the outer boundary of each caller. The CLI redacts
   before printing (lines 1114-1125); the tray redacts before storing in state
   (`finish_update`, line 824). Neither may start trusting an inner layer.
9. Cancellation MUST behave exactly as today: cancellable through download and
   verify, refused once the executor publishes the non-cancellable install
   boundary.

## Out of scope

- The state schema. State application stays where it is.
- Splitting `UpdateExecutor` internally. That is spec 16.
- Unifying the CLI's and the tray's check scheduling.

## Acceptance tests

The proof here is behavioral equivalence plus the absence of a second copy. Add
to `tests/test_lmstudio_weight_updater.py`:

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_shared_run_returns_result_and_fresh_results` | fake downloader, validator succeeds | both values returned and consistent |
| `test_shared_run_validator_rejects_non_up_to_date_model` | post-check returns `update-available` | `InstallError` with the existing message text |
| `test_shared_run_saves_hash_cache_once` | count `save_hash_cache` calls | exactly one call on success |
| `test_shared_run_saves_hash_cache_on_failure` | injected install failure | still exactly one call |
| `test_cli_and_tray_share_the_run_function` | import both modules | both reference the same function object; no second `post_install_validator` definition exists in the watcher module source |

The last one is a source-level check. Parse
`lmstudio_weight_watcher.py` with `ast` and assert no function in it contains
the string `Post-install checker did not confirm success`. That is the guard
that stops the duplication from growing back.

Also add to `tests/test_lmstudio_weight_watcher.py`:

| Test | Assertion |
| --- | --- |
| `test_finish_update_applies_returned_results` | state updated from the shared run's results, unchanged from today's behavior |

## Verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_updater -v
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_watcher -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Every one of the 136 baseline tests must still pass. This spec is a refactor:
a test that changes behavior is a bug in the refactor, not a test to update.
