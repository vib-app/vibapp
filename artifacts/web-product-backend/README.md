# VibApp Web product backend

This loopback service lets the credentialless Website Launcher use the same Rust
product modules as Desktop: NeedSpec/LLM preprocessing, Registry routing,
CodeAgent settings, immutable task construction, automatic delivery history and
the private AppStore catalog.

The browser never receives the backend token, provider credentials or filesystem
paths. The Website server owns the token and forwards only an allowlisted command
over a bounded same-origin route. A public deployment must point that route at an
equivalent trusted server-side product bridge; it must not expose this local
loopback helper to the internet.

Startup invokes the bridge's side-effect-free `health` command and requires the exact
current build-input SHA-256 plus delivery contract versions before binding the HTTP
port. Live bridge slots are retained until each child exits, even when a long-running
delivery child has already emitted its admission response. This prevents fast HTTP
responses from bypassing the four-process bound.

The public Website currently does not forward private state or mutating commands to
this service. A bearer shared by the Website server is not an end-user identity; a
public mutation path requires authenticated principals, per-principal data roots,
CSRF/anti-replay protection, and separate rate/cost limits first.

The service makes no model or CodeAgent request on startup. Those operations occur
only after the corresponding user action and explicit consent in the shared GUI.
