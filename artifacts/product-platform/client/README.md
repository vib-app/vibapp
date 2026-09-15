# VibApp product client

Status: **locally runnable prototype, not a production Launcher and not Stage 0 PASS**

This small client lets a non-technical reviewer walk through the product loop:

1. describe a need and place it in a local Builder request outbox;
2. inspect fixture or file-fed Builder progress;
3. distinguish `quarantined`, `verified`, and `rejected` verification states;
4. browse private candidate details and declared permissions;
5. record a digest-bound install intent for a verified candidate.

It never executes installation, grants permissions, publishes a package, reads
credentials, contacts a cloud service, or claims to be the daemon lifecycle authority.
The server binds only to loopback, caps request bodies at 32 KiB, caps JSON state/feed
files at 4 MiB and 500 display rows, and uses a single request loop to keep this local implementation's
resource use predictable.

## Run

Requires Python 3.10+ and no third-party packages.

```sh
cd /Users/zhuzhe/Workspace/vibapp/artifacts/product-platform/client
python3 app.py
```

Open <http://127.0.0.1:8876>. Runtime state is created under `runtime/`; delete that
ignored runtime directory to return to the seed fixture.

To preview real local feed files later, see [`interfaces/README.md`](interfaces/README.md).

## Verify

```sh
python3 -m unittest discover -s tests -v
```

The tests start only an ephemeral loopback server and cover health, static UI,
verification states, requirement handoff, a verified install intent, and rejection of
an ineligible install intent.
