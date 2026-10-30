# Native Self-Serve API/MCP meter — local component

Native auth hook is not deployed yet. Its database/Edge prerequisites are now
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

Next: inspect the actual running native auth/MCP source before layering these
three runtime files onto the current voice image. Preserve the existing call
budget/clone/campaign release: base must be the RUNNING image, currently
sha256:6feec492957e487893a54c8d13e2d1168c0a437082a8b72706d7f64a998180f6.
Use the full current Compose stack, normalize unordered bind comparisons and
retain all settings checks. Verify no active audio and use the approved incoming
artifact-only fast-forward path. After deploying, verify actual auth/helper
behavior and source parity before accepting API enforcement evidence.
The four new caps remain proposed; MFA and paid signup acceptance are not ready.
