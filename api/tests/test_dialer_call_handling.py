"""Dialer Phases 2 + 3: the agent handles call screeners and phone menus.

Pure rules (rotation, templates, screener replies, menus, key order, learned paths,
give-up) plus the session driven with a fake provider leg and a fake store, the
Twilio leg adapter's TwiML, the sorter wiring, the join hand-back, the HTTP controls
and the agent's callback tool. No call is placed and no provider API is reached.
"""

from __future__ import annotations

import contextlib
import random
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.services.telephony.dialer import agent_callback, call_handling, call_sorter
from api.services.telephony.dialer import call_handling_rules as rules
from api.services.telephony.dialer import screener_voice
from api.services.telephony.providers.twilio import dialer_call_handling as twilio_leg

_PARENT = "CA" + "abcdef0123456789" * 2
_CHILD = "CA" + "0123456789abcdef" * 2
_ACCOUNT = "00000000-0000-0000-0000-0000000000a1"
_REP = "11111111-1111-1111-1111-111111111111"


def _uuid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


# ── fakes ────────────────────────────────────────────────────────────────────────

class FakeLeg:
    provider = "twilio"

    def __init__(self, *, isolate=True, hand_back=True, play=True, digits=True):
        self.calls: list[tuple] = []
        self._isolate, self._hand_back, self._play, self._digits = isolate, hand_back, play, digits

    async def isolate(self):
        self.calls.append(("isolate",))
        return self._isolate

    async def play(self, **kwargs):
        self.calls.append(("play", kwargs))
        return self._play

    async def send_digits(self, digits):
        self.calls.append(("digits", digits))
        return self._digits

    async def stop_agent(self):
        self.calls.append(("stop_agent",))

    async def hand_back(self, *, result="person"):
        self.calls.append(("hand_back", result))
        return self._hand_back

    async def end(self):
        self.calls.append(("end",))
        return True

    def names(self):
        return [c[0] for c in self.calls]


class FakeStore(call_handling.HandlingStore):
    def __init__(self, *, candidates=None, path=None, begin=True):
        self.stages: list[tuple[str, dict]] = []
        self.finished: list[tuple[str, str]] = []
        self.plays: list[str] = []
        self.settled: list[tuple[str, str]] = []
        self.saved: list[dict] = []
        self._candidates = candidates
        self._path = path
        self._begin = begin
        self.speech_calls: list[str] = []

    async def context(self, call_key):  # pragma: no cover - not used by sessions directly
        return None

    async def begin(self, call_key, provider, prospect_call_id, kind):
        return self._begin

    async def set_stage(self, call_key, stage, detail):
        pass

    async def finish(self, call_key, status, outcome):
        self.finished.append((status, outcome))

    async def record_stage(self, call_key, stage, detail):
        self.stages.append((stage, detail))

    async def candidates(self, call_key):
        return self._candidates

    async def record_play(self, call_key, answer):
        self.plays.append(answer.answer_ref)
        return _uuid(900)

    async def settle_play(self, play_id, outcome):
        self.settled.append((play_id, outcome))

    async def ivr_path(self, call_key, number_key):
        return self._path

    async def save_ivr(self, call_key, number_key, steps, outcome, seconds, dead_end):
        self.saved.append({"steps": steps, "outcome": outcome, "dead_end": dead_end, "number_key": number_key})

    async def clip_url(self, storage_path):
        return f"https://db.example.com/signed/{storage_path}"

    async def speech(self, text, voice, account_id):
        self.speech_calls.append(text)
        return screener_voice.Speech(text=text, say_voice=screener_voice.carrier_voice(voice))

    def stage_names(self):
        return [s for s, _ in self.stages]


def _candidates(mode="rep", **extra):
    return {
        "mode": mode, "min_plays": 20,
        "candidates": extra.get("candidates", [
            {"answer_ref": f"drop:{_uuid(1)}", "style": "rep", "name": "Max - follow-up",
             "storage_path": f"{_REP}/screener.wav", "plays": 3, "put_through": 1},
        ]),
        "fallback": extra.get("fallback", []),
    }


def _context(**settings):
    base = {"screener_mode": "rep", "screener_min_plays": 20, "ivr_enabled": True,
            "ivr_max_levels": 3, "ivr_max_seconds": 60, "agent_max_hold_seconds": 60}
    base.update(settings)
    return {
        "client_account_id": _ACCOUNT, "rep_user_id": _REP, "rep_first": "Max", "rep_name": "Max Tan",
        "company": "Acme Analytics", "prospect_name": "James Ortiz", "lead_first": "James",
        "to_number": "+1 (555) 555-0101", "phone": "+15555550101", "enrichment": {"title": "VP Sales"},
        "provider": "twilio", "parent_call_sid": _PARENT, "settings": base,
    }


async def _session(kind="screener", *, store=None, leg=None, context=None, ivr_kind="menu", **kw):
    store = store or FakeStore(candidates=_candidates())
    leg = leg or FakeLeg()
    session = call_handling.HandlingSession(
        call_key=_PARENT, kind=kind, leg=leg, context=context or _context(), store=store, now=0.0,
        rng=random.Random(7), ivr_kind=ivr_kind, **kw,
    )
    assert await session.start()
    return session, store, leg


# ── rotation ─────────────────────────────────────────────────────────────────────

