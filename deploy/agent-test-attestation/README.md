# Attestation image release

This patch extends the inspected `dograh-local/dialer-release:b59e17c` image.
It preserves its dialer modules and legacy production repairs. The dirty host
checkout is not a build context. Source hashes in `patch_runtime.py` must match
before any image file is changed; a different base requires a fresh review.

1. Push this canonical commit to the production repository's incoming branch.
2. Create an isolated clean worktree and fast-forward it to incoming. Keep the
   existing production working tree and compose files intact.
3. Build with this Dockerfile, the immutable current API image ID as `BASE_IMAGE`,
   and the clean commit SHA as `SOURCE_REVISION`.
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
