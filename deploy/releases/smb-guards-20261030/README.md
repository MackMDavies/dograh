# Combined Self-Serve voice guards

Tested candidate; not deployed. Combines the canonical clone/campaign guards
with funded call reservation, independent deadlines and completion settlement.
The fourteen runtime files are a conflict-free merge into actual running source,
not a replacement from the dirty host checkout. Manifest hashes identify them.
The host build package is
`/root/sysevo-release-candidates/smb-guards-20261030-178ccd9`.

The candidate retains all 210 layers of the inspected running image. Inside it,
14 quota tests and 36 budget/runtime tests pass with networking disabled. The
actual Pipecat SDK processes CancelFrame after 0.402 seconds with a blocked
processor and no heartbeats. This does not prove carrier hangup, a real paid
subscription, or end-to-end account activation. No provider call was placed.

Both running services still match the base image and all fourteen source paths.
Their 38 active Compose files are present, the environment matches the running
containers, and adding the proposed override changes only API/worker images.
Never use this override without the full active stack. Existing server work,
Compose files, migrations, environment and incoming refs must be preserved.

The owner supplied the required git status/log preflight at f919ac3. Three
missing Enterprise markers were inserted from fresh subscription truth and
verified after commit; no existing values, subscriptions or limits changed.

Before any switch, merge the reviewed canonical code, apply the single Sysevo
escrow migration and named Edge functions through their approval path, prepare
an artifact-only incoming fast-forward from the actual server HEAD, recheck live
source/image/Compose state, and verify a no-call window. Obtain explicit switch
approval because recreation interrupts live calls. Afterward verify route
health, actual model restriction, funded stop, completion/recovery and billing.
Public Self-Serve signup stays closed until all launch requirements and the
paid payment-to-account-access/onboarding pilot pass.
