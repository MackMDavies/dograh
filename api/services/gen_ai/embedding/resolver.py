"""One embedding-selection path for live calls and private configuration planning."""

from dataclasses import dataclass

_EMBEDDING_CAPABLE_PROVIDERS = ("openai", "openrouter")


@dataclass(frozen=True, repr=False)
class ResolvedEmbeddingConfiguration:
    """Internal only: never persist, log or return this credential-bearing object."""

    api_key: str | list[str] | None = None
    model: str | None = None
    base_url: str | None = None
    provider: str | None = None
    source: str = "unconfigured"

    def __repr__(self):
        return "ResolvedEmbeddingConfiguration()"

    def as_tuple(self):
        return self.api_key, self.model, self.base_url


async def resolve_embeddings_config(organization_id: int, user_config=None):
    """Preserve the live (api_key, model, base_url) API and selection order."""
    result = await _resolve_embeddings(organization_id, user_config, False)
    return result.as_tuple()


async def resolve_embedding_configuration_details(
    organization_id: int, user_config=None, *, db=None
):
    """Resolve the same selection with provider identity for isolated QA planning."""
    return await _resolve_embeddings(organization_id, user_config, True, db)


async def _resolve_embeddings(organization_id, user_config, include_metadata, db=None):
    if db is None:
        from api.db import db_client

        db = db_client

    if user_config and getattr(user_config, "embeddings", None):
        embedding = user_config.embeddings
        if getattr(embedding, "api_key", None):
            provider = getattr(embedding, "provider", None)
            return ResolvedEmbeddingConfiguration(
                embedding.api_key,
                embedding.model,
                getattr(embedding, "base_url", None),
                getattr(provider, "value", provider),
                "personal",
            )

    if include_metadata:
        default = await db.get_default_embedding_model_connection(
            organization_id, include_metadata=True
        )
        if default:
            return ResolvedEmbeddingConfiguration(**default)
    else:
        default = await db.get_default_embedding_model_connection(organization_id)
        if default:
            return ResolvedEmbeddingConfiguration(*default, source="org_default")

    def connection_result(connection, source, shared_key=False):
        if (
            include_metadata
            and source.startswith("org_")
            and connection.organization_id != organization_id
        ):
            raise ValueError("embedding_connection_scope_changed")
        config = {} if shared_key else (connection.extra_config or {})
        return ResolvedEmbeddingConfiguration(
            connection.api_key,
            None if shared_key else config.get("model"),
            None if shared_key else config.get("base_url"),
            connection.provider,
            source,
        )

    # These queries and their precedence intentionally match live retrieval.
    connections = await db.list_connections(organization_id, "embeddings")
    connection = next((item for item in connections if item.api_key), None)
    if connection:
        return connection_result(connection, "org_embedding")

    connections = await db.list_connections(organization_id)
    for provider in _EMBEDDING_CAPABLE_PROVIDERS:
        connection = next(
            (
                item
                for item in connections
                if item.provider == provider and item.api_key
            ),
            None,
        )
        if connection:
            return connection_result(connection, "org_shared_key", True)

    # Existing platform fallback is captured explicitly; it is not a new QA key source.
    connections = await db.list_all_connections_superuser("embeddings")
    connection = next((item for item in connections if item.api_key), None)
    if connection:
        return connection_result(connection, "platform_embedding")

    connections = await db.list_all_connections_superuser()
    for provider in _EMBEDDING_CAPABLE_PROVIDERS:
        connection = next(
            (
                item
                for item in connections
                if item.provider == provider and item.api_key
            ),
            None,
        )
        if connection:
            return connection_result(connection, "platform_shared_key", True)

    return ResolvedEmbeddingConfiguration()
