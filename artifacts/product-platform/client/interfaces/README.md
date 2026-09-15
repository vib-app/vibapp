# Local file bridge

This is an experimental integration seam, not an accepted VibApp contract.

The client never calls Builder or Registry over the network. A producer may atomically
replace a local JSON feed and then launch the client with an explicit read-only path:

```sh
python3 app.py --builder-feed /absolute/path/builder-feed.json \
  --registry-feed /absolute/path/registry-feed.json
```

The Builder feed is either a JSON array or `{ "jobs": [...] }`; every item must have a
string `job_id`. The Registry feed is either a JSON array or `{ "apps": [...] }`; every
item must have a string `app_id`. Their display fields follow the examples in
`fixtures/client-state.json`. Invalid or missing feeds do not crash the client: the UI
shows the feed error and leaves that collection empty.

The client writes two append-only local handoff files below the selected `--data-dir`:

- `outbox/need-requests.jsonl` contains local-private requirement requests;
- `outbox/install-intents.jsonl` contains digest-bound requests with
  `installation_performed=false` and `next_authority=future-daemon`.

Writing an install intent is deliberately not an install, permission grant, package
promotion, Registry publication, or daemon lifecycle transition.
