"""Workforce identity, account lifecycle, and dedicated-client bootstrap routes."""

from __future__ import annotations

import hmac
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shelfwise_storage import default_tenant_profile

from . import account_notifications
from .account_notifications import NotificationUnavailable
from .account_tokens import (
    AccountTokenClaims,
    issue_account_token,
    token_digest,
    verify_account_token,
)
from .auth_credentials import login_credentials_valid, scrypt_password_hash
from .deps import (
    CURRENT_TENANT_DEP,
    OWNER_AUTH_DEP,
    SESSION_COOKIE,
    WRITE_LIMIT_DEP,
    _cookie_secure_setting,
    _env_positive_int,
)
from .state import account_store, tenant_profile_store
from .tenant import Role, TenantContext, default_tenant_context, encode_hs256_token

router = APIRouter()


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=500)


class CreateWorkAccountBody(BaseModel):
    """Owner-provisioned account for controlled recovery and assisted setup."""

    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)
    given_name: str = Field(min_length=1, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    position: str = Field(min_length=1, max_length=120)
    role: Role
    password: str = Field(min_length=12, max_length=500)


class InviteWorkAccountBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)
    given_name: str = Field(min_length=1, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    position: str = Field(min_length=1, max_length=120)
    role: Role


class ChangeWorkAccountRoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role


class PasswordPairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=12, max_length=500)
    password_confirmation: str = Field(min_length=12, max_length=500)

    @model_validator(mode="after")
    def passwords_match(self) -> PasswordPairBody:
        if self.password != self.password_confirmation:
            raise ValueError("Passwords do not match")
        return self


class ActivateInvitationBody(PasswordPairBody):
    token: str = Field(min_length=20, max_length=4096)
    email: str = Field(min_length=3, max_length=200)
    given_name: str = Field(min_length=1, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    position: str = Field(min_length=1, max_length=120)


class PasswordResetRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=200)


class PasswordResetConsumeBody(PasswordPairBody):
    token: str = Field(min_length=20, max_length=4096)


class ChangeOwnPasswordBody(PasswordPairBody):
    current_password: str = Field(min_length=1, max_length=500)


