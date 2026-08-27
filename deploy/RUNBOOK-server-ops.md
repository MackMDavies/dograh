# Server Ops Runbook — api.sysevo.io (Hetzner)

Operational checks/tasks that must be run **on the production Hetzner box** (they can't be
done from a dev machine or via the app). Run over SSH as a sudo-capable user.

---

## 1. Activate Docker log rotation

**Why:** `/etc/docker/daemon.json` sets a global `json-file` log-rotation default, but the
Docker daemon only picks it up on restart. Note the app's `docker-compose.yaml` already sets
`max-size: "10m"` per service, so the running stack is **already rotating its logs** — this is
a belt-and-suspenders default for any container started outside compose. **Low urgency.**

**Caveat:** `systemctl restart docker` restarts every container (brief downtime). Do it in a
maintenance window, or just let it apply on the next deploy restart. daemon.json defaults only
apply to containers **created after** the restart.

```bash
# 1. Validate the JSON first (a malformed daemon.json prevents Docker from starting)
sudo python3 -c "import json; json.load(open('/etc/docker/daemon.json')); print('daemon.json OK')"

# 2. Apply — restarts all containers
sudo systemctl restart docker

# 3. Confirm the logging driver
docker info --format 'driver={{.LoggingDriver}}'   # expect: driver=json-file
```

---

## 2. IPv6 / listening-port exposure check

**Why:** after the June 2026 postgres ransomware incident (exposed DB port), confirm nothing
sensitive is publicly reachable — especially on the box's second/IPv6 address. **Read-only, ~30s.**

```bash
# The second / IPv6 address
ip -6 addr

# Every listening TCP socket + owning process
sudo ss -tlnpH | awk '{print $4"  "$6}' | sort -u

# IPv6-bound listeners only
sudo ss -tlnpH | grep -E '^\S+\s+\S+\s+\S+\s+\['
```

**Expected / safe:** only **nginx** on `:80` and `:443` should be publicly reachable.

**Findings to act on:** `postgres` (5432) or `redis` (6379) bound to `::` or the public IPv6
address is an exposure — bind them to `127.0.0.1` / the private interface and/or firewall the
port. `127.0.0.1`- or private-IP-bound listeners are fine.

---

## 3. Post-call wallet-debit reconciliation (context)

Handled in code — see PR #202 (`reconcile_wallet_debits` ARQ cron + `wallet_debit`
idempotency). Deploy order for that change:

1. ✅ **DONE 2026-07-17** — Supabase migration `20260717000000_wallet_debit_idempotency.sql` applied
2. ✅ **DONE 2026-07-17** — `wallet-debit` edge function deployed (v16, idempotency + `agent_call_sessions` sync)
3. ⏳ Deploy Dograh — see section 4 below (server-only)

Because steps 1 & 2 (idempotency) are already live, the Dograh cron is safe to deploy
anytime — a re-fired debit can never double-charge.

To spot-check the gap this closes (run on the **Dograh** Postgres):

```sql
-- Completed wallet calls with billable time but no settlement marker (post-deploy backlog)
SELECT count(*) AS unsettled
FROM workflow_runs
WHERE is_completed = true
  AND api_key_id IS NULL
  AND wallet_debit_settled_at IS NULL
  AND created_at > now() - interval '24 hours';
```

---

## 4. Deploy & rollback — wallet-debit reconciliation (Dograh backend)

Server-only (SSH to the Hetzner box). Restarts `api` + `arq`, so it briefly interrupts
in-flight calls — run in a quiet window. Replace `<user>` and `<repo>` with the real
SSH user and the Dograh checkout path on the server.

### Deploy

```bash
ssh <user>@api.sysevo.io
cd <repo>                        # the Dograh checkout on the server
git pull                         # main: reconcile_wallet_debits cron + alembic d1a2b3c4e5f6
./scripts/migrate.sh             # applies d1a2b3c4e5f6 (adds workflow_runs.wallet_debit_settled_at;
                                 #   backfills ALL existing rows to settled — no retroactive charging)
docker compose restart api arq   # arq worker registers the every-10-min reconcile cron
```

### Verify

```bash
# alembic at the new head
cd <repo> && set -a && source api/.env && set +a
alembic -c api/alembic.ini current      # expect: d1a2b3c4e5f6 (head)

# worker registered + ran the cron (logs a line each run)
docker compose logs --tail=300 arq | grep -i "wallet-reconcile"
```
```sql
-- On the Dograh Postgres: unsettled billable wallet runs (should be low / trend to 0)
SELECT count(*) FROM workflow_runs
WHERE is_completed AND api_key_id IS NULL AND wallet_debit_settled_at IS NULL
  AND created_at > now() - interval '24 hours';
```

### Rollback

The change is additive (one nullable column + a partial index + a cron). To back it out:

```bash
cd <repo>
git log --oneline -5                     # note the current commit to return to later
git checkout <previous-commit>           # revert code (removes the cron + settled-marking)

# Downgrade the alembic migration (migrate.sh only upgrades — downgrade directly).
# Drops wallet_debit_settled_at + its index; the column holds only a settled marker, no billing data.
set -a && source api/.env && set +a
alembic -c api/alembic.ini downgrade f7e8d9c0b1a2

docker compose restart api arq
```

**The Supabase idempotency (migration + edge fn) does NOT need rolling back** — it's
harmless with the old Dograh code (the `wallet_debit` guard is inert when nothing
re-fires). Only if you truly must: redeploy the previous `wallet-debit` edge-function
version and `DROP INDEX wallet_transactions_debit_run_uniq;`.
