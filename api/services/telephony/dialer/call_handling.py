"""Dialer Phases 2 + 3: the agent works a call screener or a phone menu for the rep.

    prospect leg --(call sorter fork)--> CallSorter --stage 'screener' / 'ivr'-->
        maybe_start_session()  (flag + account settings + a provider that can do it)
            -> HandlingSession  (lives beside the sorter, on the worker holding its socket)
                 leg.isolate()       rep no longer connected to the prospect
                 leg.play / say      a screener answer (rep / admin recording, or agent speech)
                 leg.send_digits     menu keys
                 leg.hand_back()     person -> rep (< 1 s, one provider update)
                 leg.end()           nothing reached -> end, outcome recorded

Only machines are handled. The moment a person is heard (the sorter's terminal
'person' stage -- a receptionist or gatekeeper included) the call goes to the rep.

Every new behaviour is behind SYSEVO_DIALER_CALL_SORTER (the sorter itself) AND the
account's screener_mode / ivr_enabled (default off), so nothing changes until both
are switched on.

THE STATUS LINE. Stages written to dialer_call_stage_events (same key the browser
polls): agent_screener, screener_answer_played {name}, waiting_transfer, agent_ivr
{pressed: [...]}, ivr_pressed {digits}, handed_to_rep {by, name}, rep_take_over,
screener_no_answer, ivr_not_reached. The sorter's own 'person' (with name/gatekeeper)
still arrives on hand-back.

CONTROL. The rep (Take over / End call) and managers (Hand to rep now / End call) reach
the session through Redis (``dialer:handling:cmd:<key>``); the session keeps
``dialer:handling:live:<key>`` alive while it runs. When no session is alive (worker
restarted) the route acts on the provider directly -- see ``control_without_session``.
TAKE-OVER ORDER: the agent leaves first (no more audio is sent), then the rep joins.

Never raises into a call: every failure hands the person to the rep where it can.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable, Protocol

import httpx
from loguru import logger

from api.constants import SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from api.services.telephony.dialer import call_handling_rules as rules
from api.services.telephony.dialer import call_stages

# Before playing an answer, let the screener finish asking (quiet this long), but
# never wait more than the cap.
SCREENER_PLAY_AFTER_SILENCE = 0.8
SCREENER_PLAY_MAX_WAIT = 3.0
# After a message is offered, wait this long for the record tone before handing over.
BEEP_WAIT_SECONDS = 6.0
# Agent-style follow-up answers per call.
MAX_FOLLOW_UPS = 2
DEFAULT_MAX_HOLD_SECONDS = 60

COMMANDS = frozenset({"take_over", "hand_to_rep", "end"})
_CMD_PREFIX = "dialer:handling:cmd:"
_LIVE_PREFIX = "dialer:handling:live:"
_LIVE_TTL = 15


# ── seams ────────────────────────────────────────────────────────────────────────

class LegControl(Protocol):
    """What a provider must be able to do to the prospect's leg."""

    provider: str

    async def isolate(self) -> bool: ...
    async def play(self, *, url: str | None = None, text: str | None = None,
                   say_voice: str | None = None) -> bool: ...
    async def send_digits(self, digits: str) -> bool: ...
    async def stop_agent(self) -> None: ...
    async def hand_back(self, *, result: str = "person") -> bool: ...
    async def end(self) -> bool: ...


def _headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


