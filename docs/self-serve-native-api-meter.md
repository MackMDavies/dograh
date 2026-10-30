# Native Self-Serve API/MCP meter

Native auth hook is deployed in API and worker as image ba0623ff6614. Its database/Edge prerequisites are
live from Sysevo main 223fa55cfe: migrations 20270690000839 and 20270690000876,
subscription-api-meter v1 and the Sysevo API (legacy slug xpand-api) v96.
All deployed source files and database bodies match reviewed source. Live
internal probes verify authentication and existing Enterprise REST/MCP
pass-through without consuming allowance or changing customer data.

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

The shared journal now distinguishes platform UUID keys from native integer
keys and records platform/voice_api/voice_mcp provenance. The public REST RPC
signature is preserved; both wrappers call one private atomic core, retaining
paid invoice, terms, current period, feature/capability and replay checks.
Native MCP also checks mcp_enabled. The bridge passed 36 targeted PostgreSQL
checks across all five tiers, plus a guard-disabled negative control. The
unchanged paid setup passed 174 checks before source-matched fixture reuse.

The deployed image preserves all 211 prior filesystem layers and all fourteen
existing voice guard files. Both services match the 17 expected runtime hashes.
The switch preserved the full Compose settings and existing dirty server work.
71 offline candidate checks passed, including the 3 existing MCP auth tests.
Public backend health is verified. See deploy/releases/native-api-20261030/DEPLOYED.md.

The four new caps remain proposed. Paid Self-Serve runtime acceptance and
remaining catalog enforcement are not complete; MFA and real payment are not ready.
