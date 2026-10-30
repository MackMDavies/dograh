# Native API monthly allowance release

Candidate only. Canonical runtime code: 92184b1. This artifact-only incoming commit adds no API source, migrations, dependencies or environment variables on the dirty server.

Base is the actual running Self-Serve voice guard image 6feec492957e; candidate ba0623ff6614 preserves its 211 filesystem layers and all fourteen previous runtime file hashes. Only auth/depends.py, mcp_server/auth.py and new services/subscription_api_meter.py change.

71 offline checks pass: 14 quotas, 36 call budget, 18 native meter/auth and 3 MCP authentication. No provider calls or customer writes. Database shared-counter bridge and authenticated Edge endpoint are already deployed. Browser JWT access and explicit Enterprise postpaid behavior are preserved. Self-Serve native REST/MCP use the same account/period counter as platform requests.

Before apply: recheck images and all 17 source hashes, complete effective Compose settings, existing dirty state, and zero active voice calls. Apply only the two image fields with the complete existing stack, then verify health, source parity and runtime auth. Rollback uses the previous complete stack and base image. Approvals, enforcement acceptance, release flags and public signup remain unchanged.
