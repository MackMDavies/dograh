# Offline Self-Serve call-budget SDK probe

`python3 scripts/verify_subscription_budget_sdk.py --bundle` writes a standalone
Python script to stdout, embedding this checkout's call-budget controller. Run
that script in a disposable container using the exact inspected running image,
with networking disabled, no production mounts or credentials, a read-only root,
and a writable temporary directory. Do not run it inside the live API service.
The container needs a 30-second outer timeout; the probe has a 25-second alarm.

The probe uses the actual installed Pipecat PipelineTask and a processor blocked
on a response that never arrives, with heartbeats disabled. It requires a real
CancelFrame to arrive within one second of starting a 400ms funded deadline.
The result reports provider_calls=0 and production_modified=false. It establishes
SDK cancellation, not carrier hangup or successful live billing.

On 30 October 2026 this exercise passed against running image
`sha256:691d4d66e71583042e5a21227b16ded150327076129d5a6f76dc387e2244571a`:
CancelFrame was processed after 0.402 seconds. Recheck the image before release;
a different live base requires a fresh rehearsal.
