import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api_schemas import (
    LoginRequest,
    MemberCreate,
    MemberResponse,
    MembershipSummary,
    MemberUpdate,
    OrganizationResponse,
    OrganizationUpdate,
    RegisterRequest,
    SessionResponse,
    TokenResponse,
    UserResponse,
)
from app.auth import Context, admin, get_context
from app.db import get_db
from app.models import Membership, Organization, User
from app.records import record_audit
from app.security import create_token, hash_password, verify_password
from app.settings import settings

router = APIRouter(prefix="/api", tags=["auth"])

MAX_FAILED_LOGINS = 10
FAILED_LOGIN_WINDOW_SECONDS = 15 * 60
_failed_logins: dict[str, deque[float]] = defaultdict(deque)
_DUMMY_HASH = hash_password("timing-equaliser-password")


def _session(db: Session, user: User, organization: Organization, role: str) -> SessionResponse:
    rows = db.execute(
        select(Membership, Organization)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(Membership.user_id == user.id)
        .order_by(Membership.created_at)
    ).all()
    return SessionResponse(
        user=UserResponse(id=user.id, email=user.email, name=user.name),
        organization=OrganizationResponse.model_validate(organization),
        role=role,  # type: ignore[arg-type]
        memberships=[
            MembershipSummary(organization_id=org.id, organization_name=org.name, role=m.role)  # type: ignore[arg-type]
            for m, org in rows
        ],
    )


def _token_response(db: Session, user: User, organization: Organization, role: str) -> TokenResponse:
    token = create_token("access", user.id, settings.access_token_ttl_seconds)
    return TokenResponse(access_token=token, **_session(db, user, organization, role).model_dump())


@router.post("/auth/register", response_model=TokenResponse, status_code=201)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="An account with this email already exists; sign in instead")
    user = User(email=payload.email, name=payload.name.strip(), password_hash=hash_password(payload.password))
    # Invoices uploaded before accounts existed were moved into an ownerless workspace by
    # migration 0002; the first person to register becomes its admin.
    legacy = None
    if not db.scalar(select(func.count()).select_from(User)):
        legacy = db.scalars(
            select(Organization).where(~Organization.id.in_(select(Membership.organization_id)))
        ).first()
    organization = legacy or Organization(name=payload.organization_name.strip())
    if legacy:
        organization.name = payload.organization_name.strip()
    db.add_all([user, organization])
    db.flush()
    db.add(Membership(user_id=user.id, organization_id=organization.id, role="admin"))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists") from exc
    return _token_response(db, user, organization, "admin")


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    attempts = _failed_logins[payload.email]
    now = time.monotonic()
    while attempts and now - attempts[0] > FAILED_LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= MAX_FAILED_LOGINS:
        raise HTTPException(status_code=429, detail="Too many failed sign-in attempts; try again in 15 minutes")
    user = db.scalar(select(User).where(User.email == payload.email))
    valid = verify_password(payload.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not valid:
        attempts.append(now)
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    attempts.clear()
    membership = db.scalars(select(Membership).where(Membership.user_id == user.id).order_by(Membership.created_at)).first()
    if not membership:
        raise HTTPException(status_code=403, detail="Your account is not a member of any organization")
    organization = db.get(Organization, membership.organization_id)
    assert organization is not None
    return _token_response(db, user, organization, membership.role)


@router.get("/auth/me", response_model=SessionResponse)
def me(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> SessionResponse:
    return _session(db, context.user, context.organization, context.role)


@router.get("/organization", response_model=OrganizationResponse)
def get_organization(context: Context = Depends(get_context)) -> OrganizationResponse:
    return OrganizationResponse.model_validate(context.organization)


@router.patch("/organization", response_model=OrganizationResponse)
def update_organization(
    payload: OrganizationUpdate, context: Context = Depends(admin), db: Session = Depends(get_db)
) -> OrganizationResponse:
    organization = db.get(Organization, context.organization.id)
    assert organization is not None
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is not None:
            if key == "own_identifiers":
                value = [item.strip() for item in value if item.strip()]
            setattr(organization, key, value.strip() if isinstance(value, str) else value)
    record_audit(db, organization.id, context.actor, "organization", organization.id, "settings_updated", fields=sorted(changes))
    db.commit()
    return OrganizationResponse.model_validate(organization)


@router.get("/organization/members", response_model=list[MemberResponse])
def list_members(context: Context = Depends(get_context), db: Session = Depends(get_db)) -> list[MemberResponse]:
    rows = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.organization_id == context.organization.id)
        .order_by(User.email)
    ).all()
    return [MemberResponse(user_id=u.id, email=u.email, name=u.name, role=m.role) for m, u in rows]  # type: ignore[arg-type]


@router.post("/organization/members", response_model=MemberResponse, status_code=201)
def add_member(payload: MemberCreate, context: Context = Depends(admin), db: Session = Depends(get_db)) -> MemberResponse:
    user = db.scalar(select(User).where(User.email == payload.email))
    if not user:
        if not payload.password:
            raise HTTPException(status_code=422, detail="Set an initial password (at least 10 characters) for a new user")
        user = User(email=payload.email, name=payload.name.strip(), password_hash=hash_password(payload.password))
        db.add(user)
        db.flush()
    elif db.scalar(
        select(Membership).where(Membership.user_id == user.id, Membership.organization_id == context.organization.id)
    ):
        raise HTTPException(status_code=409, detail="This person is already a member")
    db.add(Membership(user_id=user.id, organization_id=context.organization.id, role=payload.role))
    record_audit(db, context.organization.id, context.actor, "membership", user.id, "member_added", role=payload.role)
    db.commit()
    return MemberResponse(user_id=user.id, email=user.email, name=user.name, role=payload.role)


def _membership(db: Session, organization_id: str, user_id: str) -> Membership:
    membership = db.scalar(
        select(Membership).where(Membership.user_id == user_id, Membership.organization_id == organization_id)
    )
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found")
    return membership


def _ensure_other_admin(db: Session, organization_id: str, user_id: str) -> None:
    admins = db.scalar(
        select(func.count())
        .select_from(Membership)
        .where(Membership.organization_id == organization_id, Membership.role == "admin", Membership.user_id != user_id)
    )
    if not admins:
        raise HTTPException(status_code=409, detail="An organization must keep at least one admin")


@router.patch("/organization/members/{user_id}", response_model=MemberResponse)
def update_member(
    user_id: str, payload: MemberUpdate, context: Context = Depends(admin), db: Session = Depends(get_db)
) -> MemberResponse:
    membership = _membership(db, context.organization.id, user_id)
    if membership.role == "admin" and payload.role != "admin":
        _ensure_other_admin(db, context.organization.id, user_id)
    membership.role = payload.role
    record_audit(db, context.organization.id, context.actor, "membership", user_id, "role_changed", role=payload.role)
    db.commit()
    user = db.get(User, user_id)
    assert user is not None
    return MemberResponse(user_id=user.id, email=user.email, name=user.name, role=payload.role)


@router.delete("/organization/members/{user_id}", status_code=204)
def remove_member(user_id: str, context: Context = Depends(admin), db: Session = Depends(get_db)) -> Response:
    membership = _membership(db, context.organization.id, user_id)
    if membership.role == "admin":
        _ensure_other_admin(db, context.organization.id, user_id)
    db.delete(membership)
    record_audit(db, context.organization.id, context.actor, "membership", user_id, "member_removed")
    db.commit()
    return Response(status_code=204)
