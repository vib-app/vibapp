# VibApp product platform

Status: **locally runnable / HOLD**

This folder closes one intentionally small product loop without claiming a
production release:

```text
Need -> Registry match or Builder request -> bounded Rust/WASI build
     -> independent verifier -> quarantine -> Launcher preview
```

The website, client, Registry and Builder are all real local artifacts. Nothing
here publishes, installs, grants permissions, or promotes itself to formal
Stage 0 acceptance.

## Start the visible previews

Website:

```sh
cd artifacts/product-platform/website
npm run dev
```

Open `http://localhost:3000`.

Client with real local feeds:

```sh
python3 artifacts/product-platform/integrate.py
python3 artifacts/product-platform/client/app.py \
  --port 8876 \
  --builder-feed artifacts/product-platform/integration/builder-feed.json \
  --registry-feed artifacts/product-platform/integration/registry-feed.json
```

Open `http://127.0.0.1:8876`.

## Re-run the bounded acceptance slice

```sh
python3 artifacts/product-platform/builder/run_demo.py
python3 -m unittest discover -s artifacts/product-platform/registry/tests -v
python3 -m unittest discover -s artifacts/product-platform/client/tests -v
cd artifacts/product-platform/website && npm run build
```

The Builder demo must quarantine the good package, reject the tampered package,
and keep `accepted`, `published`, and `installed` false. The integration adapter
keeps the same authority denial when it prepares client feeds.
