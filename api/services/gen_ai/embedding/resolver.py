"""Shared helper to resolve embedding API configuration from user or org settings."""

from typing import Optional, Tuple

from loguru import logger

_EMBEDDING_CAPABLE_PROVIDERS = ("openai", "openrouter")


async def resolve_embeddings_config(
    organization_id: int,
    user_config=None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve (api_key, model, base_url) for embeddings in priority order.

    1. User personal embeddings config (if user_config provided)
    2. Org default embedding model (is_default=True in org_available_models)
    3. Any active org connection with service_type="embeddings"
    4. Any active org connection from openai or openrouter (shared key fallback)
    """
    from api.db import db_client

    # 1. User personal config
    if user_config and getattr(user_config, "embeddings", None):
        emb = user_config.embeddings
        if getattr(emb, "api_key", None):
            return (emb.api_key, emb.model, getattr(emb, "base_url", None))

    # 2. Org default embedding model
    default = await db_client.get_default_embedding_model_connection(organization_id)
    if default:
        api_key, model_id, base_url = default
        logger.info(f"Using default embedding model for org {organization_id}: {model_id}")
        return (api_key, model_id, base_url)

    # 3. Any active org embedding connection
    embed_conns = await db_client.list_connections(organization_id, "embeddings")
    conn = next((c for c in embed_conns if c.api_key), None)
    if conn:
        logger.info(f"Using org embedding connection: provider={conn.provider} for org {organization_id}")
        return (
            conn.api_key,
            (conn.extra_config or {}).get("model"),
            (conn.extra_config or {}).get("base_url"),
        )

    # 4. Any connection from a provider that supports embeddings (same org)
    all_conns = await db_client.list_connections(organization_id)
    for provider in _EMBEDDING_CAPABLE_PROVIDERS:
        conn = next((c for c in all_conns if c.provider == provider and c.api_key), None)
        if conn:
            logger.info(f"Using {provider} connection key for embeddings (shared key) for org {organization_id}")
            return (conn.api_key, None, None)

    # 5. Platform-wide fallback: use any active embeddings connection across all orgs.
    #    On SaaS deployments the admin org configures API keys on behalf of all client orgs.
    platform_embed_conns = await db_client.list_all_connections_superuser("embeddings")
    conn = next((c for c in platform_embed_conns if c.api_key), None)
    if conn:
        logger.info(
            f"Using platform-level embedding connection: org={conn.organization_id} "
            f"provider={conn.provider} (fallback for org {organization_id})"
        )
        model_id = (conn.extra_config or {}).get("model")
        base_url = (conn.extra_config or {}).get("base_url")
        return (conn.api_key, model_id, base_url)

    # 6. Platform-wide fallback: any openai/openrouter connection across all orgs.
    platform_all_conns = await db_client.list_all_connections_superuser()
    for provider in _EMBEDDING_CAPABLE_PROVIDERS:
        conn = next((c for c in platform_all_conns if c.provider == provider and c.api_key), None)
        if conn:
            logger.info(
                f"Using platform-level {provider} key for embeddings "
                f"(org={conn.organization_id} fallback for org {organization_id})"
            )
            return (conn.api_key, None, None)

    return (None, None, None)
