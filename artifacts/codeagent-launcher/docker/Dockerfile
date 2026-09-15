# Build context is this small directory, never the repository or user home.
ARG BASE_IMAGE=node:22.21.1-bookworm-slim
FROM ${BASE_IMAGE}
ARG CODEX_VERSION=0.153.4
RUN npm install --global --ignore-scripts @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force
# Bound trusted Node relay startup too; entry.mjs supplies the same explicit
# defaults to the Codex wrapper and its cleared tool environment.
ENV NODE_OPTIONS="--v8-pool-size=2" UV_THREADPOOL_SIZE="2"
COPY entry.mjs /opt/vibapp/entry.mjs
RUN node /opt/vibapp/entry.mjs --write-identity
# The tiny trusted relay/watchdog stays UID 0; the provider runs UID 1000.
# Runtime caps are limited to dropping IDs and terminating the provider.
USER 0:0
WORKDIR /work
ENTRYPOINT ["node", "/opt/vibapp/entry.mjs"]
