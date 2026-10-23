"""Wire agent policy checks to the current workflow and approved org models."""

from datetime import datetime, timezone

from fastapi import HTTPException
from pipecat.frames.frames import InterruptionFrame, LLMContextFrame, LLMMessagesAppendFrame, TTSSpeakFrame

from api.services.pipecat.voice_guardrails import (
    GuardrailJudge, VoiceInputGuardrailProcessor, VoiceOutputGuardrailProcessor,
    RealtimeVoiceOutputGuardrailProcessor, build_guardrail_validation_rules,
    normalize_guardrails_config,
)


async def resolve_guardrail_models(config, organization_id, db, factory):
    rules = build_guardrail_validation_rules(config)
    values = [r.get("evaluation_model_id") for r in rules if r.get("evaluation_model_id") is not None]
    if any(type(value) is not int or value <= 0 for value in values):
        raise HTTPException(422, "Choose an available guardrail evaluation model.")
    ids = set(values)
    if not ids:
        return {}
    available = await db.list_available_models(organization_id, service_type="llm", client_only=True)
    selected = {row.id: row for row in available if row.id in ids}
    if selected.keys() != ids:
        raise HTTPException(422, "A guardrail model is no longer available in this workspace.")
    models = {}
    for key, row in selected.items():
        conn = await db.get_connection(row.connection_id, organization_id)
        if not conn or conn.service_type != "llm" or conn.provider != row.provider:
            raise HTTPException(422, "The guardrail model connection is unavailable.")
        extra = conn.extra_config or {}
        allowed = {k: extra[k] for k in ("base_url", "endpoint", "aws_access_key", "aws_secret_key", "aws_region", "project_id", "location", "credentials") if k in extra}
        models[key] = factory(row.provider, row.model_id, conn.api_key, **allowed)
    return models


async def create_guardrail_processors(*, config, engine, llm, organization_id, db, factory, on_event, realtime):
    rules = build_guardrail_validation_rules(config)
    if not rules:
        return None, None
    settings = normalize_guardrails_config(config)
    models = await resolve_guardrail_models(config, organization_id, db, factory)

    def policy_context():
        context = engine.context
        messages = context.get_messages() if context is not None else []
        system = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
        recent = [m for m in messages if isinstance(m, dict) and m.get("role") in {"user", "assistant"}][-6:]
        instructions = getattr(getattr(engine.llm, "_settings", None), "system_instruction", "") or ""
        # Realtime adapters keep current node instructions in settings, rather
        # than a system message in LLMContext. Focus checks must see those too.
        return (str(instructions)[:5000] + "\n" + "\n".join(f"{m.get('role')}: {str(m.get('content', ''))[:1000]}" for m in system[:2] + recent))[:8000]

    attempts = 0
    handling = False

    async def on_violation(rule, text):
        nonlocal attempts, handling
        if handling or getattr(engine, "_call_disposed", False):
            return
        handling = True
        try:
            action = rule.get("action", "end_call")
            attempts += 1
            if attempts > 3:
                action = "end_call"
            await on_event({"type": "voice_guardrail_violation", "timestamp": datetime.now(timezone.utc).isoformat(), "payload": {
                "rule_id": rule["id"], "rule_name": rule["name"], "direction": rule.get("direction", "output"),
                "action": action, "execution_mode": rule.get("mode", "streaming"),
            }})
            # Flush already queued audio and cancel provider generation before any action.
            await engine.llm.queue_frame(InterruptionFrame())
            if action == "end_call":
                engine._mute_pipeline = True
                await engine.end_call_with_reason("voice_guardrail_violation", abort_immediately=True)
                return
            message = settings["response_message"]
            if action == "safe_response" and not realtime:
                await engine.task.queue_frame(TTSSpeakFrame(message, append_to_context=True, persist_to_logs=True))
                return
            feedback = "The previous turn was blocked by an agent policy. Do not repeat prohibited content. "
            if action == "safe_response":
                feedback += "Say exactly this approved safe response and nothing else: " + message
            elif action == "retry":
                feedback += "Regenerate a compliant answer to the caller, following all agent instructions."
            else:
                feedback += "Briefly acknowledge the boundary and redirect to a relevant allowed topic. Suggested response: " + message
            if realtime:
                await engine.llm.queue_frame(LLMMessagesAppendFrame(messages=[{"role": "system", "content": feedback}], run_llm=True))
            else:
                engine.context.messages.append({"role": "system", "content": feedback})
                await engine.task.queue_frame(LLMContextFrame(engine.context))
        finally:
            handling = False

    def reset_attempts():
        nonlocal attempts
        attempts = 0

    judge = GuardrailJudge(llm, models=models, context_provider=policy_context, on_check=on_event, cleanup_models=True)
    input_processor = VoiceInputGuardrailProcessor(judge=judge, rules=rules, on_violation=on_violation, realtime=realtime, on_user_turn=reset_attempts)
    output_type = RealtimeVoiceOutputGuardrailProcessor if realtime else VoiceOutputGuardrailProcessor
    kwargs = {} if realtime else {"force_speech": settings["force_speech"], "force_speech_message": settings["force_speech_message"]}
    output_processor = output_type(llm=llm, rules=rules, on_violation=on_violation, judge=judge, **kwargs)
    return input_processor, output_processor
