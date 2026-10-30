# Self-Serve quota image candidate

Status: built and tested; not switched into production. The five Self-Serve
purchase gates remain closed. This record does not approve or enforce catalog
rows by itself.

The clone/campaign implementation is canonical commit
`2a600fa2bff0e40ef0f67b20235a86d4b811a962`. The candidate layers the nine quota
runtime files onto the actual running API/worker image. It preserves all 210
base layers and the running campaign-statistics visibility behavior. Do not
replace it with files copied from the server checkout: seven of the eight
overlapping checkout files differ from those in the running image.

Validation:

- All 14 focused unit tests pass inside the candidate image with networking
  disabled and an example test environment.
- The PostgreSQL 17.6 rehearsal checks concurrent clone caps 1/3/5/10/15,
  campaign caps 0/1/3/5/10, pending/failed clone reservations, released slots,
  legacy uncapped behavior, and atomic campaign start/resume claims.
- A guard-disabled negative control fails the zero-campaign-cap assertion.
- Both running ORM class definitions exactly match the rehearsal source.
- All 38 active Compose files exist. Their service fingerprints match the
  running API/worker, and adding the proposed override changes only their image
  fields. Never replace the active Compose stack with this override alone.

To reproduce the 18 database checks in an isolated checkout with application
Python dependencies installed, copy `api/.env.test.example` to `api/.env.test`
if that file is absent, then run:

```sh
python api/tests/subscription_quota_rehearsal.py
```

The script refuses any database URL that differs from the committed example.
It creates its own disposable database on a random localhost-only port,
leaves the test environment file unchanged, and removes its container afterward. It
makes no provider calls and does not run platform migrations. Add
`--negative-control` to verify that the test catches a disabled campaign guard;
that run must fail.

Before a live switch, repeat both immutable image IDs and the nine-file source
checks, obtain the owner-run git status/log preflight, preserve all staged and
unstaged server work through the required incoming-branch fast-forward path,
verify the no-call window, and obtain explicit deployment approval. A restart
can drop calls and runs the image's existing startup migrations.

The staged build package is on the host at
`/root/sysevo-release-candidates/smb-quotas-20261030-aba8eafc`; its
`compose.smb-quotas.json` was compared against the active stack. Revalidate that
state immediately before applying. The image in this record is local to that
host; do not pull a similarly named public image.
