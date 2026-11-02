# Local brain (replaces the Supabase project)

2026-10-14: the Supabase project `nqtebcfwutftgaylmaon` was restricted for `exceed_egress_quota`
(HTTP 402 on every REST call). The operator chose a local migration over a plan upgrade.

- Stack: Postgres 17 + pgvector 0.8 (`db`), PostgREST (`rest`), nginx (`gateway`) serving PostgREST
  under `/rest/v1` like Supabase. Ports: REST `127.0.0.1:54321`, Postgres `127.0.0.1:54329`.
- Clients are unchanged: `.env` has `SUPABASE_URL=http://127.0.0.1:54321` and locally minted
  `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_KEY` (HS256, roles `anon` / `service_role`).
- Secrets: `~/.config/sniper-brain/brain.env` (server) and `client.env` (the three `.env` values).
  The previous cloud values are in `~/.config/sniper-brain/cloud.env`.
- Data: docker volume `sniper-brain_sniper-brain-pg`. Restart policy `unless-stopped`; Docker
  starts at boot.
- Migrated with `pg_dump --schema=public -Fc` from Supabase. All 23 public tables matched the
  cloud row counts, with 22 functions, 22 policies and 77 indexes.
- Backups: `backup.sh` nightly at 04:17 (cron) to `~/.local/share/sniper-brain/backups`, keeps 7.

Operate:

    docker compose -f deploy/brain-local/docker-compose.yml ps
    docker compose -f deploy/brain-local/docker-compose.yml up -d      # start / repair
    docker exec -it sniper-brain-db-1 psql -U postgres                   # SQL
    python3 scripts/brain_health.py                                      # "supabase brain reachable"

Restore a backup into a fresh volume: stop the stack, `docker volume rm sniper-brain_sniper-brain-pg`,
then `deploy/brain-local/setup.sh <dump>` (secrets are kept).

Restore runs in one transaction and stops on unexpected errors. Setup omits the existing public
schema definition and its cloud schema ACL, preserves object ACLs, and requires nonempty restored
`public.memories` before reporting gateway health.

Rollback to the cloud (only once its quota resets or the plan changes): copy the three values from
`~/.config/sniper-brain/cloud.env` back into `.env`. Writes made locally since 2026-10-14 are not in
the cloud.

Not covered: the `mcp__supabase__*` tools still point at the cloud project, so brain lookups through
them read the stale cloud copy (or fail while it is restricted); use `scripts/brain_cli.py` or
`psql` instead. The running containers mount `init/` and `nginx.conf` from the main checkout.