class HandlingStore:
    """Supabase RPCs (service role). Every method fails soft (None / False)."""

    async def _rpc(self, name: str, payload: dict[str, Any]) -> Any:
        if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
            return None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(f"{SUPABASE_URL}/rest/v1/rpc/{name}", json=payload, headers=_headers())
                response.raise_for_status()
                return response.json() if response.content else None
        except Exception as exc:  # noqa: BLE001 - handling evidence never breaks a call
            logger.info(f"call handling {name} unavailable: {type(exc).__name__}")
            return None

    async def context(self, call_key: str) -> dict | None:
        result = await self._rpc("dialer_handling_context", {"p_call_key": call_key})
        return result if isinstance(result, dict) else None

    async def begin(self, call_key: str, provider: str, prospect_call_id: str, kind: str) -> bool:
        return await self._rpc("dialer_handling_begin", {
            "p_call_key": call_key, "p_provider": provider,
            "p_prospect_call_id": prospect_call_id, "p_kind": kind,
        }) is True

    async def set_stage(self, call_key: str, stage: str, detail: dict) -> None:
        await self._rpc("dialer_handling_set_stage", {"p_call_key": call_key, "p_stage": stage, "p_detail": detail})

    async def finish(self, call_key: str, status: str, outcome: str) -> None:
        await self._rpc("dialer_handling_finish", {"p_call_key": call_key, "p_status": status, "p_outcome": outcome})

    async def record_stage(self, call_key: str, stage: str, detail: dict) -> None:
        await call_stages.record_call_stage(call_key, stage, detail)

    async def candidates(self, call_key: str) -> dict | None:
        result = await self._rpc("dialer_screener_candidates", {"p_call_key": call_key})
        return result if isinstance(result, dict) else None

    async def record_play(self, call_key: str, answer: rules.ScreenerAnswer) -> str | None:
        result = await self._rpc("dialer_record_screener_play", {
            "p_call_key": call_key, "p_answer_ref": answer.answer_ref,
            "p_style": answer.style, "p_name": answer.name,
        })
        return result if isinstance(result, str) else None

    async def settle_play(self, play_id: str, outcome: str) -> None:
        await self._rpc("dialer_settle_screener_play", {"p_play_id": play_id, "p_outcome": outcome})

    async def ivr_path(self, call_key: str, number_key: str) -> dict | None:
        result = await self._rpc("dialer_ivr_path_for", {"p_call_key": call_key, "p_number_key": number_key})
        return result if isinstance(result, dict) else None

    async def save_ivr(self, call_key: str, number_key: str, steps: list[dict], outcome: str,
                       seconds: int, dead_end: str | None) -> None:
        await self._rpc("dialer_save_ivr_attempt", {
            "p_call_key": call_key, "p_number_key": number_key, "p_steps": steps,
            "p_outcome": outcome, "p_seconds": seconds, "p_dead_end": dead_end,
        })

    async def clip_url(self, storage_path: str) -> str | None:
        from api.services.telephony.providers.twilio.dialer_call_log import sign_voicemail_storage_path
        try:
            url = await sign_voicemail_storage_path(storage_path, expires_in=600)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"screener clip could not be signed: {type(exc).__name__}")
            return None
        return url if isinstance(url, str) and url.startswith("https://") else None

    async def speech(self, text: str, voice: dict | None, account_id: str):
        from api.services.telephony.dialer.screener_voice import speech_for
        return await speech_for(text, voice, account_id=account_id)


ReplyWriter = Callable[[str, dict[str, Any]], Awaitable[str]]


async def default_reply_writer(question: str, facts: dict[str, Any]) -> str:
    """A short follow-up answer from the template's facts only.

    With SYSEVO_DIALER_SCREENER_LLM and OPENAI_API_KEY set, an LLM phrases it (2.5 s
    budget, facts-only prompt); otherwise -- or on any failure -- the rule-based line.
    """
    fallback = rules.follow_up_reply(question, facts)
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if (os.environ.get("SYSEVO_DIALER_SCREENER_LLM") or "").strip().lower() not in {"1", "on", "true", "yes"} or not key:
        return fallback
    prompt = (
        "You are answering an automated call screener for a sales rep. Reply in ONE short "
        "sentence (max 20 words) using ONLY these facts; never invent anything, never "
        f"promise anything. Facts: rep={facts.get('rep_first')}, company={facts.get('company')}, "
        f"reason={facts.get('reason')}, calling_for={facts.get('prospect_first') or 'unknown'}. "
        f"The screener said: {question[-300:]}"
    )
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": os.environ.get("SYSEVO_DIALER_SCREENER_LLM_MODEL", "gpt-4o-mini"),
                      "messages": [{"role": "user", "content": prompt}], "max_tokens": 60, "temperature": 0.2},
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()
            return text[:200] if text else fallback
    except Exception as exc:  # noqa: BLE001
        logger.info(f"screener follow-up LLM unavailable: {type(exc).__name__}")
        return fallback