def _answer(n, *, plays=0, put=0, pinned=False, style="admin"):
    return rules.ScreenerAnswer(answer_ref=f"answer:{_uuid(n)}", style=style, name=f"A{n}",
                                storage_path="x/y.wav" if style != "agent" else None,
                                template="Hi" if style == "agent" else None,
                                pinned=pinned, plays=plays, put_through=put)


def test_rotation_pinned_answer_always_plays():
    pool = [_answer(1, plays=100, put=90), _answer(2, plays=0, pinned=True)]
    for seed in range(20):
        assert rules.choose_answer(pool, min_plays=20, rng=random.Random(seed)).answer_ref.endswith("2")


def test_rotation_measures_every_answer_before_favouring_one():
    pool = [_answer(1, plays=25, put=20), _answer(2, plays=4), _answer(3, plays=2)]
    assert rules.choose_answer(pool, min_plays=20).answer_ref.endswith("3")


def test_rotation_favours_best_performers_after_min_plays():
    best, worst = _answer(1, plays=100, put=60), _answer(2, plays=100, put=10)
    rng = random.Random(11)
    picks = Counter(rules.choose_answer([best, worst], min_plays=20, rng=rng).answer_ref for _ in range(2000))
    assert picks[best.answer_ref] > 0.85 * 2000
    assert picks[worst.answer_ref] > 0           # never starved completely


def test_paused_answers_are_never_candidates_and_bad_rows_are_dropped():
    candidates, fallback = rules.answers_for_mode({
        "candidates": [{"answer_ref": "answer:bad", "style": "admin", "storage_path": "p"},
                       {"answer_ref": f"answer:{_uuid(3)}", "style": "agent", "template": ""}],
        "fallback": [{"answer_ref": f"answer:{_uuid(4)}", "style": "agent", "template": "Hi {rep_first}"}],
    })
    assert candidates == [] and [f.answer_ref for f in fallback] == [f"answer:{_uuid(4)}"]


def test_template_fills_facts_and_collapses_missing_ones():
    text = rules.render_template(
        "Hi, this is {rep_first} from {company}, calling for {prospect_first} about {reason}.",
        {"rep_first": "Max Tan", "company": "Acme", "reason": "your quote", "prospect_first": "James"},
    )
    assert text == "Hi, this is Max from Acme, calling for James about your quote."
    text = rules.render_template(None, {"rep_first": "", "company": "", "prospect_first": ""})
    assert "{" not in text and "for  " not in text and "a colleague" in text and "our team" in text


@pytest.mark.parametrize(("text", "kind"), [
    ("Thanks. Please stay on the line.", "waiting"),
    ("The person you're calling is not available. Leave an additional message after the tone.", "unavailable"),
    ("Please record your name and reason for calling.", "repeat"),
    ("Could you say which company you're with?", "follow_up"),
    ("Hello? Who's this?", "person"),
    ("Okay.", None),
])
def test_screener_reply_kinds(text, kind):
    assert rules.screener_reply_kind(text) == kind


# ── screener sessions ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_screener_answer_then_waiting_then_put_through():
    session, store, leg = await _session()
    assert leg.names()[0] == "isolate" and store.stage_names() == ["agent_screener"]
    await session.on_tick(1.0, silence=1.0)           # the screener stopped asking
    assert ("play", {"url": f"https://db.example.com/signed/{_REP}/screener.wav"}) in leg.calls
    assert store.plays == [f"drop:{_uuid(1)}"]
    await session.on_text("Please stay on the line.", is_final=True, now=4.0)
    assert store.stage_names()[-1] == "waiting_transfer"
    await session.on_tick(50.0, silence=40.0)          # waiting is not "nothing": no 45 s end
    assert session.active
    await session.on_stage("person", {"name": "Sarah", "gatekeeper": True}, now=52.0)
    assert leg.names()[-2:] == ["stop_agent", "hand_back"] and leg.calls[-1] == ("hand_back", "person")
    assert store.settled == [(_uuid(900), "put_through")]
    handed = dict(store.stages)["handed_to_rep"]
    assert handed["name"] == "Sarah" and handed["gatekeeper"] is True
    assert store.stage_names()[:3] == ["agent_screener", "screener_answer_played", "agent_screener"]
    assert store.finished == [("handed", "handed:person")]


@pytest.mark.asyncio
async def test_screener_unavailable_follows_voicemail_rules_after_the_beep():
    session, store, leg = await _session()
    await session.on_tick(1.0, silence=1.0)
    await session.on_text("They're not available. Leave an additional message after the tone.", is_final=True, now=5.0)
    assert store.settled == [(_uuid(900), "voicemail")] and session.active
    await session.on_stage("beep", {"source": "tone"}, now=7.0)
    assert leg.calls[-1] == ("hand_back", "machine") and not session.active


@pytest.mark.asyncio
async def test_screener_unavailable_without_a_beep_hands_on_after_the_wait():
    session, _store, leg = await _session()
    await session.on_tick(1.0, silence=1.0)
    await session.on_stage("machine", {"kind": "screener_unavailable"}, now=5.0)
    await session.on_tick(5.0 + call_handling.BEEP_WAIT_SECONDS, silence=1.0)
    assert leg.calls[-1] == ("hand_back", "machine")


