"""Start ShelfWise for one shop with a single command.

`provision_new_shop.py` generates a shop's secrets but stops there: an operator still had
to paste the fragment into a `.env`, know which compose file to run, and know how to tell
when the stack was actually ready. That is the developer step between "downloaded the
application" and "signed in as the owner".

This script closes it. One command provisions the shop (only if it has not been provisioned
before), brings the Compose stack up, waits for the backend's real `/health` endpoint, and
prints the console URL plus the first-login credentials. Everything after sign-in - ERP/POS
connectors, retailer webhooks, camera/sensor devices, CSV import, staff accounts - is
already self-serve in the product.

    python scripts/start_shelfwise.py --company "Boxer Bramley" --owner-email owner@example.com

Re-running it on an already-provisioned directory just restarts the stack; it never
regenerates or overwrites existing secrets, because doing so would silently invalidate the
owner's password and make every stored connector credential undecryptable.

Scope, stated honestly: this deploys ShelfWise on the machine that runs it, which is what a
single shop testing the product needs. It is not a hosted multi-tenant control plane, and
it does not provision cloud infrastructure.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from provision_new_shop import provision_new_shop

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8000/health"
DEFAULT_CONSOLE_URL = "http://127.0.0.1:5173"


class StartupError(RuntimeError):
    """Raised when the stack cannot be started or never became healthy."""


def compose_command() -> list[str]:
    """Return the available Docker Compose invocation, preferring the v2 plugin."""
    if shutil.which("docker") is None:
        raise StartupError(
            "Docker is not installed or not on PATH. Install Docker Desktop (Windows/macOS) "
            "or Docker Engine (Linux), then run this command again."
        )
    probe = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose") is not None:
        return ["docker-compose"]
    raise StartupError(
        "Docker is installed but Docker Compose is not available. Install the Compose "
        "plugin, then run this command again."
    )


def ensure_provisioned(
    *,
    company: str | None,
    owner_email: str | None,
    owner_password: str | None,
    env_path: Path = ENV_PATH,
) -> tuple[bool, str | None, str | None]:
    """Provision this shop's secrets once.

    Returns `(newly_provisioned, owner_email, owner_password)`. An existing `.env` is never
    modified: regenerating `SHELFWISE_CREDENTIAL_ENCRYPTION_KEY` would make every already
    stored connector credential and device secret permanently undecryptable, and a new
    password hash would lock the owner out of their own instance.
    """
    if env_path.exists() and env_path.read_text(encoding="utf-8").strip():
        return False, None, None
    if not company or not owner_email:
        raise StartupError(
            "This shop has not been set up yet. Run again with --company and --owner-email, "
            'for example: --company "Boxer Bramley" --owner-email owner@example.com'
        )
    result = provision_new_shop(
        company_name=company,
        owner_email=owner_email,
        owner_password=owner_password,
    )
    env_path.write_text(result.env_fragment, encoding="utf-8")
    # Windows and some mounted filesystems do not support POSIX modes. The secrets are still
    # written; surfacing a hard failure here would block an otherwise fine setup.
    with contextlib.suppress(OSError):
        env_path.chmod(0o600)
    return True, result.owner_email, (result.owner_password if owner_password is None else None)


def start_stack(compose: list[str], *, build: bool = True) -> None:
    """Bring the Compose stack up, surfacing Docker's own error output on failure."""
    command = [*compose, "up", "-d"]
    if build:
        command.append("--build")
    completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if completed.returncode != 0:
        raise StartupError(
            "Docker Compose could not start the stack. The output above is Docker's own "
            "error; the most common causes are Docker Desktop not running, or ports 8000 "
            "or 5173 already being used by another application."
        )


def wait_for_health(
    url: str = DEFAULT_HEALTH_URL,
    *,
    timeout_s: float = 300.0,
    interval_s: float = 3.0,
    opener: object | None = None,
) -> dict:
    """Poll the backend's real health endpoint until it reports ready or the deadline passes.

    Bounded rather than unbounded: a stack that never becomes healthy must fail with a
    clear message instead of hanging forever and looking like a successful start.
    """
    fetch = opener or urllib.request.urlopen
    deadline = time.monotonic() + timeout_s
    last_error = "no response yet"
    while time.monotonic() < deadline:
        try:
            with fetch(url, timeout=5) as response:  # type: ignore[operator]
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok"):
                return payload
            last_error = f"health reported not-ok: {payload}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(interval_s)
    raise StartupError(
        f"ShelfWise did not become healthy within {int(timeout_s)}s (last check: {last_error}). "
        f"Run `{'docker compose'} logs backend` to see what the backend reported."
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start ShelfWise for one shop.")
    parser.add_argument("--company", default=None, help="Shop name, first run only")
    parser.add_argument("--owner-email", default=None, help="Owner login email, first run only")
    parser.add_argument(
        "--owner-password",
        default=None,
        help="Owner's first password. Omit to have one generated and shown once.",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Skip rebuilding images (faster restart of an unchanged checkout).",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for the backend to report healthy (default: 300).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        compose = compose_command()
        provisioned, owner_email, generated_password = ensure_provisioned(
            company=args.company,
            owner_email=args.owner_email,
            owner_password=args.owner_password,
        )
        if provisioned:
            print(f"Set up this shop and wrote {ENV_PATH.name}.")
        else:
            print(f"{ENV_PATH.name} already exists - keeping the existing setup and secrets.")
        print("Starting ShelfWise (first run builds images and can take several minutes)...")
        start_stack(compose, build=not args.no_build)
        print("Waiting for ShelfWise to become healthy...")
        wait_for_health(timeout_s=args.health_timeout)
    except StartupError as exc:
        print(f"\nCould not start ShelfWise: {exc}", file=sys.stderr)
        return 1

    print("\nShelfWise is running.")
    print(f"  Open: {DEFAULT_CONSOLE_URL}")
    if owner_email:
        print(f"  Sign in as: {owner_email}")
    if generated_password:
        print(f"  First password (shown once, save it now): {generated_password}")
    print(
        "\nAfter signing in, the Setup guide walks through connecting your tills, ERP, "
        "cameras and sensors, and importing your product data - all from the browser."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
