# 6. Only a listing proves a file was removed

Priority: high. Touches `lmstudio_weight_checker.py`. Serves the checker's rule
that `removed-remote` requires confirmed absence.

## Observed

`get_remote_file_metadata` (`lmstudio_weight_checker.py:627-698`) tries the root
tree and the file's parent tree, then falls back to scanning first-level
directories. At the end it decides between `RemoteFileMissing` and
`CheckerError` using two flags:

- `repo_confirmed_gone`, set only when a root tree request 404s,
- `had_error`, set on any transient `CheckerError`.

If neither flag is set, line 696-698 raises `RemoteFileMissing("Could not find
remote file metadata for ...")`. That covers the case where the parent tree
request 404'd: `fetch_into_cache` records `{}` for it at line 657-659 without
setting either flag, because the `except RemoteFileMissing` branch only sets
`repo_confirmed_gone` when `parent == ""`. The file was never looked for in a
listing that succeeded, yet the verdict is "removed".

The comment at line 656 states the intended rule exactly: "The repo root being
gone means every file in it is gone; a missing subdir is not conclusive on its
own." The code does not implement that for the subdir case.

Consequence: a repository reorganizes its files into a subdirectory, or a
transient 404 comes back from the tree endpoint for the parent path, and the
user is told the model was removed upstream. The alert then offers
`find_weight_alternatives` suggestions for a file that is still published.

Note the second-order case: a directory genuinely removed upstream produces the
same 404 on its own tree, so it too is misclassified. The right fix covers both
by requiring positive evidence, not by trying to tell the two 404 meanings
apart. See spec 5 for the sibling case, a 404 from a wrong revision.

## Required

1. `removed-remote` MUST require a successful listing that covers the file's
   location and does not contain the file.
2. A 404 on a tree request for a non-root path MUST NOT by itself produce
   `removed-remote`. It MUST produce `unresolved` unless another successful
   listing proves absence.
3. A 404 on the repository root tree MUST continue to produce `removed-remote`.
   Existing test `test_repo_404_is_removed_remote` pins this.
4. Positive absence MUST be established by walking from the root: list the root,
   and if the file's parent directory is absent from that listing, the file is
   gone. If the parent directory is present, list it and check for the file.
   Reuse `tree_cache` so this costs no extra requests when the data is already
   there.
5. The fallback flat scan of first-level directories (lines 674-687) MUST keep
   working for files that moved between directories, but a request failure
   during that scan MUST set a flag that forces `unresolved`.
6. The two failure messages MUST stay distinguishable in the alert text:
   "could not confirm remote status" (transient or inconclusive) versus
   "removed on Hugging Face" (confirmed). The user acts differently on each.
7. `fetch_tree`'s 404 must keep raising `RemoteFileMissing`. Do not collapse it
   into `CheckerError`; the root-404 behavior depends on the distinction.
8. `find_weight_alternatives` MUST only be offered when absence is confirmed.
   The existing call site at line 829-833 triggers on `remote is None`, which is
   the confirmed-missing branch, so this holds as long as requirements 1-3 are
   met. Add a test rather than trusting it.

## Out of scope

- Which revision is listed. That is spec 5.
- Suggestion quality. Existing family/quant matching stands.

## Acceptance tests

Add to `tests/test_lmstudio_weight_checker.py`. Patch `fetch_tree` with a fake
that serves per-path listings and can be told to 404 specific paths.

| Test | Fake | Assertion |
| --- | --- | --- |
| `test_parent_directory_404_is_unresolved_not_removed` | root lists a `quants` directory; listing `quants` 404s | `unresolved`; message does not claim removal |
| `test_root_404_is_removed` | root 404s | `removed-remote` |
| `test_file_absent_from_listed_parent_is_removed` | root lists the parent; parent lists other files | `removed-remote` |
| `test_parent_absent_from_listed_root_is_removed` | root listing has no such directory | `removed-remote` |
| `test_flat_scan_finds_moved_file` | file now at a different first-level directory | not `removed-remote`; resolves the real entry |
| `test_no_suggestions_when_absence_unconfirmed` | parent 404 | `suggestions` is empty |
| `test_suggestions_when_absence_confirmed` | parent listed without the file, siblings present | suggestions populated |

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_checker -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

The existing `RemoteFailureClassificationTests` must pass unchanged. If one
fails, the implementation changed a confirmed-absence verdict into an
inconclusive one, or the reverse. Both are regressions here.
