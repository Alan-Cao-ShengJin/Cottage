"""Identity: organizations, users, and agent identities.

An `AgentIdentity` has no field for a system prompt, model name, API key, or
private memory. That removes a whole class of accidental disclosure, but it is
*not* the disclosure control — free-text fields elsewhere (messages, task
descriptions, state values, artifact content) can carry anything, so the real
boundary is the authorization + policy + inspection check in `core/privacy.py`
(`docs/SECURITY.md` §2). Shape narrows the surface; it does not close it.

`host_class` lives here as a descriptive label only. Nothing in this codebase may
branch on it to decide behavior — see `domain/capabilities.py`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .capabilities import Capability, HostClass


class OrgRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class PrincipalKind(str, Enum):
    """Whether a human or an agent is behind this identity.

    Affects UI presentation and audit reading. It does *not* decide capabilities:
    a human at a browser and an autonomous agent are the same kind of principal to
    the coordination core, differing only in what they declared they can do.
    """

    HUMAN = "human"
    AGENT = "agent"


class TrustTier(str, Enum):
    """`docs/SECURITY.md` §1. Untrusted identities cannot claim or write state."""

    MEMBER = "member"
    VOUCHED = "vouched"
    UNTRUSTED = "untrusted"


class IdentityProvenance(str, Enum):
    """How this identity came to exist — and therefore what its name is worth.

    Distinct from `TrustTier`, which answers "may it act". This answers "who said it is
    who it says it is", and the two are genuinely independent: an invited guest can be
    trusted to do work while its *name* remains its own claim.

    The distinction became load-bearing when invitations became credentials (D-025). Before
    that every identity was created by, or bound by, someone with an account, so provenance
    was uniform and invisible.
    """

    #: Created for a user of the org, or bound by one at an OAuth consent screen. The
    #: display name is backed by a credential, so other participants may rely on it.
    ACCOUNT = "account"
    #: Provisioned by redeeming an invitation link. Somebody with authority in the room
    #: deliberately issued that link — so presence is authorized — but nobody vouched for
    #: *this* name: the holder chose it. Two consequences, both enforced rather than
    #: documented: the room shows the name as self-asserted, and the identity is not an org
    #: member for `org_internal` purposes however its org row reads (`core/authz.py`).
    INVITATION = "invitation"


class Organization(BaseModel):
    id: str
    name: str
    slug: str
    created_at: str


class User(BaseModel):
    id: str
    org_id: str
    email: str
    display_name: str
    role: OrgRole
    created_at: str


class AgentIdentity(BaseModel):
    """A durable principal owned by a user inside an org.

    `description` and `declared_capabilities` are the only things this identity
    reveals about the agent behind it.
    """

    id: str
    org_id: str
    owner_user_id: str
    display_name: str
    kind: PrincipalKind
    #: Descriptive only. Supplies default capabilities when a client declares none.
    host_class: HostClass = HostClass.UNKNOWN
    #: Public, human-written summary of what this agent is for. Never a prompt.
    description: str = ""
    #: What this identity typically declares. A connection may declare differently,
    #: and the connection's declaration wins for that connection.
    declared_capabilities: list[Capability] = Field(default_factory=list)
    trust: TrustTier = TrustTier.MEMBER
    #: How this identity was created, and therefore whether its name is vouched for.
    provenance: IdentityProvenance = IdentityProvenance.ACCOUNT
    created_at: str


class IdentitySummary(BaseModel):
    """What other participants in a room may see about an identity.

    In a `cross_org` room this is the whole disclosure: display name, org name,
    host label, capabilities. No email, no user id, no sibling identities
    (`docs/SECURITY.md` §4).
    """

    identity_id: str
    display_name: str
    org_id: str
    org_name: str
    kind: PrincipalKind
    host_class: HostClass = HostClass.UNKNOWN
    description: str = ""
    trust: TrustTier = TrustTier.MEMBER
    #: Shown to other participants, because a self-asserted name that looks identical to a
    #: credential-bound one is the disclosure this field exists to prevent.
    provenance: IdentityProvenance = IdentityProvenance.ACCOUNT
