# Stage 0 Provenance and Source Boundary

## VibApp baseline

- Repository: `/Users/zhuzhe/Workspace/vibapp`
- Initial state: the directory contained no source and was not a Git repository.
- Local repository initialized on 2026-08-11 with branch `main`.
- The first baseline records only evaluation, gate documents and ignore policy; it predates all VibApp source code.
- `.chief-of-staff/` is project-local operational memory and is intentionally ignored rather than committed.

The baseline commit hash is recorded below immediately after the first commit:

```text
BASELINE_COMMIT: 1b503e9200a3071d1e5db9c60f3d85a220db2f6b
```

## Flos boundary

`/Users/zhuzhe/Workspace/flos` is read-only evidence. It has no clean committed provenance or license file in the inspected snapshot. VibApp may reuse product concepts, state names and independently rewritten behavioral tests, but must not copy, link, vendor, import or derive source from Flos until ownership and license provenance are explicitly resolved.

The following Flos implementations are specifically excluded as foundations:

- Go/FlowText/FTBC runtime;
- substring/Git-backed Registry;
- AST-pointer hot reload;
- host-process proxy “sandbox”;
- unauthenticated HTTP API and file-backed multi-user state.

## External source and dependency boundary

- Dependencies must come from declared registries or pinned upstream sources and be recorded in `Cargo.lock` after source work is authorized.
- No Git dependency, copied snippet or vendored code may enter Stage 0 without an origin, exact revision, license and reason.
- No user credential, provider auth file, home directory, model transcript or cloud secret may be added to the repository or a fixture.
- Codex fixtures must be synthetic/replayed events and contain no real conversation or account data.

## Allowed and generated paths

The exact future source, project-local skill, root workspace/policy metadata and generated-output paths are normatively and exhaustively listed in [README.md](README.md). That list pre-authorizes `.agents/skills/vibapp/` for the post-acceptance bootstrap and the exact Cargo, Cargo policy and `cargo-vet` metadata needed by later tranches. Pre-authorization is not activation: neither the skill nor workspace/source metadata may be created before its tranche preconditions pass.

Synthetic replay records below `fixtures/**/*.jsonl` are contract inputs and must remain eligible for ordinary version control. They must be invented, minimal, reviewable and free of real prompts, responses, account identifiers, credentials, home paths or provider metadata. Operational JSONL is not a fixture: real model/build/runtime logs stay only in ignored local locations declared by `.gitignore`, including `.chief-of-staff/`, `logs/`, `artifacts/`, `quarantine/` and `tmp/`.

Generated output remains constrained to the ignored Stage 0 paths listed in the index. An ignore rule is hygiene, not authorization: ignored `build/`, `dist/` and `node_modules/` paths are not additional Stage 0 output locations. Any source or workspace-policy path outside the exhaustive list requires a Stage 0 ADR update and another gate review before use.
