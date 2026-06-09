"""API routes for knowledge base operations."""

import os
import tempfile
import uuid
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from api.db import db_client
from api.enums import PostHogEvent
from api.schemas.knowledge_base import (
    ChunkSearchRequestSchema,
    ChunkSearchResponseSchema,
    DocumentListResponseSchema,
    DocumentResponseSchema,
    DocumentUploadRequestSchema,
    DocumentUploadResponseSchema,
    ProcessDocumentRequestSchema,
    SetDocumentGlobalRequestSchema,
    UpdateDocumentContentSchema,
    WorkflowDocumentListResponseSchema,
)
from api.sdk_expose import sdk_expose
from api.services.auth.depends import get_user
from api.services.posthog_client import capture_event
from api.services.storage import storage_fs
from api.tasks.arq import enqueue_job
from api.tasks.function_names import FunctionNames

router = APIRouter(prefix="/knowledge-base", tags=["knowledge-base"])


@router.post(
    "/upload-url",
    response_model=DocumentUploadResponseSchema,
    summary="Get presigned URL for document upload",
)
async def get_upload_url(
    request: DocumentUploadRequestSchema,
    user=Depends(get_user),
):
    """Generate a presigned PUT URL for uploading a document.

    This endpoint:
    1. Generates a unique document UUID for organizing the S3 key
    2. Generates a presigned S3/MinIO URL for uploading the file
    3. Returns the upload URL and document metadata

    After uploading to the returned URL, call /process-document to create
    the document record and trigger processing.

    Access Control:
    * All authenticated users can upload documents scoped to their organization.
    """

    try:
        # Generate unique document UUID for S3 organization
        document_uuid = str(uuid.uuid4())

        # Generate S3 key: knowledge_base/{org_id}/{document_uuid}/{filename}
        s3_key = f"knowledge_base/{user.selected_organization_id}/{document_uuid}/{request.filename}"

        # Generate presigned PUT URL (valid for 30 minutes)
        upload_url = await storage_fs.aget_presigned_put_url(
            file_path=s3_key,
            expiration=1800,  # 30 minutes
            content_type=request.mime_type,
            max_size=100_000_000,  # 100MB max
        )

        if not upload_url:
            raise HTTPException(
                status_code=500, detail="Failed to generate presigned upload URL"
            )

        logger.info(
            f"Generated upload URL for document {document_uuid}, "
            f"user {user.id}, org {user.selected_organization_id}"
        )

        return DocumentUploadResponseSchema(
            upload_url=upload_url,
            document_uuid=document_uuid,
            s3_key=s3_key,
        )

    except Exception as exc:
        logger.error(f"Error generating upload URL: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to generate upload URL"
        ) from exc