# ── the session ──────────────────────────────────────────────────────────────────

class HandlingSession:
    """One call the agent is working. Fed by the sorter: stages, captions, audio ticks.

    ``clock`` is the sorter's audio clock (seconds of prospect audio), so timing rules
    are deterministic in tests and follow the call, not the worker.
    """

    def __init__(
        self, *, call_key: str, kind: str, leg: LegControl, context: dict[str, Any],
        store: HandlingStore | None = None, now: float = 0.0, rng: random.Random | None = None,
        reply_writer: ReplyWriter | None = None, ivr_kind: str | None = None,
    ):
        self.call_key = call_key
        self.kind = kind                      # 'screener' | 'ivr'
        self.leg = leg
        self.context = context or {}
        self.settings = dict(self.context.get("settings") or {})
        self.store = store or HandlingStore()
        self.rng = rng
        self.reply_writer = reply_writer or default_reply_writer
        self.ivr_kind = ivr_kind or "menu"
        self.started_at = now
        self.phase = "starting"               # starting|waiting_answer|after_answer|waiting|menu|beep|done
        self.active = True
        self.agent_stopped = False
        self.outcome: str | None = None
        self.events: list[str] = []           # actions in order (tests / logs)
        self._lock = asyncio.Lock()
        self._finals: list[str] = []
        self._interim = ""
        self._last_heard_at = now
        self._silence = 0.0
        # screener
        self.answer: rules.ScreenerAnswer | None = None
        self.play_id: str | None = None
        self._answer_at: float | None = None
        self._follow_ups = 0
        self._beep_deadline: float | None = None
        # ivr
        self.progress = rules.IvrProgress(
            max_levels=int(self.settings.get("ivr_max_levels") or rules.IVR_DEFAULT_MAX_LEVELS),
            max_seconds=float(self.settings.get("ivr_max_seconds") or rules.IVR_DEFAULT_MAX_SECONDS),
            started_at=now,
        )
        self.pressed: list[str] = []
        self.steps: list[dict[str, str]] = []
        self.learned: list[rules.LearnedStep] = []
        self._tried: list[str] = []
        self._last_decided = ""
        self.number_key = rules.number_key(self.context.get("to_number") or self.context.get("phone"))
        self.lead = rules.lead_facts(self.context)

    # facts ─────────────────────────────────────────────────────────────────────

    @property
    def max_hold(self) -> float:
        return float(self.settings.get("agent_max_hold_seconds") or DEFAULT_MAX_HOLD_SECONDS)

    def facts(self) -> dict[str, Any]:
        return {
            "rep_first": self.context.get("rep_first") or rules.first_word(self.context.get("rep_name")),
            "company": self.context.get("company"),
            "reason": self.settings.get("screener_reason"),
            "prospect_first": self.context.get("lead_first") or rules.first_word(self.context.get("prospect_name")),
        }

    @property
    def heard(self) -> str:
        return " ".join([*self._finals, self._interim]).strip()

    def _reset_heard(self) -> None:
        self._finals, self._interim = [], ""
        self._last_decided = ""

    async def _stage(self, stage: str, detail: dict | None = None) -> None:
        detail = {"source": "agent", **(detail or {})}
        self.events.append(f"stage:{stage}")
        await self.store.record_stage(self.call_key, stage, detail)
        await self.store.set_stage(self.call_key, stage, detail)

    # start ─────────────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Take the line from the rep. False (nothing changed) when it cannot."""
        if not await self.store.begin(self.call_key, self.leg.provider,
                                      str(self.context.get("prospect_call_id") or ""), self.kind):
            self.active = False
            return False
        if not await self.leg.isolate():
            self.events.append("isolate_failed")
            await self.store.finish(self.call_key, "ended", "isolate_failed")
            self.active = False
            return False
        self.events.append("isolate")
        if self.kind == "screener":
            self.phase = "waiting_answer"
            await self._stage("agent_screener", {"vendor": self.context.get("vendor")})
        else:
            if self.number_key:
                self.learned = rules.learned_steps(await self.store.ivr_path(self.call_key, self.number_key))
            if self.ivr_kind == "hold":
                self.phase = "waiting"
                await self._stage("waiting_transfer", {"via": "ivr"})
            else:
                self.phase = "menu"
                await self._stage("agent_ivr", {"pressed": [], "learned": bool(self.learned)})
        return True

    # inputs ────────────────────────────────────────────────────────────────────

    async def on_text(self, text: str, *, is_final: bool, now: float) -> None:
        if not self.active or not text.strip():
            return
        if is_final:
            self._finals.append(text.strip())
            self._interim = ""
        else:
            self._interim = text.strip()
        self._last_heard_at = now
        async with self._lock:
            if self.kind == "screener" and self.phase in {"after_answer", "waiting"}:
                await self._screener_reply(now, is_final=is_final)

    async def on_tick(self, now: float, *, silence: float) -> None:
        """Audio clock tick (every frame or so)."""
        if not self.active:
            return
        self._silence = silence
        async with self._lock:
            if not self.active:
                return
            if self.kind == "screener":
                await self._screener_tick(now)
            else:
                await self._ivr_tick(now)

    async def on_stage(self, stage: str, detail: dict, *, now: float) -> None:
        """A sorter verdict while the agent works the call."""
        if not self.active:
            return
        async with self._lock:
            if not self.active:
                return
            if stage == "person":
                await self._put_through(now, detail)
            elif stage == "mailbox_full":
                await self._settle("no_result")
                await self._finish_end("mailbox_full", stage=None, now=now)
            elif stage == "machine":
                await self._mailbox(now)
            elif stage == "beep" and self.phase == "beep":
                await self._hand_back("machine", by="voicemail", now=now)

    async def on_stream_end(self, *, now: float) -> None:
        """The sorter's socket closed while we held the line: the person hung up, or the
        fork was lost. Try to give the call to the rep; if it is gone, it was a hang-up."""
        if not self.active:
            return
        async with self._lock:
            if not self.active:
                return
            if await self.leg.hand_back(result="timeout"):
                await self._settle("no_result")
                await self._finish("handed", "stream_lost", now)
                await self._stage("handed_to_rep", {"by": "fail_safe"})
            else:
                await self._settle("hung_up")
                await self._finish("ended", "hung_up", now)

    async def command(self, action: str, *, by: str = "rep", now: float | None = None) -> bool:
        """Take over (rep), hand to rep (manager) or end (either)."""
        if action not in COMMANDS or not self.active:
            return False
        now = self._last_heard_at if now is None else now
        async with self._lock:
            if not self.active:
                return False
            if action == "end":
                await self._settle("no_result")
                await self._finish_end("ended_by_" + by, stage=None, now=now)
                return True
            if action == "take_over":
                await self._stage("rep_take_over", {"by": by})
            await self._settle("no_result")
            return await self._hand_back("person", by=by if action == "take_over" else "manager", now=now)

    # screener ──────────────────────────────────────────────────────────────────

    async def _screener_tick(self, now: float) -> None:
        if self.phase == "waiting_answer":
            waited = now - self.started_at
            if waited >= SCREENER_PLAY_MAX_WAIT or (self._silence >= SCREENER_PLAY_AFTER_SILENCE and waited >= 0.5):
                await self._play_answer(now)
            return
        if self.phase == "beep":
            if self._beep_deadline is not None and now >= self._beep_deadline:
                await self._hand_back("machine", by="voicemail", now=now)
            return
        if self.phase == "waiting":
            if now - self.started_at >= self.max_hold:
                await self._hand_back("person", by="max_hold", now=now)
            return
        if self.phase == "after_answer" and now - self._last_heard_at >= rules.SCREENER_NO_ANSWER_SECONDS:
            await self._settle("no_result")
            await self._finish_end("screener_no_answer", stage="screener_no_answer", now=now)

    async def _pick_answer(self) -> rules.ScreenerAnswer | None:
        payload = await self.store.candidates(self.call_key)
        if not payload:
            return None
        candidates, fallback = rules.answers_for_mode(payload)
        min_plays = int(payload.get("min_plays") or self.settings.get("screener_min_plays") or 20)
        return (rules.choose_answer(candidates, min_plays=min_plays, rng=self.rng)
                or rules.choose_answer(fallback, min_plays=min_plays, rng=self.rng))

    async def _play_answer(self, now: float) -> None:
        self.answer = await self._pick_answer()
        if self.answer is None:
            # Nothing to say (no recordings, no agent answers): the rep takes it.
            await self._hand_back("person", by="no_answer_available", now=now)
            return
        played = False
        if self.answer.style == "agent":
            text = rules.render_template(self.answer.template, self.facts())
            speech = await self.store.speech(text, self.settings.get("screener_agent_voice"),
                                             str(self.context.get("client_account_id") or ""))
            played = await self._play(url=speech.url, text=speech.text, say_voice=speech.say_voice)
        else:
            url = await self.store.clip_url(self.answer.storage_path or "")
            played = bool(url) and await self._play(url=url)
        if not played:
            await self._hand_back("person", by="play_failed", now=now)
            return
        self.play_id = await self.store.record_play(self.call_key, self.answer)
        await self._stage("screener_answer_played", {
            "name": self.answer.name, "answer_ref": self.answer.answer_ref, "style": self.answer.style,
        })
        await self._stage("agent_screener", {"answered": True})
        self._reset_heard()
        self._answer_at = now
        self._last_heard_at = now
        self.phase = "after_answer"

    async def _play(self, **kwargs) -> bool:
        if self.agent_stopped:
            return False
        ok = await self.leg.play(**kwargs)
        self.events.append("play" if ok else "play_failed")
        return ok

    async def _screener_reply(self, now: float, *, is_final: bool) -> None:
        kind = rules.screener_reply_kind(self.heard)
        if kind is None:
            return
        if kind == "unavailable":
            await self._mailbox(now)
        elif kind == "waiting":
            if self.phase != "waiting":
                self.phase = "waiting"
                await self._stage("waiting_transfer", {"via": "screener"})
            self._reset_heard()
        elif kind == "repeat":
            if not is_final:
                return
            await self._settle("no_result")
            await self._finish_end("screener_repeated", stage="screener_no_answer", now=now)
        elif kind == "follow_up":
            if not is_final:
                return
            if self.answer and self.answer.style == "agent" and self._follow_ups < MAX_FOLLOW_UPS:
                self._follow_ups += 1
                reply = await self.reply_writer(self.heard, self.facts())
                speech = await self.store.speech(reply, self.settings.get("screener_agent_voice"),
                                                 str(self.context.get("client_account_id") or ""))
                await self._play(url=speech.url, text=speech.text, say_voice=speech.say_voice)
                self._reset_heard()
            elif self.phase != "waiting":
                # A recording cannot answer a question: let the rep do it.
                await self._settle("no_result")
                await self._hand_back("person", by="follow_up", now=now)
        # 'person' words are left to the sorter's 'person' verdict (it carries the name).

    async def _mailbox(self, now: float) -> None:
        """A message is offered / a mailbox answered: Phase 1 voicemail rules.

        Wait for the record tone (the sorter's 'beep'), then hand the line on as a
        machine: the conference-join path plays the rep's voicemail in 'send' mode, or
        gives the mailbox to the rep for 'ask' / 'self'.
        """
        if self.phase == "beep":
            return
        await self._settle("voicemail")
        self.phase = "beep"
        self._beep_deadline = now + BEEP_WAIT_SECONDS

    async def _put_through(self, now: float, detail: dict) -> None:
        await self._settle("put_through")
        await self._hand_back("person", by="person", now=now, name=detail.get("name"),
                              gatekeeper=bool(detail.get("gatekeeper")))

    # ivr ───────────────────────────────────────────────────────────────────────

    async def _ivr_tick(self, now: float) -> None:
        if self.phase == "beep":
            if self._beep_deadline is not None and now >= self._beep_deadline:
                await self._hand_back("machine", by="voicemail", now=now)
            return
        if self.phase == "waiting":
            if now - self.started_at >= max(self.max_hold, self.progress.max_seconds):
                await self._hand_back("person", by="max_hold", now=now)
            return
        reason = self.progress.give_up_reason(now)
        if reason == "time":
            await self._give_up(reason, now)
            return
        text = self.heard
        if not text or text == self._last_decided or self._silence < rules.MENU_SETTLED_SILENCE_SECONDS:
            return
        self._last_decided = text
        await self._decide_menu(text, now)

    async def _decide_menu(self, text: str, now: float) -> None:
        if rules.is_not_found(text):
            # The route just tried stays in _tried, so the next choice moves on from it.
            self.progress.not_found += 1
        reason = self.progress.give_up_reason(now, text)
        if reason:
            await self._give_up(reason, now, menu=text)
            return
        choice = rules.replay_choice(len(self.steps), text, self.learned) if not self.progress.not_found else None
        if choice is None:
            choice = rules.choose_keys(text, self.lead, tried=self._tried)
        if choice is None:
            # Not a menu we can act on (yet): keep listening, bounded by the time rule.
            if len(text) > 600:
                await self._give_up("no_option", now, menu=text)
            return
        if not await self._press(choice, text):
            await self._hand_back("person", by="keys_failed", now=now)
            return
        self.progress.menus.append(text)
        if choice.reason not in {"confirm"}:
            self.progress.levels += 1
        self._tried.append(choice.reason)
        self._reset_heard()

    async def _press(self, choice: rules.KeyChoice, heard: str) -> bool:
        if self.agent_stopped:
            return False
        ok = await self.leg.send_digits(choice.digits)
        self.events.append(f"press:{choice.digits}" if ok else "press_failed")
        if not ok:
            return False
        self.pressed.append(choice.digits)
        self.steps.append({"heard": heard[:400], "pressed": choice.digits, "why": choice.reason})
        await self._stage("ivr_pressed", {"digits": choice.digits, "why": choice.reason, "label": choice.label})
        await self._stage("agent_ivr", {"pressed": list(self.pressed)})
        return True

    async def _give_up(self, reason: str, now: float, *, menu: str = "") -> None:
        dead_end = f"{reason}: {rules.menu_signature(menu or self.heard)[:120]}".strip()
        await self._save_ivr("ivr_not_reached", now, dead_end)
        await self._finish_end("ivr_not_reached", stage="ivr_not_reached", now=now,
                               detail={"reason": reason, "pressed": list(self.pressed)})

    async def _save_ivr(self, outcome: str, now: float, dead_end: str | None = None) -> None:
        if self.kind != "ivr" or not self.number_key:
            return
        steps = [{"heard": s["heard"], "pressed": s["pressed"]} for s in self.steps]
        await self.store.save_ivr(self.call_key, self.number_key, steps, outcome,
                                  int(max(0, now - self.started_at)), dead_end)

    # endings ───────────────────────────────────────────────────────────────────

    async def _settle(self, outcome: str) -> None:
        if self.play_id:
            play_id, self.play_id = self.play_id, None
            await self.store.settle_play(play_id, outcome)

    async def _hand_back(self, result: str, *, by: str, now: float, name: str | None = None,
                         gatekeeper: bool = False) -> bool:
        """The agent leaves first (no more audio), then the person goes to the rep."""
        await self.leg.stop_agent()
        self.agent_stopped = True
        self.events.append("stop_agent")
        ok = await self.leg.hand_back(result=result)
        self.events.append(f"hand_back:{result}" if ok else "hand_back_failed")
        if self.kind == "ivr":
            await self._save_ivr("person" if result == "person" and by == "person" else
                                 "mailbox" if result == "machine" else "rep_take_over", now)
        detail = {"by": by, "result": result}
        if name:
            detail["name"] = name
            detail["gatekeeper"] = gatekeeper
        if result == "person":
            await self._stage("handed_to_rep", detail)
        await self._finish("handed" if ok else "ended", f"handed:{by}" if ok else "hand_back_failed", now)
        return ok

    async def _finish_end(self, outcome: str, *, stage: str | None, now: float, detail: dict | None = None) -> None:
        await self.leg.stop_agent()
        self.agent_stopped = True
        ended = await self.leg.end()
        self.events.append("end" if ended else "end_failed")
        if stage:
            await self._stage(stage, detail or {})
        await self._finish("ended", outcome, now)

    async def _finish(self, status: str, outcome: str, now: float) -> None:
        if not self.active:
            return
        self.active = False
        self.phase = "done"
        self.outcome = outcome
        await self.store.finish(self.call_key, status, outcome)
        logger.info(f"call handling {self.kind} for {self.call_key} finished: {outcome} after "
                    f"{max(0.0, now - self.started_at):.1f}s ({self.events})")


