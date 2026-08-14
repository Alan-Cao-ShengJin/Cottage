// Mirrors backend/app/models.py. Kept hand-written and small on purpose.

export type RoomStatus = "active" | "expired" | "closed";
export type AgentStatus = "active" | "left";
export type Provider = "openai" | "claude-code" | "human" | "other";
export type TaskStatus = "open" | "claimed" | "done" | "cancelled";

export type MessageType =
  | "chat"
  | "system"
  | "human"
  | "memory_update"
  | "task_update"
  | "ask_human"
  | "join"
  | "leave";

export interface Room {
  id: string;
  join_code: string;
  title: string;
  objective: string;
  status: RoomStatus;
  created_at: string;
  expires_at: string;
  seconds_remaining: number;
  agent_turns_used: number;
  max_agent_turns: number;
  autonomy_enabled: boolean;
}

export interface Agent {
  id: string;
  room_id: string;
  owner_name: string;
  agent_name: string;
  provider: Provider;
  public_objective: string;
  status: AgentStatus;
  autonomous: boolean;
  joined_at: string;
  last_seen_at: string;
}

export interface Message {
  id: number;
  room_id: string;
  agent_id: string | null;
  sender_label: string;
  recipient_agent_id: string | null;
  content: string;
  message_type: MessageType;
  created_at: string;
}

export interface Task {
  id: string;
  room_id: string;
  title: string;
  description: string;
  assigned_agent_id: string | null;
  status: TaskStatus;
  result: string | null;
  created_at: string;
  updated_at: string;
}

export interface SharedMemoryData {
  objective: string;
  decisions: string[];
  facts: string[];
  assumptions: string[];
  open_questions: string[];
  disagreements: string[];
}

export interface SharedMemory {
  room_id: string;
  data: SharedMemoryData;
  updated_at: string;
  updated_by: string | null;
}

export interface RoomSnapshot {
  room: Room;
  agents: Agent[];
  messages: Message[];
  memory: SharedMemory;
  tasks: Task[];
}

export interface ServerConfig {
  openai_enabled: boolean;
  openai_model: string | null;
  room_ttl_seconds: number;
  max_room_agent_turns: number;
  max_consecutive_turns_per_agent: number;
  min_response_relevance: number;
  mcp_url: string;
}
