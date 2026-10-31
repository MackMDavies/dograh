from api.services.pipecat.realtime.elevenlabs_realtime import receiver_failure_message


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
