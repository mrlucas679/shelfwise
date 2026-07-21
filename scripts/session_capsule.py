"""Create and verify a disposable-droplet recovery capsule for ShelfWise."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

SECRET_NAME_PARTS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "AUTH")
CAPTURED_ENV_NAMES = (
    "APP_ENV",
    "SHELFWISE_PERSIST_ROOT",
    "TRAINING_OUTPUT_DIR",
    "HARNESS_RUN_DIR",
    "TRACE_DIR",
    "EVENT_STORE_DIR",
    "UPLOAD_DIR",
    "LOG_DIR",
    "HF_HOME",
    "TORCH_HOME",
    "TRITON_CACHE_DIR",
    "TMPDIR",
    "DATABASE_URL",
    "REDIS_URL",
    "SHELFWISE_STORE_BACKEND",
    "SHELFWISE_BUS_BACKEND",
    "LLM_ROUTINE_MODEL",
    "LLM_STRONG_MODEL",
    "LLM_COMPUTE_RESOURCE",
    "LLM_ACCELERATOR",
    "ROCM_VERSION",
    "PYTORCH_VERSION",
)
STATE_PATHS = (
    "runs",
    "reports",
    "data/harness_runs",
    "data/training",
    "data/eval",
    "shelfwise-gemma-final-adapter",
    "uploads",
    "exports",
    "logs",
)


def _decode_output(value: bytes | str | None) -> str:
    """Decode command output deterministically without failing on invalid bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run(command: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a diagnostic command without raising or exposing secrets in errors."""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, _decode_output(result.stdout), _decode_output(result.stderr)


def _write_text(path: Path, content: str) -> None:
    """Write UTF-8 diagnostic text, creating its parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _redact_env(name: str, value: str) -> str:
    """Keep configuration names while never persisting likely credential values."""
    upper = name.upper()
    if any(part in upper for part in SECRET_NAME_PARTS):
        return "[REDACTED]"
    if name in {"DATABASE_URL", "REDIS_URL"}:
        return value.split("@", 1)[-1] if "@" in value else "[CONFIGURED]"
    return value


def _path_relation(source: Path, protected: Path) -> str | None:
    """Classify source as equal to, inside, or an ancestor of a protected path."""
    source_text = os.path.normcase(str(source.resolve(strict=False)))
    protected_text = os.path.normcase(str(protected.resolve(strict=False)))
    separator = os.sep
    if source_text == protected_text:
        return "equal"
    if source_text.startswith(protected_text + separator):
        return "inside"
    if protected_text.startswith(source_text + separator):
        return "ancestor"
    return None


def _is_protected(path: Path, protected_paths: tuple[Path, ...]) -> bool:
    """Return whether a path is equal to or below a protected output tree."""
    return any(
        _path_relation(path, protected) in {"equal", "inside"}
        for protected in protected_paths
    )


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    protected_paths: tuple[Path, ...],
    copied_sources: set[str],
) -> bool:
    """Copy regular files while pruning protected output trees and duplicate sources."""
    if _is_protected(source, protected_paths):
        return False
    if source.is_file():
        source_key = os.path.normcase(str(source.resolve(strict=False)))
        if source_key in copied_sources:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied_sources.add(source_key)
        return True
    if not source.is_dir():
        return False

    copied = False
    for current, directories, files in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not _is_protected(current_path / name, protected_paths)
        ]
        for name in files:
            item = current_path / name
            if _is_protected(item, protected_paths):
                continue
            source_key = os.path.normcase(str(item.resolve(strict=False)))
            if source_key in copied_sources:
                continue
            relative = item.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied_sources.add(source_key)
            copied = True
    return copied


def _sha256_files(root: Path) -> list[str]:
    """Return sorted SHA-256 lines for every regular file below a capsule root."""
    rows: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  {path.relative_to(root).as_posix()}")
    return rows