@router.post(
    "/process-document",
    response_model=DocumentResponseSchema,
    summary="Trigger document processing",
)
async def process_document(
    request: ProcessDocumentRequestSchema,
    user=Depends(get_user),
):
    """Trigger asynchronous processing of an uploaded document.

    This endpoint should be called after successfully uploading a file to the presigned URL.
    It will:
    1. Create a document record in the database with the specified UUID
    2. Enqueue a background task to process the document (chunking and embedding)

    The document status will be updated from 'pending' -> 'processing' -> 'completed' or 'failed'.

    Embedding:
    Uses OpenAI text-embedding-3-small (1536-dimensional embeddings, requires API key configured in Model Configurations).

    Access Control:
    * Users can only process documents in their organization.
    """

    try:
        # Extract filename from s3_key
        filename = request.s3_key.split("/")[-1]

        # Create document record with the specific UUID from upload
        document = await db_client.create_document(
            organization_id=user.selected_organization_id,
            created_by=user.id,
            filename=filename,
            file_size_bytes=0,  # Will be updated by background task
            file_hash="",  # Will be computed by background task
            mime_type="application/octet-stream",  # Will be detected by background task
            custom_metadata={"s3_key": request.s3_key},
            document_uuid=request.document_uuid,  # Use UUID from upload
            retrieval_mode=request.retrieval_mode,
        )

        # Enqueue background task for processing
        await enqueue_job(
            FunctionNames.PROCESS_KNOWLEDGE_BASE_DOCUMENT,
            document.id,
            request.s3_key,
            user.selected_organization_id,
            str(user.provider_id),
            128,  # max_tokens (default)
            request.retrieval_mode,
        )

        logger.info(
            f"Created document {request.document_uuid} (id={document.id}) and enqueued processing "
            f"with OpenAI embeddings, org {user.selected_organization_id}"
        )

        capture_event(
            distinct_id=str(user.provider_id),
            event=PostHogEvent.KNOWLEDGE_BASE_CREATED,
            properties={
                "document_id": document.id,
                "document_uuid": str(request.document_uuid),
                "filename": filename,
                "retrieval_mode": request.retrieval_mode,
                "organization_id": user.selected_organization_id,
            },
        )

        return DocumentResponseSchema(
            id=document.id,
            document_uuid=request.document_uuid,
            filename=filename,
            file_size_bytes=0,
            file_hash="",
            mime_type="application/octet-stream",
            processing_status="pending",
            processing_error=None,
            total_chunks=0,
            retrieval_mode=request.retrieval_mode,
            custom_metadata={"s3_key": request.s3_key},
            docling_metadata={},
            source_url=None,
            created_at=document.created_at,
            updated_at=document.updated_at,
            organization_id=user.selected_organization_id,
            created_by=user.id,
            is_active=True,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error processing document: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to process document"
        ) from exc


