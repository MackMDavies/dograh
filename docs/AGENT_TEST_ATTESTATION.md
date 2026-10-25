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

## Private voice rehearsal source (19 October 2026)

`VoiceControlClient` talks only to the fixed internal Supabase voice controller.
It bounds response sizes, refuses redirects, checks fixture hashes and WAV
durations, and independently compares stored output hash/size/PCM measurements.
Claims are never automatically retried. Provider bodies and credentials are not
included in its fixed error codes. Missing saved scripts remain missing.

`run_voice_script` plays the exact saved fixture order after native pipeline
readiness. It supports an agent opening, caller-first flows, response waits and
deliberate interruptions. It rejects changed fixtures, missed interruption
windows, early pipeline closure, missing speech and excess duration. The private
output writer records first audio synchronously, avoiding asynchronous observer
queue delays. Per-input timing refers to native speech segments; overlapping
speech can produce a negative interval and must not count as a low-latency pass.
The result explicitly says `quality_evaluated: false`.

Private execution annotations now persist with initial workflow-run creation,
before a model or action can start. Customer history, daily reports, CSV exports,
usage/model breakdowns and public access tokens exclude private test runs. An
authenticated internal transcript reader retains explicit access. Standard S3
links reject private runs even for superusers; private audio belongs in the
dedicated access-controlled test bucket. Background completion and sandbox
processing can still read the private run.

Verification used the local test database, synthetic provider boundaries and the
actual paced PCM pipeline. The broader sandbox regression passed 143 checks; a
final privacy/rehearsal run passed 20 checks after two additional closure/timeout
cases were added. Disabling customer visibility filtering made both privacy DB
tests fail, and restoring it returned them to passing. No real provider, client
destination or production database was used. This source is not a runtime
activation: authenticated durable voice dispatch, one-time job orchestration,
result ingestion and live speech/provider evaluation remain to be wired and
verified before advertising automated voice execution.

The final media/client/rehearsal run passed 91 checks, including six invalid
duration cases and an opening with speaking events but no actual PCM. The latter
must time out before any caller fixture plays.

### Interruption timing follow-up (local source, 25 October 2026)

The immutable audio-evidence sidecar can include timing v2 when an output
interruption or write cancellation was observed. It retains bounded native
interruption events, active caller/response identities and the exact cancelled
PCM tail. Submission, cancellation, nominal packet end and bot stop remain
separate. The retained WAV is unchanged; a cancelled packet's nominal end does
not prove completed playback. Existing timing v1 and voice-status v1 stay
readable. Missing evidence does not establish an interruption or speech verdict.

Timing v3 includes bounded changes in the existing callback's shutdown,
queued-speech, node-forbids-interruption and unmuted states during caller
playback. Samples bind to the caller timing and contain no node IDs or
conversation text. The record explicitly leaves the first-bot-complete and
function-call mute strategies unobserved. This telemetry does not change mute
behavior; a complete v3 record requires at least one callback sample per caller,
and the data cannot alone decide whether an interruption was correct.

Input submission can yield to downstream processing before its coroutine
resumes. The private input records the active caller before yielding and marks
timing invalid if submission fails. A deterministic race test failed before this
ordering fix. The failed-submission check prevents a prepared frame from becoming
complete evidence.

The final 695-check Linux/Python 3.13.17 selection passed against the pinned
Pipecat source. It includes actual paced output cancellation and ordinary and
interrupted scripts through job persistence and the authenticated reader. The
speaker/VAD stand-ins are synthetic; no speech provider, carrier or production
service was used. Engine input/result retention has separate PostgreSQL checks.

These observations do not score configured interruption policy. Initial-opening
muting, queued transition/tool speech, per-node `allow_interrupt` and provider
turn strategies must inform the eventual criteria. Purpose-authored coverage,
speech calibration and authenticated release acceptance remain unfinished. This
local source is not a production rollout.

## Durable private voice jobs and reports (local source, 19 October 2026)

The authenticated internal voice routes accept only a reserved session UUID and
its private lease in a POST body. A workflow/org lookup precedes controller or
configuration access. No recipient, URL, organization override or provider option
is accepted. The queue must advertise a fresh worker protocol heartbeat before a
job can be admitted.

ARQ stores only the nonsecret session UUID in its logged arguments. Its separate
private context has a fixed expiry and binds the lease, original organization,
workflow and requesting actor. A global three-slot Redis guard spans processes;
owner-safe release and conservative expiry quarantine uncertain cleanup. A job
claims its reservation once. Redelivery of claimed or terminal work cannot create
another paid pipeline.

The native runtime rechecks the original organization and provider owner before
resolving provider credentials. Execution registers the private runtime row with
the controller before starting a pipeline. The output is uploaded/read back and
its report is saved immutably before completion is acknowledged. The private
report binds the organization, parent run, voice session, exact native definition,
configuration hashes, elapsed duration and sealed audio hash/size. Moving the
workflow to another organization does not expose the earlier report. Report
responses contain the bounded script/timing evidence with quality explicitly
unevaluated, and never a transcript, raw tool arguments or provider secrets.

The local native regression passed 212 checks. Real Redis/ARQ connections and
local SQL rows verified durable dispatch, duplicate/retry protection, capacity,
private persistence and the native pipeline path. Speech recognition, model,
synthesis and controller boundaries in these integration checks were synthetic.
Removing the runtime-owner check caused its expected regression failure; restoring
it passed 29 job/pipeline checks before the organization report binding was added.
No production database, provider call, carrier or client destination was used.

These are local source changes, not runtime activation. Supabase-to-native
admission/checkpoint orchestration, saved voice scenario/fixture generation,
main-report ingestion, independent quality evaluation and credential-backed live
provider verification remain required. Runtime activation still requires a fresh
source/image reconciliation and the authorized quiet window for the API/worker.


The local `/workflow/test-voice/capabilities` probe requires both user and internal
sandbox authentication. It reports the protocol, fresh worker heartbeat, shared
concurrency limit, duration envelope and saved-script requirement, with independent
quality evaluation explicitly false. Redis pool/read failures are bounded and
redacted; the probe does not enqueue a job or start a provider. A separate real
Redis check verifies ready -> expired heartbeat -> unavailable behaviour.

The readiness/queue/pipeline verification passed 45 checks, including missing user
or internal credentials, invalid authenticated organization IDs, redacted pool
failures, and real Redis heartbeat expiry. Readiness probes never enqueue work.

Private voice admission and report requests now have an eight-second overall
server deadline, including Redis pool acquisition, scoped preflight and queue or
report I/O. This is below the bridge's ten-second network deadline. Cooperative
stalled dependencies are cancelled, and the existing fixed 503 errors disclose
no lease, credential or internal connection text. A timeout does not prove an
already enqueued job was cancelled; immutable session identity, one-time claim
and reserved budgets continue to protect uncertain dispatch outcomes.

All four new cancellation checks failed before the route deadline was added.
After the fix, 68 route, capacity, task and job checks passed; Ruff check/format
also passed for the changed files. These local tests use synthetic dependencies,
do not contact providers and do not activate the production runtime.