@pytest.mark.asyncio
async def test_screener_silence_for_45_seconds_ends_the_call():
    session, store, leg = await _session()
    await session.on_tick(1.0, silence=1.0)
    await session.on_tick(1.0 + rules.SCREENER_NO_ANSWER_SECONDS, silence=45.0)
    assert ("end",) in leg.calls and "hand_back" not in leg.names()
    assert store.stage_names()[-1] == "screener_no_answer"
    assert store.settled == [(_uuid(900), "no_result")]
    assert store.finished == [("ended", "screener_no_answer")]


@pytest.mark.asyncio
async def test_screener_repeating_itself_ends_the_call():
    session, store, leg = await _session()
    await session.on_tick(1.0, silence=1.0)
    await session.on_text("Please record your name and reason for calling.", is_final=True, now=6.0)
    assert ("end",) in leg.calls and store.stage_names()[-1] == "screener_no_answer"


@pytest.mark.asyncio
async def test_screener_hang_up_is_attributed_to_the_answer():
    session, store, _leg = await _session(leg=FakeLeg(hand_back=False))
    await session.on_tick(1.0, silence=1.0)
    await session.on_stream_end(now=8.0)
    assert store.settled == [(_uuid(900), "hung_up")] and store.finished == [("ended", "hung_up")]


@pytest.mark.asyncio
async def test_rep_without_a_recording_falls_back_to_the_next_style():
    store = FakeStore(candidates=_candidates(candidates=[], fallback=[
        {"answer_ref": f"answer:{_uuid(5)}", "style": "agent", "name": "Agent intro",
         "template": "Hi, this is {rep_first} from {company} for {prospect_first}."}]))
    session, store, leg = await _session(store=store, context=_context(
        screener_agent_voice={"provider": "google", "voice_id": "en-US-Neural2-F", "name": "F"}))
    await session.on_tick(3.0, silence=0.0)            # cap reached even while it talks
    play = [c for c in leg.calls if c[0] == "play"][0][1]
    assert play["text"] == "Hi, this is Max from Acme Analytics for James."
    assert play["say_voice"] == "Google.en-US-Neural2-F"


@pytest.mark.asyncio
async def test_agent_style_answers_a_follow_up_from_its_facts():
    store = FakeStore(candidates=_candidates(candidates=[
        {"answer_ref": f"answer:{_uuid(6)}", "style": "agent", "name": "Agent", "template": "Hi, it's {rep_first}."}]))
    session, store, leg = await _session(store=store, context=_context(screener_reason="your quote request"))
    await session.on_tick(1.0, silence=1.0)
    await session.on_text("What is this regarding?", is_final=True, now=4.0)
    plays = [c[1] for c in leg.calls if c[0] == "play"]
    assert plays[-1]["text"] == "It's about your quote request." and session.active


@pytest.mark.asyncio
async def test_recorded_style_hands_a_follow_up_to_the_rep():
    session, _store, leg = await _session()
    await session.on_tick(1.0, silence=1.0)
    await session.on_text("Can you tell me what company you're with?", is_final=True, now=4.0)
    assert leg.calls[-1] == ("hand_back", "person")


@pytest.mark.asyncio
async def test_no_answer_available_gives_the_call_to_the_rep():
    session, _store, leg = await _session(store=FakeStore(candidates=_candidates(candidates=[])))
    await session.on_tick(1.0, silence=1.0)
    assert leg.calls[-1] == ("hand_back", "person") and "play" not in leg.names()


@pytest.mark.asyncio
async def test_take_over_agent_leaves_before_the_rep_joins_and_says_nothing_more():
    session, store, leg = await _session()
    await session.on_tick(1.0, silence=1.0)
    assert await session.command("take_over", by="rep")
    names = leg.names()
    assert names.index("stop_agent") < names.index("hand_back")
    assert "rep_take_over" in store.stage_names() and store.stage_names()[-1] == "handed_to_rep"
    before = len(leg.calls)
    await session.on_text("What is this regarding?", is_final=True, now=9.0)
    await session.on_tick(60.0, silence=50.0)
    assert len(leg.calls) == before                     # nothing played after the take-over
    assert not await session.command("end")             # already finished


@pytest.mark.asyncio
async def test_manager_end_ends_the_line():
    session, store, leg = await _session()
    assert await session.command("end", by="manager")
    assert ("end",) in leg.calls and store.finished == [("ended", "ended_by_manager")]


@pytest.mark.asyncio
async def test_isolation_failure_changes_nothing():
    session = call_handling.HandlingSession(call_key=_PARENT, kind="screener", leg=FakeLeg(isolate=False),
                                            context=_context(), store=FakeStore(), now=0.0)
    assert not await session.start() and not session.active


# ── flags off = unchanged ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_off_never_starts_a_session(monkeypatch):
    monkeypatch.delenv("SYSEVO_DIALER_CALL_SORTER", raising=False)
    store = FakeStore()
    store.context = AsyncMock(return_value=_context())
    factory = AsyncMock()
    assert await call_handling.maybe_start_session(
        call_key=_PARENT, leg_id=_CHILD, stage="screener", detail={}, now=0, leg_factory=factory, store=store,
    ) is None
    store.context.assert_not_awaited() and factory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("stage", "settings", "detail"), [
    ("screener", {"screener_mode": "off"}, {}),
    ("ivr", {"ivr_enabled": False}, {"kind": "menu"}),
    ("ivr", {"ivr_enabled": True}, {"kind": "intercept"}),
    ("person", {"screener_mode": "rotate"}, {}),
])
async def test_settings_off_never_starts_a_session(monkeypatch, stage, settings, detail):
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    store = FakeStore()
    store.context = AsyncMock(return_value=_context(**settings))
    factory = AsyncMock()
    assert await call_handling.maybe_start_session(
        call_key=_PARENT, leg_id=_CHILD, stage=stage, detail=detail, now=0, leg_factory=factory, store=store,
    ) is None
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_signalwire_calls_stay_with_the_rep(monkeypatch):
    assert await call_handling.default_leg_factory(_PARENT, "sw-leg", {"provider": "signalwire"}) is None


