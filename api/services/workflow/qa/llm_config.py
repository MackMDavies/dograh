"""LLM configuration resolution and token usage accumulation."""

import random

from loguru import logger

from api.db import db_client
from api.db.models import WorkflowRunModel
from api.services.configuration.org_provider_resolver import (
    enrich_overrides_with_org_api_keys,
    resolve_org_provider_config,
)
from api.services.configuration.resolve import resolve_effective_config
from api.services.workflow.dto import QANodeData


async def resolve_llm_config(
    qa_data: QANodeData, workflow_run: WorkflowRunModel
) -> tuple[str, str, str, dict]:
    """Resolve the LLM provider, model, API key, and extra kwargs for QA analysis.

    If the QA node has its own LLM configuration (qa_use_workflow_llm=False),
    use those settings directly. Otherwise, fall back to the user's configured LLM.

    Returns:
        (provider, model, api_key, service_kwargs) tuple — service_kwargs can be
        passed directly to create_llm_service_from_provider as keyword arguments.
    """
    if not qa_data.qa_use_workflow_llm:
        provider = qa_data.qa_provider or "openai"
        kwargs = {}
        if provider == "azure":
            kwargs["endpoint"] = qa_data.qa_endpoint or ""
        return (
            provider,
            qa_data.qa_model,
            qa_data.qa_api_key,
            kwargs,
        )

    # Fall back to user's configured LLM
    provider, model, api_key, kwargs = await resolve_user_llm_config(workflow_run)

    if qa_data.qa_model and qa_data.qa_model != "default":
        model = qa_data.qa_model

    return provider, model, api_key, kwargs


async def _apply_org_resolution(user_configuration, workflow_run: WorkflowRunModel):
    """Layer org provider connections + run model_overrides onto the user config.

    Never raises: QA analysis is a post-call job, so a provider-lookup failure
    degrades to the personal config rather than losing the analysis entirely.
    """
    org_id = getattr(getattr(workflow_run, "workflow", None), "organization_id", None)
    if not org_id:
        return user_configuration

    try:
        user_configuration = await resolve_org_provider_config(
            org_id, user_configuration
        )

        run_configs = (
            getattr(getattr(workflow_run, "definition", None), "workflow_configurations", None)
            or {}
        )
        raw_overrides = run_configs.get("model_overrides")
        if raw_overrides:
            raw_overrides = await enrich_overrides_with_org_api_keys(
                raw_overrides, org_id
            )
            user_configuration = resolve_effective_config(
                user_configuration, raw_overrides
            )
    except Exception as e:
        logger.warning(
            f"QA analysis: org LLM resolution failed ({e}); "
            "falling back to personal configuration"
        )

    return user_configuration


async def resolve_user_llm_config(
    workflow_run: WorkflowRunModel,
) -> tuple[str, str, str, dict]:
    """Resolve the effective LLM for QA analysis.

    Mirrors the live call pipeline's resolution so analysis sees the same
    credentials the call itself used: personal UserConfiguration, then the
    organisation's provider connection, then the run's model_overrides.
    Reading only the personal config meant every analysed call on an
    org-managed key failed with "no_api_key".

    Returns:
        (provider, model, api_key, service_kwargs) tuple
    """
    user_id = None
    if workflow_run.workflow and workflow_run.workflow.user:
        user_id = workflow_run.workflow.user.id

    llm_config: dict = {}
    if user_id:
        user_configuration = await db_client.get_user_configurations(user_id)
        user_configuration = await _apply_org_resolution(
            user_configuration, workflow_run
        )
        llm_config = user_configuration.model_dump(exclude_none=True).get("llm", {})

    provider = llm_config.get("provider", "openai")
    api_key = llm_config.get("api_key", "")
    if isinstance(api_key, list):
        api_key = random.choice(api_key)
    model = llm_config.get("model", "gpt-4.1")

    kwargs = {}
    if provider == "azure":
        kwargs["endpoint"] = llm_config.get("endpoint", "")
    elif provider == "openrouter" and llm_config.get("base_url"):
        kwargs["base_url"] = llm_config["base_url"]

    return provider, model, api_key, kwargs


def accumulate_token_usage(total: dict, response) -> None:
    """Add token counts from an LLM response to the running total dict."""
    if not response.usage:
        return
    total["prompt_tokens"] = total.get("prompt_tokens", 0) + (
        response.usage.prompt_tokens or 0
    )
    total["completion_tokens"] = total.get("completion_tokens", 0) + (
        response.usage.completion_tokens or 0
    )
    total["total_tokens"] = total.get("total_tokens", 0) + (
        response.usage.total_tokens or 0
    )
    total["cache_read_input_tokens"] = total.get("cache_read_input_tokens", 0) + (
        getattr(response.usage, "cache_read_input_tokens", 0) or 0
    )
    cache_creation = getattr(response.usage, "cache_creation_input_tokens", None)
    if cache_creation is not None:
        total["cache_creation_input_tokens"] = (
            total.get("cache_creation_input_tokens") or 0
        ) + cache_creation