# ── starting a session from the sorter ───────────────────────────────────────────

LegFactory = Callable[[str, str, dict[str, Any]], Awaitable["LegControl | None"]]


def wants_handling(stage: str, detail: dict, settings: dict[str, Any]) -> str | None:
    """'screener' / 'ivr' when the account handles this stage, else None."""
    if stage == "screener" and str(settings.get("screener_mode") or "off") != "off":
        return "screener"
    if stage == "ivr" and settings.get("ivr_enabled") is True and (detail or {}).get("kind") in {"menu", "hold", None}:
        return "ivr"
    return None


async def maybe_start_session(
    *, call_key: str, leg_id: str, stage: str, detail: dict, now: float,
    leg_factory: LegFactory | None = None, store: HandlingStore | None = None,
) -> HandlingSession | None:
    """Start handling when the flag, the account and the provider all allow it."""
    from api.services.telephony.dialer.call_sorter import call_sorter_enabled

    if stage not in {"screener", "ivr"} or not call_sorter_enabled() or not call_key or not leg_id:
        return None
    store = store or HandlingStore()
    context = await store.context(call_key)
    if not context:
        return None
    kind = wants_handling(stage, detail, context.get("settings") or {})
    if not kind:
        return None
    context = {**context, "prospect_call_id": leg_id, "vendor": (detail or {}).get("vendor")}
    factory = leg_factory or default_leg_factory
    leg = await factory(call_key, leg_id, context)
    if leg is None:
        return None
    session = HandlingSession(call_key=call_key, kind=kind, leg=leg, context=context, store=store,
                              now=now, ivr_kind=(detail or {}).get("kind"))
    if not await session.start():
        return None
    return session


