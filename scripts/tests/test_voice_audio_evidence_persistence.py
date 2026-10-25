"""Actual immutable JSONB writes in the existing explicitly pinned local fixture."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from test_voice_tool_trace_persistence import (
    isolated_trace_database as isolated_trace_database,
    REPORT,
    TRACE,
)

EVIDENCE = json.loads((Path(__file__).resolve().parents[2] / "api/tests/fixtures/voiceAudioEvidence.v1.json").read_text())


@pytest.mark.asyncio
async def test_audio_evidence_is_immutable_and_idempotent_beside_prior_reports(isolated_trace_database):
    save, read = isolated_trace_database
    assert await save(audio_evidence=EVIDENCE)
    expected = await read()
    assert expected["agent_test_voice_result"] == REPORT
    assert expected["agent_test_voice_tool_trace"] == TRACE
    assert expected["agent_test_voice_audio_evidence"] == EVIDENCE
    assert await save(audio_evidence=copy.deepcopy(EVIDENCE))
    changed = copy.deepcopy(EVIDENCE);changed["evidence"]["complete"] = False
    assert not await save(audio_evidence=changed)
    assert await read() == expected


@pytest.mark.asyncio
async def test_conflicting_result_or_trace_cannot_partially_append_audio_evidence(isolated_trace_database):
    save, read = isolated_trace_database
    assert await save()
    expected = await read()
    assert not await save(result={"changed": True}, audio_evidence=EVIDENCE)
    assert not await save(trace={"changed": True}, audio_evidence=EVIDENCE)
    assert await read() == expected
    assert "agent_test_voice_audio_evidence" not in expected


@pytest.mark.asyncio
async def test_prior_writers_preserve_new_audio_evidence(isolated_trace_database):
    save, read = isolated_trace_database
    assert await save(audio_evidence=EVIDENCE)
    expected = await read()
    assert await save()
    assert await save(trace=None)
    assert await read() == expected


@pytest.mark.asyncio
async def test_only_one_concurrent_audio_evidence_writer_can_win(isolated_trace_database):
    save, read = isolated_trace_database
    first = copy.deepcopy(EVIDENCE);second = copy.deepcopy(EVIDENCE)
    second["evidence"]["complete"] = False
    outcomes = await asyncio.gather(*(save(audio_evidence=value) for value in [first, second]))
    assert sorted(outcomes) == [False, True]
    assert (await read())["agent_test_voice_audio_evidence"] == [first, second][outcomes.index(True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [{"run_id":124},{"workflow_id":186},{"organization_id":43},{"session_id":"wrong"}])
async def test_wrong_runtime_workflow_account_or_session_cannot_add_audio(scope, isolated_trace_database):
    save, read = isolated_trace_database
    before = await read()
    assert not await save(audio_evidence=EVIDENCE, **scope)
    assert await read() == before
