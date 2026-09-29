# 5. Repos on a non-`main` branch look deleted

Priority: high. Touches `lmstudio_weight_checker.py`. Serves `SPEC.md`
section 3 and the checker's own rule that a transient or inconclusive answer is
`unresolved`, never `removed-remote`.

## Observed

`fetch_tree` hardcodes the revision in both URL shapes
(`lmstudio_weight_checker.py:705,707`):

```python
url = f"{HF_API_ROOT}/{repo_part}/tree/main/{parent_part}?expand=true"
url = f"{HF_API_ROOT}/{repo_part}/tree/main?expand=true"
```

A repository whose default branch is `master`, `dev`, or anything else returns
HTTP 404 for that path. `fetch_json` turns 404 into `RemoteFileMissing`
(line 741-742). `fetch_into_cache` then treats a 404 on the root tree as proof
the repository is gone and sets `repo_confirmed_gone` (line 654-659). The final
verdict at line 689 is `RemoteFileMissing("Repository removed on Hugging
Face")`, and `compare_artifact` maps that to `removed-remote` (line 803-804).

So a model that is perfectly healthy, and still published, is reported to the
user as "removed from Hugging Face". Two of those and the user stops trusting
the tool. The existing tests do not catch it: `test_repo_404_is_removed_remote`
patches `fetch_tree` to raise, which is the genuinely-gone case, and
`test_confirmed_missing_tree_is_removed_remote` returns an empty tree from a
successful listing, which is the genuine-removal case. Neither exercises a real
404 from a wrong revision.

## Required

1. The revision used for tree listings MUST be the repository's default
   revision, resolved from the Hugging Face API rather than assumed.
2. Resolution MUST be cached per repository for the life of one `run_check`
   call, so a repo with twenty shards makes one metadata request, not twenty.
   Reuse the existing `tree_cache` dictionary or add a sibling cache with the
   same lifetime.
3. Resolution failure MUST NOT produce a removal verdict. If the default
   revision cannot be determined, every artifact in that repository MUST be
   reported `unresolved` with a message that distinguishes "could not determine
   the repository's default revision" from "could not reach Hugging Face".
4. A definitive "the repository itself does not exist" MUST still be reported
   `removed-remote`. The existing `test_repo_404_is_removed_remote` must keep
   passing.
5. A definitive "the repository exists, the default revision was listed
   successfully, and the file is not in that listing" MUST still be reported
   `removed-remote`. The existing
   `test_confirmed_missing_tree_is_removed_remote` must keep passing.
6. The default revision MUST NOT be inferred from which URLs happen to work by
   trial and error. One metadata request answers it; guessing costs requests and
   reintroduces the ambiguity this spec removes.
7. The `Authorization` header behavior, token sources, and redaction rules in
   `fetch_json` MUST be shared by the new request, not duplicated. A new
   function that builds a request MUST go through the same code path.
8. Timeouts MUST be respected: the metadata request takes the same
   `timeout_seconds` as tree requests.

## Out of scope

- Distinguishing a removed directory from a removed file. That is spec 6.
- Retries, backoff, or request coalescing beyond the per-run cache.

## Acceptance tests

Add to `tests/test_lmstudio_weight_checker.py`, in the existing
`RemoteFailureClassificationTests` style: patch the HTTP layer
(`lmstudio_weight_checker.fetch_json`) with a fake that serves a repo whose
default branch is `master`, and assert on `compare_artifact` verdicts. No
network.

| Test | Fake response | Assertion |
| --- | --- | --- |
| `test_default_branch_other_than_main_is_checked` | metadata says `master`; tree for `master` contains the file with a matching LFS oid | `up-to-date`; never `removed-remote` |
| `test_default_branch_metadata_is_cached_per_repo` | three artifacts in one repo | `fetch_json` called exactly once for the metadata URL |
| `test_metadata_failure_is_unresolved_not_removed` | metadata request raises `CheckerError` | `unresolved`; message mentions the default revision, not "removed" |
| `test_repo_missing_still_removed` | metadata request 404s | `removed-remote` (existing test also covers the tree-level path) |
| `test_listed_tree_without_file_still_removed` | metadata ok, listing ok, file absent | `removed-remote` |
| `test_metadata_request_uses_token_and_timeout` | assert on the built `Request` headers and the timeout argument | `Authorization` present when the env token is set; timeout equals the passed value |

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_checker -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Check by hand against one real public model repository: fetch its metadata,
confirm the `sha` is a 40-character commit ID, then fetch its tree using that
SHA as the revision. Record the repository, SHA, and result in the review. Do
not download model files. This checks the live default-commit path used when
the API does not expose a default branch name.
