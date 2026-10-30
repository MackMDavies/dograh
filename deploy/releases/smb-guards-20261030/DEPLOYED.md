# Production switch — 30 October 2026

API and worker now run `dograh-local/smb-guards:candidate-20261030-178ccd9`,
image `sha256:6feec492957e487893a54c8d13e2d1168c0a437082a8b72706d7f64a998180f6`.
All fourteen runtime source hashes match the manifest in both services.

The owner supplied the git preflight. The server fast-forwarded from f919ac3
to artifact-only incoming commit `8b016c209b0415b82274b4312a52871e15495edb`;
all seven added paths are release artifacts/tests. Existing staged/unstaged
work and non-image service settings were verified unchanged. The original
38-file Compose stack is retained with the new image override appended.

Before each switch, live heartbeats, current slots, unfinished live-audio runs
and recent initializations were zero. The 135 historical unfinished rows were
all textchat, latest 18 October; no historical rows were changed. There were
no recent rep-dialer calls. One campaign was in running state without live audio.

The first attempt restored the original image after a settings-comparison
rejection; external health and both original service states were verified.
The retry normalized unordered bind lists, retaining checks for mount values,
resource limits, commands and environment. Five checks show ordering is accepted
and changed values rejected. The retry completed with all settings preserved.

Public health returns status ok. API startup completed, the worker loop started,
and neither service is restarting; startup logs contain no tracebacks. The safe
receipt is on the host under the build package as deployment-receipt.json.

Sysevo migration 20270690000802, subscription-call-budget v1 and wallet-debit
v95 are live and source-verified. Authenticated synthetic probes deny missing
funding and premium Max before reservation. No provider call or paid signup was
performed. The 50 isolated image tests and SDK cancellation probe are preparation
evidence, not proof of carrier hangup or a production paid account lifecycle.
Self-Serve purchase gates remain closed pending full launch acceptance.
