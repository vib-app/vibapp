# Registry validation record

Date: 2026-08-24 Asia/Shanghai  
State: locally implemented and tested, `experimental-product-hold`; not independently
accepted, deployed, published, installed, or promoted through any Stage 0 Gate.

## Deterministic checks

```bash
python3 -m py_compile artifacts/registry/registry_service.py \
  artifacts/registry/tests/test_registry_service.py
python3 -m unittest discover -s artifacts/registry/tests -p 'test_*.py' -v
find artifacts/registry -type f -name '*.json' -print0 | xargs -0 -n1 jq empty
```

Result: 13/13 unit and loopback HTTP tests passed; every JSON file parsed. Tests cover
hard-filter ordering, no-match, explicit consent, provider failure, invalid dimensions,
response-size rejection, kind/capability filtering, negative constraints, stable catalog,
bounded HTTP input/output, recommendation, and refinement. Test embeddings are visibly
named `test-fixture-embedder`; they are not claimed as live evidence.

## Live LocalAI evidence

Provider health and direct API checks succeeded at
`http://192.168.199.170:8081`; the response reported model
`text-embedding-ada-002`, 2 vectors, and 384 dimensions.

```bash
python3 artifacts/registry/registry_service.py search \
  --need artifacts/registry/fixtures/needs/focus.json \
  --embedding-consent --limit 5
python3 artifacts/registry/registry_service.py search \
  --need artifacts/registry/fixtures/needs/unrelated.json \
  --embedding-consent --limit 5
```

- `evidence/live-focus-search.json`: `route=recommendation`, top app
  `ai.vibapp.fixture.focus-board`, cosine `0.908717`, hybrid `0.711897`, live 384-d
  embedding, SHA-256 `bbb03cacfa77128f424741955e9b0171377183921cb40e846a66b31f1ea43f14`.
- `evidence/live-unrelated-search.json`: `route=refinement`, reason
  `no-acceptable-semantic-match`, live 384-d embedding, SHA-256
  `24f838a582268fbdaaa90f2e84d0acad68106c17aa92662f63ca177ca75a746b`.
- Both responses have `codeagent_handoff.created=false` and
  `codeagent_handoff.permitted=false`.

## Resource observation

`/usr/bin/time -l` around one live CLI search reported 30,113,792 bytes maximum RSS,
18,956,768 bytes peak memory footprint, zero swaps, and 0.20 seconds wall time. This is
one current-host observation, not a soak-test guarantee. Enforced limits are documented
in `README.md` and exercised for oversized incoming/provider payloads.

## Remaining risk

The five records and their trust/publication metadata are synthetic. No database,
pgvector index, production publisher verification, moderation, revocation feed, labeled
relevance corpus, authentication layer, independent security review, deployment, or
cross-platform soak test was performed. Threshold quality is therefore not production
evidence.
