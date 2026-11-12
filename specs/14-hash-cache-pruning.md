# 14. Hash cache cannot evict absolute-path keys

Priority: low. Touches `lmstudio_weight_checker.py`. Growth is unbounded but
slow; this is a leak, not a failure.

## Observed

`prune_hash_cache` (`lmstudio_weight_checker.py:1129-1142`) drops an entry when
`(models_root / key).is_file()` is false:

```python
for key in list(cache.keys()):
    if (models_root / key).is_file():
        continue
    del cache[key]
```

That form only makes sense for relative keys, which `_hash_cache_key` produces
when the path resolves under `models_root` (line 1119-1126). For any other path
it stores the resolved absolute path, and `models_root / <absolute Windows path>`
is a nonsense path that never exists, so the entry is deleted on the next run —
unless the absolute path is on the same drive, where `pathlib` discarding the
left operand can produce a path that exists and keeps the entry alive
indefinitely.

Either way the behavior is accidental rather than intended. Both cases are
reachable: models installed under a path that no longer resolves under
`models_root` after a settings change, and cache files written before the
relative-key change.

The cache is saved on every run (`save_hash_cache`, called from the checker's
`main`, the updater's CLI, and the watcher), so entries that never evict grow
the file and the state directory forever.

## Required

1. Keys that are absolute paths MUST be evaluated as absolute paths, not joined
   onto `models_root`.
2. An entry whose key does not identify an existing file MUST be removed,
   whether the key is relative or absolute. Preserve the current in-place
   mutation so callers that hold the reference still persist the pruned set.
3. An entry whose key resolves outside `models_root` MUST NOT be pruned merely
   for being outside. The current docstring promises this; keep the promise and
   make the test assert it by using a real file outside the root.
4. The eviction decision MUST be testable without a real outside-the-root file
   if it can be, but at minimum one test MUST use a real temp file outside the
   root, because that is the case the current code gets wrong.
5. Pruning MUST NOT raise on a malformed key (empty string, a key with a NUL, a
   path that `Path` rejects on this platform). A corrupt cache MUST degrade to
   "drop that entry", not to a checker crash.
6. The cache file's size on disk is not a hard requirement, but a test MUST
   assert a bounded entry count after pruning a cache stuffed with dead keys.

## Out of scope

- Changing the cache format or the key scheme for new entries.
- Capping total entries by age or count. If you want that, it is a new spec.

## Acceptance tests

Add to `tests/test_lmstudio_weight_checker.py`, in the existing
`HashCachePersistenceTests` style.

| Test | Setup | Assertion |
| --- | --- | --- |
| `test_prune_removes_dead_absolute_key` | cache key is an absolute path to a deleted file | entry removed |
| `test_prune_keeps_live_absolute_key_outside_root` | absolute path to a real file outside `models_root` | entry kept |
| `test_prune_keeps_live_relative_key` | relative key for a real file under the root | entry kept |
| `test_prune_removes_dead_relative_key` | relative key for a deleted file | entry removed |
| `test_prune_tolerates_malformed_keys` | keys `""`, `"a\x00b"`, a drive-only string | no exception; malformed entries removed |
| `test_prune_bounds_entry_count` | 1000 dead keys plus one live key | exactly the live entry remains |

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lmstudio_weight_checker -v
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

The existing `test_prune_hash_cache_mutates_in_place` must pass unchanged. It
pins requirement 2's in-place behavior.
