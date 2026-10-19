# Attestation image release

This patch extends the inspected `dograh-local/dialer-release:b59e17c` image.
It preserves its dialer modules and legacy production repairs. The dirty host
checkout is not a build context. Source hashes in `patch_runtime.py` must match
before any image file is changed; a different base requires a fresh review.

1. Push this canonical commit to the production repository's incoming branch.
2. Create an isolated clean worktree and fast-forward it to incoming. Keep the
   existing production working tree and compose files intact.
3. Assign a dedicated local tag to the verified immutable current API image ID.
   Build with that tag as `BASE_IMAGE` and the clean commit SHA as
   `SOURCE_REVISION`. Docker's `FROM` requires an image reference, not a bare
   configuration ID. The build installs files as root, then restores the inspected
   base image's `dograh` runtime user.
4. Run `verify_image.py` inside the candidate image with networking disabled.
   It uses synthetic fixtures and mocks before any quota, model or database work.
5. Verify that the existing API/worker environment, volumes and compose services
   are preserved. Confirm zero active calls immediately before switching images.
6. Recreate only API and worker with an additional image-only compose override.
   Retain the previous image and compose invocation for rollback.
7. Verify internal/public health, authenticated sandbox capabilities, a signed
   sandbox session, and an exact candidate regression/publication cycle. A healthy
   container or capability flag alone does not prove the last two checks.

Deploy Sysevo's candidate promotion guards first. Existing sandbox sessions made
before this release must be recreated. No provider credentials are changed, no
client endpoint is rewired in place, and no customer messages are sent by this
release procedure.

## Private PCM voice testing (source development)

`api/services/pipecat/sandbox_audio_transport.py` supplies native Pipecat audio
input/output for headless test sessions. It accepts mono 16-bit, 16 kHz WAV
fixtures lasting 20 ms to 30 seconds, paces input and output, bounds captured PCM
by the reserved session duration, and stops a session at its duration limit
(maximum 120 seconds). Captured WAV bytes stay in memory; this transport does not
fetch URLs, dial numbers or publish recordings.

The standard voice pipeline rejects this transport unless its WebRTC voice run
has a valid server-signed sandbox binding. Text sessions and unverified carrier
modes cannot be repurposed as PCM test runs. Sandbox runs skip tenant integration
collectors, supervisor access, client call analytics, campaign failure accounting
and disposition-taxonomy updates. Candidate voice flags use the pinned definition.
Normal call lifecycle tests remain part of regression coverage.

This is not an advertised or deployed real-voice test capability. The remaining
work includes durable session/job orchestration, atomic execution and voice-minute
admission, private retained evidence and access controls, scripted response/turn
evaluation, configured transport effects, and wiring the Sysevo worker and UI.
Local tests use synthetic PCM and mocked external speech services; they do not
prove recognition, pronunciation or real provider quality. The existing live
attestation image does not contain these new pipeline changes.

Source verification on 19 October 2026: 79 focused runtime checks, 40 pipeline /
session-service / versioning checks, and ten database-backed text-session checks
passed. The PCM adapter also ran through the standard pipeline lifecycle against
the local test database. Temporarily disabling the integration isolation and
audio deadline guards made their checks fail. Speech-provider calls were mocked.

The SYSEVO vendored Dograh tree currently lacks the earlier sandbox helper
modules. Reconciling that mirror with the canonical fork is still outstanding;
copying only the new pipeline file would leave unresolved imports.