class PlatformBootstrapBody(PasswordPairBody):
    company_name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    given_name: str = Field(min_length=1, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    position: str = Field(min_length=1, max_length=120)


@router.get("/auth/setup-status")
def account_setup_status() -> dict[str, bool]:
    """Tell the login UI whether the dedicated client stack still needs its first owner."""
    tenant_id = default_tenant_context().tenant_id
    return {"bootstrap_required": not bool(account_store.list(tenant_id))}


@router.post("/platform/bootstrap", dependencies=[WRITE_LIMIT_DEP])
def bootstrap_client_owner(
    body: PlatformBootstrapBody,
    x_bootstrap_key: str | None = Header(default=None, alias="x-bootstrap-key"),
) -> JSONResponse:
    """Create the configured dedicated client and its only bootstrap owner exactly once."""
    expected_key = os.getenv("SHELFWISE_PLATFORM_BOOTSTRAP_KEY", "")
    if not expected_key:
        raise HTTPException(status_code=503, detail="Platform bootstrap is not configured")
    if not x_bootstrap_key or not hmac.compare_digest(x_bootstrap_key, expected_key):
        raise HTTPException(status_code=401, detail="Invalid platform bootstrap credential")
    if not os.getenv("TENANT_AUTH_SECRET", ""):
        raise HTTPException(status_code=503, detail="Tenant authentication is unavailable")
    tenant_id = default_tenant_context().tenant_id
    try:
        account = account_store.create_first_owner(
            _account_payload(
                tenant_id=tenant_id,
                email=body.email,
                given_name=body.given_name,
                surname=body.surname,
                position=body.position,
                role=Role.OWNER,
                password=body.password,
            )
        )
        tenant_profile_store.upsert(
            {**default_tenant_profile(tenant_id, name=body.company_name), "name": body.company_name}
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    account_store.record_audit(
        tenant_id,
        action="platform_owner_bootstrapped",
        account_id=account["id"],
        actor_id="platform_bootstrap",
    )
    full_account = account_store.get_by_id(tenant_id, str(account["id"]))
    if full_account is None:
        raise HTTPException(status_code=500, detail="Platform bootstrap could not be completed")
    return _account_session_response(full_account)


@router.post("/auth/login", dependencies=[WRITE_LIMIT_DEP])
def company_login(body: LoginBody) -> JSONResponse:
    """Authenticate an active workforce account or migrate the configured legacy owner."""
    tenant_id = default_tenant_context().tenant_id
    account = account_store.get_by_email(tenant_id, body.email)
    if account is not None:
        if not account["active"] or not login_credentials_valid(
            email=body.email,
            password=body.password,
            configured_email=account["email"],
            configured_hash=account["password_hash"],
        ):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return _account_session_response(account)

    legacy_account = _migrate_or_recover_legacy_owner(body, tenant_id=tenant_id)
    if legacy_account is None:
        if (
            not account_store.list(tenant_id)
            and not os.getenv("SHELFWISE_LOGIN_EMAIL", "").strip()
        ):
            raise HTTPException(status_code=503, detail="Company login is not configured")
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return _account_session_response(legacy_account)


@router.post("/auth/activate", dependencies=[WRITE_LIMIT_DEP])
def activate_invitation(body: ActivateInvitationBody) -> JSONResponse:
    """Consume an unexpired invitation after the invited worker confirms their identity."""
    claims = _verified_token(body.token, purpose="activation")
    account = account_store.get_by_id(claims.tenant_id, claims.account_id)
    if account is None or not _identity_matches(account, body):
        raise HTTPException(status_code=400, detail="Invalid or expired invitation")
    activated = account_store.activate_invitation(
        claims.tenant_id,
        claims.account_id,
        token_hash=token_digest(body.token),
        password_hash=scrypt_password_hash(body.password),
    )
    if activated is None:
        raise HTTPException(status_code=400, detail="Invalid or expired invitation")
    account_store.record_audit(
        claims.tenant_id,
        action="invitation_activated",
        account_id=claims.account_id,
        actor_id=claims.account_id,
    )
    full_account = account_store.get_by_id(claims.tenant_id, claims.account_id)
    if full_account is None:
        raise HTTPException(status_code=500, detail="Account activation could not be completed")
    return _account_session_response(full_account)


@router.post("/auth/password-reset/request", dependencies=[WRITE_LIMIT_DEP])
def request_password_reset(body: PasswordResetRequestBody) -> dict[str, str]:
    """Send a generic password reset response without disclosing account existence."""
    _require_email_delivery()
    tenant_id = default_tenant_context().tenant_id
    account = account_store.get_by_email(tenant_id, body.email)
    if account is not None and account["active"]:
        _issue_and_send_account_link(account, purpose="password_reset")
        account_store.record_audit(
            tenant_id,
            action="password_reset_requested",
            account_id=account["id"],
            actor_id=account["id"],
        )
    return {"status": "accepted"}


@router.post("/auth/password-reset/consume", dependencies=[WRITE_LIMIT_DEP])
def consume_password_reset(body: PasswordResetConsumeBody) -> JSONResponse:
    """Consume a reset token once and invalidate every previously issued account session."""
    claims = _verified_token(body.token, purpose="password_reset")
    account = account_store.consume_reset_token(
        claims.tenant_id,
        claims.account_id,
        token_hash=token_digest(body.token),
        password_hash=scrypt_password_hash(body.password),
    )
    if account is None:
        raise HTTPException(status_code=400, detail="Invalid or expired password reset")
    account_store.record_audit(
        claims.tenant_id,
        action="password_reset_completed",
        account_id=claims.account_id,
        actor_id=claims.account_id,
    )
    full_account = account_store.get_by_id(claims.tenant_id, claims.account_id)
    if full_account is None:
        raise HTTPException(status_code=500, detail="Password reset could not be completed")
    return _account_session_response(full_account)


@router.post("/auth/change-password", dependencies=[WRITE_LIMIT_DEP])
def change_own_password(
    body: ChangeOwnPasswordBody,
    ctx: TenantContext = CURRENT_TENANT_DEP,
) -> JSONResponse:
    """Change the signed-in worker's password and replace all previous sessions."""
    account = account_store.get_by_id(ctx.tenant_id, ctx.user_id)
    if account is None or not login_credentials_valid(
        email=account["email"],
        password=body.current_password,
        configured_email=account["email"],
        configured_hash=account["password_hash"],
    ):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    updated = account_store.set_password(
        ctx.tenant_id,
        ctx.user_id,
        password_hash=scrypt_password_hash(body.password),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    account_store.record_audit(
        ctx.tenant_id,
        action="password_changed",
        account_id=ctx.user_id,
        actor_id=ctx.user_id,
    )
    full_account = account_store.get_by_id(ctx.tenant_id, ctx.user_id)
    if full_account is None:
        raise HTTPException(status_code=500, detail="Password change could not be completed")
    return _account_session_response(full_account)


@router.get("/accounts")
def list_work_accounts(ctx: TenantContext = OWNER_AUTH_DEP) -> dict[str, object]:
    """List non-secret staff account records for the signed-in owner."""
    return {"accounts": account_store.list(ctx.tenant_id)}


@router.get("/accounts/audit")
def list_account_audit(ctx: TenantContext = OWNER_AUTH_DEP) -> dict[str, object]:
    """Return identity-only account lifecycle events without credentials or personal data."""
    return {"events": account_store.list_audit(ctx.tenant_id)}


@router.post("/accounts", dependencies=[WRITE_LIMIT_DEP])
def create_work_account(
    body: CreateWorkAccountBody,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Create an assisted-recovery account whose first session must change its password."""
    _reject_owner_role(body.role)
    try:
        account = account_store.create(
            {
                **_account_payload(
                    tenant_id=ctx.tenant_id,
                    email=body.email,
                    given_name=body.given_name,
                    surname=body.surname,
                    position=body.position,
                    role=body.role,
                    password=body.password,
                ),
                "must_change_password": True,
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    account_store.record_audit(
        ctx.tenant_id,
        action="account_created",
        account_id=account["id"],
        actor_id=ctx.user_id,
    )
    return {"account": account}


@router.post("/accounts/invitations", dependencies=[WRITE_LIMIT_DEP])
def invite_work_account(
    body: InviteWorkAccountBody,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Create and deliver the normal single-use staff invitation."""
    _reject_owner_role(body.role)
    _require_email_delivery()
    try:
        account = account_store.create(
            {
                "tenant_id": ctx.tenant_id,
                "email": body.email,
                "given_name": body.given_name,
                "surname": body.surname,
                "position": body.position,
                "role": body.role.value,
                "active": False,
                "status": "invited",
            }
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    full_account = account_store.get_by_id(ctx.tenant_id, account["id"])
    if full_account is None:
        raise HTTPException(status_code=500, detail="Invitation could not be created")
    _issue_and_send_account_link(full_account, purpose="activation")
    account_store.record_audit(
        ctx.tenant_id,
        action="account_invited",
        account_id=account["id"],
        actor_id=ctx.user_id,
    )
    return {"account": account}


@router.post("/accounts/{account_id}/invitation", dependencies=[WRITE_LIMIT_DEP])
def resend_work_account_invitation(
    account_id: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, str]:
    """Replace an invited worker's token and retry delivery without duplicating the account."""
    _require_email_delivery()
    account = account_store.get_by_id(ctx.tenant_id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    if account.get("status") != "invited":
        raise HTTPException(status_code=409, detail="Only pending invitations can be resent")
    _issue_and_send_account_link(account, purpose="activation")
    account_store.record_audit(
        ctx.tenant_id,
        action="invitation_resent",
        account_id=account_id,
        actor_id=ctx.user_id,
    )
    return {"status": "sent"}


@router.post("/accounts/{account_id}/password-reset", dependencies=[WRITE_LIMIT_DEP])
def owner_password_reset(
    account_id: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, str]:
    """Let an owner deliver recovery without learning or setting the worker's password."""
    _require_email_delivery()
    account = account_store.get_by_id(ctx.tenant_id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    _issue_and_send_account_link(account, purpose="password_reset")
    account_store.record_audit(
        ctx.tenant_id,
        action="password_reset_requested_by_owner",
        account_id=account_id,
        actor_id=ctx.user_id,
    )
    return {"status": "sent"}


@router.post("/accounts/{account_id}/deactivate", dependencies=[WRITE_LIMIT_DEP])
def deactivate_work_account(
    account_id: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Deactivate a worker and atomically invalidate their versioned sessions."""
    if account_id == ctx.user_id:
        raise HTTPException(status_code=409, detail="Owners cannot deactivate their own account")
    current = account_store.get_by_id(ctx.tenant_id, account_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    if current["role"] == Role.OWNER.value:
        active_owners = [
            account
            for account in account_store.list(ctx.tenant_id)
            if account["role"] == Role.OWNER.value and account["active"]
        ]
        if len(active_owners) <= 1:
            raise HTTPException(
                status_code=409,
                detail="The last active owner cannot be deactivated",
            )
    account = account_store.set_active(ctx.tenant_id, account_id, active=False)
    account_store.record_audit(
        ctx.tenant_id,
        action="account_deactivated",
        account_id=account_id,
        actor_id=ctx.user_id,
    )
    return {"account": account}


@router.post("/accounts/{account_id}/reactivate", dependencies=[WRITE_LIMIT_DEP])
def reactivate_work_account(
    account_id: str,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Restore a worker while leaving all older sessions invalid."""
    current = account_store.get_by_id(ctx.tenant_id, account_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    if current.get("status") == "invited":
        raise HTTPException(status_code=409, detail="Resend the pending invitation instead")
    account = account_store.set_active(ctx.tenant_id, account_id, active=True)
    account_store.record_audit(
        ctx.tenant_id,
        action="account_reactivated",
        account_id=account_id,
        actor_id=ctx.user_id,
    )
    return {"account": account}


@router.post("/accounts/{account_id}/role", dependencies=[WRITE_LIMIT_DEP])
def change_work_account_role(
    account_id: str,
    body: ChangeWorkAccountRoleBody,
    ctx: TenantContext = OWNER_AUTH_DEP,
) -> dict[str, object]:
    """Change a staff role and invalidate tokens carrying the former authorization."""
    _reject_owner_role(body.role)
    current = account_store.get_by_id(ctx.tenant_id, account_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    if current["role"] == Role.OWNER.value:
        raise HTTPException(status_code=409, detail="The bootstrap owner role cannot be changed")
    account = account_store.set_role(ctx.tenant_id, account_id, role=body.role.value)
    account_store.record_audit(
        ctx.tenant_id,
        action="account_role_changed",
        account_id=account_id,
        actor_id=ctx.user_id,
    )
    return {"account": account}


def _account_payload(
    *,
    tenant_id: str,
    email: str,
    given_name: str,
    surname: str,
    position: str,
    role: Role,
    password: str,
) -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "email": email,
        "given_name": given_name,
        "surname": surname,
        "position": position,
        "role": role.value,
        "password_hash": scrypt_password_hash(password),
        "active": True,
        "status": "active",
    }


def _account_session_response(account: dict[str, object]) -> JSONResponse:
    secret = os.getenv("TENANT_AUTH_SECRET", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Tenant authentication is unavailable")
    ctx = TenantContext(
        tenant_id=str(account["tenant_id"]),
        user_id=str(account["id"]),
        role=Role(str(account["role"])),
        session_version=int(account["session_version"]),
    )
    lifetime = _env_positive_int("SHELFWISE_LOGIN_SESSION_SECONDS", 43_200)
    token = encode_hs256_token(
        {**ctx.token_claims(), "exp": int(datetime.now(UTC).timestamp()) + lifetime},
        secret=secret,
    )
    session = {
        **ctx.to_dict(),
        "must_change_password": bool(account.get("must_change_password", False)),
    }
    response = JSONResponse({"session": session, "mode": "jwt"})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=lifetime,
        httponly=True,
        secure=_cookie_secure_setting(),
        samesite="strict",
        path="/",
    )
    return response


def _migrate_or_recover_legacy_owner(
    body: LoginBody,
    *,
    tenant_id: str,
) -> dict[str, object] | None:
    secret = os.getenv("TENANT_AUTH_SECRET", "")
    configured_email = os.getenv("SHELFWISE_LOGIN_EMAIL", "").strip().lower()
    configured_hash = os.getenv("SHELFWISE_LOGIN_PASSWORD_HASH", "").strip()
    if not secret or not configured_email or not configured_hash:
        return None
    if not login_credentials_valid(
        email=body.email,
        password=body.password,
        configured_email=configured_email,
        configured_hash=configured_hash,
    ):
        return None
    existing_accounts = account_store.list(tenant_id)
    if existing_accounts:
        recovery_enabled = (
            os.getenv("SHELFWISE_LEGACY_OWNER_RECOVERY_ENABLED", "").strip().lower() == "true"
        )
        if not recovery_enabled:
            return None
    try:
        payload = _account_payload(
            tenant_id=tenant_id,
            email=configured_email,
            given_name=os.getenv("SHELFWISE_LOGIN_GIVEN_NAME", "Legacy"),
            surname=os.getenv("SHELFWISE_LOGIN_SURNAME", "Owner"),
            position=os.getenv("SHELFWISE_LOGIN_POSITION", "Business Owner"),
            role=Role.OWNER,
            password=body.password,
        )
        account = (
            account_store.create(payload)
            if existing_accounts
            else account_store.create_first_owner(payload)
        )
    except ValueError:
        account = account_store.get_by_email(tenant_id, configured_email)
        if account is None:
            return None
    account_store.record_audit(
        tenant_id,
        action="legacy_owner_migrated",
        account_id=str(account["id"]),
        actor_id="system:legacy_migration",
    )
    return account_store.get_by_id(tenant_id, str(account["id"]))


def _issue_and_send_account_link(
    account: dict[str, object],
    *,
    purpose: str,
) -> None:
    token_purpose = "activation" if purpose == "activation" else "password_reset"
    lifetime_name = (
        "SHELFWISE_INVITATION_SECONDS"
        if token_purpose == "activation"
        else "SHELFWISE_PASSWORD_RESET_SECONDS"
    )
    issued = issue_account_token(
        purpose=token_purpose,
        tenant_id=str(account["tenant_id"]),
        account_id=str(account["id"]),
        secret=os.getenv("TENANT_AUTH_SECRET", ""),
        lifetime_seconds=_env_positive_int(
            lifetime_name,
            86_400 if purpose == "activation" else 3600,
        ),
    )
    if purpose == "activation":
        stored = account_store.set_invitation(
            str(account["tenant_id"]),
            str(account["id"]),
            token_hash=issued.token_hash,
            expires_at=issued.expires_at,
        )
        email_purpose = "activate"
    else:
        stored = account_store.set_reset_token(
            str(account["tenant_id"]),
            str(account["id"]),
            token_hash=issued.token_hash,
            expires_at=issued.expires_at,
        )
        email_purpose = "reset-password"
    if stored is None:
        raise HTTPException(status_code=404, detail="Work account not found")
    try:
        account_notifications.send_account_link(
            recipient=str(account["email"]),
            given_name=str(account["given_name"]),
            purpose=email_purpose,
            token=issued.token,
        )
    except NotificationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _require_email_delivery() -> None:
    try:
        account_notifications.ensure_account_email_configured()
    except NotificationUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _verified_token(token: str, *, purpose: str) -> AccountTokenClaims:
    try:
        return verify_account_token(
            token,
            purpose="activation" if purpose == "activation" else "password_reset",
            secret=os.getenv("TENANT_AUTH_SECRET", ""),
        )
    except ValueError as exc:
        detail = (
            "Invalid or expired invitation"
            if purpose == "activation"
            else "Invalid or expired password reset"
        )
        raise HTTPException(status_code=400, detail=detail) from exc


def _identity_matches(account: dict[str, object], body: ActivateInvitationBody) -> bool:
    return (
        hmac.compare_digest(str(account["email"]), body.email.strip().lower())
        and hmac.compare_digest(str(account["given_name"]), body.given_name.strip())
        and hmac.compare_digest(str(account["surname"]), body.surname.strip())
        and hmac.compare_digest(str(account["position"]), body.position.strip())
    )


def _reject_owner_role(role: Role) -> None:
    if role is Role.OWNER:
        raise HTTPException(
            status_code=422,
            detail="Additional owners require an operator-reviewed recovery process",
        )