class CapsuleBuilder:
    """Collect application, runtime, database, and environment recovery evidence."""

    def __init__(
        self,
        repo: Path,
        root: Path,
        capsule: Path,
        *,
        strict: bool,
        archive: Path | None = None,
    ) -> None:
        self.repo = repo.resolve()
        self.root = root.resolve()
        self.capsule = capsule.resolve()
        self.archive = archive.resolve() if archive else None
        self.strict = strict
        self.failures: list[str] = []
        self.skipped: list[str] = []
        self.exclusions: list[dict[str, str]] = []
        self._copied_sources: set[str] = set()

    @property
    def _protected_paths(self) -> tuple[Path, ...]:
        """Return active capsule and archive paths that must never be copied."""
        return tuple(path for path in (self.capsule, self.archive) if path is not None)

    def _exclude(self, source: Path, reason: str) -> None:
        """Record one auditable source exclusion without duplicating manifest entries."""
        entry = {"path": str(source), "reason": reason}
        if entry not in self.exclusions:
            self.exclusions.append(entry)

    def _copy_source(self, source: Path, destination: Path, *, reason: str) -> bool:
        """Copy a source while excluding active output trees and duplicate files."""
        if _is_protected(source, self._protected_paths):
            self._exclude(source, reason)
            return False
        return _copy_tree(
            source,
            destination,
            protected_paths=self._protected_paths,
            copied_sources=self._copied_sources,
        )

    def _is_repo_state_source(self, source: Path) -> bool:
        """Return whether a repository source is captured under the durable state section."""
        return any(
            _path_relation(source, (self.repo / state_path).resolve()) in {"equal", "inside"}
            for state_path in STATE_PATHS
        )

    def build(self) -> Path:
        """Build the capsule, write checksums, and return its directory."""
        self.capsule.mkdir(parents=True, exist_ok=False)
        self._capture_git()
        self._capture_environment()
        self._capture_databases()
        self._capture_state()
        manifest = {
            "schema_version": "1",
            "created_at": datetime.now(UTC).isoformat(),
            "repo": str(self.repo),
            "persist_root": str(self.root),
            "strict": self.strict,
            "failures": self.failures,
            "skipped": self.skipped,
            "exclusions": self.exclusions,
        }
        _write_text(self.capsule / "manifest.json", json.dumps(manifest, indent=2) + "\n")
        _write_text(self.capsule / "SHA256SUMS", "\n".join(_sha256_files(self.capsule)) + "\n")
        if self.failures and self.strict:
            raise RuntimeError("capsule incomplete: " + "; ".join(self.failures))
        return self.capsule

    def _capture_git(self) -> None:
        """Capture tracked changes, staged changes, status, and untracked files."""
        destination = self.capsule / "application"
        for name, command in {
            "git-status.txt": ["git", "status", "--short"],
            "git-diff.patch": ["git", "diff", "--binary"],
            "git-diff-cached.patch": ["git", "diff", "--cached", "--binary"],
            "git-head.txt": ["git", "rev-parse", "HEAD"],
        }.items():
            code, stdout, stderr = _run(command, cwd=self.repo)
            _write_text(destination / name, stdout or stderr)
            if code:
                self.failures.append(f"git:{name}")
        code, stdout, stderr = _run(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=self.repo
        )
        _write_text(destination / "untracked-files.txt", stdout or stderr)
        if code:
            self.failures.append("git:untracked-files")
            return
        for relative in stdout.splitlines():
            source = self.repo / relative
            if not source.exists() or source.is_dir():
                continue
            if _is_protected(source, self._protected_paths):
                self._exclude(source, "active capsule or archive output")
                continue
            if self._is_repo_state_source(source):
                self._exclude(source, "captured under state")
                continue
            self._copy_source(
                source,
                destination / "untracked-files" / relative,
                reason="active capsule or archive output",
            )

    def _capture_environment(self) -> None:
        """Capture reproducibility metadata while redacting credentials."""
        destination = self.capsule / "environment"
        values = {
            name: _redact_env(name, os.getenv(name, ""))
            for name in CAPTURED_ENV_NAMES
            if os.getenv(name, "")
        }
        values.update({"python": sys.version.replace("\n", " "), "platform": platform.platform()})
        _write_text(destination / "environment.json", json.dumps(values, indent=2) + "\n")
        commands = {
            "pip-freeze.txt": [sys.executable, "-m", "pip", "freeze"],
            "docker-images.txt": ["docker", "images"],
            "docker-ps.txt": ["docker", "ps", "-a"],
            "rocm-info.txt": ["rocminfo"],
            "gpu-info.txt": ["rocm-smi"],
            "apt-packages.txt": ["apt-mark", "showmanual"],
            "systemd-units.txt": ["systemctl", "list-unit-files"],
        }
        for name, command in commands.items():
            code, stdout, stderr = _run(command)
            if code == 127:
                self.skipped.append(f"environment:{name}")
            elif code:
                self.failures.append(f"environment:{name}")
            _write_text(destination / name, stdout or stderr)

    def _capture_databases(self) -> None:
        """Export PostgreSQL and Redis through application-aware tools when configured."""
        database_url = os.getenv("DATABASE_URL", "")
        if database_url:
            postgres_dir = self.capsule / "databases"
            code, _stdout, stderr = _run(
                ["pg_dump", "-Fc", database_url, "-f", str(postgres_dir / "postgres.dump")]
            )
            if code:
                self.failures.append("postgres:pg_dump")
                _write_text(postgres_dir / "pg_dump.error.txt", stderr)
            code, stdout, stderr = _run(["pg_dumpall", "--globals-only"])
            _write_text(postgres_dir / "postgres-globals.sql", stdout or stderr)
            if code:
                self.failures.append("postgres:pg_dumpall")
        else:
            self.skipped.append("postgres:DATABASE_URL-not-configured")

        redis_url = os.getenv("REDIS_URL", "")
        if redis_url:
            redis_dir = self.capsule / "databases"
            redis_dir.mkdir(parents=True, exist_ok=True)
            for command, name in (
                (["redis-cli", "-u", redis_url, "INFO", "persistence"], "redis-persistence.txt"),
                (["redis-cli", "-u", redis_url, "CONFIG", "GET", "appendonly"], "redis-config.txt"),
                (
                    ["redis-cli", "-u", redis_url, "--rdb", str(redis_dir / "redis.rdb")],
                    "redis-rdb.txt",
                ),
            ):
                code, stdout, stderr = _run(command)
                _write_text(redis_dir / name, stdout or stderr)
                if code:
                    self.failures.append(f"redis:{name}")
        else:
            self.skipped.append("redis:REDIS_URL-not-configured")

    def _capture_state(self) -> None:
        """Copy durable training, harness, runtime, upload, and report state."""
        destination = self.capsule / "state"
        self._capture_persist_root(destination / "persist-root")
        sources: list[tuple[str, Path]] = []
        for name in STATE_PATHS:
            sources.append((name, self.repo / name))
        configured = {
            "training": os.getenv("TRAINING_OUTPUT_DIR", ""),
            "harness": os.getenv("HARNESS_RUN_DIR", ""),
            "traces": os.getenv("TRACE_DIR", ""),
            "events": os.getenv("EVENT_STORE_DIR", ""),
            "uploads": os.getenv("UPLOAD_DIR", ""),
            "logs": os.getenv("LOG_DIR", ""),
        }
        for name, raw in configured.items():
            if raw:
                sources.append((name, Path(raw)))
        for name, source in sources:
            if not source.exists():
                self.skipped.append(f"state:{name}")
                continue
            try:
                self._copy_source(
                    source,
                    destination / name,
                    reason="active capsule or archive output",
                )
            except OSError as exc:
                self.failures.append(f"state:{name}:{type(exc).__name__}")

    def _capture_persist_root(self, destination: Path) -> None:
        """Copy every durable-root child except the capsule directory itself."""
        if not self.root.exists():
            self.skipped.append("state:persist-root-not-found")
            return
        destination.mkdir(parents=True, exist_ok=True)
        for child in self.root.iterdir():
            if child.name.casefold() == "capsules":
                self._exclude(child, "capsule archive tree")
                continue
            try:
                self._copy_source(
                    child,
                    destination / child.name,
                    reason="active capsule or archive output",
                )
            except OSError as exc:
                self.failures.append(f"state:persist-root:{child.name}:{type(exc).__name__}")


