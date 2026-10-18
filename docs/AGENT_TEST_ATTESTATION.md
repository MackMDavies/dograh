# Agent-test candidate configuration attestation

Sandbox text sessions accept the optional `expected_workflow_hash` field alongside
their existing published or candidate draft version pin. The value uses
`workflow_snapshot_hash` over the graph, workflow configurations, and template
context variables. A mismatch returns HTTP 409 before quota or runtime work starts.
The selected workflow is checked against the authenticated organization before
any version configuration is read.

The runtime attaches a server-generated `workflow_binding` beneath the trusted
`annotations.sysevo_test_sandbox` marker. It includes workflow ID, runtime session
ID, version, version kind, configuration hash, agent test-run ID, and scenario ID.
An HMAC-SHA256 signature covers those fields using the existing internal sandbox
credential. Caller-provided sandbox markers are discarded. No signing credential
or raw configuration is included in the binding.

Before every model/tool pipeline, the runtime re-hashes the bound definition and
verifies the signed identity. Editing a draft in place, moving to a different
version, replaying another session's binding, or altering the annotation fails
before the pipeline starts. Ordinary non-sandbox text sessions are unaffected.

`/workflow/test-sandbox/capabilities` advertises `exact_candidate_hash_pinning`
only with these checks installed. It also lists the three implemented bounded
repair operations. The Sysevo worker requires this capability and independently
verifies the response signature before candidate promotion. Older ordinary
diagnostic clients remain compatible; older sandbox sessions without a signed
binding must be recreated.

The same runtime patch aligns isolated HTTP action classification with Sysevo:
memory save/lookup use run-scoped fixtures, supported Resend/Twilio operations use
their fixed test adapters, and unsupported operations on known providers never
fall through to a generic CRM ledger. Custom webhook rehearsals remain internal;
they do not call the configured client endpoint.

## Verification and rollout

Tests cover configuration drift, identity and signature tampering, replay,
organization isolation, early hash rejection, server-owned response metadata,
adapter routing, and bounded repair idempotence. A shared synthetic HMAC vector
is checked independently by Python and the Sysevo TypeScript verifier.

Deploy Sysevo's fail-closed repair guards before enabling this runtime capability.
The runtime rollout must preserve unrelated production changes, use the canonical
repository and a clean fast-forward deployment, and avoid interrupting live calls.
Do not infer deployment from passing local tests. Verify the capability response,
a signed sandbox session, and a complete candidate regression/publication cycle
against a dedicated Sysevo fixture before calling the new path operational.

On 18 October 2026 the 38 focused runtime checks passed. Disabling the execution
binding check caused the configuration-drift test to fail; restoring it returned
that test to passing. A wider regression run passed 80 non-database checks;
local database tests initially could not open a socket inside the sandbox.
After granting the test process access to the local database and installing its
missing vector extension, all ten text-session integration tests passed. All 32
workflow-versioning integration tests also passed, including actual draft-row
mutation rejection, unchanged publication after rejection, organization
isolation, and idempotent publication of the exact candidate. No LLM, provider,
customer endpoint, or production database was used by these database tests.

The production capability probe did not yet advertise
`exact_candidate_hash_pinning`. The running API image and the dirty host checkout
were different deployment sources; this patch alone is not a production rollout.
