"""The call sorter's phrase rules, measured against real (anonymised) call openings.

Fixture: api/tests/fixtures/dialer_call_sorter_openings.json -- the answering party's
side of production dialer calls (dialer_call_analysis transcripts), hand-labelled.
  split=dev      used while writing the rules
  split=holdout  labelled blind afterwards and never tuned on
`label` is the verdict on the first 10 s of their speech; `final`, where present, the
verdict on the whole ~30 s window (mailbox-full after a greeting, a person picking up
through a screener, the iPhone screener's "leave an additional message").

Run with -s to see the per-class report.

The fixture holds prospect call transcripts, so it is kept OUT of this public repo
(listed in .git/info/exclude). Without it these measurements are skipped.
"""

import collections
import json
from pathlib import Path

import pytest

from api.services.telephony.dialer.call_sorter_rules import (
    STAGE_FOR_KIND,
    classify_opening,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dialer_call_sorter_openings.json"
if not FIXTURE.exists():
    pytest.skip("private call-opening fixture is not present", allow_module_level=True)
ITEMS = json.loads(FIXTURE.read_text())["items"]
OPENING_SECONDS = 10.0
DEEPGRAM_LAG = 0.3


def _view(segments, cut):
    """Text heard by `cut` seconds, and how long the far end was then quiet."""
    heard = [s for s in segments if s["t"] <= cut]
    if not heard:
        return "", 0.0
    following = next((s for s in segments if s["t"] > cut), None)
    silence = 99.0 if following is None else following["t"] - heard[-1]["e"]
    return " ".join(s["x"] for s in heard), silence


def _metrics(pairs):
    confusion = collections.Counter(pairs)
    kinds = sorted({k for pair in confusion for k in pair} - {"unknown"})
    rows = {}
    for kind in kinds:
        tp = confusion[(kind, kind)]
        fp = sum(v for (exp, got), v in confusion.items() if got == kind and exp != kind)
        fn = sum(v for (exp, got), v in confusion.items() if exp == kind and got != kind)
        rows[kind] = {
            "n": tp + fn,
            "precision": tp / (tp + fp) if tp + fp else 1.0,
            "recall": tp / (tp + fn) if tp + fn else 1.0,
        }
    return rows, confusion


def _report(title, rows):
    lines = [f"\n{title}", f"{'class':22s} {'n':>4s} {'precision':>9s} {'recall':>7s}"]
    for kind, row in rows.items():
        lines.append(f"{kind:22s} {row['n']:4d} {row['precision']:9.3f} {row['recall']:7.3f}")
    print("\n".join(lines))


def _opening_pairs(split):
    pairs = []
    for item in ITEMS:
        if item.get("split", "dev") != split:
            continue
        text, silence = _view(item["segments"], OPENING_SECONDS)
        pairs.append((item["label"], classify_opening(text, silence_seconds=silence).kind))
    return pairs


@pytest.mark.parametrize("split,floor", [("dev", 0.97), ("holdout", 0.95)])
def test_opening_precision_and_recall_per_class(split, floor):
    rows, confusion = _metrics(_opening_pairs(split))
    _report(f"call sorter -- first {OPENING_SECONDS:.0f} s, split={split}", rows)
    for kind, row in rows.items():
        assert row["precision"] >= floor, (kind, row, confusion)
        assert row["recall"] >= floor - 0.12, (kind, row, confusion)


def test_whole_window_transitions():
    pairs = []
    for item in ITEMS:
        if "final" not in item:
            continue
        text, silence = _view(item["segments"], 99.0)
        pairs.append((item["final"], classify_opening(text, silence_seconds=silence).kind))
    rows, confusion = _metrics(pairs)
    _report("call sorter -- whole window, items whose verdict changes", rows)
    assert len(pairs) >= 20
    assert all(exp == got for exp, got in pairs), confusion


def test_the_costly_mistakes_never_happen_on_the_fixture():
    """Streamed sentence by sentence, the way the sorter sees a call:
    a voicemail is never taken for a screener, a screener never for a machine,
    and a live person never for a machine or a full mailbox."""
    bad = []
    for item in ITEMS:
        segments = [s for s in item["segments"] if s["t"] <= OPENING_SECONDS]
        kinds = []
        for k, segment in enumerate(segments):
            text = " ".join(s["x"] for s in segments[: k + 1])
            following = segments[k + 1]["t"] if k + 1 < len(segments) else None
            silence = 99.0 if following is None else following - segment["e"]
            kind = classify_opening(text, silence_seconds=silence).kind
            if kind != "unknown" and (not kinds or kinds[-1] != kind):
                kinds.append(kind)
        label = item["label"]
        if label == "voicemail" and "screener" in kinds:
            bad.append((item["id"], kinds))
        if label == "screener" and {"voicemail", "screener_unavailable", "mailbox_full"} & set(kinds):
            bad.append((item["id"], kinds))
        if label == "person" and {"voicemail", "mailbox_full"} & set(kinds):
            bad.append((item["id"], kinds))
    assert bad == []


def test_decision_latency_on_the_fixture():
    """Seconds from the far end's first word to the first verdict, at sentence
    granularity (live interim results land earlier than this)."""
    times = collections.defaultdict(list)
    for item in ITEMS:
        segments = [s for s in item["segments"] if s["t"] <= OPENING_SECONDS]
        for k, segment in enumerate(segments):
            text = " ".join(s["x"] for s in segments[: k + 1])
            following = segments[k + 1]["t"] if k + 1 < len(segments) else None
            gap = 99.0 if following is None else following - segment["e"]
            verdict = classify_opening(text)
            when = segment["e"] + DEEPGRAM_LAG
            if not verdict.decided:
                verdict = classify_opening(text, silence_seconds=gap)
                when = segment["e"] + min(gap, 2.5)
            if verdict.decided:
                times[item["label"]].append(when)
                break
    print("\ncall sorter -- seconds to first verdict (median / p90)")
    for kind, values in sorted(times.items()):
        values.sort()
        print(f"{kind:14s} n={len(values):4d} {values[len(values) // 2]:5.1f} {values[int(len(values) * 0.9)]:5.1f}")
    person = sorted(times["person"])
    assert person[len(person) // 2] <= 4.0


# ── the rules that matter, spelled out ──────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Hi. If you record your name and reason for calling, I'll see if this person is available.",
    "Hello. Please state your name after the tone, and Google Voice will try to connect you.",
    "Hey. Thanks for calling. To connect and talk, just say your name and what you're calling about after the beep.",
    "Hi. I'm Call Assist by Google recording this call for the person you're trying to reach. Before I try to connect you, can I ask what you're calling about?",
    "Hi. I'm a call assistant recording this call for the person you're trying to reach. Please say who you are and why you're calling.",
])
def test_a_screener_asking_for_a_name_is_never_a_voicemail(text):
    verdict = classify_opening(text, silence_seconds=5.0)
    assert verdict.kind == "screener"
    assert verdict.stage == "screener"


