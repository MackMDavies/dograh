-- ============================================================================
-- Phase 2 migration: re-key Dograh orgs from per-user → per-client-account.
-- Runs on the DOGRAH Postgres (api.sysevo.io server), NOT Supabase.
--
-- Current data is 1:1 (each user = one account = one org), so this is a safe
-- PROVIDER_ID RENAME: the org keeps every row (workflows, phone numbers, KB,
-- campaigns, api keys, usage…); only its key changes from
--   supabase_org_{user_id}  →  supabase_org_acct_{client_account_id}
-- After this, the Phase-1 depends.py (which computes supabase_org_acct_{account})
-- resolves to the SAME org, so the account's user(s) see its agents.
--
-- ORDER OF OPERATIONS (see runbook): backup → STOP Dograh (or maintenance) →
-- run this → deploy Phase-1 code → START Dograh. Doing it during downtime avoids
-- an old-code login re-creating a stray per-user org between rename and deploy.
--
-- The script ABORTS if any account has >1 seat/org (would need a data MERGE, not
-- a rename — none exist today; build the merge path when the first multi-seat
-- account appears).
--
-- REGENERATE the mapping right before running (catches users created since it was
-- authored) from Supabase:
--   select 'INSERT INTO _map VALUES ('''||user_id||''','''||client_account_id||''');'
--   from public.client_users;   -- (earliest row per user if a user maps to >1)
-- ============================================================================

BEGIN;

CREATE TEMP TABLE _map (user_id text, account_id text) ON COMMIT DROP;
INSERT INTO _map VALUES
  ('436e51e1-e513-4665-86e1-770d33a36cdc', 'c9c1dbc3-358f-4518-b81f-cbdb3d43297d'),
  ('90653578-5622-4a2e-9ad0-450c18ffea5b', '3afb245c-400d-4178-8558-180d3faa644a'),
  ('90b7495c-28bb-43f7-86db-58fd09295cc5', '99eb7c74-5c9e-4239-ab31-52e166973000'),
  ('94bec25b-e517-40ea-829e-b30a7ae2fdb4', 'af86adab-e9e7-4243-9f66-fe4dab6280c8'),
  ('c9efebc7-62a4-4cbb-bd1e-427a57789180', '0079d85b-c4f1-4b4a-b277-98595c21d259');

-- Guard 1: abort on any multi-seat account (more than one source org per account).
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM (
    SELECT m.account_id
    FROM _map m
    JOIN organizations o ON o.provider_id = 'supabase_org_' || m.user_id
    GROUP BY m.account_id
    HAVING count(*) > 1
  ) x;
  IF n > 0 THEN
    RAISE EXCEPTION 'Aborting: % multi-seat account(s) need a data MERGE, not a rename. Build the merge path first.', n;
  END IF;
END $$;

-- Guard 2: abort if a target key already exists (e.g. Phase-1 deployed too early
-- and auto-created an empty account org) — that case needs a MERGE, not a rename.
DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n
  FROM _map m
  JOIN organizations o ON o.provider_id = 'supabase_org_acct_' || m.account_id;
  IF n > 0 THEN
    RAISE EXCEPTION 'Aborting: % account org(s) already exist — deploy Phase-1 AFTER this migration, or use the merge path.', n;
  END IF;
END $$;

-- DRY RUN — run this SELECT first (outside/instead of the UPDATE) to review:
--   SELECT o.id, o.provider_id AS from_key, 'supabase_org_acct_'||m.account_id AS to_key
--   FROM organizations o JOIN _map m ON o.provider_id = 'supabase_org_'||m.user_id
--   ORDER BY o.id;

-- APPLY the rename.
UPDATE organizations o
SET provider_id = 'supabase_org_acct_' || m.account_id
FROM _map m
WHERE o.provider_id = 'supabase_org_' || m.user_id;

-- Verify inside the transaction before committing:
--   SELECT provider_id FROM organizations WHERE provider_id LIKE 'supabase_org_acct_%';
-- If the count/keys look right:
COMMIT;
-- else: ROLLBACK;
