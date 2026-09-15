# VibApp ecosystem boundary

VibApp is the application platform. A generated product is a VibApp package, not a
macOS, Windows, Linux, or browser application. The same canonical Rust
`wasm32-wasip2` Component is admitted by a VibApp Client and rendered through the
host-owned semantic UI surface.

```text
                         VibApp Client
 user ── search/create ── Launcher + AppStore + trusted chrome
                              │
                 ┌────────────┴────────────┐
                 │                         │
          Registry has match        Registry has no match
                 │                         │
          immutable package       Receptionist → CodeAgent
                 │                  → Builder → Verifier
                 └────────────┬────────────┘
                              │
                       verified candidate
                              │
                 daemon-owned install / grants
                              │
                  digest-bound VibApp Runtime
                              │
             Component → semantic UI / service events
```

## Product invariants

- The user chooses application behavior, not a host OS, CPU architecture, or internal
  execution profile.
- Registry admission may still use the current Client's OS/profile as an internal hard
  filter. That context is not the generated product target.
- UI, service, and hybrid are behavior shapes. A UI surface closes independently from
  a daemon-managed service.
- The Client owns layout. Semantic UI is arranged into `compact`, `regular`, or `wide`
  size classes without giving the Component DOM, CSS, or native window handles.
- A private generated candidate may be shown only as an isolated preview. Installation,
  enablement, permissions, updates, rollback, and uninstall remain daemon-authoritative.
- AppStore publication is a separate verifier and publisher transition; successful
  generation or preview never publishes automatically.

## Current implementation truth

| Capability | Current state |
| --- | --- |
| Registry-first AppStore discovery | Implemented with the bounded local Registry |
| No-match routing to a real CodeAgent queue | Durable handoff implemented; external runner not provisioned |
| Generated private candidate discovery | Implemented for digest-bound UI-only candidate records |
| Launcher isolated first-surface preview | Implemented through the separate `vibapp-runtime` process |
| Responsive host-rendered surface | Implemented for compact, regular, and wide Client windows |
| Persistent interactive app session | Not implemented; current private launch is first-surface preview only |
| Daemon-owned installation and service lifecycle | Not implemented |
| Public AppStore ingestion/publication | Not implemented |

This distinction is intentional: the current Client now has the correct ecosystem
shape and a real private preview launch seam, while it does not mislabel preview as an
installed or continuously running application.