async def default_leg_factory(call_key: str, leg_id: str, context: dict[str, Any]) -> LegControl | None:
    """Twilio only. SignalWire keeps 'rep handles it' (see the module report in
    signalwire_call_sorter: listening is not documented to survive a Calling API update)."""
    if str(context.get("provider") or "twilio") != "twilio":
        return None
    from api.services.telephony.providers.twilio.dialer_call_handling import TwilioHandlingLeg
    return await TwilioHandlingLeg.create(call_key=call_key, leg_sid=leg_id, context=context)


# ── control from routes (Redis) ──────────────────────────────────────────────────

def _redis():
    import redis.asyncio as aioredis
    return aioredis.from_url(os.environ.get("REDIS_URL") or "redis://redis:6379")


async def is_live(call_key: str) -> bool:
    """A session is running for this call on some worker. False on any failure."""
    client = None
    try:
        client = _redis()
        return bool(await asyncio.wait_for(client.exists(_LIVE_PREFIX + call_key), timeout=0.5))
    except Exception:  # noqa: BLE001
        return False
    finally:
        if client is not None:
            with suppress(Exception):
                await client.aclose()


async def publish_command(call_key: str, action: str, by: str) -> int:
    """Number of sessions that received it (0: none alive)."""
    client = _redis()
    try:
        return int(await client.publish(_CMD_PREFIX + call_key, json.dumps({"action": action, "by": by})))
    finally:
        with suppress(Exception):
            await client.aclose()


