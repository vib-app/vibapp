# VibApp Registry retrieval service

Status: `experimental-product-hold`. This ignored artifact is not a Stage 0 Gate
deliverable, public Registry, production service, verifier, installer, publisher, or
CodeAgent. It demonstrates the product's retrieval-first decision boundary without
claiming formal acceptance.

## What it does

One request follows this fixed order:

1. strictly validate the `NeedSpec` and retrieval constraints;
2. hard-filter package/contract/WASI/WIT, platform/profile, app kind, required
   interfaces, permission ceiling, negative constraints, privacy, publisher trust,
   artifact verification/revocation, and publication state;
3. after explicit embedding consent, embed only eligible candidates with the configured
   OpenAI-like embedding endpoint (LocalAI `text-embedding-ada-002`, 384 dimensions,
   remains the default);
4. rank with keyword overlap plus embedding cosine and attach evidence paths that
   point only to candidate/need facts;
5. return `route=recommendation` only for an acceptable existing app, otherwise
   return `route=refinement`.

Every response has `codeagent_handoff.created=false`. Registry never creates a build
or CodeAgent task. A later orchestrator may ask clarifying questions after
`refinement`; a separate consented agent runner owns any subsequent development.

## CLI

The implementation uses the Python standard library only.

```bash
python3 artifacts/registry/registry_service.py validate-need \
  --need artifacts/registry/fixtures/needs/focus.json

python3 artifacts/registry/registry_service.py search \
  --need artifacts/registry/fixtures/needs/focus.json \
  --embedding-consent --limit 5
```

Without `--embedding-consent`, eligible candidates produce an explicit
`embedding-consent-required` refinement and the provider is not called. An unavailable,
timed-out, oversized, malformed, wrong-count, non-finite, or wrong-dimension
provider response produces a degraded refinement; lexical scoring is never relabeled as
vector scoring and never becomes a recommendation fallback.

Optional CLI hard filters are repeatable:

```bash
python3 artifacts/registry/registry_service.py search \
  --need artifacts/registry/fixtures/needs/focus.json \
  --kind ui \
  --required-interface vibapp:experimental-v0/kv@0.0.1 \
  --embedding-consent
```

## Loopback HTTP service

```bash
python3 artifacts/registry/registry_service.py serve --host 127.0.0.1 --port 8787
curl --fail http://127.0.0.1:8787/healthz
curl --fail http://127.0.0.1:8787/v1/registry/search \
  -H 'Content-Type: application/json' \
  --data-binary @artifacts/registry/fixtures/query.focus.json
```

The search endpoint accepts exactly `need`, `constraints`, and `limit`. The complete
example request is [fixtures/query.focus.json](fixtures/query.focus.json). The server
binds only to `127.0.0.1`; it has no authentication and must not be exposed publicly.

## Resource and network bounds

| Boundary | Limit |
| --- | ---: |
| NeedSpec | 64 KiB |
| HTTP request | 96 KiB |
| catalog | 512 KiB / 50 records |
| result | 512 KiB / 20 returned matches |
| embedding batch | 128 texts / 256 Ki characters / 384 KiB body |
| one embedding response | 2 MiB, exactly 384 finite numbers per vector |
| outbound embedding timeout | 5 seconds |
| inbound/outbound concurrency | 4 / 2 |
| vector cache | 256 entries |

Desktop may pass one exact, bounded embedding configuration over stdin; its optional
bearer key never appears in argv or environment. Remote endpoints require HTTPS, while
loopback and private-LAN HTTP remain allowed. Redirects are disabled. Model ID and
dimensions are validated and reported with each result.
Hard-filtered searches with zero eligible candidates do not contact the model.

## Validation

```bash
python3 -m py_compile \
  artifacts/registry/registry_service.py \
  artifacts/registry/tests/test_registry_service.py

python3 -m unittest discover \
  -s artifacts/registry/tests -p 'test_*.py' -v
```

The live evidence files demonstrate both branches with the real LocalAI embedding
service: `evidence/live-focus-search.json` recommends the exact focus fixture, while
`evidence/live-unrelated-search.json` returns refinement. They are operational evidence,
not a relevance benchmark or independent Gate verdict.

## Known limits

- The catalog has five synthetic records; no PostgreSQL/pgvector, accounts, moderation,
  publisher trust root, revocation feed, or production corpus exists here.
- Thresholds were smoke-tested on two examples, not calibrated on a labeled evaluation
  set. Production recommendation quality remains unproven.
- Candidate facts are fixture metadata. Their synthetic verification/publication fields
  demonstrate filtering but do not establish real-world trust.
- The process is bounded and loopback-only, but it has not received an independent
  security assessment or cross-platform soak test.
