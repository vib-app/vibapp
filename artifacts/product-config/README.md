# Product model configuration

The desktop Settings page is the primary configuration surface. Bring Your Own Model
applies to every current non-Code-Agent model scenario:

- requirement intake and NeedSpec preprocessing use one OpenAI-like generation model;
- Registry semantic retrieval uses a separate OpenAI-like embedding model.

The generation profile supports both Chat Completions and Responses. API keys are
write-only from the UI: the client reports only `hasApiKey`, stores secrets separately
in its permission-restricted local settings directory, and passes them to trusted
Python helpers over bounded stdin. Keys are never placed in command-line arguments or
child-process environment variables. CodeAgent adapters remain independently managed
and never inherit these settings.

The legacy standalone analyzer still supports these non-secret environment variables:

- `VIBAPP_LLM_ENABLED`: `1` or `0`.
- `VIBAPP_LLM_BASE_URL`: defaults to `http://192.168.199.170:8081`.
- `VIBAPP_LLM_MODEL`: defaults to `qwen3.8-27b-uncensored-mtp-q4`.
- `VIBAPP_LLM_TIMEOUT_SECONDS`: request timeout, bounded to 5–60 seconds.
- `VIBAPP_LLM_MAX_TOKENS`: response budget, bounded to 256–1024 tokens.
- `VIBAPP_LLM_TEMPERATURE`: bounded to 0–0.3; the default is `0`.

Copy or source `llm.env.example` when an explicit process-level override is
needed. The defaults are also compiled into the product-layer analyzer so a
Finder-launched macOS app has the same test model even though it does not
inherit a terminal's shell environment.

The base URL is restricted to the approved LocalAI endpoint. Loopback URLs are
accepted only when `VIBAPP_LLM_ALLOW_TEST_LOOPBACK=1`, which is used by isolated
tests. Need text is sent only after the user grants the client’s LAN AI consent.
Remote provider URLs must use HTTPS. HTTP remains accepted for loopback and private-LAN
model servers, including the default `192.168.199.170` service. This configuration does
not apply to the Stage 0 deterministic contract.
