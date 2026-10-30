"""The call sorter: listen to the prospect's side of a dialer call and say who answered.

    prospect leg --(Twilio <Stream track=inbound_track> | SWML tap)--> our websocket
        -> Deepgram (LiveTranscriber, the same client live captions use)
        -> call_sorter_rules.classify_opening on every partial/final transcript
        -> AudioWatcher for silence and the record beep
        -> record_call_stage(call_ref, stage, detail)

Stages it emits (names from the shared CONTRACT, dialer_call_stage_events.stage):

    person        somebody live. detail: name, gatekeeper, rule, confidence.
                  TERMINAL -- the stream is closed straight away; nobody is
                  transcribed once they are talking to the rep.
    machine       a voicemail greeting, or a screener that has given up and offers
                  a message (detail.kind = "voicemail" | "screener_unavailable").
                  Keeps listening, for the beep or a mailbox-full announcement.
    beep          the record tone, heard in the audio (detail.source = "tone").
                  TERMINAL. Only ever after `machine`.
    mailbox_full  "the mailbox is full" / "has not been set up". TERMINAL.
    screener      iPhone Call Screening / Google Call Screen asking who is calling.
                  Keeps listening: it becomes person, machine, or stays.
    ivr           a menu, hold queue, recorded-call notice or carrier intercept.
                  Keeps listening: a menu can end in a person or a mailbox.

Each stage is emitted at most once and only forwards (ivr/screener -> person/
machine/mailbox_full; machine -> beep/mailbox_full). Nothing after a terminal stage.

THE SEAM. `record_call_stage` is the one function the rest of the dialer implements.
It resolves, in order: a recorder installed with `register_stage_recorder`, then
`record_call_stage` exported by api/services/telephony/dialer/call_stage_events.py
(the dialer_call_stage_events writer -- call_stages.report_listening_stage), else a
stub that logs and drops, so the sorter is harmless to run without it.

The latest stage is also kept in Redis (`dialer:sorter:<call_ref>`, 15 min) so a
webhook on ANOTHER worker -- the AMD result handler deciding whether to play a drop --
can `await wait_for_sorter_stage(call_ref, timeout=4.0)` for F5's "unsure AMD: listen
up to 4 s more".

Never raises into a call. Every failure costs the sorting, not the call.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from contextlib import suppress
from typing import AsyncIterator, Awaitable, Callable

from loguru import logger

from api.services.telephony.dialer.call_sorter_audio import AudioWatcher
from api.services.telephony.dialer.call_sorter_rules import SorterVerdict, classify_opening

SORTER_STAGES = ("person", "machine", "beep", "mailbox_full", "screener", "ivr")
TERMINAL_STAGES = frozenset({"person", "beep", "mailbox_full"})
_NEXT: dict[str | None, frozenset[str]] = {
    None: frozenset({"person", "machine", "mailbox_full", "screener", "ivr"}),
    "ivr": frozenset({"person", "machine", "mailbox_full", "screener"}),
    "screener": frozenset({"person", "machine", "mailbox_full"}),
    "machine": frozenset({"beep", "mailbox_full"}),
}

# F5: an unsure verdict gets this long after the far end first speaks before the
# optional LLM fallback is asked, and the fallback gets this long to answer.
UNSURE_AFTER_SECONDS = 4.0
LLM_BUDGET_SECONDS = 4.0
# Hard cap on how long one call's audio is transcribed (cost bound). A menu or a
# screener can take a while; nothing useful is decided after this.
MAX_LISTEN_SECONDS = 90.0
# After `machine`, how long to wait for a beep before giving up listening.
BEEP_WAIT_SECONDS = 45.0

TOKEN_TTL_SECONDS = 300
_TOKEN_DOMAIN = "dialer-call-sorter.v1"

StageRecorder = Callable[[str, str, dict], Awaitable[None]]
HandlingStarter = Callable[..., Awaitable[object | None]]
LlmFallback = Callable[[str], Awaitable[SorterVerdict | None]]


# ── switches ─────────────────────────────────────────────────────────────────────

def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "on", "true", "yes")


def call_sorter_enabled() -> bool:
    """OFF unless SYSEVO_DIALER_CALL_SORTER is set: it costs a Deepgram stream per
    answered call and the provider forks are not yet proven on live calls."""
    return _env_flag("SYSEVO_DIALER_CALL_SORTER") and bool(
        (os.environ.get("DEEPGRAM_API_KEY") or "").strip()
    )


def beep_trial_user(rep_user_id: str | None) -> bool:
    """Beep-timed voicemail trial: the sorter listens to these reps' single calls
    (SYSEVO_DIALER_BEEP_TRIAL_USERS, comma-separated user ids) and the recording
    starts on the beep it hears. Everything else about the sorter stays off unless
    SYSEVO_DIALER_CALL_SORTER is set."""
    if not rep_user_id or not (os.environ.get("DEEPGRAM_API_KEY") or "").strip():
        return False
    users = {u.strip() for u in (os.environ.get("SYSEVO_DIALER_BEEP_TRIAL_USERS") or "").split(",")}
    return rep_user_id in users


def llm_fallback_enabled() -> bool:
    return _env_flag("SYSEVO_DIALER_SORTER_LLM")


# ── the seam: record_call_stage ──────────────────────────────────────────────────

_registered_recorder: StageRecorder | None = None


def register_stage_recorder(recorder: StageRecorder | None) -> None:
    """Install the real stage-event writer (or None to go back to the stub)."""
    global _registered_recorder
    _registered_recorder = recorder


def _discover_recorder() -> StageRecorder | None:
    try:
        from api.services.telephony.dialer import call_stage_events  # type: ignore
    except Exception:  # noqa: BLE001 - module not written yet
        return None
    recorder = getattr(call_stage_events, "record_call_stage", None)
    return recorder if callable(recorder) else None


async def record_call_stage(call_ref: str, stage: str, detail: dict) -> None:
    """Record one stage of a dialer call. THE integration seam -- see module doc.

    `call_ref` is dialer_calls.parent_call_sid (Twilio: the rep leg's CallSid, the
    `call-<sid>` conference name's suffix; SignalWire: the rep call id the SWML
    webhooks carry as `call_id`). `stage` is one of SORTER_STAGES. Never raises.
    """
    if stage not in SORTER_STAGES:
        logger.warning(f"call sorter: refusing unknown stage {stage!r} for {call_ref}")
        return
    recorder = _registered_recorder or _discover_recorder()
    if recorder is None:
        logger.info(f"call sorter stage (no recorder yet) {call_ref}: {stage} {detail}")
        return
    try:
        await recorder(call_ref, stage, detail)
    except Exception as exc:  # noqa: BLE001 - recording a stage must never cost the call
        logger.warning(f"call sorter: stage recorder failed for {call_ref}/{stage}: {exc}")


# ── cross-worker latest stage (Redis) ────────────────────────────────────────────

def _state_key(call_ref: str) -> str:
    return f"dialer:sorter:{call_ref}"


def _redis_client():
    import redis.asyncio as aioredis

    return aioredis.from_url(os.environ.get("REDIS_URL") or "redis://redis:6379")


async def publish_sorter_state(call_ref: str, stage: str, detail: dict, *, client=None) -> None:
    """Keep the latest stage where any worker can read it. Never raises."""
    owned = client is None
    payload = json.dumps({"stage": stage, "detail": detail, "at": time.time()})
    try:
        client = client or _redis_client()
        await client.set(_state_key(call_ref), payload, ex=900)
        await client.publish(_state_key(call_ref), payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"call sorter: state publish failed for {call_ref}: {exc}")
    finally:
        if owned and client is not None:
            with suppress(Exception):
                await client.aclose()


async def latest_sorter_stage(call_ref: str, *, client=None) -> dict | None:
    """{"stage", "detail", "at"} for the latest stage, or None."""
    owned = client is None
    try:
        client = client or _redis_client()
        raw = await client.get(_state_key(call_ref))
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"call sorter: state read failed for {call_ref}: {exc}")
        return None
    finally:
        if owned and client is not None:
            with suppress(Exception):
                await client.aclose()


async def wait_for_sorter_stage(
    call_ref: str,
    *,
    stages: tuple[str, ...] = ("person", "machine", "mailbox_full", "screener", "beep"),
    timeout: float = UNSURE_AFTER_SECONDS,
    client=None,
) -> dict | None:
    """Wait up to `timeout` seconds for the sorter to reach one of `stages`.

    For the AMD handler's unsure case (F5): returns the stage dict as soon as one
    lands (or immediately when it already has), None on timeout or any failure.
    Subscribes before reading, so a stage published in between is not missed.
    """
    owned = client is None
    pubsub = None
    try:
        client = client or _redis_client()
        pubsub = client.pubsub()
        await pubsub.subscribe(_state_key(call_ref))
        current = await latest_sorter_stage(call_ref, client=client)
        if current and current.get("stage") in stages:
            return current
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=min(remaining, 0.5)
            )
            if not message:
                continue
            try:
                state = json.loads(message.get("data") or "")
            except (TypeError, ValueError):
                continue
            if state.get("stage") in stages:
                return state
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"call sorter: wait failed for {call_ref}: {exc}")
        return None
    finally:
        if pubsub is not None:
            with suppress(Exception):
                await pubsub.unsubscribe(_state_key(call_ref))
            with suppress(Exception):
                await pubsub.aclose()
        if owned and client is not None:
            with suppress(Exception):
                await client.aclose()


async def _start_handling(**kwargs):
    """Default starter: call_handling.maybe_start_session (flag + account settings)."""
    if not kwargs.get("leg_id"):
        return None
    from api.services.telephony.dialer.call_handling import maybe_start_session
    return await maybe_start_session(**kwargs)


# ── websocket credential (provider-neutral) ──────────────────────────────────────

def _signature(secret: str, call_ref: str, leg_id: str, expires_at: int) -> str:
    message = f"{_TOKEN_DOMAIN}.{call_ref}.{leg_id}.{expires_at}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def issue_sorter_token(
    *, secret: str, call_ref: str, leg_id: str, now: int | None = None
) -> str:
    """`<expiry>.<hex hmac>` binding one call and one provider leg, or "".

    Domain-separated from the live-caption and /sw-tap tokens, so a credential for
    one of those streams can never open this one (and vice versa).
    """
    if not secret or not call_ref or not leg_id:
        return ""
    expires_at = (int(time.time()) if now is None else now) + TOKEN_TTL_SECONDS
    return f"{expires_at}.{_signature(secret, call_ref, leg_id, expires_at)}"


def verify_sorter_token(
    *, secret: str, call_ref: str, leg_id: str, token: str, now: int | None = None
) -> bool:
    if not secret or not call_ref or not leg_id or not isinstance(token, str):
        return False
    if token.count(".") != 1:
        return False
    raw_expiry, signature = token.split(".", 1)
    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return False
    current = int(time.time()) if now is None else now
    if expires_at <= current or expires_at > current + TOKEN_TTL_SECONDS:
        return False
    return hmac.compare_digest(signature, _signature(secret, call_ref, leg_id, expires_at))


def wss_url(backend_endpoint: str, path: str) -> str:
    """https://host -> wss://host/<path>; "" for anything that is not TLS."""
    base = str(backend_endpoint or "").strip().rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + path
    if base.startswith("wss://"):
        return base + path
    return ""


