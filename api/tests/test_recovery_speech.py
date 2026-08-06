"""The agent must never go silent when the LLM stalls.

A rate-limited or slow provider previously produced pure dead air: the caller
said "Hello? Hello?" and hung up. A short human line buys the pipeline time and
keeps the call alive.
"""

from api.services.pipecat.recovery_speech import (
    MAX_RECOVERY_LINES_PER_CALL,
    pick_recovery_line,
)


def test_speaks_something_on_the_first_stall():
    line = pick_recovery_line(0)
    assert line
    assert line.strip() == line


def test_consecutive_stalls_do_not_repeat_the_same_line():
    assert pick_recovery_line(0) != pick_recovery_line(1)


def test_gives_up_after_a_few_attempts_rather_than_looping_forever():
    # If the provider is genuinely down, filler on every turn is worse than
    # letting the call fail — the caller should not be strung along.
    assert pick_recovery_line(MAX_RECOVERY_LINES_PER_CALL) is None


def test_lines_are_short_enough_to_speak_quickly():
    for i in range(MAX_RECOVERY_LINES_PER_CALL):
        line = pick_recovery_line(i)
        assert len(line) <= 60, f"too long to be natural filler: {line!r}"


def test_lines_avoid_the_agents_banned_openers():
    banned = ("got it", "thanks for that", "sure", "great", "alright",
              "absolutely", "fantastic", "perfect")
    for i in range(MAX_RECOVERY_LINES_PER_CALL):
        line = pick_recovery_line(i).lower()
        assert not any(line.startswith(b) for b in banned), line


def test_negative_index_is_safe():
    assert pick_recovery_line(-1) is not None
