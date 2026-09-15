# OpenCode / fixed LocalAI source-authoring image

Experimental, outside accepted Stage 0. The shared launcher admits one consent-bound
source-authoring job, not a build, verification, installation or publication.

## Reproducible inputs and identity

`Dockerfile.opencode` pins the resolved Node 22.21.1 Bookworm-slim base digest and
`opencode-ai@1.18.27`; its optional native packages also have exact 1.18.27 versions.
Npm checks registry archive integrity. Package scripts are disabled. The reviewed
`install-opencode.mjs` copies only the already-installed Linux/glibc executable
into the npm launcher path; it never runs npm postinstall, downloads a fallback or
changes host configuration. Native x64 requires its usual AVX2 capability; the
verified local image is Linux arm64.

`PROVENANCE.opencode.json` records the primary package integrities and observed
native/bundle hashes. Every build writes the entire installed provider bundle hash
and a hash covering both the shared entrypoint and OpenCode hook. The OpenCode
identity also declares the exact relay compatibility policy; a prior stream-only
image cannot be admitted under the new policy. Preflight resolves
the mutable local tag to an immutable Docker image ID; the identity also includes
host executor/adapter/launcher bytes, selected model, endpoint and policy. Changing
any bound value requires a new task observation/consent, never an in-place retry.

```sh
docker build -f artifacts/codeagent-launcher/docker/Dockerfile.opencode \
  -t vibapp-codeagent-opencode:local artifacts/codeagent-launcher/docker
python3 artifacts/codeagent-adapter/codeagent_adapter.py \
  --cloud-agent artifacts/cloud-agent/cloud_agent.py \
  preflight --provider opencode --model qwen3.8-27b-uncensored-mtp-q4
```

Preflight uses image inspection and a credential-free, network-disabled identity
probe only. It does not contact LocalAI or consume model requests/consent.

## Containment and relay

The existing Docker executor provides no external container network, host mounts,
Docker socket or credentials, and enforces task ceilings within 2 GiB RAM, two CPUs,
64 PIDs, 64 MiB combined scratch and 1800 seconds. The root-owned watchdog and
loopback relay are separate from the UID 1000 author. The immutable image selects
the provider hook; no start-frame field can change it. Source collection, immutable
scaffold comparison, at most two separate offline-compiler repair rounds, exact
container cleanup and quiescent success receipts are shared with Codex.

OpenCode receives only synthetic configuration. `--pure`/`OPENCODE_PURE`, disabled
project config, plugins/MCP/LSP/formatting/sharing/updates/autocompaction, and an
isolated root-owned HOME/config prevent host or generated project configuration
from being loaded on a repair round. Only data/cache/state/tmp are UID-writable.
The model CLI has no compiler and no network; file tools operate within the
container. No application code is supplied by this adapter.

The image-bound `agent.build.prompt` replaces OpenCode's generic system prompt;
it does not append a competing lower-priority instruction. It requires reading
the immutable Cargo/WIT scaffolds, native lowercase tool names with complete JSON
arguments, and physical Rust source edits. It forbids delegation, compilation,
lint/tests, installation, network and background processes. No XML/text-to-tool
execution fallback is present. This removes the generic prompt's conflicting
capitalized tool references and compile instructions; it does not claim to repair
a server-side tool parser or establish real-model application correctness.

Its sole loopback path is `POST /v1/chat/completions`. The host constructs the only
upstream destination, `http://192.168.199.170:8081/v1/chat/completions`, with no auth
header, redirect handling or caller-supplied route/headers. This LAN HTTP exception
is explicit and does not generalize to arbitrary HTTP URLs. The selected upstream
model is always `qwen3.8-27b-uncensored-mtp-q4`, never the voice model. The optional
OpenCode selection alias is `vibapp/` plus that exact ID.

