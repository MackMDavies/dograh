"""Scoped, version-preserving migration of registered demo/provisioned workflows.

Prepare: python -m scripts.migrate_generated_agents_to_elevenlabs_realtime manifest.json plan.json
Apply:   python -m scripts.migrate_generated_agents_to_elevenlabs_realtime manifest.json plan.json --apply

The plan contains private configuration snapshots. Keep it on the server, mode 0600.
No image changes or restarts are required. An apply aborts if any target changed.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

MODEL = "eleven_realtime_v1_mini"
PROVIDER = "elevenlabs_realtime"


def realtime_configuration(config, voice):
    result = copy.deepcopy(config or {})
    overrides = result.setdefault("model_overrides", {})
    old = overrides.get("realtime") or {}
    # Never reuse another provider's API key, endpoint, or model tuning.
    realtime = copy.deepcopy(old) if old.get("provider") == PROVIDER else {}
    realtime.update(provider=PROVIDER, model=MODEL, voice=voice)
    overrides.update(is_realtime=True, realtime=realtime)
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def snapshot(workflow, definitions):
    return {
        "id": workflow.id, "uuid": workflow.workflow_uuid,
        "organization_id": workflow.organization_id, "user_id": workflow.user_id,
        "status": str(workflow.status), "released_definition_id": workflow.released_definition_id,
        "row_config": workflow.workflow_configurations,
        "definitions": [{
            "id": d.id, "status": d.status, "version_number": d.version_number,
            "workflow_json": d.workflow_json,
            "workflow_configurations": d.workflow_configurations,
            "template_context_variables": d.template_context_variables,
        } for d in definitions],
    }


async def resolve_configuration(db, user_id, organization_id, overrides):
    # Older layered production images inline this same resolution sequence.
    from api.services.configuration.org_provider_resolver import (
        enrich_config_from_org_connections, enrich_overrides_with_org_api_keys,
        resolve_org_provider_config,
    )
    from api.services.configuration.resolve import resolve_effective_config
    config = await db.get_user_configurations(user_id)
    if organization_id:
        config = await resolve_org_provider_config(organization_id, config)
        config = await enrich_config_from_org_connections(organization_id, config)
        if overrides:
            overrides = await enrich_overrides_with_org_api_keys(overrides, organization_id)
    return resolve_effective_config(config, overrides)


async def main():
    from sqlalchemy import or_, select
    from api.db import db_client
    from api.db.models import WorkflowModel, WorkflowDefinitionModel, VoiceLibraryModel

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("model") != MODEL:
        raise RuntimeError("Manifest must explicitly request the approved Realtime model")

    async with db_client.async_session() as session:
        workflows = {}
        for target in manifest["targets"]:
            conditions = []
            if target.get("workflow_id"):
                conditions.append(WorkflowModel.id == target["workflow_id"])
            if target.get("workflow_uuid"):
                conditions.append(WorkflowModel.workflow_uuid == target["workflow_uuid"])
            if not conditions:
                raise RuntimeError("Manifest target has no workflow identity")
            found = (await session.execute(select(WorkflowModel).where(or_(*conditions)))).scalars().all()
            if len(found) != 1:
                raise RuntimeError("Target must resolve to exactly one existing workflow")
            w = found[0]
            if target.get("workflow_uuid") and w.workflow_uuid != target["workflow_uuid"]:
                raise RuntimeError("Manifest ID and UUID disagree")
            workflows[w.id] = w

        prepared = json.loads(args.plan.read_text()) if args.apply else {"model": MODEL, "items": []}
        if args.apply and set(workflows) != {p["before"]["id"] for p in prepared["items"]}:
            raise RuntimeError("Target set changed since preparation")
        for wid in sorted(workflows):
            w = (await session.execute(select(WorkflowModel).where(WorkflowModel.id == wid).with_for_update())).scalar_one()
            definitions = (await session.execute(select(WorkflowDefinitionModel).where(
                WorkflowDefinitionModel.workflow_id == wid,
            ).order_by(WorkflowDefinitionModel.id).with_for_update())).scalars().all()
            live = next((d for d in definitions if d.id == w.released_definition_id and d.status == "published"), None)
            if live is None:
                raise RuntimeError(f"Workflow {wid} has no released published definition")
            before = snapshot(w, definitions)
            if args.apply:
                item = next(p for p in prepared["items"] if p["before"]["id"] == wid)
                if fingerprint(before) != item["fingerprint"]:
                    raise RuntimeError(f"Workflow {wid} changed; prepare again")
                cfg = item["configuration"]
            else:
                effective = await resolve_configuration(db_client, w.user_id, w.organization_id,
                    (live.workflow_configurations or {}).get("model_overrides"))
                old_rt = (live.workflow_configurations or {}).get("model_overrides", {}).get("realtime") or {}
                voice = old_rt.get("voice") if old_rt.get("provider") == PROVIDER else None
                tts = effective.tts
                if not voice and tts and tts.provider in {"elevenlabs", "dograh_clone"}:
                    voice = tts.voice
                tts_override = (live.workflow_configurations or {}).get("model_overrides", {}).get("tts") or {}
                if tts_override.get("voice_uuid"):
                    library = (await session.execute(select(VoiceLibraryModel).where(
                        VoiceLibraryModel.uuid == tts_override["voice_uuid"],
                        or_(VoiceLibraryModel.organization_id == w.organization_id, VoiceLibraryModel.is_public.is_(True)),
                    ))).scalars().first()
                    if library and library.provider in {"elevenlabs", "dograh_clone"} and library.provider_voice_id:
                        voice = library.provider_voice_id
                voice = voice or "brian"
                cfg = realtime_configuration(live.workflow_configurations, voice)
                verified = await resolve_configuration(db_client, w.user_id, w.organization_id, cfg["model_overrides"])
                if not (verified.is_realtime and verified.realtime and verified.realtime.provider == PROVIDER
                        and verified.realtime.model == MODEL and verified.realtime.api_key):
                    raise RuntimeError(f"Workflow {wid} cannot resolve an authenticated ElevenLabs Realtime configuration")
                prepared["items"].append({"before": before, "fingerprint": fingerprint(before), "configuration": cfg})

            if args.apply:
                # Add a published version of the SAME graph. Existing drafts keep their
                # graph/prompts; only the requested model policy is patched on them.
                version = WorkflowDefinitionModel(
                    workflow_id=wid, workflow_json=copy.deepcopy(live.workflow_json),
                    workflow_configurations=cfg,
                    template_context_variables=copy.deepcopy(live.template_context_variables),
                    status="published", is_current=True,
                    version_number=max((d.version_number or 0 for d in definitions), default=0) + 1,
                    published_at=datetime.now(UTC),
                )
                session.add(version)
                await session.flush()
                for d in definitions:
                    if d.status == "published":
                        d.status, d.is_current = "archived", False
                    elif d.status == "draft":
                        d.workflow_configurations = realtime_configuration(d.workflow_configurations, cfg["model_overrides"]["realtime"]["voice"])
                w.released_definition_id = version.id
                w.workflow_configurations = realtime_configuration(w.workflow_configurations, cfg["model_overrides"]["realtime"]["voice"])
                # Mirror semantics of publish_workflow_draft without publishing user drafts.
                w.workflow_definition = copy.deepcopy(live.workflow_json)
                w.template_context_variables = copy.deepcopy(live.template_context_variables)

        if args.apply:
            await session.commit()
        else:
            fd = os.open(args.plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(prepared, file, default=str)
            await session.rollback()
        print(json.dumps({"applied" if args.apply else "prepared": True, "workflows": len(workflows), "model": MODEL}))


if __name__ == "__main__":
    asyncio.run(main())
