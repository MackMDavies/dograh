/**
 * Hard ceiling on simultaneous calls, per organisation.
 *
 * Mirrors the backend's `MAX_SYSTEM_CONCURRENCY` (api/constants.py), which is
 * the real enforcement point — this copy exists so the form can reject an
 * out-of-range value before a round trip, not as the source of truth.
 *
 * 20 is the measured capacity of the current box, not a round number: 4 cores
 * shared with the other services, FASTAPI_WORKERS=2 (two event loops carry every
 * voice pipeline), ~2,096 MiB of container headroom. Raising it is a hardware
 * decision — see the note in api/constants.py.
 */
export const MAX_SYSTEM_CONCURRENCY = 20;
