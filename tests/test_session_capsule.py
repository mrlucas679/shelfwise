from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import session_capsule


def _git(repo: Path, *arguments: str) -> None:
    """Run one Git fixture command and fail the test on setup errors."""
    subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_path_relation_handles_sibling_nested_ancestor_and_case(tmp_path: Path) -> None:
    """Classify protected paths with OS-correct case normalization and boundaries.

    `_path_relation` compares through `os.path.normcase`, which is a no-op on POSIX
    (case-sensitive filesystems) and case-folds on Windows - so a differently-cased
    path is a genuinely different, unrelated path on Linux/macOS, not an alias of the
    original. Assert the platform-correct outcome instead of hardcoding Windows
    behavior, which made this test fail deterministically in Linux CI.
    """
    protected = tmp_path / "Capsule" / "active"
    sibling = tmp_path / "Capsule" / "other"
    case_insensitive_fs = os.path.normcase("A") == os.path.normcase("a")

    assert session_capsule._path_relation(protected, protected) == "equal"
    assert session_capsule._path_relation(protected / "file.txt", protected) == "inside"
    assert session_capsule._path_relation(protected.parent, protected) == "ancestor"
    assert session_capsule._path_relation(sibling, protected) is None
    recased = session_capsule._path_relation(Path(str(protected).lower()), protected)
    assert recased == ("equal" if case_insensitive_fs else None)


def test_path_relation_resolves_symlink_target(tmp_path: Path) -> None:
    """Treat a symlink into the active output tree as protected."""
    protected = tmp_path / "capsule"
    protected.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(protected, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    assert session_capsule._path_relation(link / "file.txt", protected) == "inside"


def test_run_decodes_invalid_utf8_with_replacement() -> None:
    """Invalid diagnostic bytes must become readable text rather than raising."""
    code, stdout, stderr = session_capsule._run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'prefix\\xff\\n')",
        ]
    )

    assert code == 0
    assert stdout == "prefix\ufffd\n"
    assert stderr == ""


def test_capsule_excludes_output_tree_and_deduplicates_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synthetic repository captures reports once and never captures its active capsule."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Capsule Test")
    tracked = repo / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "fixture")

    report = repo / "reports" / "report.json"
    report.parent.mkdir()
    report.write_text('{"result": "fixture"}\n', encoding="utf-8")
    ordinary = repo / "notes.txt"
    ordinary.write_text("ordinary untracked file\n", encoding="utf-8")
    root = repo / "recovery"
    capsule = root / "capsules" / "active"
    archive = root / "capsules" / "active.tar.gz"

    real_run = session_capsule._run

    def fake_run(command: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
        """Keep the fixture offline while preserving real Git metadata behavior."""
        if command and command[0] == "git":
            return real_run(command, cwd=cwd)
        return 0, "", ""

    monkeypatch.setattr(session_capsule, "_run", fake_run)
    monkeypatch.setenv("DATABASE_URL", "postgres://user:fixture-secret@db.example/db")
    builder = session_capsule.CapsuleBuilder(
        repo,
        root,
        capsule,
        strict=True,
        archive=archive,
    )

    built = builder.build()
    manifest = json.loads((built / "manifest.json").read_text(encoding="utf-8"))
    files = {
        path.relative_to(built).as_posix()
        for path in built.rglob("*")
        if path.is_file()
    }

    assert "state/reports/report.json" in files
    assert "application/untracked-files/reports/report.json" not in files
    assert "application/untracked-files/notes.txt" in files
    assert not any(
        path.startswith("application/untracked-files/recovery/capsules/") for path in files
    )
    assert any(item["reason"] == "captured under state" for item in manifest["exclusions"])
    assert any(item["reason"] == "capsule archive tree" for item in manifest["exclusions"])

    archive_path = tmp_path / "capsule.tar.gz"
    session_capsule._write_archive(built, archive_path)
    restore_target = tmp_path / "restore"
    session_capsule._safe_extract_tar(archive_path, restore_target)
    restored = restore_target / built.name

    assert session_capsule._verify(argparse.Namespace(capsule=restored)) == 0
    environment = json.loads(
        (built / "environment" / "environment.json").read_text(encoding="utf-8")
    )
    assert environment["DATABASE_URL"] == "db.example/db"
    assert "fixture-secret" not in json.dumps(environment)


def test_archive_cannot_be_written_inside_capsule(tmp_path: Path) -> None:
    """Reject an archive destination that would recursively contain the capsule."""
    capsule = tmp_path / "capsule"

    with pytest.raises(ValueError, match="inside the capsule"):
        session_capsule._write_archive(capsule, capsule / "archive.tar.gz")
