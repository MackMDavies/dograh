/**
 * Hard ceiling on simultaneous calls, per organisation.
 *
 * Mirrors the backend's `MAX_SYSTEM_CONCURRENCY` (api/constants.py), which is
 * the real enforcement point — this copy exists so the form can reject an
 * out-of-range value before a round trip, not as the source of truth. Keep the
 * two in step; raising either is a capacity decision (RAM per voice pipeline,
 * CPU for VAD and audio framing), not just a config change.
 */
export const MAX_SYSTEM_CONCURRENCY = 50;
