# Controlled RoomHash collaboration adapter

This adapter owns temporary shared-session policy above RoomHash's RTC stack.
`create` and `join` require a host-provided opaque grant accepted by the injected
authorizer and an absolute `expires_at`. The nil UUID is reserved for local-only
state and is rejected before transport access.

The runnable RoomHash factory uses the current pinned Trystero/Werift modules and
the current RTC configuration helpers. It intentionally does not instantiate
RoomHash `MeshNode`, whose `channels` messages implement channel gossip and
recursive discovery. Only the private `vibapp-collaboration-v1` action exists at
this boundary.

Desktop runs this adapter inside its trusted bounded Node companion. Website loads
the reviewed browser adapter only in the trusted parent page. Both surfaces expose
the same app-bound create/join/leave/send/receive/status operations; a launcher UI
cannot substitute another app id, and guests never receive the raw transport. The
shared GUI provides explicit confirmation, visible expiry, peer count, and leave
controls for an installed app.

This is a Launcher/product broker, not a guest ABI. The accepted Stage 0 WIT has no
collaboration import, receive event, channel handle, or permission declaration, so a
generated Component cannot call this broker directly. Adding that ability requires a
future capability-gated WIT revision and its normal acceptance process; it is not
implied by the JavaScript/Rust host adapters documented here.

Inbound queues and messages are bounded, canonical Base64 is enforced at the desktop
process boundary, and event ids are deduplicated. Custom TURN credentials remain
write-only. The Desktop Node companion reports `turn-unsupported` because the current
headless RoomHash RTC configuration cannot safely apply that override. The Website
product broker likewise refuses a stored custom TURN setting rather than exposing its
write-only credential to browser code. Ordinary fixed-tracker P2P/RTC remains a
separate path.

Deterministic tests validate authorization, UUID, expiry, message size, session count,
cleanup, inbound bounds, deduplication, and browser policy. The host controller also
executes the real current RoomHash collaboration factory. A future app ABI revision is
still required before untrusted generated Components can directly consume live
collaboration events; today the broker is a launcher/product capability.

A channel UUID is only a rendezvous secret. The current broker does not turn it into
an authenticated VibApp user identity or an application-level authorization model;
applications that eventually use shared events still need protocol-level identity,
ordering, conflict, snapshot, and recovery rules appropriate to their use case.