async def serve_commands(session: HandlingSession, stop: asyncio.Event) -> None:
    """Heartbeat + command listener beside one session. Never raises."""
    client = pubsub = None
    try:
        client = _redis()
        pubsub = client.pubsub()
        await pubsub.subscribe(_CMD_PREFIX + session.call_key)
        next_beat = 0.0
        while not stop.is_set() and session.active:
            if time.monotonic() >= next_beat:
                with suppress(Exception):
                    await client.set(_LIVE_PREFIX + session.call_key, "1", ex=_LIVE_TTL)
                next_beat = time.monotonic() + 5
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not message:
                continue
            with suppress(Exception):
                command = json.loads(message.get("data") or b"{}")
                await session.command(str(command.get("action")), by=str(command.get("by") or "rep"))
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.info(f"call handling control for {session.call_key} ended: {type(exc).__name__}")
    finally:
        if client is not None:
            with suppress(Exception):
                await client.delete(_LIVE_PREFIX + session.call_key)
        if pubsub is not None:
            with suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()
        if client is not None:
            with suppress(Exception):
                await client.aclose()


async def control_without_session(
    *, call_key: str, action: str, by: str, authorization: dict[str, Any],
    leg_factory: LegFactory | None = None, store: HandlingStore | None = None,
) -> bool:
    """No session is alive (its worker restarted): act on the leg directly."""
    store = store or HandlingStore()
    context = await store.context(call_key) or {}
    leg_id = str(authorization.get("prospect_call_id") or "")
    if not leg_id:
        return False
    leg = await (leg_factory or default_leg_factory)(call_key, leg_id, {**context, "provider": authorization.get("provider")})
    if leg is None:
        return False
    if action == "end":
        ok = await leg.end()
        await store.finish(call_key, "ended", "ended_by_" + by)
        return ok
    ok = await leg.hand_back(result="person")
    if ok:
        if action == "take_over":
            await store.record_stage(call_key, "rep_take_over", {"by": by, "source": "agent"})
        await store.record_stage(call_key, "handed_to_rep", {"by": by, "source": "agent"})
    await store.finish(call_key, "handed" if ok else "ended", f"handed:{by}" if ok else "hand_back_failed")
    return ok


