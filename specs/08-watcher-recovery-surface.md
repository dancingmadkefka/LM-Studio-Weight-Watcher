# 8. Tray recovery failures are invisible

Priority: high. Touches `lmstudio_weight_watcher.py`. Serves `SPEC.md`
section 4 (abandoned data is reported) and the M2 requirement that errors are
visible.

## Observed

`finish_recovery` (`lmstudio_weight_watcher.py:477-506`) only records anything
in state when `self.state["active_update"]` is a dict:

```python
active = self.state.get("active_update")
if not outcome.error and isinstance(active, dict):
    restored = sum(action.action == "restored" for action in outcome.actions)
    ...
    self.state = record_update_finished(...)
    save_state(self.state_path, self.state)
    self.status_var.set(message)
```

Three cases produce no visible signal:

1. Recovery found `reported` actions (ambiguous state, per spec 7) and there is
   no `active_update` record. The state file is not touched, the detail window
   does not mention it, and the only trace is the count in a status line that
   `self.run_check_async(reschedule=True)` at line 505 immediately replaces.
   That crash-in-the-middle-of-a-swap case is exactly the one that needs the
   user's attention.
2. `outcome.error` sets the status string, then line 505 starts a check whose
   `finish_check` overwrites it. The error is gone from the UI.
3. When there is an `active_update`, `record_update_finished(success=False,
   message=...)` records a generic "Previous update was interrupted" for the
   restored case and "Recovery details require attention" otherwise. The action
   paths never reach the user, and nothing is persisted when the caller is a
   user who never opens the window.

The tray icon stays green when this happens, so the user believes the models
are fine.

## Required

1. Any non-empty recovery result MUST be visible without opening a log file.
   That means: a status line that is not immediately overwritten, and one entry
   in the alert surface, or a toast, when actions require attention.
2. A recovery result MUST be persisted so a restart does not hide it. Use the
   existing state container: `record_update_finished` with a message that names
   the counts, or a dedicated key. Do not add a second state file.
3. The automatic check scheduled at line 505 MUST NOT erase the recovery
   message. Order the operations so the persisted message survives, and have
   `finish_check` preserve a pending recovery notice instead of clearing it.
4. A recovery result that reports restored files MUST be distinguishable from
   one that needs attention. Restored is informational; attention is an action
   item. Both MUST be persisted; only the second MUST raise the tray icon or
   toasts.
5. `outcome.error` MUST be persisted as well as shown. The user's next action
   differs between "recovery could not run" and "recovery ran and found
   nothing".
6. Redaction applies: `outcome.error` is already redacted at line 468. Keep it
   that way for anything persisted or shown.
7. No requirement here may block startup. If recovery raises, the app MUST still
   run its check and still show models.
8. The quit-after-update path (`quit_after_update`, lines 501-504) MUST keep
   working, including when recovery produced actions that need attention. Do
   not silently drop a pending notice on exit; write it to state before
   quitting.

## Out of scope

- What recovery decides. That is spec 7.
- Toast plumbing. `watcher_toasts.py` and the dedupe tests already exist; reuse
  them.

## Acceptance tests

Add to `tests/test_lmstudio_weight_watcher.py`, following the existing pattern
of constructing outcomes directly and calling the `finish_*` handlers with a
stubbed root.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_recovery_reported_actions_are_visible` | `RecoveryWorkerOutcome` with two `reported` actions, no `active_update` | state carries a message mentioning the count; status line mentions it; icon or toast raised |
| `test_recovery_message_survives_following_check` | as above, then call `finish_check` | the recovery message is still in state and still shown |
| `test_recovery_error_is_persisted` | outcome with `error` set, empty actions | error text in state and in the status line after a check |
| `test_recovery_restored_is_informational` | one `restored` action | message recorded; icon not raised |
| `test_recovery_quit_after_update_persists_notice` | `quit_after_update` set with pending attention actions | state written before `quit()` |
| `test_recovery_no_actions_stays_quiet` | empty actions, no error | no state change, no toast |

`recover_interrupted_jobs` is already patched in the existing watcher test
setup; keep using that seam rather than touching the filesystem.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_watcher -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```
