"""Typed API surface. These pydantic models are the contract shared by the REST
API, the SSE stream and the MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RoomStatus = Literal["active", "expired", "closed"]
AgentStatus = Literal["active", "left"]
TaskStatus = Literal["open", "claimed", "done", "cancelled"]
Provider = Literal["openai", "claude-code", "human", "other"]
MessageType = Literal[
    "chat",
    "system",
    "human",
    "memory_update",
    "task_update",
    "ask_human",
    "join",
    "leave",
]

# The action space an autonomous agent chooses from each time it is woken.
AgentAction = Literal[
    "IGNORE",
    "RESPOND",
    "ASK_AGENT",
    "UPDATE_MEMORY",
    "CREATE_TASK",
    "ASK_HUMAN",
    "LEAVE",
]


# --------------------------------------------------------------------------
# Core entities
# --------------------------------------------------------------------------


class Room(BaseModel):
    id: str
    join_code: str
    title: str
    objective: str
    status: RoomStatus
    created_at: str
    expires_at: str
    seconds_remaining: int
    agent_turns_used: int
    max_agent_turns: int
    autonomy_enabled: bool


class Agent(BaseModel):
    id: str
    room_id: str
    owner_name: str
    agent_name: str
    provider: Provider
    public_objective: str
    status: AgentStatus
    autonomous: bool
    joined_at: str
    last_seen_at: str


class Message(BaseModel):
    id: int
    room_id: str
    agent_id: str | None
    sender_label: str
    recipient_agent_id: str | None
    content: str
    message_type: MessageType
    created_at: str


class Task(BaseModel):
    id: str
    room_id: str
    title: str
    description: str
    assigned_agent_id: str | None
    status: TaskStatus
    result: str | None
    created_at: str
    updated_at: str


class SharedMemoryData(BaseModel):
    """Compact structured working memory. Explicitly *not* the raw transcript."""

    objective: str = ""
    decisions: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)


class SharedMemory(BaseModel):
    room_id: str
    data: SharedMemoryData
    updated_at: str
    updated_by: str | None


class RoomSnapshot(BaseModel):
    """Everything the UI (or an agent) needs to render room state in one call."""

    room: Room
    agents: list[Agent]
    messages: list[Message]
    memory: SharedMemory
    tasks: list[Task]


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


class CreateRoomRequest(BaseModel):
    title: str = Field(min_length=1, max_length=140)
    objective: str = Field(min_length=1, max_length=4000)
    ttl_seconds: int | None = Field(default=None, ge=60, le=24 * 60 * 60)


class JoinRoomRequest(BaseModel):
    join_code: str = Field(min_length=1, max_length=32)
    owner_name: str = Field(min_length=1, max_length=80)
    agent_name: str = Field(min_length=1, max_length=80)
    provider: Provider = "other"
    public_objective: str = Field(default="", max_length=2000)
    autonomous: bool = False


class JoinRoomResponse(BaseModel):
    agent: Agent
    agent_token: str
    room: Room


class PostMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    recipient_agent_id: str | None = None
    message_type: MessageType = "chat"


class MemoryPatch(BaseModel):
    """Additive-by-default patch. `replace_*` fields overwrite a whole list.

    Additive updates keep concurrent agents from clobbering each other, which
    matters because both agents write memory without coordination.
    """

    objective: str | None = None
    add_decisions: list[str] = Field(default_factory=list)
    add_facts: list[str] = Field(default_factory=list)
    add_assumptions: list[str] = Field(default_factory=list)
    add_open_questions: list[str] = Field(default_factory=list)
    add_disagreements: list[str] = Field(default_factory=list)
    resolve_open_questions: list[str] = Field(default_factory=list)
    replace_decisions: list[str] | None = None
    replace_facts: list[str] | None = None
    replace_assumptions: list[str] | None = None
    replace_open_questions: list[str] | None = None
    replace_disagreements: list[str] | None = None


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    assign_to_self: bool = False


class CompleteTaskRequest(BaseModel):
    result: str = Field(default="", max_length=4000)


class HumanMessageRequest(BaseModel):
    """A message typed by a human in the browser."""

    content: str = Field(min_length=1, max_length=8000)
    sender_label: str = Field(default="Human", max_length=80)
    recipient_agent_id: str | None = None


class SpawnGptAgentRequest(BaseModel):
    owner_name: str = Field(default="Human A", max_length=80)
    agent_name: str = Field(default="GPT", max_length=80)
    public_objective: str = Field(default="", max_length=2000)
    private_instructions: str = Field(default="", max_length=8000)


class AutonomyRequest(BaseModel):
    enabled: bool
