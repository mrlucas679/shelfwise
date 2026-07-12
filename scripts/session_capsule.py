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


def _run(command: list[str], *, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a diagnostic command without raising or exposing secrets in errors."""
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout, result.stderr


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


def _copy_path(source: Path, destination: Path) -> bool:
    """Copy a file or directory when it exists, without following missing paths."""
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)
    return True


def _sha256_files(root: Path) -> list[str]:
    """Return sorted SHA-256 lines for every regular file below a capsule root."""
    rows: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  {path.relative_to(root).as_posix()}")
    return rows


class CapsuleBuilder:
    """Collect application, runtime, database, and environment recovery evidence."""

    def __init__(self, repo: Path, root: Path, capsule: Path, *, strict: bool) -> None:
        self.repo = repo.resolve()
        self.root = root.resolve()
        self.capsule = capsule.resolve()
        self.strict = strict
        self.failures: list[str] = []
        self.skipped: list[str] = []

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
            if source.exists() and not source.is_dir():
                _copy_path(source, destination / "untracked-files" / relative)

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
            if source.resolve() == self.capsule or not source.exists():
                self.skipped.append(f"state:{name}")
                continue
            try:
                _copy_path(source, destination / name)
            except OSError as exc:
                self.failures.append(f"state:{name}:{type(exc).__name__}")

    def _capture_persist_root(self, destination: Path) -> None:
        """Copy every durable-root child except the capsule directory itself."""
        if not self.root.exists():
            self.skipped.append("state:persist-root-not-found")
            return
        destination.mkdir(parents=True, exist_ok=True)
        capsule_parent = self.capsule.parent.resolve()
        for child in self.root.iterdir():
            if child.resolve() == capsule_parent or child.name == "capsules":
                continue
            try:
                _copy_path(child, destination / child.name)
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
    builder = CapsuleBuilder(repo, root, capsule, strict=args.strict)
    try:
        path = builder.build()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    archive = Path(args.archive) if args.archive else path.with_suffix(".tar.gz")
    if not args.no_archive:
        _write_archive(path, archive)
    print(
        json.dumps(
            {"capsule": str(path), "archive": str(archive), "failures": builder.failures},
            indent=2,
        )
    )
    return 0 if not builder.failures else 1


def _write_archive(capsule: Path, archive: Path) -> None:
    """Write gzip or zstd tar archives using the strongest available local tool."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".tar.zst"):
        code, _stdout, stderr = _run(
            ["tar", "--zstd", "-cf", str(archive), "-C", str(capsule.parent), capsule.name]
        )
        if code:
            raise RuntimeError(f"tar --zstd failed: {stderr}")
        return
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(capsule, arcname=capsule.name)


def _safe_extract_tar(archive: Path, target: Path) -> None:
    """Extract a gzip archive only when every member stays below the target root."""
    with tarfile.open(archive, "r:gz") as handle:
        target_resolved = target.resolve()
        for member in handle.getmembers():
            candidate = (target / member.name).resolve()
            if candidate != target_resolved and target_resolved not in candidate.parents:
                raise RuntimeError(f"archive path escapes restore target: {member.name}")
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
        code, _stdout, stderr = _run(["tar", "--zstd", "-xf", str(archive), "-C", str(target)])
        if code:
            print(f"tar --zstd restore failed: {stderr}", file=sys.stderr)
            return 1
    else:
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
