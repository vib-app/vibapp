# VibApp local CodeAgent adapters

Status: **experimental product integration outside the accepted Stage 0 tranche**.
This directory is not Stage 0 implementation or acceptance evidence.

The adapter converts an immutable, consent-bound VibApp task into an untrusted
source handoff for a separate Builder and Verifier. It has no compile, verify,
install, launch, sign, or publish authority.

## Current Docker paths (2026-09-07)

The product's Codex provider now uses `docker_provider.py` and the provider-neutral
`codeagent-launcher/docker_executor.py`. Build the deliberately small image context:

```sh
docker build --tag vibapp-codeagent-codex:local \
  --build-arg CODEX_VERSION=0.153.4 artifacts/codeagent-launcher/docker
```

The tag is resolved to an immutable image ID before task consent. The image, CLI
bundle, selected model/connection, host adapter and isolation policy hashes become
part of that task's execution identity. A later image upgrade is allowed, but an
already admitted task cannot silently switch to different executable bytes.

Docker Desktop must be running on macOS. The workload has no external network,
host mount, Docker socket or model credential. Only Responses requests to the
selected model pass through a bounded host-side stdin/stdout relay. The host reads
only the selected HTTPS Responses connection in Codex configuration (or its normal
login credential); it does not import user rules, plugins, MCP or other providers.
Credentials are neither written into the image nor copied into the workload.

Default product ceilings: one host-user delivery pipeline, 2 GiB memory, 64 PIDs,
two CPUs, 64 MiB scratch storage and 1800 seconds. A separate privileged watchdog
accounts whole-container CPU usage; authoring runs as UID 1000. The independent
offline Builder can send diagnostics back for at most two repair rounds in the
same admitted job, under the original deadline and consent. Compiler success is
not verification, installation or publication.

Cancellation and recovery resolve the exact owned image/container labels, remove
the complete container and confirm absence. A success receipt requires both the
container and relay/compiler threads to be quiescent. Recovery never resubmits a
spent consent. Failed source/checks remain private under the attempt's
`output/executions/` directory, never in My Apps or the public AppStore.

Codex's reviewed request policy allows 48 total model network attempts: at most
40 during initial authoring and 8 shared across both possible compiler-repair
rounds. Transient retries consume both the current phase and total allowance;
their separate job-wide ceiling is eight retries and never resets after a success
or phase change. The reserve cannot be spent
on initial authoring or reset between repairs. The 30-minute authoring deadline,
container resource caps and separate Builder/Verifier gates remain unchanged.
Preflight exposes the central policy and binds it into the provider execution
identity. The adapter appends an initial notice to the actual prompt and hashes
those final bytes; the host relay supplies current phase/used/remaining counts on
each Responses request. This does not change the task-v3 provider_request shape.
Changed policy or prompt instructions require fresh identity-bound consent, not
an automatic extension of a running attempt.

Only source already exported to a host compiler check is retained on failure.
Failure before the first export still loses the container's partial tmpfs source
when cleanup completes; bounded failed-source quarantine is not implemented here.
Neither a trace excerpt nor a failed compiler snapshot is a source-ready handoff.

OpenCode now uses the same supervisor/export/compiler-feedback path with its own
image-bound hook and a fixed, credential-free LAN chat relay. Build and observe it
from the repository root:

```sh
docker build -f artifacts/codeagent-launcher/docker/Dockerfile.opencode \
  -t vibapp-codeagent-opencode:local artifacts/codeagent-launcher/docker
python3 artifacts/codeagent-adapter/codeagent_adapter.py \
  --cloud-agent artifacts/cloud-agent/cloud_agent.py \
  preflight --provider opencode --model qwen3.8-27b-uncensored-mtp-q4
```

This path pins OpenCode 1.18.27 and the exact LocalAI model
`qwen3.8-27b-uncensored-mtp-q4` (client context setting 32768, output at most 8192
tokens). The actual shared server YAML was observed at 120000 on 2026-09-07;
the client setting and byte cap are not proof of an enforced server token window.
The only upstream is `http://192.168.199.170:8081/v1/chat/completions`, represented
honestly by the narrow `fixed-lan-http` execution identity. This explicitly selected
LAN hop is unencrypted and credential-free; arbitrary HTTP endpoints are not enabled.
No global OpenCode configuration, auth store, plugin or user rule is read. The
image uses `--pure`, disabled project config, root-owned synthetic HOME/config and
isolated writable XDG data/cache, plus an image-bound source-only system prompt.
The fixed compatibility policy requests native JSON upstream (`stream=false`) and
encodes validated native calls as SSE for OpenCode (`stream=true`), addressing the
observed shared server streaming-tool truncation without XML parsing or guessed
arguments. Requests, modes and hashes are private attempt evidence; raw response
reasoning is not forwarded, persisted or rendered in the UI.

See [OpenCode image and relay notes](../codeagent-launcher/docker/README.opencode.md)
for resolved package provenance, limits and offline checks. A passing protocol test
does not claim that an application is correct or accepted.

All legacy host-process paths remain paused, including OpenCode. Claude Code and
Gemini do not gain contained execution merely by sharing the launcher interface.

## Legacy host-process safety gate

Live host-process provider execution remains paused. The former macOS supervisor
observed one process group, but a provider could fork, call `setsid(2)`, close its
stdio, leave that process group, and continue changing the workspace after the
supervisor returned. Seatbelt restricts file/network authority; it does not turn a
PGID into an inescapable process container.

