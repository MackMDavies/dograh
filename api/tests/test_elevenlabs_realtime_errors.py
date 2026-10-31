from api.services.pipecat.realtime.elevenlabs_realtime import (
    opening_input_text,
    receiver_failure_message,
)


def test_quota_disconnect_returns_actionable_safe_message():
    message = receiver_failure_message(
        RuntimeError("received 1002: This request exceeds your quota limit.")
    )

    assert message == (
        "ElevenLabs quota limit reached. Check the connected account's plan or "
        "available quota, then try again."
    )
    assert "1002" not in message


def test_other_disconnects_keep_generic_restart_message():
    assert receiver_failure_message(RuntimeError("network closed")) == (
        "ElevenLabs Realtime stopped receiving responses. Please restart the call."
    )


def test_non_english_opening_translates_the_authored_greeting():
    message = opening_input_text("Hi, thanks for calling Sysevo.", "es")

    assert "configured language (es)" in message
    assert "Translate the greeting below naturally" in message
    assert "say exactly the greeting" not in message
    assert "Hi, thanks for calling Sysevo." in message


def test_english_opening_preserves_exact_greeting_behavior():
    assert opening_input_text("Hello.", "en-US") == (
        "Begin the conversation by saying exactly the greeting below. "
        "Do not add an introduction or any other words.\n\n<greeting>Hello.</greeting>"
    )
