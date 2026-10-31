# Provider compatibility-route release

Canonical fix: `818ef23d95841ab3844591fe0820bb5b4d824b6c`. Applies the same paid number slot to the legacy inline-number API. The earlier multi-config cap and all inherited voice changes are retained.

Artifact-only incoming fast-forward; no server source files are overwritten. Prepare builds from the actual running API image, retains its layers/configuration, runs nine offline checks and imports the app with no network. Apply requires unchanged services/configuration and no audio activity, changes API only and verifies worker/environment/mounts/Git preservation. A failed switch attempts controlled rollback.

Commands: `python3 release.py prepare`, then `python3 release.py apply`. Full provider onboarding and ownership verification remain separate.
