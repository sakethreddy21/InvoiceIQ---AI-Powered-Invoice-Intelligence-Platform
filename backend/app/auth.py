"""Request authentication, organization scoping and role checks."""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Membership, Organization, User
from app.security import read_token

ROLES = ("admin", "accountant", "viewer")
# Which roles may perform each class of action.
CAN_EDIT = {"admin", "accountant"}
CAN_APPROVE = {"admin", "accountant"}
CAN_ADMIN = {"admin"}


@dataclass(frozen=True)
class Context:
    user: User
    organization: Organization
    role: str

    @property
    def actor(self) -> str:
        return self.user.email


def get_context(
    authorization: str | None = Header(default=None),
    x_organization_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Context:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue", headers={"WWW-Authenticate": "Bearer"})
    payload = read_token(authorization[7:].strip(), "access")
    user = db.get(User, str(payload["sub"])) if payload else None
    if not user:
        raise HTTPException(status_code=401, detail="Your session has expired; sign in again", headers={"WWW-Authenticate": "Bearer"})
    query = select(Membership).where(Membership.user_id == user.id)
    if x_organization_id:
        query = query.where(Membership.organization_id == x_organization_id)
    membership = db.scalars(query.order_by(Membership.created_at)).first()
    if not membership:
        raise HTTPException(status_code=403, detail="You are not a member of this organization")
    organization = db.get(Organization, membership.organization_id)
    assert organization is not None
    return Context(user=user, organization=organization, role=membership.role)


def require(roles: set[str]):
    def dependency(context: Context = Depends(get_context)) -> Context:
        if context.role not in roles:
            raise HTTPException(status_code=403, detail="Your role does not allow this action")
        return context

    return dependency


editor = require(CAN_EDIT)
approver = require(CAN_APPROVE)
admin = require(CAN_ADMIN)