# ── the session ──────────────────────────────────────────────────────────────────

class CallSorter:
    """One call's sorting state. Feed it captions and audio; it emits stages.

    `done` turns true at a terminal stage, at the listening cap, or after a machine
    verdict that never beeps; the transport then closes the stream.
    """

    def __init__(
        self,
        call_ref: str,
        *,
        emit: StageRecorder = record_call_stage,
        publish_state: Callable[[str, str, dict], Awaitable[None]] | None = publish_sorter_state,
        llm_fallback: LlmFallback | None = None,
        provider: str = "",
        max_listen_seconds: float = MAX_LISTEN_SECONDS,
        leg_id: str = "",
        handling_starter: HandlingStarter | None = None,
    ):
        self.call_ref = call_ref
        # Phases 2 + 3: the provider leg this audio comes from, and the agent's session
        # once it takes a screener / menu off the rep (see call_handling.py).
        self.leg_id = leg_id
        self._handling_starter = handling_starter or _start_handling
        self.handling = None
        self._handling_control: asyncio.Task | None = None
        self._handling_stop: asyncio.Event | None = None
        self._emit = emit
        self._publish_state = publish_state
        self._llm = llm_fallback if (llm_fallback is not None and llm_fallback_enabled()) else None
        self._provider = provider
        self._max_listen = max_listen_seconds
        self.audio = AudioWatcher()
        self._finals: list[str] = []
        self._interim = ""
        self._first_text_at: float | None = None
        self._machine_at: float | None = None
        self._llm_task: asyncio.Task | None = None
        # Where the transcript stood when the current stage was reached, so a menu
        # can be judged on what came AFTER it (a person picking up at
        # the end of a menu would otherwise lose to the menu's earlier "press 1").
        self._stage_from_final = 0
        self._last_was_final = True
        self.stage: str | None = None
        self.stages: list[str] = []
        self.verdict: SorterVerdict = SorterVerdict()
        self.done = False
        self.stop_reason = ""

    # inputs ─────────────────────────────────────────────────────────────────────

    @property
    def transcript(self) -> str:
        return " ".join([*self._finals, self._interim]).strip()

    async def on_caption(self, payload: dict) -> None:
        """LiveTranscriber callback: one Deepgram result (contract C1 shape)."""
        if self.done:
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        if payload.get("is_final"):
            self._finals.append(text)
            self._interim = ""
            self._last_was_final = True
        else:
            self._interim = text
            self._last_was_final = False
        if self._first_text_at is None:
            self._first_text_at = self.audio.seconds
        await self._evaluate()
        if self.handling is not None:
            with suppress(Exception):
                await self.handling.on_text(text, is_final=bool(payload.get("is_final")), now=self.audio.seconds)

    async def on_audio(self, frame: bytes) -> None:
        """One frame of prospect mu-law audio."""
        if self.done:
            return
        before = self.audio.silence_seconds
        beep = self.audio.feed(frame)
        if beep is not None and self.stage == "machine":
            await self._advance("beep", {
                "source": "tone",
                "frequency_hz": beep.frequency_hz,
                "at_seconds": beep.at_seconds,
            })
            return
        now = self.audio.seconds
        if self.handling is not None:
            with suppress(Exception):
                await self.handling.on_tick(now, silence=self.audio.silence_seconds)
            if not self.handling.active:
                self._end_handling()
        if now >= self._max_listen:
            self._finish("listen_cap")
            return
        if self._machine_at is not None and now - self._machine_at >= BEEP_WAIT_SECONDS:
            self._finish("no_beep")
            return
        # Re-judge when the quiet crosses a threshold the person rule reads, and once
        # the unsure window has passed (for the optional LLM fallback).
        after = self.audio.silence_seconds
        if self.transcript and any(before < mark <= after for mark in (1.5, 2.5)):
            await self._evaluate()
        self._maybe_ask_llm()

    # decisions ──────────────────────────────────────────────────────────────────

    async def _evaluate(self) -> None:
        silence = self.audio.silence_seconds
        # Screeners are judged whole: their rules already read what follows the
        # screener's own words, and need those words to call "leave an additional
        # message" a screener giving up rather than a fresh greeting.
        if self.stage == "ivr":
            tail = " ".join([*self._finals[self._stage_from_final:], self._interim]).strip()
            if tail:
                after = classify_opening(tail, silence_seconds=silence)
                if after.stage in _NEXT[self.stage]:
                    await self._apply(after)
                    return
        await self._apply(classify_opening(self.transcript, silence_seconds=silence))

    async def _apply(self, verdict: SorterVerdict) -> None:
        stage = verdict.stage
        if self.done or stage is None or stage == self.stage:
            return
        if stage not in _NEXT.get(self.stage, frozenset()):
            return
        self.verdict = verdict
        detail = {
            "kind": verdict.kind,
            "rule": verdict.rule,
            "confidence": verdict.confidence,
            **verdict.detail,
        }
        if verdict.kind == "person":
            detail["name"] = verdict.name
            detail["gatekeeper"] = verdict.gatekeeper
        if stage == "machine":
            detail["end_of_greeting_cue"] = verdict.end_of_greeting_cue
        await self._advance(stage, detail)

    async def _advance(self, stage: str, detail: dict) -> None:
        detail = {
            "source": "call_sorter",
            "provider": self._provider,
            "elapsed_ms": int(self.audio.seconds * 1000),
            **detail,
        }
        self.stage = stage
        self.stages.append(stage)
        # Reached on an interim: its final is still to come and belongs to this stage.
        self._stage_from_final = len(self._finals) + (0 if self._last_was_final else 1)
        if stage == "machine":
            self._machine_at = self.audio.seconds
        if stage in TERMINAL_STAGES:
            self._finish(stage)
        await self._emit(self.call_ref, stage, detail)
        if self._publish_state is not None:
            with suppress(Exception):
                await self._publish_state(self.call_ref, stage, detail)
        await self._drive_handling(stage, detail)
        # The words lag the audio by a few hundred ms, so "...at the tone." can be
        # transcribed after the tone itself has already gone by. A tone heard since
        # the far end started talking is that beep.
        beep = self.audio.beep
        if (
            stage == "machine"
            and beep is not None
            and self._first_text_at is not None
            and beep.at_seconds >= self._first_text_at - 1.0
        ):
            await self._advance("beep", {
                "source": "tone",
                "frequency_hz": beep.frequency_hz,
                "at_seconds": beep.at_seconds,
                "heard_before_verdict": True,
            })

    async def _drive_handling(self, stage: str, detail: dict) -> None:
        """Phases 2 + 3: start the agent on a screener / menu, or tell it what was heard."""
        try:
            if self.handling is not None:
                await self.handling.on_stage(stage, detail, now=self.audio.seconds)
                if not self.handling.active:
                    self._end_handling()
                return
            if stage not in {"screener", "ivr"} or self.done:
                return
            session = await self._handling_starter(
                call_key=self.call_ref, leg_id=self.leg_id, stage=stage, detail=detail,
                now=self.audio.seconds,
            )
            if session is None:
                return
            self.handling = session
            # The agent may hold the line for a while: keep listening past the usual cap.
            settings = getattr(session, "settings", {}) or {}
            budget = max(float(settings.get("agent_max_hold_seconds") or 60),
                         float(settings.get("ivr_max_seconds") or 60))
            self._max_listen = max(self._max_listen, self.audio.seconds + budget + 75.0)
            self._start_handling_control(session)
        except Exception as exc:  # noqa: BLE001 - the agent is optional; the call is not
            logger.warning(f"call handling for {self.call_ref} failed: {type(exc).__name__}: {exc}")

    def _start_handling_control(self, session) -> None:
        try:
            from api.services.telephony.dialer.call_handling import serve_commands
        except Exception:  # noqa: BLE001
            return
        self._handling_stop = asyncio.Event()
        with suppress(RuntimeError):
            self._handling_control = asyncio.create_task(serve_commands(session, self._handling_stop))

    def _end_handling(self) -> None:
        if self._handling_stop is not None:
            self._handling_stop.set()

    async def close_handling(self) -> None:
        """The stream ended: a session still holding the line hands it on."""
        if self.handling is not None and self.handling.active:
            with suppress(Exception):
                await self.handling.on_stream_end(now=self.audio.seconds)
        self._end_handling()
        if self._handling_control is not None:
            with suppress(BaseException):
                await asyncio.wait_for(self._handling_control, timeout=3.0)

    def _finish(self, reason: str) -> None:
        if not self.done:
            self.done = True
            self.stop_reason = reason
        if self._llm_task is not None and not self._llm_task.done():
            self._llm_task.cancel()

    def _maybe_ask_llm(self) -> None:
        if (
            self._llm is None
            or self._llm_task is not None
            or self.stage is not None
            or self._first_text_at is None
            or self.audio.seconds - self._first_text_at < UNSURE_AFTER_SECONDS
        ):
            return
        self._llm_task = asyncio.create_task(self._ask_llm(self.transcript))

    async def _ask_llm(self, text: str) -> None:
        """The optional fallback: one question, a hard budget, and the rules still win
        if they decide first (an earlier stage blocks this one by the order rules)."""
        try:
            verdict = await asyncio.wait_for(self._llm(text), timeout=LLM_BUDGET_SECONDS)
        except Exception as exc:  # noqa: BLE001 - timeout or provider failure: stay unsure
            logger.info(f"call sorter LLM fallback gave nothing for {self.call_ref}: {type(exc).__name__}")
            return
        if isinstance(verdict, SorterVerdict) and verdict.decided and self.stage is None:
            verdict = SorterVerdict(
                verdict.kind, min(verdict.confidence, 0.8), f"llm:{verdict.rule or 'fallback'}",
                name=verdict.name, gatekeeper=verdict.gatekeeper,
                end_of_greeting_cue=verdict.end_of_greeting_cue, detail=dict(verdict.detail),
            )
            await self._apply(verdict)


