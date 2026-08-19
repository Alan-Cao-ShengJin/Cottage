"""Whose words a message carries.

A room holds humans and agents, and an agent is often the *only* way its human speaks
here. So one participant's messages can carry two completely different things: the agent's
own account of the work, and its person's words relayed through it. Those want opposite
treatment — the first is coordination other agents should act on, the second is a
conversation between people that must not bill every agent in the room for reading it.

The room already had a rule for this and it keyed on the **speaker's identity kind**
(D-089, extending a309cfb): an undirected message from a human identity does not wake an
agent's filtered subscription. That is right whenever the human is a participant in their
own name — a person in the browser — and wrong in the case that actually happens, which is
a person typing into their agent's interface. The room then sees an agent talking, wakes
everyone, and "anyone want lunch?" costs a model turn per subscriber.

The reported symptom was the same defect from the agent's side: *the agent cannot tell a
prompt from a chat message.* It cannot, and it should not have to guess silently — so the
message declares whose words it carries, and the wake rule reads the declaration instead
of inferring from a label about the speaker. Behaviour derives from something declared
about the message, never from what kind of thing is holding the keyboard (principle 4).

This is a **claim**, like `declared_model` on an attachment (D-054). Nothing verifies that
a human really said it, and nothing needs to: the only thing it changes is whether other
agents are woken, so the worst a wrong claim does is make a message quieter or louder than
it should be. Provenance that matters — which participant, which runtime — is still
stamped server-side and cannot be forged.
"""

from __future__ import annotations

from enum import Enum


class Speaker(str, Enum):
    """Whose words this message carries, as its author declares them."""

    #: The participant's own words about the work. The default, and the coordination case:
    #: other agents may be woken for it, because it is the channel they act on.
    AGENT = "agent"
    #: A person's words, relayed by the participant on their behalf. Delivered to everyone
    #: exactly as before — nothing is withheld — but it does not *wake* an agent unless it
    #: was addressed to that agent specifically.
    HUMAN = "human"


#: Declarations that mean "a person said this", however that person reached the room.
#: A set rather than an equality check because the identity-kind path and the declaration
#: path answer the same question, and `relevance` treats them identically.
HUMAN_SPEECH: frozenset[Speaker] = frozenset({Speaker.HUMAN})