The compatibility mode is explicitly `localai-native-json-to-sse-v1`: upstream
`stream=false`, downstream `stream=true`, never a silent retry/fallback. Main's
direct 2026-09-07 probe observed the shared server's stream reply terminate at
literal tool markup while the otherwise identical nonstream reply returned a
complete native tool call. The host therefore accepts a bounded single-choice
native JSON response and encodes its visible content/native calls as SSE. It does
not parse XML or infer arguments. Names must match exactly one offered tool
case-insensitively, and native spelling/argument bytes are preserved; OpenCode's
existing SDK repair handles `Bash` → `bash`. Required argument fields/types are
checked before emitting any success SSE, and the SDK retains its full schema gate.
Malformed, unknown, ambiguous, duplicate-ID or incomplete calls fail closed.
The observed LocalAI4.9 complete JSON can put `index: 0` on every call. That
nonstream-only zero/default or the exact array position is accepted; outgoing
SSE indices are always rebuilt from array order, with distinct validated IDs.
No streaming fragments are reordered or combined by this complete-JSON adapter.
Reasoning fields are excluded, not repurposed as visible content or tool input.
The host policy explicitly sets a 300-second socket timeout for a complete
LocalAI response, recorded in per-call evidence. This does not extend the outer
job deadline (at most 1800 seconds). Cancellation/deadline cleanup still shuts
down the socket before closing it, interrupting a blocked `getresponse()`; a real
loopback-socket regression verifies prompt interruption without contacting a model.

The host requires streaming downstream chat requests, validated text messages/function tools,
at most 64 calls per job, 256 KiB request bodies, 256 messages, 48 tool definitions,
32 calls per message, bounded tool results and 16384 output tokens. Source-writing
instructions request separate modules and at most 180 lines per write/edit to
avoid truncated JSON arguments. The authoring CLI has no shell tool; it uses
scoped file tools, while compiler execution remains separate. The upstream
response must be native JSON at most 8 MiB; the resulting downstream SSE is also
bounded at 8 MiB. The OpenCode client model configuration selects a 32768-token
context limit; requests cannot set context-extension options. This is not proof of
the server's actual configured window. Read-only inspection on 2026-09-07 found
the shared LAN model YAML at `context_size: 120000`, differing from the LocalAI
skill's older 32768 observation; this adapter did not change or restart it. The
host's 256 KiB byte cap is not a tokenizer-based 32768-token guarantee. Context
overflow errors fail closed, without automatic model/window changes.

Host-owned `upstream-request-NN.json` files contain the actual canonical request,
fixed endpoint/model and request hash, mode 0600. A corresponding response record
contains status, byte count, response hash, cancellation state and bounded native
call metadata (name, argument keys/hash, finish reason, visible content byte count).
Both records explicitly bind upstream/downstream stream modes and compatibility
reason. Raw native response reasoning is neither forwarded nor persisted; trace extraction excludes reasoning and arbitrary log
events, and private traces are not trusted UI. Evidence stays outside the container
and is not part of any public package.

## Offline checks

```sh
python3 -m unittest discover -s artifacts/codeagent-launcher/tests
python3 -m unittest discover -s artifacts/codeagent-adapter/tests
node --test artifacts/codeagent-launcher/tests/opencode-config.test.mjs
VIBAPP_TEST_OPENCODE_IMAGE=1 python3 -m unittest discover \
  -s artifacts/codeagent-launcher/tests -p test_opencode_container.py -v
```

The explicit final check runs the real image/CLI and production JSON-to-SSE gateway
with a mocked HTTP connection, never a LAN connection or model. It verifies the
source-only system prompt, native `Write` capitalization repaired by the actual
SDK, an actual `write` tool round trip, exact exported fixture bytes, and
whole-container cleanup/quiescence.
It is protocol evidence, not model/application quality evidence. The default test
suite skips it when Docker was not explicitly opted into.

Reviewed primary references: [OpenCode providers](https://opencode.ai/docs/providers/),
[CLI](https://opencode.ai/docs/cli/), and exact v1.18.27
[config precedence](https://github.com/anomalyco/opencode/blob/v1.18.27/packages/opencode/src/config/config.ts),
[configuration paths](https://github.com/anomalyco/opencode/blob/v1.18.27/packages/opencode/src/config/paths.ts),
[flags](https://github.com/anomalyco/opencode/blob/v1.18.27/packages/core/src/flag/flag.ts),
[noninteractive stdin/run](https://github.com/anomalyco/opencode/blob/v1.18.27/packages/opencode/src/cli/cmd/run.ts).
Actual local 1.18.27 `run --help` and UID 1000 `debug config` were checked with
network disabled; mutable documentation alone was not treated as protocol proof.
