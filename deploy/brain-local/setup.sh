#!/usr/bin/env bash
# One-time setup of the local brain (see docker-compose.yml). Idempotent for the secrets: an
# existing ~/.config/sniper-brain/brain.env is kept. Prints no secret.
#   deploy/brain-local/setup.sh                 # secrets + stack up
#   deploy/brain-local/setup.sh <dump.fc>       # ... then restore a pg_dump -Fc of public
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HOME/.config/sniper-brain"
mkdir -p "$CONF" && chmod 700 "$CONF"

if [ ! -f "$CONF/brain.env" ]; then
  python3 - "$CONF" <<'PY'
import base64, hashlib, hmac, json, os, secrets, sys
conf = sys.argv[1]
def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def jwt(secret, role):
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"role": role, "iss": "sniper-brain-local", "iat": 1791936000,
                           "exp": 4102444800}).encode())
    sig = b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"
pg, auth, js = (secrets.token_urlsafe(32) for _ in range(3))
with open(os.path.join(conf, "brain.env"), "w") as f:
    f.write(f"POSTGRES_PASSWORD={pg}\nAUTHENTICATOR_PASSWORD={auth}\nPGRST_JWT_SECRET={js}\n"
            f"PGRST_DB_URI=postgres://authenticator:{auth}@db:5432/postgres\n")
with open(os.path.join(conf, "client.env"), "w") as f:
    f.write("SUPABASE_URL=http://127.0.0.1:54321\n"
            f"SUPABASE_ANON_KEY={jwt(js, 'anon')}\nSUPABASE_SERVICE_KEY={jwt(js, 'service_role')}\n")
for name in ("brain.env", "client.env"):
    os.chmod(os.path.join(conf, name), 0o600)
print("secrets written to", conf)
PY
fi

docker compose -f "$HERE/docker-compose.yml" up -d --wait
DB="$(docker compose -f "$HERE/docker-compose.yml" ps -q db)"

if [ "${1:-}" != "" ]; then
  DUMP="$(readlink -f "$1")"
  docker cp "$DUMP" "$DB:/tmp/restore.dump"
  # public already exists; its cloud schema ACL is replaced by init/00_roles.sh. Keep object ACLs.
  RESTORE_LIST="$(mktemp)"
  trap 'rm -f "$RESTORE_LIST"' EXIT
  docker exec "$DB" pg_restore --list /tmp/restore.dump > "$RESTORE_LIST"
  awk '!($4 == "SCHEMA" && $5 == "-" && $6 == "public") &&
       !($4 == "ACL" && $5 == "-" && $6 == "SCHEMA" && $7 == "public")' \
    "$RESTORE_LIST" > "$RESTORE_LIST.filtered"
  mv "$RESTORE_LIST.filtered" "$RESTORE_LIST"
  docker cp "$RESTORE_LIST" "$DB:/tmp/restore.list"
  docker exec "$DB" pg_restore -U postgres -d postgres --no-owner --role=postgres \
    --single-transaction --exit-on-error --use-list=/tmp/restore.list /tmp/restore.dump
  docker exec "$DB" rm -f /tmp/restore.dump /tmp/restore.list
  RESTORED_MEMORIES="$(docker exec "$DB" psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
    -Atc 'SELECT count(*) FROM public.memories;')"
  if ! [[ "$RESTORED_MEMORIES" =~ ^[1-9][0-9]*$ ]]; then
    echo "Restore verification failed: public.memories must contain restored data" >&2
    exit 1
  fi
  docker exec "$DB" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c "
    GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
    GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO service_role;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO service_role;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO service_role;
    ANALYZE;" >/dev/null
  docker exec "$DB" psql -U postgres -d postgres -c "NOTIFY pgrst, 'reload schema';" >/dev/null
fi
curl -fsS http://127.0.0.1:54321/health