def test_screener_vendor_is_reported():
    assert classify_opening("If you record your name and reason for calling").detail["vendor"] == "iphone"
    assert classify_opening("Hi. I'm call assist by Google recording this call").detail["vendor"] == "google"


def test_screener_that_offers_a_message_counts_as_a_machine():
    verdict = classify_opening(
        "Hi. If you record your name and reason for calling, I'll see if this person is available. "
        "I'm sorry. This person is not available. If you would like to leave an additional message, "
        "please reply after the tone."
    )
    assert verdict.kind == "screener_unavailable"
    assert verdict.stage == "machine"
    assert verdict.end_of_greeting_cue is True


def test_screener_that_declines_without_a_message_stays_a_screener():
    verdict = classify_opening(
        "Hi. I'm Call Assist by Google recording this call for the person you're trying to reach. "
        "Before I try to connect you, can I ask what you're calling about? "
        "Unfortunately, the person you're calling cannot take your call right now. Have a nice day."
    )
    assert verdict.kind == "screener"
    assert verdict.detail.get("declined") is True


def test_person_picking_up_through_a_screener():
    verdict = classify_opening(
        "Hi. If you record your name and reason for calling, I'll see if this person is available. "
        "Thanks, Jordan. Please stay on the line. Hello? Who's this?"
    )
    assert verdict.kind == "person"