Every legacy host-provider `execute` path fails closed with
`local-live-containment-unavailable` before a provider process starts. When an
identity can be safely observed, a `run` records it and the closed gate, then fails before
single-use consent is consumed. This is a safety hold, not permanent removal of
local Codex, Claude Code, OpenCode, or Gemini support. The execution seam is kept
for a future container, VM, launchd, or Codex-harness backend that can prove:

- every descendant stays inside one observable/cancellable job boundary;
- timeout and cancellation terminate the complete descendant tree;
- the provider is quiescent before source inspection or handoff;
- CPU, RSS, PID, disk, output, and wall-time limits are kernel-enforced or
  independently observable.

Offline synthetic providers still exercise the complete task → source audit →
handoff flow. Tests never contact a provider or the network.

## Legacy identity-only preflight

`preflight` is still useful while execution is paused. A successful observation
returns secret-free identity data with `identity_observed=true`,
`execution_available=false`, and the containment blocker. It includes:

- `adapter_id=local-codeagent-adapter`, adapter version, and SHA-256 of the exact
  adapter script bytes;
- selected provider/model, resolved executable path, observed/pinned executable
  version and SHA-256;
- a complete `vibapp.provider-execution-identity.experimental-v1` value containing
  the endpoint identity, runtime package identity, non-secret config digest, and
  aggregate identity digest;
- provider-specific publisher/bundle observations when available.

For CLIs whose endpoint is selected internally and cannot be observed without
executing the provider, the endpoint identity uses the schema's exact
`provider-managed` sentinel; it does not guess a public URL. OpenCode is stricter:
its selected public endpoint and config identity must be safely observable or
preflight fails.

The Desktop can bind this exact value into the immutable task and consent instead
of guessing identity from a provider alias. `run` re-observes and deep-compares the
identity before consent use. A mismatch fails with
`provider-execution-identity-mismatch`.

Codex identity observation verifies the reviewed Homebrew Cask layout, exact bytes,
and OpenAI Developer ID signature without executing the Codex binary. The old live
`--version`/`exec --help` probe is not used by product preflight while descendant
containment is unavailable. The previously exercised CLI version interval is
reported as advisory metadata (`version_within_exercised_range`); a newer signed
Codex is not rejected only because its version changed. Live execution still needs
both a compatible protocol observation and an accepted containment backend.

## Legacy host OpenCode configuration and credentials

The retained host-only `OpenCodeProvider` (not the current registry route) receives
stricter treatment because its shared config/auth stores may
contain several providers and credentials:

- only private `~/.config/opencode/opencode.json` strict JSON is inspected;
- JSONC, comments (including unterminated comments), duplicate keys, non-finite
  numbers, escaped JSON strings, unknown selected-provider fields, and a missing
  provider/model are rejected;
- the selected provider must expose an explicit credential-free canonical HTTPS
  `baseURL`; its public endpoint, runtime package, and secret-free config digest are
  returned as identity;
- credential-bearing fields such as API keys, tokens, authorization headers,
  passwords, and secrets are rejected before JSON deserialization;
- the adapter never parses or rewrites their plaintext values, never copies the
  whole OpenCode `auth.json`, and never creates an ephemeral credential-bearing
  OpenCode config;
- `_environment` fails with `opencode-scoped-credential-unavailable` until OpenCode
  offers a reviewed per-provider credential handle/interface.

Presence of the shared auth file may be reported as metadata only. Its bytes are
never opened by the adapter.

## Authority-owned provider result

Provider prose and provider-authored `provider-last-message.json` are not control
plane truth. After a contained provider becomes quiescent, the adapter removes that
single untrusted path without following links and deterministically derives a new
record from:

- the immutable task's job, target world, app kind, and required imports; and
- a no-follow inventory of the real files below `source/`.

The existing CloudAgent validator and independent workspace audit then check the
derived record, exact Cargo/WIT scaffolds, source allowlist, byte/file limits, and
target consistency. The derived record says only that source was observed; it does
not claim compilation, correctness, verification, or acceptance.

The reviewed Docker Codex UI instructions therefore ask for physical source and
a brief completion message, not a provider-authored final inventory or JSON file.
Native/legacy and OpenCode instruction policies are unchanged. Host-derived
inventory, immutable-scaffold validation and independent acceptance still apply.

## Commands

Observe one provider identity without making a provider request:

```sh
python3 codeagent_adapter.py \
  --cloud-agent ../cloud-agent/cloud_agent.py \
  preflight --provider opencode --model qwen3.8-27b-uncensored-mtp-q4
```

List identity observations for selected providers:

```sh
python3 codeagent_adapter.py \
  --cloud-agent ../cloud-agent/cloud_agent.py \
  providers --provider opencode --model qwen3.8-27b-uncensored-mtp-q4
```

Read durable status without invoking a provider:

```sh
python3 codeagent_adapter.py \
  --cloud-agent ../cloud-agent/cloud_agent.py \
  status --status-file /private/operational/status/job.json
```

`run` requires exact job/consent confirmation, explicit model, and external
cost acknowledgement. Unsupported legacy host paths terminate at the containment gate with
`consent_consumed=false`, `provider_process_started=false`, and
`external_request_attempted=false`.

## Offline validation

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

The regression suite includes a no-network fork + `setsid` + closed-stdio escape
fixture. It proves the unsafe live command is rejected before process start and
cannot write after the supervisor returns. It also covers cancellation-before-start,
strict/duplicate-key OpenCode parsing, secret-config rejection, identity binding,
deterministic control-record derivation, Cargo/WIT integrity, bounded handoff, and
consent non-consumption at failed gates.
