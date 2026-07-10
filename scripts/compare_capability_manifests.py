from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from shelfwise_capabilities import (  # noqa: E402
    CapabilityContractError,
    CapabilityManifest,
    CapabilityPolicy,
    ContractViolation,
    DeploymentProfileSnapshot,
    assert_contract,
    canonical_json,
    compare_baseline_manifests,
    discover_manifest,
    load_json_model,
    manifest_schema,
    normalize_policy,
    normalize_profiles,
)

CAPABILITY_DIR = Path("capabilities")
MANIFEST_PATH = CAPABILITY_DIR / "manifest.json"
SCHEMA_PATH = CAPABILITY_DIR / "manifest.schema.json"
POLICY_PATH = CAPABILITY_DIR / "policy.json"
PROFILES_PATH = CAPABILITY_DIR / "profiles.json"


def main() -> int:
    """Generate or validate the normalized capability contract."""
    args = _parse_args()
    repo_root = args.repo_root.resolve()
    paths = _contract_paths(repo_root)
    policy = normalize_policy(load_json_model(paths["policy"], CapabilityPolicy))
    profiles = normalize_profiles(load_json_model(paths["profiles"], DeploymentProfileSnapshot))
    discovered = discover_manifest(repo_root, policy, profiles)

    if args.write:
        _write_snapshots(paths, policy, profiles, discovered)
        print(
            f"wrote {len(discovered.capabilities)} capabilities "
            f"({discovered.fingerprint})"
        )
        return 0

    manifest = load_json_model(paths["manifest"], CapabilityManifest)
    violations = _normalization_violations(paths, manifest, policy, profiles)
    try:
        assert_contract(manifest, policy, repo_root, discovered=discovered)
    except CapabilityContractError as exc:
        violations.extend(exc.violations)

    baseline = _load_baseline(args, repo_root)
    if baseline is not None:
        violations.extend(compare_baseline_manifests(baseline, manifest, policy))

    if violations:
        raise CapabilityContractError(_deduplicate_violations(violations))
    print(f"capability contract OK: {len(manifest.capabilities)} capabilities")
    print(manifest.fingerprint)
    return 0


def _parse_args() -> argparse.Namespace:
    """Parse CLI inputs for generation and base-branch comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root containing capabilities/, src/, and tests/.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Regenerate normalized manifest, schema, policy, and profile snapshots.",
    )
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument(
        "--base-ref",
        help="Git ref whose capabilities/manifest.json is the regression baseline.",
    )
    baseline.add_argument(
        "--base-manifest",
        type=Path,
        help="Manifest file used as the regression baseline.",
    )
    return parser.parse_args()


def _contract_paths(repo_root: Path) -> dict[str, Path]:
    """Return absolute paths for every committed contract snapshot."""
    return {
        "manifest": repo_root / MANIFEST_PATH,
        "schema": repo_root / SCHEMA_PATH,
        "policy": repo_root / POLICY_PATH,
        "profiles": repo_root / PROFILES_PATH,
    }


def _write_snapshots(
    paths: dict[str, Path],
    policy: CapabilityPolicy,
    profiles: DeploymentProfileSnapshot,
    manifest: CapabilityManifest,
) -> None:
    """Write all normalized generated and policy contract documents."""
    paths["manifest"].parent.mkdir(parents=True, exist_ok=True)
    paths["manifest"].write_text(canonical_json(manifest), encoding="utf-8")
    paths["schema"].write_text(canonical_json(manifest_schema()), encoding="utf-8")
    paths["policy"].write_text(canonical_json(policy), encoding="utf-8")
    paths["profiles"].write_text(canonical_json(profiles), encoding="utf-8")


def _normalization_violations(
    paths: dict[str, Path],
    manifest: CapabilityManifest,
    policy: CapabilityPolicy,
    profiles: DeploymentProfileSnapshot,
) -> list[ContractViolation]:
    """Check that all committed JSON documents are canonical and schema-current."""
    violations: list[ContractViolation] = []
    expected_text = {
        "manifest": canonical_json(manifest),
        "policy": canonical_json(policy),
        "profiles": canonical_json(profiles),
    }
    for name, expected in expected_text.items():
        if paths[name].read_text(encoding="utf-8") != expected:
            violations.append(
                ContractViolation(
                    "non_normalized_snapshot",
                    f"{paths[name].relative_to(paths['manifest'].parents[1])} needs --write",
                )
            )
    committed_schema = json.loads(paths["schema"].read_text(encoding="utf-8"))
    if committed_schema != manifest_schema():
        violations.append(
            ContractViolation(
                "schema_drift",
                f"{SCHEMA_PATH.as_posix()} differs from the typed model; run --write",
            )
        )
    if paths["schema"].read_text(encoding="utf-8") != canonical_json(committed_schema):
        violations.append(
            ContractViolation(
                "non_normalized_snapshot",
                f"{SCHEMA_PATH.as_posix()} needs --write",
            )
        )
    return violations


def _load_baseline(args: argparse.Namespace, repo_root: Path) -> CapabilityManifest | None:
    """Load an optional baseline from a file, explicit ref, or GitHub base branch."""
    if args.base_manifest:
        path = args.base_manifest
        if not path.is_absolute():
            path = repo_root / path
        return load_json_model(path, CapabilityManifest)

    base_ref = args.base_ref
    if not base_ref:
        github_base = os.getenv("GITHUB_BASE_REF", "").strip()
        base_ref = f"origin/{github_base}" if github_base else None
    if not base_ref:
        return None

    ref_check = subprocess.run(
        ["git", "cat-file", "-e", f"{base_ref}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if ref_check.returncode != 0:
        raise RuntimeError(f"baseline git ref does not resolve: {base_ref}")
    result = subprocess.run(
        ["git", "show", f"{base_ref}:{MANIFEST_PATH.as_posix()}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if "exists on disk, but not in" in result.stderr or "does not exist" in result.stderr:
            print(f"baseline {base_ref} has no capability manifest; regression check skipped")
            return None
        raise RuntimeError(result.stderr.strip() or "failed to read baseline capability manifest")
    return CapabilityManifest.model_validate_json(result.stdout)


def _deduplicate_violations(
    violations: list[ContractViolation],
) -> list[ContractViolation]:
    """Remove duplicate failures while retaining deterministic output order."""
    unique = {
        (item.code, item.capability_id, item.message): item
        for item in violations
    }
    return [unique[key] for key in sorted(unique, key=lambda item: tuple(str(v) for v in item))]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CapabilityContractError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
