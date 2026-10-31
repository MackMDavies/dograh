# Self-Serve assistant billing exemption

Candidate fix, not deployed. A regression on the actual pipeline dispatch block confirmed that a customer-editable syra_voice flag could bypass the prepaid call budget. The same flag also controlled the existing embed concurrency exemption and post-call billing exclusion.

Only the default and alternate workflow IDs in the platform's service-owned syra_voice_config registry may now receive that exemption. The existing internal call-budget endpoint adds a service-authenticated assistant lookup returning only a boolean. The live registry has RLS, no browser write grants and no browser policies. No embed tokens or credentials are returned. Missing, malformed or unavailable verification grants no exemption. Attested test sandboxes keep their existing separate policy.

The pipeline obtains a trusted result once for budgeting, memory and live-call behavior. Embed and completion paths use the same shared Syra predicate. Customer flags and copied workflows cannot authorize free voice; registered platform assistants retain their existing behavior. Reservation, settlement, premium-model restrictions and public native request/response shapes are unchanged. No migration, dependency or new environment variable is required.

The regression failed before the fix. Forty-five focused native tests and thirteen Edge tests pass, including registered alternate voices, copied flags, invalid identities, unavailable policy, worker completion and execution of the actual pipeline assignment. The Edge entry point typechecks. Image verification and deployment are pending.

The running pipeline differs from canonical main. The release therefore applies only the reviewed assistant patch to the captured running source; every unrelated live byte is preserved and a reverse-patch check confirms that. The other two modified runtime modules match the canonical base exactly. Record this intentional runtime variant in the release artifact; do not claim whole-file canonical parity for the pipeline.

Remaining launch work includes voice-minute and concurrency verification, other entitlements, production commercial/operating setup and the real paid signup/onboarding pilot. This change opens no subscriptions and records no catalog approvals or enforcement states.
