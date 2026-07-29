"""Generate the one-time deployment secrets for a new ShelfWise shop.

ShelfWise is one backend deployment per tenant (see `default_tenant_context` in
`shelfwise_backend/tenant.py` and `/auth/login` in `shelfwise_backend/app.py`): the tenant
id and the first owner's login are both environment configuration, not something created
through the running API. Every self-serve capability that exists once an owner is signed in
- connecting ERP/POS systems and cameras, importing a CSV, inviting staff - assumes that
first deploy step already happened.

This script is the one remaining manual step, reduced to "run one command, get a ready-to-
paste .env fragment" instead of "hand-derive a scrypt hash and a Fernet-compatible secret."
It performs no deployment itself and makes no network call - it only generates the values a
new shop's `.env` needs, so provisioning a shop stays a deploy-time decision an operator (or
a scripted CI job) makes deliberately, not something reachable from the public frontend.

Usage:
    python scripts/provision_new_shop.py --company "Boxer Bramley" \\
        --owner-email owner@boxer-bramley.example --owner-password 'a real passphrase'

    # Or let it generate a first-login password for you to hand to the owner once:
    python scripts/provision_new_shop.py --company "Boxer Bramley" \\
        --owner-email owner@boxer-bramley.example
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shelfwise_backend.auth_credentials import scrypt_password_hash


@dataclass(frozen=True, slots=True)
class ShopProvisioningResult:
    tenant_id: str
    owner_email: str
    owner_password: str
    env_fragment: str


def slugify_tenant_id(company_name: str) -> str:
    """Turn a company name into the env-var-safe tenant id `SHELFWISE_TENANT_ID` expects."""
    slug = re.sub(r"[^a-z0-9]+", "_", company_name.strip().lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        raise ValueError("company name has no usable characters for a tenant id")
    return slug


def provision_new_shop(
    *,
    company_name: str,
    owner_email: str,
    owner_password: str | None = None,
    generate_secret: bool = True,
) -> ShopProvisioningResult:
    """Build the env-var block for one new shop's backend deployment.

    Returns the plaintext owner password only in-memory, once, for the caller to relay to
    the owner out of band (it is never written to the env fragment - only its scrypt hash
    is). Passing an explicit `owner_password` lets the owner set their own first password
    instead of receiving a generated one.
    """
    tenant_id = slugify_tenant_id(company_name)
    password = owner_password or secrets.token_urlsafe(18)
    password_hash = scrypt_password_hash(password)
    tenant_secret = secrets.token_urlsafe(32) if generate_secret else ""
    credential_key = secrets.token_urlsafe(32) if generate_secret else ""

    lines = [
        f"SHELFWISE_TENANT_ID={tenant_id}",
        "SHELFWISE_AUTH_MODE=jwt",
        f"TENANT_AUTH_SECRET={tenant_secret}",
        f"SHELFWISE_LOGIN_EMAIL={owner_email.strip().lower()}",
        f"SHELFWISE_LOGIN_PASSWORD_HASH={password_hash}",
        f"SHELFWISE_CREDENTIAL_ENCRYPTION_KEY={credential_key}",
    ]
    return ShopProvisioningResult(
        tenant_id=tenant_id,
        owner_email=owner_email.strip().lower(),
        owner_password=password,
        env_fragment="\n".join(lines) + "\n",
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", required=True, help="Shop/company name, e.g. 'Boxer Bramley'")
    parser.add_argument("--owner-email", required=True, help="First owner login email")
    parser.add_argument(
        "--owner-password",
        default=None,
        help="First owner password. Omit to have one generated and printed once.",
    )
    parser.add_argument(
        "--write-env",
        metavar="PATH",
        default=None,
        help="Append the generated variables to this .env file instead of only printing them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    result = provision_new_shop(
        company_name=args.company,
        owner_email=args.owner_email,
        owner_password=args.owner_password,
    )

    print(f"Tenant id: {result.tenant_id}")
    print(f"Owner email: {result.owner_email}")
    if args.owner_password is None:
        print(
            "Generated owner password (relay this once, it is never shown again): "
            f"{result.owner_password}"
        )
    print()
    print("Add these to the new shop's .env, then deploy that instance:")
    print(result.env_fragment)

    if args.write_env:
        target = Path(args.write_env)
        with target.open("a", encoding="utf-8") as handle:
            if target.stat().st_size > 0:
                handle.write("\n")
            handle.write(result.env_fragment)
        print(f"Appended to {target}")

    print(
        "Next: deploy this backend instance with the above .env, then sign in at "
        "/auth/login with the owner email/password above - everything after that "
        "(connectors, cameras, CSV import, staff accounts) is self-serve in the frontend."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