def _default_root(repo: Path) -> Path:
    """Resolve the durable root without forcing a machine-specific absolute path."""
    configured = os.getenv("SHELFWISE_PERSIST_ROOT", "").strip()
    return Path(configured) if configured else repo / "persist"


def _create(args: argparse.Namespace) -> int:
    """Create a capsule and optionally a compressed archive."""
    repo = Path(args.repo).resolve()
    root = Path(args.root).resolve() if args.root else _default_root(repo).resolve()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    capsule = root / "capsules" / f"shelfwise-session-{timestamp}"
    archive = Path(args.archive).resolve() if args.archive else capsule.with_suffix(".tar.gz")
    builder = CapsuleBuilder(repo, root, capsule, strict=args.strict, archive=archive)
    try:
        path = builder.build()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.no_archive:
        try:
            _write_archive(path, archive)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"archive creation failed: {exc}", file=sys.stderr)
            return 2
    print(
        json.dumps(
            {"capsule": str(path), "archive": str(archive), "failures": builder.failures},
            indent=2,
        )
    )
    return 0 if not builder.failures else 1


def _write_archive(capsule: Path, archive: Path) -> None:
    """Write a portable gzip archive that uses the same safe Python restore path."""
    if _path_relation(archive, capsule) in {"equal", "inside"}:
        raise ValueError("archive path must not be inside the capsule directory")
    if archive.name.endswith(".tar.zst"):
        raise ValueError("zstd capsules are not supported; use a .tar.gz archive")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(capsule, arcname=capsule.name)


