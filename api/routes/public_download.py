"""Public download endpoints for workflow recordings and transcripts.

These endpoints provide secure, token-based public access to workflow artifacts
without requiring authentication. Content is proxied directly through the API
server so MinIO/S3 does not need to be publicly accessible.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from loguru import logger

from api.db import db_client
from api.services.storage import get_storage_for_backend

router = APIRouter(prefix="/public/download")


@router.get("/workflow/{token}/{artifact_type}")
async def download_workflow_artifact(
    token: str,
    artifact_type: Literal["recording", "transcript"],
    inline: bool = Query(
        default=False, description="Display inline in browser instead of download"
    ),
):
    """Download a workflow recording or transcript via public access token.

    Proxies content directly from storage — MinIO/S3 does not need to be
    publicly accessible. Content is streamed through this API server.

    Args:
        token: The public access token (UUID format)
        artifact_type: Type of artifact - "recording" or "transcript"
        inline: If true, sets Content-Disposition to inline for browser preview

    Returns:
        Response with the artifact content and appropriate content-type
    """
    # 1. Lookup workflow run by token
    workflow_run = await db_client.get_workflow_run_by_public_token(token)
    if not workflow_run:
        logger.warning(f"Invalid public access token: {token[:8]}...")
        raise HTTPException(status_code=404, detail="Invalid or expired token")

    # 2. Get file path based on artifact type
    if artifact_type == "recording":
        file_path = workflow_run.recording_url
        media_type = "audio/wav"
        filename = f"recording-{workflow_run.id}.wav"
    else:  # transcript
        file_path = workflow_run.transcript_url
        media_type = "text/plain; charset=utf-8"
        filename = f"transcript-{workflow_run.id}.txt"

    if not file_path:
        logger.warning(
            f"Artifact not found: type={artifact_type}, workflow_run_id={workflow_run.id}"
        )
        raise HTTPException(
            status_code=404,
            detail=f"No {artifact_type} available for this workflow run",
        )

    # 3. Get storage backend for this workflow run
    try:
        storage = get_storage_for_backend(workflow_run.storage_backend)
    except ValueError as e:
        logger.error(f"Invalid storage backend: {workflow_run.storage_backend}")
        raise HTTPException(status_code=500, detail="Storage configuration error")

    # 4. Read content directly from storage (no browser redirect needed)
    try:
        content = await storage.aread_bytes(file_path)
    except Exception as e:
        logger.error(f"Failed to read artifact from storage: {e}")
        raise HTTPException(status_code=500, detail="Failed to read artifact")

    if content is None:
        logger.warning(f"Storage returned no content for {artifact_type}: {file_path}")
        raise HTTPException(
            status_code=404,
            detail=f"Artifact not found in storage: {file_path}",
        )

    disposition = "inline" if inline else "attachment"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Cache-Control": "private, max-age=3600",
        "Access-Control-Allow-Origin": "*",
    }

    logger.info(
        f"Serving {artifact_type} for workflow_run_id={workflow_run.id}, "
        f"size={len(content)} bytes, token={token[:8]}..."
    )

    return Response(content=content, media_type=media_type, headers=headers)
