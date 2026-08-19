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

export type RuntimeOperationalState = "monitoring" | "working" | "waiting";

export interface RuntimeOperation {
  state: RuntimeOperationalState;
  summary: string;
  waiting_reason: string;
  task_id: string | null;
  work_id: string | null;
  updated_at: string | null;
}

export interface RuntimeView {
  ref: string;
  is_attachment: boolean;
  label: string;
  liveness: Liveness;
  connection_count: number;
  delivery_modes: DeliveryMode[];
  last_seen_at: string | null;
  operation: RuntimeOperation | null;
  declared: {
    role: "control_surface" | "companion" | "unspecified";
    executor_kind: string;
    model: string;
    host_class: HostClass;
    is_resumable: boolean;
  };
}

export interface Presence {
  participant_id: string;
  liveness: Liveness;
  connection_count: number;
  delivery_modes: DeliveryMode[];
  negotiated_capabilities: Capability[];
  runtime: RuntimePolicy | null;
  last_seen_at: string | null;
  runtimes: RuntimeView[];
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
  /** How the identity was created: `account` or `invitation`. */
  provenance?: "account" | "invitation";
  /**
   * True when nobody vouched for this display name — the participant redeemed an
   * invitation and chose it. Surfaced because a self-asserted name that renders
   * identically to a credential-bound one is the confusion the flag exists to prevent.
   */
  name_is_self_asserted?: boolean;
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

/** Whose words a message carries, as its author declared them (D-090). */
export type Speaker = "agent" | "human";

export interface Message {
  id: string;
  seq: number;
  participant_id: string | null;
  body: string;
  about_ref: string | null;
  privacy_class: PrivacyClass;
  to_participant_id: string | null;
  /**
   * `agent` is the participant's own account of the work; `human` means it was relaying
   * its person. One seat carries both, and a reader that cannot tell them apart is
   * reading a transcript in which everybody sounds like an agent.
   */
  speaking_for: Speaker;
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
  /** Latest visible durable narration per runtime (legacy notes may be participant-scoped). */
  latest_activity: EventEnvelope[];
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
