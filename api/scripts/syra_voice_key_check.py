"""Read-only: which LLM key does the Syra voice workflow actually carry?

Prints, for workflow 206 and its three most recent runs, the definition in use and the
LENGTH and first 4 characters of the llm override's api_key — enough to tell an OpenAI key
(sk-p…, ~164) from Syra's own (hex, 64) without printing either. Changes nothing.

    docker exec dograh-api-1 python -m api.scripts.syra_voice_key_check
"""
import asyncio

from sqlalchemy import select

from api.db import db_client
from api.db.models import WorkflowDefinitionModel, WorkflowModel, WorkflowRunModel

WORKFLOW_ID = 206


def _key(cfg):
    k = ((cfg or {}).get("model_overrides") or {}).get("llm", {}).get("api_key")
    if isinstance(k, list):
        return f"LIST of {len(k)}: " + ", ".join(f"len={len(x)} head={x[:4]}" for x in k)
    return f"len={len(k or '')} head={(k or '')[:4]}" if k else "none"


async def main():
    async with db_client.async_session() as s:
        wf = (await s.execute(select(WorkflowModel).where(WorkflowModel.id == WORKFLOW_ID))).scalar_one_or_none()
        if wf is None:
            print("workflow not found")
            return
        print(f"workflow {wf.id}: released_definition_id={wf.released_definition_id}")
        print(f"  workflow row configs: {_key(getattr(wf, 'workflow_configurations', None))}")
        defs = (await s.execute(
            select(WorkflowDefinitionModel).where(WorkflowDefinitionModel.workflow_id == WORKFLOW_ID).order_by(WorkflowDefinitionModel.id.desc()).limit(6)
        )).scalars().all()
        for d in defs:
            print(f"  definition {d.id} v{getattr(d, 'version_number', '?')} {getattr(d, 'status', '?')}: {_key(d.workflow_configurations)}")
        runs = (await s.execute(
            select(WorkflowRunModel).where(WorkflowRunModel.workflow_id == WORKFLOW_ID).order_by(WorkflowRunModel.id.desc()).limit(3)
        )).scalars().all()
        for r in runs:
            print(f"  run {r.id}: definition_id={getattr(r, 'definition_id', None)} created={r.created_at}")


asyncio.run(main())
