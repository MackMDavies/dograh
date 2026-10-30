# Deployed 30 October 2026

API and worker run `dograh-local/native-api:candidate-20261030-92184b1`, immutable image `sha256:ba0623ff661466c67e1f1ccd52c4535b502aef65932dcaca0d2e5709d068c782`. Server artifact-only HEAD is `d9cc7ca50c15cfbdaff39e98817b1f1a38e37a14`.

The switch verified both services' 17 runtime source hashes, complete configuration preservation, and unchanged existing staged/unstaged server work. All 211 base filesystem layers and the previous fourteen voice-guard files are preserved. The full 39-file existing Compose stack was retained and the native API image override appended. Two pre-switch voice checks reported no live heartbeats, recent concurrency slots, running audio workflows or recent starting audio workflows; the recent rep dialer check was also zero. Public `/api/v1/health` returned status ok after release.

71 offline checks passed before the switch: quotas 14, call budget 36, native meter/auth 18, MCP auth 3. Database and named Edge prerequisites are deployed from Sysevo main 223fa55cfe. Security advisor findings are unchanged; the two new meter functions do not appear in the warnings.

Live runtime probes also verified the deployed helper for both REST and MCP, existing Enterprise pass-through, mandatory service-secret authentication, rejection without that secret, and actual native-auth rejection of an invalid key. No raw API key was read or disclosed and no customer data changed.

This proves deployment and health, not paid Self-Serve acceptance. The four engineer-delegated new API caps still need audited approval; tier enforcement acceptance, outstanding release checks and a real Stripe signup/account/onboarding pilot remain. No provider calls were placed and signups remain closed.

Rollback anchor: previous complete Compose stack and image `sha256:6feec492957e487893a54c8d13e2d1168c0a437082a8b72706d7f64a998180f6`. Never substitute an older voice image.