@router.get(
    "/documents",
    response_model=DocumentListResponseSchema,
    summary="List documents",
    **sdk_expose(
        method="list_documents",
        description="List knowledge base documents available to the authenticated organization.",
    ),
)
async def list_documents(
    status: Annotated[
        Optional[str],
        Query(description="Filter by processing status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    user=Depends(get_user),
):
    """List all documents for the user's organization.

    Access Control:
    * Users can only see documents from their organization.
    """

    try:
        documents = await db_client.get_documents_for_organization(
            organization_id=user.selected_organization_id,
            processing_status=status,
            limit=limit,
            offset=offset,
        )

        # Convert to response schema
        document_list = [
            DocumentResponseSchema(
                id=doc.id,
                document_uuid=doc.document_uuid,
                filename=doc.filename,
                file_size_bytes=doc.file_size_bytes,
                file_hash=doc.file_hash,
                mime_type=doc.mime_type,
                processing_status=doc.processing_status,
                processing_error=doc.processing_error,
                total_chunks=doc.total_chunks,
                retrieval_mode=doc.retrieval_mode,
                custom_metadata=doc.custom_metadata,
                docling_metadata=doc.docling_metadata,
                source_url=doc.source_url,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
                organization_id=doc.organization_id,
                created_by=doc.created_by,
                is_active=doc.is_active,
                is_global=doc.is_global,
            )
            for doc in documents
        ]

        return DocumentListResponseSchema(
            documents=document_list,
            total=len(document_list),
            limit=limit,
            offset=offset,
        )

    except Exception as exc:
        logger.error(f"Error listing documents: {exc}")
        raise HTTPException(status_code=500, detail="Failed to list documents") from exc


@router.get(
    "/documents/{document_uuid}",
    response_model=DocumentResponseSchema,
    summary="Get document details",
)
async def get_document(
    document_uuid: str,
    user=Depends(get_user),
):
    """Get details of a specific document.

    Access Control:
    * Users can only access documents from their organization.
    """

    try:
        document = await db_client.get_document_by_uuid(
            document_uuid=document_uuid,
            organization_id=user.selected_organization_id,
        )

        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return DocumentResponseSchema(
            id=document.id,
            document_uuid=document.document_uuid,
            filename=document.filename,
            file_size_bytes=document.file_size_bytes,
            file_hash=document.file_hash,
            mime_type=document.mime_type,
            processing_status=document.processing_status,
            processing_error=document.processing_error,
            total_chunks=document.total_chunks,
            retrieval_mode=document.retrieval_mode,
            custom_metadata=document.custom_metadata,
            docling_metadata=document.docling_metadata,
            source_url=document.source_url,
            created_at=document.created_at,
            updated_at=document.updated_at,
            organization_id=document.organization_id,
            created_by=document.created_by,
            is_active=document.is_active,
            is_global=document.is_global,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error getting document: {exc}")
        raise HTTPException(status_code=500, detail="Failed to get document") from exc


@router.patch(
    "/documents/{document_uuid}/global",
    response_model=DocumentResponseSchema,
    summary="Set or unset a document as global",
)
async def set_document_global(
    document_uuid: str,
    request: SetDocumentGlobalRequestSchema,
    user=Depends(get_user),
):
    """Toggle whether a document is available to all agents in the organization.

    Global documents are automatically available to every agent without explicit
    assignment. Non-global documents must be assigned to specific agents.
    """
    try:
        document = await db_client.set_document_global(
            document_uuid=document_uuid,
            organization_id=user.selected_organization_id,
            is_global=request.is_global,
        )
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return DocumentResponseSchema(
            id=document.id,
            document_uuid=document.document_uuid,
            filename=document.filename,
            file_size_bytes=document.file_size_bytes,
            file_hash=document.file_hash,
            mime_type=document.mime_type,
            processing_status=document.processing_status,
            processing_error=document.processing_error,
            total_chunks=document.total_chunks,
            retrieval_mode=document.retrieval_mode,
            custom_metadata=document.custom_metadata,
            docling_metadata=document.docling_metadata,
            source_url=document.source_url,
            created_at=document.created_at,
            updated_at=document.updated_at,
            organization_id=document.organization_id,
            created_by=document.created_by,
            is_active=document.is_active,
            is_global=document.is_global,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error setting document global: {exc}")
        raise HTTPException(status_code=500, detail="Failed to update document") from exc


@router.delete(
    "/documents/{document_uuid}",
    summary="Delete document",
)
async def delete_document(
    document_uuid: str,
    user=Depends(get_user),
):
    """Soft delete a document and its chunks.

    Access Control:
    * Users can only delete documents from their organization.
    """

    try:
        success = await db_client.delete_document(
            document_uuid=document_uuid,
            organization_id=user.selected_organization_id,
        )

        if not success:
            raise HTTPException(status_code=404, detail="Document not found")

        logger.info(
            f"Deleted document {document_uuid}, "
            f"user {user.id}, org {user.selected_organization_id}"
        )

        return {"success": True, "message": "Document deleted successfully"}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error deleting document: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to delete document"
        ) from exc


@router.post(
    "/documents/{document_uuid}/retry",
    response_model=DocumentResponseSchema,
    summary="Retry processing a failed document",
)
async def retry_document_processing(
    document_uuid: str,
    user=Depends(get_user),
):
    """Re-enqueue processing for a document that previously failed.

    Only documents in 'failed' status can be retried. The document is reset
    to 'pending' and its existing chunks (if any) are cleared before re-queuing.
    """
    try:
        document = await db_client.get_document_by_uuid(
            document_uuid=document_uuid,
            organization_id=user.selected_organization_id,
        )
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        if document.processing_status != "failed":
            raise HTTPException(
                status_code=400,
                detail=f"Document is '{document.processing_status}', not 'failed' — cannot retry.",
            )

        s3_key = (document.custom_metadata or {}).get("s3_key", "")
        if not s3_key:
            raise HTTPException(
                status_code=400,
                detail="Document has no S3 key stored; cannot re-process.",
            )

        # Clear any stale chunks and reset status to pending.
        await db_client.delete_document_chunks(document.id)
        await db_client.reset_document_for_retry(document.id)

        await enqueue_job(
            FunctionNames.PROCESS_KNOWLEDGE_BASE_DOCUMENT,
            document.id,
            s3_key,
            user.selected_organization_id,
            str(user.provider_id),
            128,
            document.retrieval_mode or "chunked",
        )

        logger.info(
            f"Retrying document {document_uuid} (id={document.id}), "
            f"org {user.selected_organization_id}"
        )

        refreshed = await db_client.get_document_by_uuid(
            document_uuid=document_uuid,
            organization_id=user.selected_organization_id,
        )
        return DocumentResponseSchema(
            id=refreshed.id,
            document_uuid=refreshed.document_uuid,
            filename=refreshed.filename,
            file_size_bytes=refreshed.file_size_bytes,
            file_hash=refreshed.file_hash,
            mime_type=refreshed.mime_type,
            processing_status=refreshed.processing_status,
            processing_error=refreshed.processing_error,
            total_chunks=refreshed.total_chunks,
            retrieval_mode=refreshed.retrieval_mode,
            custom_metadata=refreshed.custom_metadata,
            docling_metadata=refreshed.docling_metadata,
            source_url=refreshed.source_url,
            created_at=refreshed.created_at,
            updated_at=refreshed.updated_at,
            organization_id=refreshed.organization_id,
            created_by=refreshed.created_by,
            is_active=refreshed.is_active,
            is_global=getattr(refreshed, "is_global", False),
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error retrying document {document_uuid}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to retry document processing") from exc


_EDITABLE_MIME_TYPES = frozenset({
    "text/plain", "text/markdown", "text/x-markdown",
    "text/csv", "application/json", "text/html",
})


@router.put(
    "/documents/{document_uuid}/content",
    response_model=DocumentResponseSchema,
    summary="Replace text content and re-process",
)
async def update_document_content(
    document_uuid: str,
    request: UpdateDocumentContentSchema,
    user=Depends(get_user),
):
    """Overwrite a text document's content in storage and re-trigger chunking/embedding.

    Only text-based MIME types are supported (text/plain, text/markdown, text/csv,
    application/json, text/html). Binary formats (PDF, DOCX, images) are rejected.
    """
    try:
        document = await db_client.get_document_by_uuid(
            document_uuid=document_uuid,
            organization_id=user.selected_organization_id,
        )
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        mime = (document.mime_type or "").split(";")[0].strip()
        if mime and mime not in _EDITABLE_MIME_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Document type '{mime}' does not support inline editing.",
            )

        s3_key = (document.custom_metadata or {}).get("s3_key", "")
        if not s3_key:
            raise HTTPException(status_code=400, detail="Document has no S3 key stored.")

        ext = os.path.splitext(document.filename)[1] or ".txt"
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=ext, delete=False, encoding="utf-8"
            ) as fh:
                fh.write(request.text)
                temp_path = fh.name

            success = await storage_fs.aupload_file(temp_path, s3_key)
            if not success:
                raise HTTPException(status_code=500, detail="Failed to write updated content to storage.")
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

        await db_client.delete_document_chunks(document.id)
        await db_client.reset_document_for_retry(document.id)

        await enqueue_job(
            FunctionNames.PROCESS_KNOWLEDGE_BASE_DOCUMENT,
            document.id,
            s3_key,
            user.selected_organization_id,
            str(user.provider_id),
            128,
            document.retrieval_mode or "chunked",
        )

        logger.info(
            f"Updated content for document {document_uuid} (id={document.id}), "
            f"org {user.selected_organization_id}"
        )

        refreshed = await db_client.get_document_by_uuid(
            document_uuid=document_uuid,
            organization_id=user.selected_organization_id,
        )
        return DocumentResponseSchema(
            id=refreshed.id,
            document_uuid=refreshed.document_uuid,
            filename=refreshed.filename,
            file_size_bytes=refreshed.file_size_bytes,
            file_hash=refreshed.file_hash,
            mime_type=refreshed.mime_type,
            processing_status=refreshed.processing_status,
            processing_error=refreshed.processing_error,
            total_chunks=refreshed.total_chunks,
            retrieval_mode=refreshed.retrieval_mode,
            custom_metadata=refreshed.custom_metadata,
            docling_metadata=refreshed.docling_metadata,
            source_url=refreshed.source_url,
            created_at=refreshed.created_at,
            updated_at=refreshed.updated_at,
            organization_id=refreshed.organization_id,
            created_by=refreshed.created_by,
            is_active=refreshed.is_active,
            is_global=getattr(refreshed, "is_global", False),
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error updating document content {document_uuid}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to update document content") from exc


@router.post(
    "/search",
    response_model=ChunkSearchResponseSchema,
    summary="Search for similar chunks",
)
async def search_chunks(
    request: ChunkSearchRequestSchema,
    user=Depends(get_user),
):
    """Search for document chunks similar to the query.

    This endpoint uses vector similarity search to find relevant chunks.
    Results are returned without threshold filtering - apply similarity
    thresholds at the application layer after optional reranking.

    Access Control:
    * Users can only search documents from their organization.
    """

    try:
        from api.services.gen_ai import OpenAIEmbeddingService, resolve_embeddings_config

        user_config = await db_client.get_user_configurations(user.id)
        embeddings_api_key, embeddings_model, embeddings_base_url = await resolve_embeddings_config(
            organization_id=user.selected_organization_id,
            user_config=user_config,
        )

        embedding_service = OpenAIEmbeddingService(
            db_client=db_client,
            api_key=embeddings_api_key,
            model_id=embeddings_model or "text-embedding-3-small",
            base_url=embeddings_base_url,
        )

        # Perform search
        results = await embedding_service.search_similar_chunks(
            query=request.query,
            organization_id=user.selected_organization_id,
            limit=request.limit,
            document_uuids=request.document_uuids,
        )

        # Apply similarity threshold if provided
        if request.min_similarity is not None:
            results = [r for r in results if r["similarity"] >= request.min_similarity]

        # Convert to response schema
        from api.schemas.knowledge_base import ChunkResponseSchema

        chunks = [
            ChunkResponseSchema(
                id=r["id"],
                document_id=r["document_id"],
                chunk_text=r["chunk_text"],
                contextualized_text=r.get("contextualized_text"),
                chunk_index=r["chunk_index"],
                chunk_metadata=r["chunk_metadata"],
                filename=r["filename"],
                document_uuid=r["document_uuid"],
                similarity=r["similarity"],
            )
            for r in results
        ]

        return ChunkSearchResponseSchema(
            chunks=chunks,
            query=request.query,
            total_results=len(chunks),
        )

    except Exception as exc:
        logger.error(f"Error searching chunks: {exc}")
        raise HTTPException(status_code=500, detail="Failed to search chunks") from exc


@router.get(
    "/documents/{document_uuid}/assignments",
    summary="List workflows this document is assigned to",
)
async def get_document_assignments(
    document_uuid: str,
    user=Depends(get_user),
):
    """Return all workflows that have this document explicitly assigned."""
    try:
        assignments = await db_client.get_workflow_assignments_for_document(
            document_uuid=document_uuid,
            organization_id=user.selected_organization_id,
        )
        return {"assignments": assignments}
    except Exception as exc:
        logger.error(f"Error getting document assignments: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to get document assignments"
        ) from exc


# ─── Per-agent workflow assignment endpoints ───────────────────────────────────


async def _resolve_workflow_id(workflow_uuid: str, organization_id: int) -> int:
    """Resolve workflow_uuid to workflow_id, verifying org ownership."""
    workflow = await db_client.get_workflow_by_uuid(workflow_uuid, organization_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow.id


@router.get(
    "/workflows/{workflow_uuid}/documents",
    response_model=WorkflowDocumentListResponseSchema,
    summary="List documents available to a workflow",
)
async def list_workflow_documents(
    workflow_uuid: str,
    user=Depends(get_user),
):
    """List all documents available to a specific agent (assigned + global).

    Returns documents that are either explicitly assigned to this workflow
    or marked as global (available to all agents in the organization).
    """
    try:
        workflow_id = await _resolve_workflow_id(
            workflow_uuid, user.selected_organization_id
        )
        docs = await db_client.get_documents_for_workflow(
            workflow_id=workflow_id,
            organization_id=user.selected_organization_id,
        )
        document_list = [
            DocumentResponseSchema(
                id=doc.id,
                document_uuid=doc.document_uuid,
                filename=doc.filename,
                file_size_bytes=doc.file_size_bytes,
                file_hash=doc.file_hash,
                mime_type=doc.mime_type,
                processing_status=doc.processing_status,
                processing_error=doc.processing_error,
                total_chunks=doc.total_chunks,
                retrieval_mode=doc.retrieval_mode,
                custom_metadata=doc.custom_metadata,
                docling_metadata=doc.docling_metadata,
                source_url=doc.source_url,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
                organization_id=doc.organization_id,
                created_by=doc.created_by,
                is_active=doc.is_active,
                is_global=doc.is_global,
            )
            for doc in docs
        ]
        return WorkflowDocumentListResponseSchema(
            documents=document_list, total=len(document_list)
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error listing workflow documents: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to list workflow documents"
        ) from exc


@router.post(
    "/workflows/{workflow_uuid}/documents/{document_uuid}",
    summary="Assign a document to a workflow",
)
async def assign_document_to_workflow(
    workflow_uuid: str,
    document_uuid: str,
    user=Depends(get_user),
):
    """Assign a knowledge base document to a specific agent.

    The document will be available to this agent during calls. Documents can
    also be marked as global to make them available to all agents.
    """
    try:
        workflow_id = await _resolve_workflow_id(
            workflow_uuid, user.selected_organization_id
        )
        created = await db_client.assign_document_to_workflow(
            document_uuid=document_uuid,
            workflow_id=workflow_id,
            organization_id=user.selected_organization_id,
        )
        if not created:
            # Either document not found or already assigned — both are OK
            return {"success": True, "message": "Document assignment confirmed"}

        logger.info(
            f"Assigned document {document_uuid} to workflow {workflow_uuid}, "
            f"user {user.id}, org {user.selected_organization_id}"
        )
        return {"success": True, "message": "Document assigned to agent"}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error assigning document to workflow: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to assign document"
        ) from exc


@router.delete(
    "/workflows/{workflow_uuid}/documents/{document_uuid}",
    summary="Unassign a document from a workflow",
)
async def unassign_document_from_workflow(
    workflow_uuid: str,
    document_uuid: str,
    user=Depends(get_user),
):
    """Remove a document assignment from a specific agent.

    Note: global documents will still be available to all agents even after
    unassignment. Use PATCH /documents/{uuid}/global to change the global flag.
    """
    try:
        workflow_id = await _resolve_workflow_id(
            workflow_uuid, user.selected_organization_id
        )
        removed = await db_client.unassign_document_from_workflow(
            document_uuid=document_uuid,
            workflow_id=workflow_id,
            organization_id=user.selected_organization_id,
        )
        if not removed:
            raise HTTPException(status_code=404, detail="Assignment not found")

        logger.info(
            f"Unassigned document {document_uuid} from workflow {workflow_uuid}, "
            f"user {user.id}, org {user.selected_organization_id}"
        )
        return {"success": True, "message": "Document unassigned from agent"}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Error unassigning document from workflow: {exc}")
        raise HTTPException(
            status_code=500, detail="Failed to unassign document"
        ) from exc
