# 17. Small hygiene items

Priority: low. Independent of every other spec. Each item is a few lines and
needs no design discussion.

## 17.1 Unused import in the updater

`lmstudio_weight_updater.py:19` imports `ArtifactResult` from the checker and
never uses it. The name is only used in tests and in the checker itself.

**Required.** Remove it from the import list in `lmstudio_weight_updater.py`.
`tests/test_lmstudio_weight_updater.py:13` imports it from the checker directly
and is unaffected.

**Check.** `grep -n ArtifactResult lmstudio_weight_updater.py` returns nothing
after the change.

## 17.2 `_emit` swallows the cancellation exception

`UpdateExecutor._emit` (`lmstudio_weight_updater.py:628-652`) wraps the progress
callback in `except Exception: pass` with the comment that UI observers must not
compromise a transaction. That is right for observer bugs. It is wrong for
`UpdateCancelled`, which a callback can raise: the executor would then continue
downloading and installing after a cancellation won.

**Required.**

1. `_emit` MUST re-raise `UpdateCancelled`. Every other exception stays
   swallowed.
2. It MUST still re-raise after the transaction is safe, meaning the existing
   call sites that treat `UpdateCancelled` as the terminal signal keep working.
   Verify the call sites rather than assuming.
3. Redaction behavior on observer exceptions must not change; nothing is
   recorded today, and nothing should be recorded after.

**Acceptance test.** A progress callback that raises `UpdateCancelled` on the
first `downloading` event causes `execute` to raise `UpdateCancelled`, and no
artifact is installed.

## 17.3 A bare `OSError` masks the locked-file message

`HuggingFaceDownloader.download` calls `progress(path.stat().st_size,
...)` at line 604 with no guard. If the file vanished between the download and
the stat, the `OSError` surfaces from the downloader with no context.

**Required.** Wrap the final stat so a failure raises `UpdateError` naming the
artifact and the path, chained from the `OSError`.

**Acceptance test.** Patch `Path.stat` to raise `OSError` on the post-download
call; assert `UpdateError` naming the artifact.

## 17.4 The watcher's detail pane and selection path are untested

`_refresh_action_states` (`lmstudio_weight_watcher.py:1296-1329`) is already
covered by `test_refresh_action_states_disables_update_for_removed_only` and
`test_refresh_action_states_enables_update_for_updatable`. Leave those alone.

Two neighbours are not covered:

- `_render_detail` (lines 1329-1389), which builds the selection detail text
  from an alert's artifacts and suggestions,
- `_on_tree_select` (lines 1267-1296), which publishes the selection and calls
  `_render_detail`.

Both are reachable from `finish_update` and `refresh_tree`, so a change to the
alert shape can break the detail pane with a clean test suite.

**Required.**

1. Assert `_render_detail` text for three alert shapes: an alert with no
   artifacts, an alert with a `removed-remote` artifact carrying suggestions,
   and an alert whose only artifact is `missing-local`. The third is the one
   that must state the expected destination rather than implying the file is
   gone upstream.
2. Assert `_on_tree_select` with no selection and with a selection resolves to
   the expected alert and does not raise.
3. Do not introduce a real `tk.Tk()` in tests. Use the existing stubbed-root
   pattern from `tests/test_lmstudio_weight_watcher.py`. If stubbing a widget
   turns out to be impossible, say so in the commit message and leave the item
   open rather than adding a test that needs a display.

**Acceptance test.** The three detail shapes and the two selection states,
asserting on the produced text, not on a screenshot.

## 17.5 `--json` and `--dry-run` interact confusingly in the CLI

`lmstudio_weight_updater.py:1053-1067` prints the plan as JSON for `--dry-run`,
prints a human line when `--json` is absent, returns early for `--dry-run`, then
handles `--yes` and the interactive prompt. `--dry-run --json` prints JSON, and
`--dry-run` without `--json` also prints JSON. The flag does nothing in dry-run
mode.

**Required.** Either make `--dry-run --json` the documented machine-readable
mode and have `--dry-run` alone print the human line, or document that dry-run
is always JSON. Pick one. Whichever you pick, the `--help` text (lines 988 and
994) and the README Scripts section must agree with it. Today the README shows
`--dry-run` printing a plan and never mentions `--json` for the updater, so both
options need a README line.

**Acceptance test.** One test per mode asserting the shape of stdout: starts
with `{` and parses as JSON, or does not.

## Verification for all five

```powershell
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Keep these as separate commits, one per item, so a revert does not take the
others with it.