async def run_call_sorter(
    sorter: CallSorter,
    frames: AsyncIterator[bytes],
    *,
    transcriber_factory,
) -> None:
    """Pump one stream's audio into Deepgram and the sorter until it is done.

    `transcriber_factory(call_id, on_text, speaker=..., source=...)` is
    LiveTranscriber in production. Returns when the stream ends or the sorter has
    decided; the caller then closes the provider socket, which stops the fork.
    """
    try:
        async with transcriber_factory(
            f"{sorter.call_ref}:sorter", sorter.on_caption,
            speaker="prospect", source="call_sorter",
        ) as transcriber:
            if not getattr(transcriber, "enabled", False):
                logger.info(f"call sorter has no transcription for {sorter.call_ref}; audio only")
            async for frame in frames:
                if not frame:
                    continue
                await transcriber.feed(frame)
                await sorter.on_audio(frame)
                if sorter.done:
                    break
    except Exception as exc:  # noqa: BLE001 - a sorting fault must never surface
        logger.info(f"call sorter stream for {sorter.call_ref} ended: {type(exc).__name__}: {exc}")
    finally:
        sorter._finish(sorter.stop_reason or "stream_ended")
        with suppress(Exception):
            await sorter.close_handling()
    logger.info(
        f"call sorter closed for {sorter.call_ref}: stages={sorter.stages} "
        f"reason={sorter.stop_reason} audio={sorter.audio.seconds:.1f}s"
    )


__all__ = [
    "BEEP_WAIT_SECONDS",
    "CallSorter",
    "LLM_BUDGET_SECONDS",
    "MAX_LISTEN_SECONDS",
    "SORTER_STAGES",
    "TERMINAL_STAGES",
    "UNSURE_AFTER_SECONDS",
    "call_sorter_enabled",
    "issue_sorter_token",
    "latest_sorter_stage",
    "llm_fallback_enabled",
    "publish_sorter_state",
    "record_call_stage",
    "register_stage_recorder",
    "run_call_sorter",
    "verify_sorter_token",
    "wait_for_sorter_stage",
    "wss_url",
]
