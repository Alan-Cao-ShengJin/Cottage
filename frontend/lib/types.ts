/**
 * ARP types mirrored from `backend/app/domain`. Kept hand-written rather than
 * generated so the frontend contract is reviewable in a diff — and deliberately
 * narrow: the UI only declares what it actually renders.
 */

export type PrincipalKind = "human" | "agent";

/** Descriptive label only. Never branch on this to decide behavior — read `runtime`. */
export type HostClass =
  | "browser_human"
  | "interactive_client"
  | "persistent_local"
  | "native_remote_a2a"
  | "unknown";

export type Capability =
  | "supports_push"
  | "supports_poll"
  | "can_receive_events"
  | "can_initiate_followup"
  | "can_execute_background"
  | "requires_human_presence"
  | "supports_resume"
  | "supports_tools"
  | "supports_artifacts";

export type DeliveryMode = "push" | "long_poll" | "attended_pull" | "none";

export type Liveness =
  | "live_push"
  | "live_poll"
  | "attended"
  | "idle"
  | "stale"
  | "disconnected";

export type TrustTier = "member" | "vouched" | "untrusted";
export type PrivacyClass = "room_public" | "org_internal" | "participant_private";
export type RoomStatus = "open" | "closed" | "purged";
export type RoomVisibility = "internal" | "cross_org";

export type WorkStatus = "active" | "paused" | "blocked" | "done";

export type TaskStatus =
  | "proposed"
  | "open"
  | "claimed"
  | "in_progress"
  | "blocked"
  | "done"
  | "cancelled";

export type ConflictKind =
  | "duplicate_task"
  | "overlapping_work"
  | "claim_race"
  | "state_cas_failure"
  | "artifact_divergence";

/** What a participant may do, derived from its negotiated capabilities. */
export interface RuntimePolicy {
  delivery_mode: DeliveryMode;
  heartbeat_interval_s: number;
  may_claim: boolean;
  max_lease_seconds: number;
  lease_renewable_unattended: boolean;
  claim_denied_reason: string | null;
}

export interface Presence {
  participant_id: string;
  liveness: Liveness;
  connection_count: number;
  delivery_modes: DeliveryMode[];
  negotiated_capabilities: Capability[];
  runtime: RuntimePolicy | null;
  last_seen_at: string | null;
}

export interface IdentityView {
  identity_id: string;
  display_name: string;
  org_id: string;
  org_name: string;
  kind: PrincipalKind;
  host_class: HostClass;
  description: string;
  trust: TrustTier;
}

export interface Participant {
  id: string;
  room_id: string;
  org_id: string;
  role: "observer" | "collaborator" | "owner";
  scopes: string[];
  trust: TrustTier;
  state: "invited" | "joined" | "left" | "removed";
  identity: IdentityView;
  presence: Presence | null;
  joined_at: string | null;
}

export interface Room {
  id: string;
  org_id: string;
  name: string;
  purpose: string;
  visibility: RoomVisibility;
  status: RoomStatus;
  event_seq: number;
  created_at: string;
  expires_at: string | null;
}

export interface WorkDeclaration {
  id: string;
  participant_id: string;
  headline: string;
  status: WorkStatus;
  targets: string[];
  task_id: string | null;
  note: string;
  started_at: string;
  heartbeat_at: string;
  expected_done_by: string | null;
  ended_at: string | null;
  /** Owner's presence lapsed — shown, but not to be trusted as current. */
  stale: boolean;
}

export interface TaskClaim {
  lease_id: string;
  participant_id: string;
  fence: number;
  claimed_at: string;
  expires_at: string;
  heartbeat_interval_s: number;
}

export interface Task {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  targets: string[];
  priority: number;
  created_by_participant_id: string;
  fence: number;
  claim: TaskClaim | null;
  result: string;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  seq: number;
  participant_id: string | null;
  body: string;
  about_ref: string | null;
  privacy_class: PrivacyClass;
  to_participant_id: string | null;
  created_at: string;
}

export interface Conflict {
  id: string;
  kind: ConflictKind;
  status: "open" | "resolved" | "dismissed";
  subject_refs: string[];
  participant_ids: string[];
  detail: string;
  detected_at: string;
}

export interface RoomSnapshot {
  type: "snapshot";
  protocol: string;
  room: Room;
  /** The cursor this snapshot is consistent with; resume from here. */
  snapshot_seq: number;
  you: {
    participant_id: string;
    role: string;
    scopes: string[];
    trust: TrustTier;
    org_id: string;
  };
  participants: Participant[];
  work: WorkDeclaration[];
  tasks: Task[];
  messages: Message[];
  conflicts: Conflict[];
}

export interface EventEnvelope {
  protocol: string;
  room_id: string;
  seq: number;
  id: string;
  type: string;
  ts: string;
  actor: {
    participant_id: string | null;
    display_name: string;
    kind: PrincipalKind | null;
    org_id: string | null;
  };
  privacy_class: PrivacyClass;
  audience: string;
  causation_id: string | null;
  payload: Record<string, unknown>;
}
