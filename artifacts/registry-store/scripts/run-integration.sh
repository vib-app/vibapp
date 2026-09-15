#!/usr/bin/env bash
set -euo pipefail

image='pgvector/pgvector:pg16'
container="vibapp-registry-store-test-$$"
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "SKIP: required pre-existing image $image is unavailable; this script will not pull it" >&2
  exit 3
fi

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker run -d --pull=never --name "$container" \
  --network none --memory 1g --cpus 2 --pids-limit 64 \
  --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=512m,uid=999,gid=999,mode=0700 \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  "$image" >/dev/null

ready='false'
for _ in $(seq 1 40); do
  # The official entrypoint briefly starts a bootstrap server, stops it, and
  # then starts the final server. pg_isready alone can succeed in that narrow
  # bootstrap window and race the first migration. Wait for the entrypoint's
  # final-init marker as well as a live connection.
  if docker logs "$container" 2>&1 \
      | grep -Fq 'PostgreSQL init process complete; ready for start up.' \
    && docker exec "$container" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
    ready='true'
    break
  fi
  sleep 0.25
done
if [[ "$ready" != 'true' ]]; then
  echo 'FAIL: ephemeral PostgreSQL did not become ready' >&2
  exit 1
fi

for migration in "$root_dir"/migrations/*.sql; do
  docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres < "$migration"
done

docker exec -i "$container" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres < "$root_dir/tests/integration.sql"
