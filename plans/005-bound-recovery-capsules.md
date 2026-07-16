# Plan 005: Make recovery capsules bounded and non-recursive

> **Executor instructions**: Work only in the capsule script and focused tests. Use temporary
> repositories and synthetic files; never test against real credentials.
>
> **Drift check**: `git diff --stat 9c907b3..HEAD -- scripts/session_capsule.py tests/test_session_capsule.py`

## Status

- **State**: COMPLETE (2026-07-13)
- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: plan 001
- **Category**: correctness / DX
- **Planned at**: commit `9c907b3`, 2026-07-13

## Why this matters

The teardown capsule succeeded, but it copied files from its own output directory into
`application/untracked-files/recovery/capsules/...`, creating recursive duplication. The 266 MB
archive also captured report artifacts twice through Git untracked capture and state capture. During
creation, a subprocess reader hit a Windows cp1252 decode error even though the command reported no
failures. Recovery tooling must be smaller and more trustworthy than the state it protects.

## Current state

- `scripts/session_capsule.py:125-136` lists every untracked file and copies it without excluding the
  capsule/archive paths.
- `_capture_state` separately copies `reports`, training, adapters, and other durable paths.
- `_capture_persist_root` excludes a child named `capsules`, but that protection does not apply to
  `_capture_git`.
- `_run` uses `subprocess.run(..., text=True)` with platform-default decoding.
- No `tests/test_session_capsule.py` currently exists.
- The observed capsule contains `application/untracked-files/recovery/capsules/...`.

## Commands

| Purpose | Command | Expected success |
|---|---|---|
| Focused tests | `python -m pytest -q tests/test_session_capsule.py` | all pass |
| Lint | `python -m ruff check scripts/session_capsule.py tests/test_session_capsule.py` | exit 0 |
| Verify | `python scripts/session_capsule.py verify <fixture-capsule>` | PASS |

## Scope

**In scope**: `scripts/session_capsule.py` and new focused tests.

**Out of scope**: changing database export formats, deleting existing archives, modifying current
recovery evidence, or adding compression dependencies.

## Steps

### Step 1: Centralize path-exclusion logic

Add a helper that resolves whether a source is equal to, inside, or an ancestor of the active capsule
or archive output. Apply it to Git untracked copying, persist-root copying, and state sources. Reject
an archive path inside the capsule directory before writing.

**Verify**: path-unit tests cover sibling, descendant, ancestor, symlink-resolved, and Windows case
normalization behavior.

### Step 2: Deduplicate state and untracked capture

Untracked files below known `STATE_PATHS` should be listed in Git metadata but copied only under
`state/`, not again under `application/untracked-files`. Record exclusions and reasons in the capsule
manifest so completeness remains auditable.

**Verify**: a synthetic report appears exactly once in the capsule; a normal untracked source file is
still copied under application metadata.

### Step 3: Make subprocess decoding deterministic

Capture subprocess bytes and decode explicitly as UTF-8 with replacement (or use explicit
`encoding="utf-8", errors="replace"`). Record replacement/command failures without crashing reader
threads. Never echo secret command arguments into errors.

**Verify**: a fixture emitting invalid UTF-8 creates a readable diagnostic and no uncaught thread
exception.

### Step 4: Add end-to-end capsule regression tests

Create a temporary Git repository with tracked, staged, modified, untracked, report, and nested output
files. Build, verify, archive, extract, and verify again. Assert no path contains a second
`capsules/.../application/untracked-files/.../capsules` segment and secret-like environment values are
redacted.

**Verify**: focused tests and Ruff pass.

## Test plan

- Path exclusion and archive-location unit tests.
- Recursive-output regression matching the observed `recovery/capsules` layout.
- Duplicate report suppression.
- Invalid-byte subprocess output.
- Secret-name redaction and checksum tamper detection.

## Done criteria

- [x] Capsule output never captures itself or its archive.
- [x] Durable reports are not duplicated under untracked files.
- [x] Invalid console bytes cannot crash a reader thread.
- [x] Build/extract/verify regression test passes.
- [x] Existing recovery archives remain untouched.

## STOP conditions

- Fixing recursion would omit ordinary untracked source files without recording them.
- A test requires reading a real `.env` or credential.
- Backward compatibility would require mutating an existing capsule.

## Maintenance notes

Every new durable state path must be added to the deduplication policy. Review archive size and
manifest exclusions during teardown drills, not only after a real droplet is about to be destroyed.
