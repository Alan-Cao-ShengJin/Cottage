"""The disclosure boundary: authorization, policy, and inspection.

Domain shape (no field for a prompt or a key) removes accidental leakage paths, but
it cannot be the control — a message body, a task description, a state value, or an
artifact summary is free text and can carry anything. So every content-bearing
command passes through `check_disclosure`, which runs three independent gates and
returns a `DisclosureDecision` that is stamped onto the resulting event:

1. **Authorization** — may this participant assert this class, to this audience, in
   this room? (Untrusted identities are confined to `room_public`; only owning-org
   members may assert `org_internal`.)
2. **Policy** — does the room's visibility permit the class at all? An
   `org_internal` payload in a `cross_org` room is *rejected*, never downgraded: a
   downgrade would perform the disclosure it was supposed to prevent.
3. **Inspection** — does the content trip a hard rule? Secret-shaped strings and
   over-cap payloads are refused.

Rejection is always a hard error, never a silent scrub. A scrub teaches the caller
that the channel accepted that content, which is exactly the wrong lesson
(`docs/SECURITY.md` §2).

`visible_to` is the matching read-side gate, applied at both projection and fanout
so two participants in one room legitimately receive different event sets.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence

from ..domain.disclosure import Audience, Disclosure, DisclosureDecision, Provenance
from ..domain.events import EventEnvelope
from ..domain.identity import TrustTier
from ..domain.room import (
    Participant,
    PrivacyClass,
    Room,
    RoomVisibility,
    Scope,
)
from ..util import utcnow_iso
from . import authz
from .errors import InvalidCommand, PrivacyViolation

# ---------------------------------------------------------------------------
# Inspection rules
# ---------------------------------------------------------------------------

#: High-confidence secret shapes. Every pattern here must be something that has
#: essentially no legitimate reason to appear in coordination content — a false
#: positive blocks real work, so "looks vaguely sensitive" is not enough.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\b")),
    ("bearer_header", re.compile(r"(?i)\bauthorization\s*[:=]\s*bearer\s+\S{16,}")),
    # No leading \b: the interesting cases are `db_password=`, `MY_SECRET:`, and
    # `openai_api_key =`, where the preceding character is a word character and a
    # boundary assertion would never fire.
    (
        "password_assignment",
        re.compile(r"(?i)(?:pass(?:word|wd)|secret|api[_-]?key)\s*[:=]\s*[\"']?\S{8,}"),
    ),
    ("connection_string", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s:@/]+@")),
)

#: Text that signals the payload is an agent's own private context rather than a
#: deliberate disclosure. Matching this is a strong hint the client is misusing the
#: channel, so it is refused with an explanatory message.
_PRIVATE_CONTEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("system_prompt", re.compile(r"(?i)^\s*(?:system|developer)\s*(?:prompt|message)\s*[:=]")),
    ("chat_transcript", re.compile(r"(?i)<\|(?:im_start|im_end|system|assistant)\|>")),
    ("reasoning_block", re.compile(r"(?i)<(?:thinking|scratchpad|antthinking)>")),
)

MAX_TEXT_FIELD_CHARS = 8_000
MAX_TOTAL_PAYLOAD_BYTES = 128_000
#: Entropy screen for long unbroken tokens, which is what a raw credential looks
#: like when it does not match a known vendor prefix.
ENTROPY_MIN_LENGTH = 40
ENTROPY_THRESHOLD_BITS_PER_CHAR = 4.0


#: This system's own identifiers: a known prefix and 22 Crockford base32 characters.
#:
#: They are **not secrets**. Every event payload carries them, every projection prints
#: them, and coordinating at all means naming the task or participant you mean. But an
#: id inside a URL path is a long unbroken high-entropy run, so the credential screen
#: refused a message for quoting `/api/rooms/room_.../directives` — a participant
#: blocked from saying where a thing lives, by a rule meant to stop credential leaks.
#:
#: Neutralising them before the entropy scan keeps the screen honest rather than
#: weakening it: a real credential next to an id still trips, because only the id
#: itself is masked. This is not the security control regardless — the boundary is
#: authorization and policy, and inspection is a backstop against an obvious mistake
#: (`docs/SECURITY.md` §2).
_OWN_ID = re.compile(
    r"\b(?:org|usr|aid|room|inv|par|con|att|evt|msg|wrk|tsk|prp|clm|art|cft|dir|cmd)"
    r"_[0-9A-HJKMNP-TV-Z]{22}\b"
)


def _shannon_bits_per_char(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _high_entropy_token(text: str) -> str | None:
    """Find a long, unbroken, high-entropy run. Returns the rule name if found.

    Deliberately conservative: the run must contain no whitespace, be long, mix
    character classes, and clear an entropy bar. Prose, file paths, and code
    identifiers do not, so this catches an opaque credential without blocking a
    participant from pasting a stack trace.
    """
    text = _OWN_ID.sub("<id>", text)
    for token in re.findall(rf"[A-Za-z0-9+/=_\-]{{{ENTROPY_MIN_LENGTH},}}", text):
        classes = sum(bool(re.search(pattern, token)) for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]"))
        if classes >= 3 and _shannon_bits_per_char(token) >= ENTROPY_THRESHOLD_BITS_PER_CHAR:
            return "high_entropy_token"
    return None


def _walk_text(value: object, *, depth: int = 0) -> Iterable[str]:
    """Yield every string inside a nested payload, so inspection cannot be evaded
    by burying a credential in a JSON value."""
    if depth > 12:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for k, v in value.items():
            yield str(k)
            yield from _walk_text(v, depth=depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            yield from _walk_text(item, depth=depth + 1)


def inspect_content(*values: object, max_text_chars: int = MAX_TEXT_FIELD_CHARS) -> None:
    """Raise `PrivacyViolation` if any value trips a hard rule.

    The matched substring is never included in the error or the logs — echoing a
    detected credential back through an error channel would defeat the check.
    """
    total = 0
    for value in values:
        for text in _walk_text(value):
            total += len(text)
            if len(text) > max_text_chars:
                raise PrivacyViolation(
                    "A text field exceeds the maximum size for room content.",
                    rule="max_text_chars",
                    limit=max_text_chars,
                )
            for rule, pattern in _SECRET_PATTERNS:
                if pattern.search(text):
                    raise PrivacyViolation(
                        "This content looks like it contains a credential. "
                        "Credentials must never enter a room; share a reference instead.",
                        rule=rule,
                    )
            for rule, pattern in _PRIVATE_CONTEXT_PATTERNS:
                if pattern.search(text):
                    raise PrivacyViolation(
                        "This content looks like your own private context (prompt, "
                        "reasoning, or transcript). Share only the conclusion you "
                        "intend to disclose.",
                        rule=rule,
                    )
            entropy_rule = _high_entropy_token(text)
            if entropy_rule:
                raise PrivacyViolation(
                    "This content contains an opaque high-entropy token, which is "
                    "how a leaked credential looks. Remove it or share a reference.",
                    rule=entropy_rule,
                )
    if total > MAX_TOTAL_PAYLOAD_BYTES:
        raise PrivacyViolation(
            "Payload exceeds the maximum total size for room content.",
            rule="max_total_bytes",
            limit=MAX_TOTAL_PAYLOAD_BYTES,
        )


# ---------------------------------------------------------------------------
# The write-side gate
# ---------------------------------------------------------------------------


def check_disclosure(
    *,
    room: Room,
    participant: Participant,
    disclosure: Disclosure,
    content: Sequence[object] = (),
    known_participant_ids: Sequence[str] | None = None,
    max_text_chars: int | None = None,
) -> DisclosureDecision:
    """Authorize, policy-check, and inspect a contribution. Raises on refusal."""
    checks: list[str] = []
    cls = disclosure.privacy_class

    # --- 1. authorization -------------------------------------------------
    if participant.trust == TrustTier.UNTRUSTED and cls != PrivacyClass.ROOM_PUBLIC:
        raise PrivacyViolation(
            "An untrusted participant may only contribute `room_public` content.",
            privacy_class=cls.value,
            trust=participant.trust.value,
        )
    if cls == PrivacyClass.ORG_INTERNAL and participant.org_id != room.org_id:
        raise PrivacyViolation(
            "Only members of the room's owning organization may assert `org_internal` content.",
            privacy_class=cls.value,
        )
    checks.append("authorization")

    # --- 2. policy --------------------------------------------------------
    if room.visibility == RoomVisibility.CROSS_ORG and cls == PrivacyClass.ORG_INTERNAL:
        # Rejected, not downgraded: a downgrade would disclose to foreign orgs the
        # very content the class exists to withhold.
        raise PrivacyViolation(
            "`org_internal` content cannot be contributed to a cross-organization "
            "room. Reclassify it as `room_public` only if it is genuinely shareable.",
            privacy_class=cls.value,
            room_visibility=room.visibility.value,
        )
    if disclosure.audience == Audience.ORG and room.visibility == RoomVisibility.CROSS_ORG:
        raise PrivacyViolation(
            "An `org` audience is meaningless in a cross-organization room.",
            audience=disclosure.audience.value,
        )
    checks.append("policy")

    # --- 3. audience resolution -------------------------------------------
    restricted: list[str] | None = None
    if disclosure.audience == Audience.PARTICIPANT:
        target = disclosure.to_participant_id
        if not target:
            raise InvalidCommand(
                "`to_participant_id` is required when the audience is `participant`."
            )
        if known_participant_ids is not None and target not in known_participant_ids:
            raise InvalidCommand(
                "The addressed participant is not a member of this room.",
                to_participant_id=target,
            )
        # The author is included so its own client renders what it sent.
        restricted = sorted({target, participant.id})
    checks.append("audience")

    # --- 4. inspection ----------------------------------------------------
    inspect_content(*content, max_text_chars=max_text_chars or MAX_TEXT_FIELD_CHARS)
    checks.append("inspection")

    return DisclosureDecision(
        privacy_class=cls,
        audience=disclosure.audience,
        to_participant_id=disclosure.to_participant_id,
        restricted_to_participant_ids=restricted,
        checks_passed=checks,
    )


def build_provenance(
    participant: Participant,
    *,
    source: str | None = None,
    confidence: float | None = None,
    derived_from: Sequence[str] = (),
) -> Provenance:
    """Stamp attribution server-side.

    `asserted_by_participant_id` and `asserted_at` are not client-supplied, so an
    assertion cannot be attributed to someone else. `unverified` follows trust, so
    the UI can label an untrusted claim without the reader having to know the
    trust model.
    """
    return Provenance(
        asserted_by_participant_id=participant.id,
        asserted_at=utcnow_iso(),
        source=source,
        confidence=confidence,
        derived_from=list(derived_from),
        unverified=participant.trust == TrustTier.UNTRUSTED,
    )


# ---------------------------------------------------------------------------
# The read-side gate
# ---------------------------------------------------------------------------


def visible_to(event: EventEnvelope, *, recipient: Participant, room: Room) -> bool:
    """Whether `recipient` may receive this event.

    Applied at projection *and* at fanout. A recipient filtered out of an event
    still sees the `seq` advance elsewhere in the stream; gaps in one recipient's
    view are expected and must not be read as loss (`docs/SECURITY.md` §6).
    """
    # A room admin of the owning org may audit directed content, per §6.
    is_admin_of_owner = recipient.has(Scope.ROOM_ADMIN) and recipient.org_id == room.org_id

    if (
        event.restricted_to_participant_ids is not None
        and recipient.id not in event.restricted_to_participant_ids
        and not is_admin_of_owner
    ):
        return False

    if event.privacy_class == PrivacyClass.ORG_INTERNAL:
        # See the note in `projections._visible_to`: same rule, and it must stay one rule.
        return authz.can_see_org_internal(recipient, room)

    if event.privacy_class == PrivacyClass.PARTICIPANT_PRIVATE:
        return event.actor.participant_id == recipient.id or is_admin_of_owner

    return True


def filter_events(
    events: Iterable[EventEnvelope], *, recipient: Participant, room: Room
) -> list[EventEnvelope]:
    return [e for e in events if visible_to(e, recipient=recipient, room=room)]