@pytest.mark.parametrize("text", [
    "Hi. You've reached Jordan. Please leave your name, number, and the reason for your call after the tone.",
    "Your call has been forwarded to voice mail. The person you're trying to reach is not available. At the tone, please record your message.",
    "Please leave your message for (555) 010-0000.",
    "Telephone number (555) 010-0000 can't take your call now.",
])
def test_voicemail_greetings(text):
    verdict = classify_opening(text)
    assert verdict.kind == "voicemail"
    assert verdict.stage == "machine"


def test_end_of_greeting_cue_only_when_the_greeting_says_so():
    assert classify_opening("You've reached Jordan. Please leave a message.").end_of_greeting_cue is False
    assert classify_opening("You've reached Jordan. Please leave a message after the tone.").end_of_greeting_cue is True


@pytest.mark.parametrize("text,reason", [
    ("Please leave your message for Jordan. Sorry. Mailbox is full.", "full"),
    ("(555) 010-0000 is not available. The mailbox is full and cannot accept any messages at this time.", "full"),
    ("I'm sorry. The person you are trying to reach has a voice mailbox that has not been set up yet.", "not_set_up"),
])
def test_mailbox_full_beats_the_greeting_before_it(text, reason):
    verdict = classify_opening(text)
    assert verdict.kind == "mailbox_full"
    assert verdict.detail["reason"] == reason


@pytest.mark.parametrize("text,kind", [
    ("Thank you for calling Jordan. For sales, press 1. For support, press 2.", "menu"),
    ("Thank you for calling Jordan. Please hold for the next available agent.", "hold"),
    ("The number you have dialed is no longer in service.", "intercept"),
])
def test_ivr_kinds(text, kind):
    verdict = classify_opening(text)
    assert verdict.kind == "ivr"
    assert verdict.detail["kind"] == kind


def test_menu_that_goes_to_a_mailbox_is_a_voicemail():
    text = ("Thank you for contacting Jordan. Please press 1 to leave a detailed message. "
            "Record your message and press pound, or press star to contact the operator.")
    assert classify_opening(text).kind == "voicemail"
    assert classify_opening("Thank you for calling Jordan. To leave a message in our general mailbox, press 3.").kind == "ivr"


def test_receptionist_is_a_person_and_a_gatekeeper_with_a_name():
    verdict = classify_opening("Thank you for calling Jordan Systems. This is Sarah. How can I help you?")
    assert verdict.kind == "person"
    assert verdict.gatekeeper is True
    assert verdict.name == "Sarah"


def test_direct_pickup_is_a_person_not_a_gatekeeper():
    verdict = classify_opening("Hello. This is James.", silence_seconds=2.0)
    assert verdict.kind == "person"
    assert verdict.gatekeeper is False
    assert verdict.name == "James"


def test_unavailable_boss_with_a_receptionist_is_a_person():
    verdict = classify_opening("Jordan Systems. He's not available right now. Can I take a message?")
    assert verdict.kind == "person"
    assert verdict.gatekeeper is True


def test_a_short_hello_needs_the_quiet_after_it():
    assert classify_opening("Hello. This is James.").kind == "unknown"
    assert classify_opening("Hello. This is James.", silence_seconds=1.0).kind == "unknown"
    assert classify_opening("Hello. This is James.", silence_seconds=1.6).kind == "person"
    # One word needs longer: greetings pause after "Hi." too.
    assert classify_opening("Hi.", silence_seconds=2.0).kind == "unknown"
    assert classify_opening("Hi.", silence_seconds=2.6).kind == "person"
    # "Hello?" asks a question: no wait needed.
    assert classify_opening("Hello?").kind == "person"


def test_a_cut_off_interim_is_never_taken_for_a_person():
    assert classify_opening("Hey. This is Jordan. I couldn't", silence_seconds=5.0).kind == "unknown"


def test_recorded_call_notice_then_a_person():
    assert classify_opening("This call may be recorded for quality assurance purposes.", silence_seconds=3).kind == "ivr"
    assert classify_opening("Being recorded. Hi. Yeah. It's Jordan.", silence_seconds=3).kind == "person"


def test_stage_mapping_matches_the_contract():
    contract = {"person", "machine", "beep", "mailbox_full", "screener", "ivr"}
    assert set(STAGE_FOR_KIND.values()) <= contract


def test_empty_and_noise():
    assert classify_opening("").kind == "unknown"
    assert classify_opening("   ").kind == "unknown"
    assert classify_opening(None).kind == "unknown"  # type: ignore[arg-type]
