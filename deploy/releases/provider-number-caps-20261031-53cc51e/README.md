# Provider number-cap release

Canonical fix: `53cc51efdcd8aea47b3af49c33dd8a93edb94b90` in MackMDavies/dograh.
This artifact-only incoming commit preserves the server's existing source and staged changes. Its three payload files match the published canonical commit.

Run `python3 release.py prepare` first. It builds from the actual running API image, verifies inherited layers/configuration, runs eight offline checks and imports the application without network access. It refuses source or environment drift.

Run `python3 release.py apply` only after successful preparation. It rechecks the running services, complete active Compose stack and absence of active calls, then changes only the API image. Environment, mounts, resource settings, worker identity and unrelated Git work are verified afterward. A failed switch attempts a controlled rollback to the saved running base image; `python3 release.py rollback` is also available.

No frontend provider connection is enabled by this release. Provider ownership verification and the onboarding selection/binding flow remain separate work.
