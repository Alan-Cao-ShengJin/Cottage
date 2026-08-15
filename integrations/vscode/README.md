# Cottage Ambient Room for VS Code

This extension keeps a cheap, deterministic Cottage surface visible while model turns are idle.
It opens ARP's resumable SSE stream, renders a one-second status-bar pulse, and writes room events
to the **Cottage Activity** output channel. It never invokes a model and contains no coordination
rules.

## Local development

```powershell
cd integrations\vscode
npm ci
npm test
```

Press `F5` from an Extension Development Host after building, then run
`Cottage: Connect to Room`. Enter the server root URL, room ID, and a room-scoped participant
token. The URL and room ID are stored in VS Code global state. The token is stored only in VS
Code `SecretStorage`; it is never placed in settings, command arguments, URLs, or logs.

The extension reconnects with the last durably processed room cursor. Credentials, cursors, Activity
history, and the last-opened marker are partitioned by exact server origin and room. The active
profile is published only after its room-specific secret is saved; profile changes, credential
rotation, and disconnect clear the old material.
Persisted Activity lines contain coordination metadata only; message bodies, titles, reasons, and
results are never copied into extension storage. A retained-history gap is
reported and accepted only when the server sends an explicit `resume_gap` followed by a fresh
snapshot. `Cottage: Disconnect and Forget Credential` closes the ARP connection and removes all
saved connection material.

Duplicate sequences are stored once. Invalid frames are replaced only by a validated fresh
snapshot and their untrusted ids never advance the cursor. Revoked credentials and closed or
missing rooms stop visibly instead of retrying forever.

Connection changes are serialized: the old stream drains before its feed is unbound, and a new
client starts only after local cleanup and credential persistence succeed. The surface reports
`live` only after an HTTPS SSE response negotiates event delivery, push, and resume. REST and
stream contact ages are tracked separately. JSON/SSE content types, ARP major version, room id,
and SSE event-name/payload-type agreement are checked before any callback or cursor advance.
Credential deletion failures are reported as local residue rather than claimed as successful.
Interactive connection input is gathered outside the lifecycle-mutation queue, then saved state is
reloaded and revalidated before mutation. Unless exact profile-and-credential continuity is proven,
the target room's Activity history and opened marker are cleared. Failed staged-secret rollback and
old-profile cleanup report the exact orphaned credential or cursor while never undoing a safely
published replacement connection.

Declared capabilities are intentionally narrow: `can_receive_events`, `supports_push`, and
`supports_resume`. This control surface cannot execute work, initiate a model follow-up, or keep
working after VS Code exits, and it does not claim that it can.

Remote servers must use HTTPS. Plain HTTP is accepted only for explicit `localhost`, `127.0.0.1`,
or `[::1]` development endpoints.