def _safe_extract_tar(archive: Path, target: Path) -> None:
    """Extract only regular files and directories that remain below the restore root."""
    with tarfile.open(archive, "r:gz") as handle:
        target_resolved = target.resolve()
        for member in handle.getmembers():
            candidate = (target / member.name).resolve()
            if candidate != target_resolved and target_resolved not in candidate.parents:
                raise RuntimeError(f"archive path escapes restore target: {member.name}")
            # A link can make a later, otherwise in-root member write outside ``target``.
            # Devices/FIFOs also have no legitimate place in a portable diagnostic capsule.
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"archive member type is not allowed: {member.name}")
        handle.extractall(target)


def _restore(args: argparse.Namespace) -> int:
    """Restore a capsule archive without deleting existing target data."""
    archive = Path(args.archive).resolve()
    target = Path(args.target).resolve()
    if not archive.exists():
        print(f"archive not found: {archive}", file=sys.stderr)
        return 2
    if target.exists() and any(target.iterdir()) and not args.force:
        print(
            "restore target is non-empty; pass --force only after an explicit backup",
            file=sys.stderr,
        )
        return 2
    target.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".tar.zst"):
        print("zstd capsules are not supported; use a .tar.gz archive", file=sys.stderr)
        return 2
    _safe_extract_tar(archive, target)
    print(json.dumps({"verdict": "PASS", "restored_to": str(target)}, indent=2))
    return 0


def _verify(args: argparse.Namespace) -> int:
    """Verify SHA-256 entries in an existing capsule directory."""
    root = Path(args.capsule).resolve()
    checksum_file = root / "SHA256SUMS"
    if not checksum_file.exists():
        print("SHA256SUMS is missing", file=sys.stderr)
        return 2
    failures: list[str] = []
    for line in checksum_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        path = root / relative
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            failures.append(relative)
    if failures:
        print(json.dumps({"verdict": "FAIL", "files": failures}, indent=2))
        return 1
    print(json.dumps({"verdict": "PASS", "capsule": str(root)}, indent=2))
    return 0


def main() -> int:
    """Run the safe capsule create or verify command."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--repo", type=Path, default=Path.cwd())
    create.add_argument("--root", type=Path)
    create.add_argument("--archive", type=Path)
    create.add_argument(
        "--strict", action="store_true", help="Fail when configured DB exports fail"
    )
    create.add_argument("--no-archive", action="store_true")
    create.set_defaults(handler=_create)
    verify = subparsers.add_parser("verify")
    verify.add_argument("capsule", type=Path)
    verify.set_defaults(handler=_verify)
    restore = subparsers.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--target", type=Path, required=True)
    restore.add_argument(
        "--force",
        action="store_true",
        help="Allow restore into a non-empty target after making a separate backup",
    )
    restore.set_defaults(handler=_restore)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
