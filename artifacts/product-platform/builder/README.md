# VibApp local Builder

This directory is a deliberately narrow local vertical slice:

```text
NeedSpec JSON
  -> deterministic Rust source generator
  -> offline, resource-limited Docker compiler
  -> untrusted candidate + .vibapp transport
  -> separate verifier process
  -> quarantine only
```

It targets the frozen `vibapp:experimental-v0@0.0.1` `ui-only-reference` world. The guest exports one launcher UI, no service entrypoint, and no live network/provider behavior. The world still has the five exact typed imports required by the accepted contract; the generated guest does not call them.

Run the complete good + tampered demonstration:

```sh
python3 artifacts/product-platform/builder/run_demo.py
```

Or run each trust role separately:

```sh
python3 artifacts/product-platform/builder/build_product.py \
  artifacts/product-platform/builder/needs/good.json --json

python3 artifacts/product-platform/builder/verify_candidate.py \
  artifacts/product-platform/builder/runs/<run-id>/candidate --json
```

The Builder writes `runs/<run-id>/candidate/handoff.json` with these integration fields:

- `status` is always `builder-output-untrusted`;
- `package_digest_sha256`, `component_sha256`, `manifest_sha256`;
- `archive` and `archive_sha256`;
- `builder_image_digest`, `network`, and `promotion_authority`.

The verifier writes `quarantine/<package-digest>/verifier-record.json`. Its status is `quarantined`; the booleans `accepted`, `published`, and `installed` remain false.

After a successful demonstration, integrations can read the stable
`builder/latest.json` feed. It points to the current candidate handoff,
Builder job, verifier record, quarantined archive, and demo report; it never grants
acceptance, publication, or installation authority.

Important limitations: this is a macOS ARM64 local product implementation, handles one deterministic UI-only template, uses a `.vibapp` ZIP only as a local transport convention, and does not instantiate the Component in the Stage 0 host. Its minimal single-threaded bump allocator is suitable only for this short-lived proof and does not reclaim memory. Component bytes are repeatable, while package digests vary because provenance includes build time. It does not satisfy formal Gate 5/Tranche 6 independent acceptance, publication, install, signing, deployment, cloud isolation, or multi-tenant security.
