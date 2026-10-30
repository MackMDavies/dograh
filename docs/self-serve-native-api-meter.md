# Native Self-Serve API/MCP meter — local component

Not deployed. This component depends on a new authenticated
subscription-api-meter endpoint and shared-counter database bridge which are
not implemented yet. Do not ship the auth hook before those dependencies.

The normal native API-key authentication path still validates key expiry,
revocation and owner existence. It then binds monthly usage to the persisted
key organization and its provider account UUID. User-selected organizations,
raw keys and caller request IDs are not forwarded. A fresh server UUID identifies
each operation. Native MCP passes an explicit boolean flag for its independent
plan capability check; ordinary browser JWT authentication never enters this
meter. No additional environment variable is needed: the service endpoint derives
from the existing pre-call URL and authenticates with the existing shared secret.

A successful Self-Serve admission skips native postpaid request accounting.
Legacy/Enterprise replies preserve the established counter; explicit legacy
markers retain outage behavior. Unknown/Self-Serve outages and malformed replies
fail closed. Missing/deleted org bindings and malformed platform UUIDs cannot
become legacy bypasses. Exhaustion maps to 429, entitlement denial 403 and service
or configuration failure 503, with private errors removed.

Eighteen standard-library tests pass. They cover key-bound account identity,
request UUID uniqueness, MCP flag validation, OSS pass-through, legacy behavior,
missing/malformed identities, errors and quota denials. Tests execute the actual
API auth, MCP auth and browser JWT branches with synthetic dependencies. Before
the hook was added, the real auth function entered the postpaid counter for the
synthetic Self-Serve request and admitted the rejected request; three regressions
failed. Existing pytest MCP auth expectations were updated for the new internal
keyword, but that dependency-heavy suite has not been run in this local shell.

Next, preserve and extend the existing atomic Supabase request journal rather
than inventing platform key UUIDs for native integer key IDs. Current live
consume_subscription_api_request body SHA256 is
b08753047d3c35b32c152c48fc75d22ae8794dd8badd98c5636c550168647815;
its exact definition is captured at
/private/tmp/sysevo-live-api-meter-definition-20261030.sql. Snapshot and hash-guard
existing source before replacing it. Keep current paid receipt, frozen terms,
plan/feature approval, period expiry, account locking and replay checks; count
both surfaces against the same account/period. Native MCP must also honor the
mcp_enabled flag. Preserve the public REST RPC signature and legacy behavior.
Prove concurrent final-request contention, replay binding, both key realms and
all five tiers before accepting enforcement. The platform-side MCP fix and cap
staging are in the Sysevo branch fix/subscription-api-shared-meter (de91964325).