class HandlingControlError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_ACTION_RPC = {"take_over": "take_over", "hand_to_rep": "hand_to_rep", "end": "end"}


async def control(call_key: str, action: str, user_id: str, *, store: HandlingStore | None = None,
                  leg_factory: LegFactory | None = None) -> dict[str, Any]:
    """Route entry: authorise, then reach the running session or act directly.

    Raises HandlingControlError(403 | 404 | 409 | 503).
    """
    if action not in COMMANDS:
        raise HandlingControlError(400, "Unknown action.")
    store = store or HandlingStore()
    auth = await store._rpc("dialer_handling_authorize", {
        "p_call_key": call_key, "p_user_id": user_id, "p_action": _ACTION_RPC[action],
    })
    if not isinstance(auth, dict):
        raise HandlingControlError(404, "This call is not being handled by the agent.")
    if auth.get("allowed") is not True:
        status = str(auth.get("status") or "")
        if status in {"handed", "ended"}:
            raise HandlingControlError(409, "The agent has already finished with this call.")
        if not status:
            raise HandlingControlError(404, "This call is not being handled by the agent.")
        raise HandlingControlError(403, "You can't do that on this call.")
    by = "manager" if auth.get("as") == "manager" else "rep"
    try:
        receivers = await publish_command(call_key, action, by)
    except Exception as exc:  # noqa: BLE001 - Redis down: act directly
        logger.info(f"call handling command not published: {type(exc).__name__}")
        receivers = 0
    if receivers:
        return {"state": "ended" if action == "end" else "handed", "via": "session"}
    if not await control_without_session(call_key=call_key, action=action, by=by, authorization=auth,
                                         leg_factory=leg_factory, store=store):
        raise HandlingControlError(503, "The call could not be changed. It may have just ended.")
    return {"state": "ended" if action == "end" else "handed", "via": "direct"}


__all__ = [
    "COMMANDS", "HandlingControlError", "control", "HandlingSession", "HandlingStore", "LegControl", "control_without_session",
    "default_reply_writer", "is_live", "maybe_start_session", "publish_command",
    "serve_commands", "wants_handling",
]