@pytest.mark.asyncio
async def test_flag_and_settings_on_starts_and_isolates(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    store = FakeStore(candidates=_candidates())
    store.context = AsyncMock(return_value=_context(screener_mode="rotate"))
    leg = FakeLeg()
    session = await call_handling.maybe_start_session(
        call_key=_PARENT, leg_id=_CHILD, stage="screener", detail={"vendor": "iphone"}, now=2.0,
        leg_factory=AsyncMock(return_value=leg), store=store,
    )
    assert session is not None and session.kind == "screener" and leg.names() == ["isolate"]


# ── sorter wiring ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sorter_hands_stages_text_and_ticks_to_the_session():
    events = []

    class Probe:
        active = True
        settings = {"agent_max_hold_seconds": 60, "ivr_max_seconds": 60}

        async def on_stage(self, stage, detail, *, now):
            events.append(("stage", stage))

        async def on_text(self, text, *, is_final, now):
            events.append(("text", text))

        async def on_tick(self, now, *, silence):
            events.append("tick")

        async def on_stream_end(self, *, now):
            events.append("end")

    started = []

    async def starter(**kwargs):
        started.append(kwargs)
        return Probe()

    async def emit(*_a):
        return None

    sorter = call_sorter.CallSorter(_PARENT, emit=emit, publish_state=None, leg_id=_CHILD,
                                    handling_starter=starter)
    sorter._start_handling_control = lambda session: None
    await sorter.on_caption({"text": "Please record your name and reason for calling.", "is_final": True})
    assert sorter.stage == "screener" and started[0]["leg_id"] == _CHILD
    assert sorter._max_listen > call_sorter.MAX_LISTEN_SECONDS
    await sorter.on_caption({"text": "Please stay on the line.", "is_final": True})
    await sorter.on_audio(b"\xff" * 160)
    assert ("text", "Please stay on the line.") in events and "tick" in events
    await sorter.close_handling()
    assert "end" in events


@pytest.mark.asyncio
async def test_sorter_without_a_leg_never_starts_handling(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")

    async def emit(*_a):
        return None

    with patch("api.services.telephony.dialer.call_handling.maybe_start_session", new=AsyncMock()) as start:
        sorter = call_sorter.CallSorter(_PARENT, emit=emit, publish_state=None)
        await sorter.on_caption({"text": "Please record your name and reason for calling.", "is_final": True})
    start.assert_not_awaited()
    assert sorter.handling is None


# ── phone menus: rules ───────────────────────────────────────────────────────────

_MENU = ("Thank you for calling Acme. For sales, press 1. For accounts payable, press 2. "
         "For the company directory, press 9. For all other inquiries, press 0.")


def test_parse_menu_reads_both_phrasings():
    assert [(o.key, o.label) for o in rules.parse_menu(_MENU)] == [
        ("1", "sales"), ("2", "accounts payable"), ("9", "company directory"), ("0", "all other inquiries"),
    ]
    options = rules.parse_menu("Press two for billing. Press three to reach the operator.")
    assert [(o.key, o.label) for o in options] == [("2", "billing"), ("3", "operator")]


def test_key_order_extension_first():
    lead = rules.LeadFacts(extension="204", last_name="Ortiz", job_title="VP Sales")
    choice = rules.choose_keys("If you know your party's extension, dial it now. " + _MENU, lead)
    assert (choice.digits, choice.reason) == ("204", "extension")


def test_key_order_dial_by_name_before_department():
    lead = rules.LeadFacts(last_name="Ortiz", job_title="VP Sales")
    assert rules.choose_keys(_MENU, lead).reason == "directory"
    spelled = rules.choose_keys("Please spell the last name of the person you are trying to reach.", lead)
    assert (spelled.digits, spelled.reason) == ("67849", "dial_by_name")
    confirm = rules.choose_keys("Ortiz, James. If this is correct, press 1.", lead, tried=["dial_by_name"])
    assert (confirm.digits, confirm.reason) == ("1", "confirm")


def test_key_order_department_from_job_title_then_operator():
    sales = rules.LeadFacts(job_title="Head of Business Development")
    assert rules.choose_keys(_MENU, sales, tried=["directory"]).digits == "1"
    finance = rules.LeadFacts(job_title="Financial Controller")
    assert rules.choose_keys(_MENU, finance).digits == "2"
    owner = rules.LeadFacts(job_title="Owner")
    assert (rules.choose_keys(_MENU, owner).digits, rules.choose_keys(_MENU, owner).reason) == ("0", "operator")
    assert rules.choose_keys("For sales press 1.", owner).digits == "0"


def test_lead_facts_find_extension_and_title():
    facts = rules.lead_facts({"prospect_name": "James Ortiz", "phone": "+1 555 555 0101 x204",
                              "source_row": {"Job Title": "CFO"}})
    assert (facts.extension, facts.last_name, facts.job_title, facts.department) == ("204", "Ortiz", "CFO", "accounts")
    assert rules.extension_from({"enrichment": {"extension": "ext. 77"}}) == "77"


def test_give_up_rules():
    progress = rules.IvrProgress(max_levels=3, max_seconds=60, started_at=0)
    assert progress.give_up_reason(10) is None
    assert progress.give_up_reason(60) == "time"
    progress.levels = 3
    assert progress.give_up_reason(10) == "levels"
    progress = rules.IvrProgress(started_at=0)
    progress.menus.append(_MENU)
    assert progress.give_up_reason(5, _MENU.replace("Thank you", "Thanks")) == "same_menu"
    progress = rules.IvrProgress(started_at=0)
    progress.not_found = 2
    assert progress.give_up_reason(5) == "not_found"


def test_learned_path_replays_only_where_the_menu_is_unchanged():
    steps = rules.learned_steps({"outcome": "person", "successes": 2, "steps": [
        {"heard": _MENU, "pressed": "9"}, {"heard": "Spell the last name", "pressed": "67849"}]})
    assert rules.replay_choice(0, _MENU, steps) == rules.KeyChoice("9", "learned")
    assert rules.replay_choice(1, "Our menu has changed. For support press 4.", steps) is None
    assert rules.learned_steps({"outcome": "ivr_not_reached", "successes": 0, "steps": [{"pressed": "1"}]}) == []


# ── phone menu sessions ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_menu_session_presses_in_order_and_saves_the_path_on_a_person():
    session, store, leg = await _session("ivr", store=FakeStore())
    assert store.stage_names() == ["agent_ivr"]
    await session.on_text(_MENU, is_final=True, now=6.0)
    await session.on_tick(6.5, silence=0.5)                         # still talking: wait
    assert "digits" not in leg.names()
    await session.on_tick(8.0, silence=1.5)
    assert ("digits", "9") in leg.calls                             # directory (dial by name)
    await session.on_text("Please spell the last name of your party.", is_final=True, now=10.0)
    await session.on_tick(12.0, silence=1.5)
    assert ("digits", "67849") in leg.calls
    assert dict(store.stages)["agent_ivr"]["pressed"] == ["9", "67849"]
    await session.on_stage("person", {"name": "James"}, now=20.0)
    assert leg.calls[-1] == ("hand_back", "person")
    assert store.saved[-1]["outcome"] == "person" and [s["pressed"] for s in store.saved[-1]["steps"]] == ["9", "67849"]
    assert store.saved[-1]["number_key"] == "5555550101"


@pytest.mark.asyncio
async def test_menu_session_replays_a_learned_path():
    store = FakeStore(path={"outcome": "person", "successes": 3, "steps": [{"heard": _MENU, "pressed": "2"}]})
    session, store, leg = await _session("ivr", store=store)
    await session.on_text(_MENU, is_final=True, now=5.0)
    await session.on_tick(6.5, silence=1.5)
    assert ("digits", "2") in leg.calls                             # learned, not the directory
    assert dict(store.stages)["ivr_pressed"]["why"] == "learned"


@pytest.mark.asyncio
async def test_menu_session_gives_up_on_the_same_menu_twice():
    session, store, leg = await _session("ivr", store=FakeStore(),
                                         context={**_context(), "prospect_name": "", "enrichment": {}})
    await session.on_text(_MENU, is_final=True, now=5.0)
    await session.on_tick(6.5, silence=1.5)                         # operator 0
    await session.on_text(_MENU, is_final=True, now=12.0)
    await session.on_tick(13.5, silence=1.5)
    assert ("end",) in leg.calls and store.stage_names()[-1] == "ivr_not_reached"
    assert store.saved[-1]["outcome"] == "ivr_not_reached" and store.saved[-1]["dead_end"].startswith("same_menu")
    assert store.finished == [("ended", "ivr_not_reached")]


@pytest.mark.asyncio
async def test_menu_session_gives_up_after_not_found_twice():
    session, store, leg = await _session("ivr", store=FakeStore())
    await session.on_text("Please spell the last name of your party.", is_final=True, now=3.0)
    await session.on_tick(4.5, silence=1.5)
    await session.on_text("That name was not found. Please spell the last name.", is_final=True, now=8.0)
    await session.on_tick(9.5, silence=1.5)
    await session.on_text("No matches. Invalid entry.", is_final=True, now=12.0)
    await session.on_tick(13.5, silence=1.5)
    assert store.stage_names()[-1] == "ivr_not_reached"


@pytest.mark.asyncio
async def test_menu_session_times_out_and_mailbox_goes_to_voicemail_rules():
    session, store, leg = await _session("ivr", store=FakeStore())
    await session.on_tick(61.0, silence=10.0)
    assert store.stage_names()[-1] == "ivr_not_reached"
    session, store, leg = await _session("ivr", store=FakeStore())
    await session.on_stage("machine", {"kind": "voicemail"}, now=9.0)
    await session.on_stage("beep", {}, now=11.0)
    assert leg.calls[-1] == ("hand_back", "machine") and store.saved[-1]["outcome"] == "mailbox"


@pytest.mark.asyncio
async def test_menu_level_cap():
    session, store, leg = await _session("ivr", store=FakeStore(), context=_context(ivr_max_levels=1))
    await session.on_text(_MENU, is_final=True, now=5.0)
    await session.on_tick(6.5, silence=1.5)
    await session.on_text("For orders press 4. For returns press 5.", is_final=True, now=9.0)
    await session.on_tick(10.5, silence=1.5)
    assert store.stage_names()[-1] == "ivr_not_reached"
    assert dict(store.stages)["ivr_not_reached"]["reason"] == "levels"


@pytest.mark.asyncio
async def test_hold_queue_waits_silently_then_hands_to_rep():
    session, store, leg = await _session("ivr", store=FakeStore(), ivr_kind="hold")
    assert store.stage_names() == ["waiting_transfer"]
    await session.on_tick(30.0, silence=5.0)
    assert session.active and "digits" not in leg.names()
    await session.on_tick(61.0, silence=5.0)
    assert leg.calls[-1] == ("hand_back", "person")


# ── Twilio leg adapter ───────────────────────────────────────────────────────────

def test_twilio_held_twiml_always_ends_in_the_fail_safe_hand_back():
    url = twilio_leg.join_url(backend="https://api.example.com", conference_name=f"call-{_PARENT}",
                              parent_call_sid=_PARENT, handled="timeout")
    twiml = twilio_leg.held_twiml(twilio_leg.digits_body("2"), hold_seconds=90, fallback_url=url)
    assert '<Play digits="w2"/><Pause length="90"/>' in twiml
    assert "handled=timeout" in twiml and "dialer-conference-join" in twiml
    with pytest.raises(ValueError):
        twilio_leg.digits_body("2<Hangup/>")
    assert twilio_leg.play_body(text="Hi & bye", say_voice="Polly.Joanna") == '<Say voice="Polly.Joanna">Hi &amp; bye</Say>'


def test_twilio_parallel_join_url_keeps_the_leg_identity():
    url = twilio_leg.join_url(backend="https://api.example.com/", conference_name=f"call-{_PARENT}",
                              parent_call_sid=_PARENT, parallel_leg_id=_uuid(7), parallel_entry_id=_uuid(8))
    assert f"parallel_leg_id={_uuid(7)}" in url and "handled=person" in url


def _client():
    client = MagicMock()
    client.conferences.list.return_value = [SimpleNamespace(sid="CF1")]
    return client


@pytest.mark.asyncio
async def test_twilio_isolate_keeps_the_reps_room_then_takes_the_leg():
    client = _client()
    leg = twilio_leg.TwilioHandlingLeg(call_key=_PARENT, leg_sid=_CHILD, parent_call_sid=_PARENT,
                                       conference_name=f"call-{_PARENT}", backend="https://api.example.com",
                                       hold_seconds=90, client=client)
    assert await leg.isolate()
    client.conferences.return_value.participants.return_value.update.assert_called_once_with(
        end_conference_on_exit=False)
    twiml = client.calls.return_value.update.call_args.kwargs["twiml"]
    assert "<Pause" in twiml and "handled=timeout" in twiml
    assert await leg.send_digits("5")
    assert await leg.hand_back(result="person")
    assert client.calls.return_value.update.call_args.kwargs["url"].endswith("handled=person")
    assert not await leg.play(text="late")                        # nothing after the hand-back


@pytest.mark.asyncio
async def test_twilio_end_releases_the_rep_on_a_single_call():
    leg = twilio_leg.TwilioHandlingLeg(call_key=_PARENT, leg_sid=_CHILD, parent_call_sid=_PARENT,
                                       conference_name=f"call-{_PARENT}", backend="https://api.example.com",
                                       hold_seconds=90, client=_client())
    with patch.object(twilio_leg, "cancel_call", new=AsyncMock(return_value=True)) as cancel:
        assert await leg.end()
    assert [c.kwargs["call_sid"] for c in cancel.await_args_list] == [_CHILD, _PARENT]
    leg.parallel_leg_id = _uuid(7)
    with patch.object(twilio_leg, "cancel_call", new=AsyncMock(return_value=True)) as cancel:
        await leg.end()
    assert [c.kwargs["call_sid"] for c in cancel.await_args_list] == [_CHILD]


def test_screener_voice_mapping_and_wav():
    assert screener_voice.carrier_voice({"provider": "polly", "voice_id": "Joanna-Neural"}) == "Polly.Joanna-Neural"
    assert screener_voice.carrier_voice({"provider": "elevenlabs", "voice_id": "abc"}) is None
    assert screener_voice.carrier_voice({"provider": "google", "voice_id": "bad id!"}) is None
    wav = screener_voice.mulaw_wav(b"\xff" * 801)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and len(wav) % 2 == 0
    assert screener_voice.cache_path(_ACCOUNT, {"provider": "elevenlabs", "voice_id": "v"}, "hi").startswith(
        f"screener-tts/{_ACCOUNT}/")


# ── join hand-back (Twilio route) ────────────────────────────────────────────────

_ROUTES = "api.services.telephony.providers.twilio.routes"


def _join(params, *, answered_by="", live=False):
    from api.services.telephony.providers.twilio.routes import router
    app = FastAPI()
    app.include_router(router)
    patches = {
        "validate": patch(f"{_ROUTES}.RequestValidator.validate", return_value=True),
        "token": patch(f"{_ROUTES}._resolve_dialer_auth_token", new=AsyncMock(return_value="t")),
        "child": patch(f"{_ROUTES}.update_dialer_call_child_sid", new=AsyncMock()),
        "amd": patch(f"{_ROUTES}.update_dialer_call_amd_result", new=AsyncMock(return_value=True)),
        "row": patch(f"{_ROUTES}.get_dialer_call_child_leg", new=AsyncMock(return_value={"rep_user_id": _REP})),
        "policy": patch(f"{_ROUTES}.get_team_voicemail_policy", new=AsyncMock(return_value={"on_machine": "drop_vm", "voicemail_mode": "ask"})),
        "settings": patch(f"{_ROUTES}.get_auto_voicemail_settings", new=AsyncMock(return_value=(False, None, None))),
        "evidence": patch(f"{_ROUTES}.get_voicemail_drop_evidence", new=AsyncMock(return_value={})),
        "stage": patch(f"{_ROUTES}.call_stages.record_call_stage", new=AsyncMock(return_value=True)),
        "consult": patch(f"{_ROUTES}.sorter_gate.consult_sorter", new=AsyncMock()),
        "live": patch(f"{_ROUTES}.call_handling.is_live", new=AsyncMock(return_value=live)),
    }
    with contextlib.ExitStack() as stack:
        m = {k: stack.enter_context(v) for k, v in patches.items()}
        response = TestClient(app).post(
            "/dialer-conference-join",
            params={"conference_name": f"call-{_PARENT}", "parent_call_sid": _PARENT, **params},
            data={"AnsweredBy": answered_by, "CallSid": _CHILD} if answered_by else {"CallSid": _CHILD},
            headers={"X-Twilio-Signature": "sig"},
        )
    return response, m


def test_handed_person_joins_the_rep_without_consulting_the_sorter(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    response, m = _join({"handled": "person"})
    assert "<Conference" in response.text
    m["consult"].assert_not_awaited()
    assert m["amd"].await_args.kwargs["answered_by"] == "human"


def test_handed_machine_follows_phase1_voicemail_rules(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    response, m = _join({"handled": "machine"})
    m["consult"].assert_not_awaited()
    assert m["amd"].await_args.kwargs["answered_by"] == "machine_end_beep"
    assert "<Conference" in response.text            # 'ask' mode: the mailbox goes to the rep


def test_join_waits_while_the_agent_holds_the_line(monkeypatch):
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    response, m = _join({}, answered_by="human", live=True)
    assert "<Conference" not in response.text and "handled=timeout" in response.text


def test_join_unchanged_with_the_flag_off(monkeypatch):
    monkeypatch.delenv("SYSEVO_DIALER_CALL_SORTER", raising=False)
    response, m = _join({}, answered_by="human", live=True)
    m["live"].assert_not_awaited()
    assert "<Conference" in response.text


# ── HTTP controls ────────────────────────────────────────────────────────────────

def _controls_app():
    from api.services.auth.sysevo_roles import require_sales_dialer_role
    from api.services.telephony.dialer.handling_routes import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_sales_dialer_role] = lambda: SimpleNamespace(provider_id=_REP)
    return TestClient(app)


def test_take_over_reaches_the_running_session():
    store_rpc = AsyncMock(return_value={"allowed": True, "as": "rep", "provider": "twilio",
                                        "prospect_call_id": _CHILD, "status": "active"})
    with patch.object(call_handling.HandlingStore, "_rpc", new=store_rpc), \
         patch.object(call_handling, "publish_command", new=AsyncMock(return_value=1)) as publish:
        response = _controls_app().post(f"/dialer/handling/{_PARENT}/take-over")
    assert response.status_code == 200 and response.json() == {"state": "handed", "via": "session"}
    publish.assert_awaited_once_with(_PARENT, "take_over", "rep")
    assert store_rpc.await_args.args[1]["p_user_id"] == _REP


def test_hand_to_rep_acts_directly_when_no_session_is_alive():
    store_rpc = AsyncMock(side_effect=[
        {"allowed": True, "as": "manager", "provider": "twilio", "prospect_call_id": _CHILD, "status": "active"},
        _context(), None,
    ])
    leg = FakeLeg()
    with patch.object(call_handling.HandlingStore, "_rpc", new=store_rpc), \
         patch.object(call_handling, "publish_command", new=AsyncMock(return_value=0)), \
         patch.object(call_handling, "default_leg_factory", new=AsyncMock(return_value=leg)), \
         patch.object(call_handling.call_stages, "record_call_stage", new=AsyncMock(return_value=True)):
        response = _controls_app().post(f"/dialer/handling/{_PARENT}/hand-to-rep")
    assert response.json() == {"state": "handed", "via": "direct"} and leg.calls == [("hand_back", "person")]


@pytest.mark.parametrize(("auth", "code"), [
    (None, 404),
    ({"allowed": False, "status": "handed"}, 409),
    ({"allowed": False, "status": "active"}, 403),
])
def test_controls_refuse_what_the_caller_may_not_do(auth, code):
    with patch.object(call_handling.HandlingStore, "_rpc", new=AsyncMock(return_value=auth)):
        response = _controls_app().post(f"/dialer/handling/{_PARENT}/end")
    assert response.status_code == code


def test_controls_reject_a_bad_key():
    assert _controls_app().post("/dialer/handling/bad key!/end").status_code in {400, 404}


# ── the agent's callback tool ────────────────────────────────────────────────────

_NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_callback_tool_stores_the_agreed_time():
    booker = AsyncMock(return_value={"entry_id": _uuid(1), "due_at": "2026-10-21T19:30:00+00:00"})
    result = await agent_callback.handle_tool_call(
        {"when": "2026-10-21T15:30:00-04:00", "note": "after lunch"},
        {"dialer_extra_answer_id": _uuid(2), "dialer_original_call_id": _CHILD}, now=_NOW, booker=booker,
    )
    assert result["status"] == "booked"
    call_id, due, note = booker.await_args.args
    assert call_id == _CHILD and due == datetime(2026, 10, 21, 19, 30, tzinfo=UTC) and note == "after lunch"


@pytest.mark.asyncio
async def test_callback_tool_clamps_and_refuses_bad_input():
    booker = AsyncMock(return_value={"due_at": "x"})
    ctx = {"dialer_extra_answer_id": _uuid(2), "dialer_original_call_id": _CHILD, "phone_timezone": "America/New_York"}
    await agent_callback.handle_tool_call({"when": "2026-10-20T11:01:00"}, ctx, now=_NOW, booker=booker)
    assert booker.await_args.args[1] == _NOW + agent_callback.MIN_AHEAD          # naive = their zone, clamped
    await agent_callback.handle_tool_call({"when": "2027-06-01T10:00:00Z"}, ctx, now=_NOW, booker=booker)
    assert booker.await_args.args[1] == _NOW + agent_callback.MAX_AHEAD
    assert (await agent_callback.handle_tool_call({"when": "tomorrow"}, ctx, now=_NOW, booker=booker))["status"] == "error"
    assert (await agent_callback.handle_tool_call({"when": "2026-10-21T10:00:00Z"}, {}, now=_NOW, booker=booker))["status"] == "error"


@pytest.mark.asyncio
async def test_callback_tool_is_advertised_only_on_dialer_agent_runs():
    from api.services.workflow.pipecat_engine_context_composer import compose_functions_for_node
    node = SimpleNamespace(document_uuids=[], tool_uuids=[], out_edges=[])
    without = await compose_functions_for_node(node=node, custom_tool_manager=None, kb_document_uuids=[])
    with_tool = await compose_functions_for_node(node=node, custom_tool_manager=None, kb_document_uuids=[],
                                                 dialer_callback_tz="America/New_York")

    def names(fns):
        return {getattr(f, "name", None) or (f.get("name") if isinstance(f, dict) else None) for f in fns}

    assert agent_callback.TOOL_NAME not in names(without)
    assert agent_callback.TOOL_NAME in names(with_tool)


def test_callback_parse_rejects_the_past():
    assert agent_callback.parse_when("2026-10-19T10:00:00Z", now=_NOW) is None
    assert agent_callback.parse_when("2026-10-22T10:00:00Z", now=_NOW) == _NOW + timedelta(days=1, hours=19)


@pytest.mark.asyncio
async def test_one_not_found_is_counted_once_however_long_the_silence():
    session, store, leg = await _session("ivr", store=FakeStore(),
                                         context={**_context(), "prospect_name": "", "enrichment": {}})
    await session.on_text("Sorry, that is not a valid entry.", is_final=True, now=3.0)
    for t in range(5, 15):
        await session.on_tick(float(t), silence=float(t - 3))
    assert session.progress.not_found == 1 and session.active


# ── SignalWire keypad / direct calls: the sorter tap (Phase 2 gap) ──────────────

from api.tests.test_signalwire_dialer_routes import (  # noqa: E402
    _authed, _dialer_compliance_env, _payload_of, connect_deps,  # noqa: F401 - fixtures
)


def _taps(document):
    return [step["tap"] for step in document["sections"]["main"] if "tap" in step]


@pytest.mark.asyncio
async def test_keypad_call_gets_a_prospect_side_sorter_tap(connect_deps, monkeypatch):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_dialer_connect
    monkeypatch.setenv("SYSEVO_DIALER_CALL_SORTER", "1")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg")
    document = _payload_of(await handle_sw_dialer_connect(_authed(
        {"params": {"lead": "+14155550123"}}, query={"call_id": "sw-call-0001"})))
    main = document["sections"]["main"]
    sorter = [t for t in _taps(document) if t.get("control_id") == "call-sorter"]
    assert len(sorter) == 1 and sorter[0]["direction"] == "listen"
    assert "/sw-call-sorter?" in sorter[0]["uri"] and "call_ref=sw-call-0001" in sorter[0]["uri"]
    tap_index = next(i for i, s in enumerate(main) if s.get("tap", {}).get("control_id") == "call-sorter")
    assert tap_index < next(i for i, s in enumerate(main) if "connect" in s)


@pytest.mark.asyncio
async def test_keypad_call_unchanged_with_the_sorter_off(connect_deps, monkeypatch):
    from api.services.telephony.dialer.signalwire_routes import handle_sw_dialer_connect
    monkeypatch.delenv("SYSEVO_DIALER_CALL_SORTER", raising=False)
    document = _payload_of(await handle_sw_dialer_connect(_authed(
        {"params": {"lead": "+14155550123"}}, query={"call_id": "sw-call-0001"})))
    assert not [t for t in _taps(document) if t.get("control_id") == "call-sorter"]
