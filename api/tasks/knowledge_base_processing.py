"""ARQ background task for processing knowledge base documents.

All supported file types are processed locally — no external MPS dependency.
Text files are chunked directly; PDFs and DOCX files have their text extracted
with pypdf / python-docx before going through the same local chunker.
"""

import os
import tempfile
from typing import Optional

from loguru import logger

from api.db import db_client
from api.db.models import KnowledgeBaseChunkModel
from api.services.gen_ai import OpenAIEmbeddingService, resolve_embeddings_config
from api.services.storage import storage_fs

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# MIME types handled entirely locally (no MPS call)
_TEXT_MIME_TYPES = frozenset({
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "text/csv",
    "application/json",
    "text/html",
})

_PDF_MIME = "application/pdf"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _extract_pdf_text(file_path: str) -> str:
    """Extract plain text from a PDF using pypdf."""
    from pypdf import PdfReader  # lazy import — only needed for PDFs

    reader = PdfReader(file_path)
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            pages.append(text)
    return "\n\n".join(pages)


def _extract_docx_text(file_path: str) -> str:
    """Extract plain text from a DOCX file using python-docx."""
    from docx import Document  # lazy import — only needed for DOCX

    doc = Document(file_path)
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _chunk_text_locally(text: str, max_chars: int = 600) -> list[dict]:
    """Split plain text into paragraph-based chunks without calling MPS."""
    raw = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks: list[str] = []
    parts: list[str] = []
    length = 0

    for para in raw:
        if length + len(para) > max_chars and parts:
            chunks.append("\n\n".join(parts))
            parts = [para]
            length = len(para)
        else:
            parts.append(para)
            length += len(para)
    if parts:
        chunks.append("\n\n".join(parts))

    # Further split any chunk still over limit (long single paragraphs)
    final: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final.append(chunk)
            continue
        sentences = chunk.replace(". ", ".\n").split("\n")
        sub: list[str] = []
        sub_len = 0
        for sent in sentences:
            if sub_len + len(sent) > max_chars and sub:
                final.append(" ".join(sub))
                sub = [sent]
                sub_len = len(sent)
            else:
                sub.append(sent)
                sub_len += len(sent)
        if sub:
            final.append(" ".join(sub))

    return [
        {
            "chunk_text": c,
            "contextualized_text": c,
            "chunk_index": i,
            "chunk_metadata": {"source": "local_chunker"},
            "token_count": max(1, len(c) // 4),
        }
        for i, c in enumerate(final)
        if c.strip()
    ]


async def process_knowledge_base_document(
    ctx,
    document_id: int,
    s3_key: str,
    organization_id: int,
    created_by_provider_id: str,
    max_tokens: int = 128,
    retrieval_mode: str = "chunked",
):
    """Download a KB document from S3, extract its text locally, chunk, embed, and store."""
    logger.info(
        f"Processing knowledge base document: document_id={document_id}, "
        f"s3_key={s3_key}, org={organization_id}, mode={retrieval_mode}"
    )

    temp_file_path = None

    try:
        await db_client.update_document_status(document_id, "processing")

        filename = s3_key.split("/")[-1]
        file_extension = os.path.splitext(filename)[1] or ".bin"

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=file_extension)
        temp_file_path = temp_file.name
        temp_file.close()

        logger.info(f"Downloading file from S3: {s3_key}")
        download_success = await storage_fs.adownload_file(s3_key, temp_file_path)
        if not download_success:
            raise Exception(f"Failed to download file from S3: {s3_key}")
        if not os.path.exists(temp_file_path):
            raise FileNotFoundError(f"Downloaded file not found: {temp_file_path}")

        file_size = os.path.getsize(temp_file_path)
        logger.info(f"Downloaded file size: {file_size} bytes")

        if file_size > MAX_FILE_SIZE_BYTES:
            error_message = (
                f"File size ({file_size / (1024 * 1024):.1f}MB) exceeds the "
                f"maximum allowed size of {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB."
            )
            logger.warning(f"Document {document_id}: {error_message}")
            await db_client.update_document_status(
                document_id, "failed", error_message=error_message
            )
            return

        file_hash = db_client.compute_file_hash(temp_file_path)
        mime_type = db_client.get_mime_type(temp_file_path)

        document = await db_client.get_document_by_id(document_id)
        if not document:
            raise Exception(f"Document {document_id} not found")

        # Reject duplicates (same hash already ingested for this org).
        existing_doc = await db_client.get_document_by_hash(file_hash, organization_id)
        if existing_doc and existing_doc.id != document_id:
            error_message = (
                f"This file is a duplicate of '{existing_doc.filename}'. "
                f"Please delete the duplicate files and consolidate them into a "
                f"single unique file before uploading."
            )
            logger.warning(
                f"Duplicate document detected: {document_id} is duplicate of "
                f"{existing_doc.id} ({existing_doc.filename})"
            )
            await db_client.update_document_metadata(
                document_id,
                file_size_bytes=file_size,
                file_hash=file_hash,
                mime_type=mime_type,
            )
            await db_client.update_document_status(
                document_id,
                "failed",
                error_message=error_message,
                docling_metadata={
                    "duplicate_of": existing_doc.document_uuid,
                    "duplicate_filename": existing_doc.filename,
                },
            )
            return

        await db_client.update_document_metadata(
            document_id,
            file_size_bytes=file_size,
            file_hash=file_hash,
            mime_type=mime_type,
        )

        # ── Extract raw text from all supported file types ────────────────────
        base_mime = (mime_type or "").split(";")[0].strip()
        raw_text: Optional[str] = None

        if base_mime in _TEXT_MIME_TYPES:
            logger.info(f"Text file (mime={mime_type}), reading directly")
            with open(temp_file_path, encoding="utf-8", errors="replace") as fh:
                raw_text = fh.read()
        elif base_mime == _PDF_MIME:
            logger.info(f"PDF detected, extracting text locally (doc={document_id})")
            try:
                raw_text = _extract_pdf_text(temp_file_path)
            except Exception as exc:
                raise RuntimeError(f"PDF text extraction failed: {exc}") from exc
        elif base_mime == _DOCX_MIME:
            logger.info(f"DOCX detected, extracting text locally (doc={document_id})")
            try:
                raw_text = _extract_docx_text(temp_file_path)
            except Exception as exc:
                raise RuntimeError(f"DOCX text extraction failed: {exc}") from exc
        else:
            raise RuntimeError(
                f"Unsupported file type '{mime_type}'. "
                "Supported types: PDF, DOCX, TXT, Markdown, CSV, JSON."
            )

        if not raw_text or not raw_text.strip():
            raise RuntimeError(
                "No text could be extracted from the document. "
                "The file may be empty, image-only, or password-protected."
            )

        logger.info(f"Extracted {len(raw_text)} chars from document {document_id}")

        if retrieval_mode == "full_document":
            await db_client.update_document_full_text(document_id, raw_text)
            await db_client.update_document_status(
                document_id, "completed", total_chunks=0,
                docling_metadata={"source": "local_extractor"},
            )
            logger.info(f"full_document {document_id} stored ({len(raw_text)} chars)")
            return

        mps_chunks = _chunk_text_locally(raw_text, max_chars=max_tokens * 5)
        docling_metadata: dict = {"source": "local_extractor"}

        # Chunked mode: resolve embedding config via shared priority-ordered resolver.
        user_config = await db_client.get_user_configurations(document.created_by) if document.created_by else None
        embeddings_api_key, embeddings_model, embeddings_base_url = await resolve_embeddings_config(
            organization_id=organization_id,
            user_config=user_config,
        )

        if not embeddings_api_key:
            error_message = (
                "No embedding API key found. Add an Embedding provider in AI Models "
                "or configure an OpenAI connection to process documents."
            )
            logger.warning(f"Document {document_id}: {error_message}")
            await db_client.update_document_status(
                document_id, "failed", error_message=error_message
            )
            return

        embedding_service = OpenAIEmbeddingService(
            db_client=db_client,
            api_key=embeddings_api_key,
            model_id=embeddings_model or "text-embedding-3-small",
            base_url=embeddings_base_url,
        )

        if not mps_chunks:
            logger.warning(f"Document {document_id}: chunker returned zero chunks")

        chunk_records = []
        chunk_texts = []
        for chunk in mps_chunks:
            contextualized = chunk.get("contextualized_text") or chunk["chunk_text"]
            chunk_records.append(
                KnowledgeBaseChunkModel(
                    document_id=document_id,
                    organization_id=organization_id,
                    chunk_text=chunk["chunk_text"],
                    contextualized_text=contextualized,
                    chunk_index=chunk["chunk_index"],
                    chunk_metadata=chunk.get("chunk_metadata") or {},
                    embedding_model=embedding_service.get_model_id(),
                    embedding_dimension=embedding_service.get_embedding_dimension(),
                    token_count=chunk.get("token_count", 0),
                )
            )
            chunk_texts.append(contextualized)

        logger.info(
            f"Generating embeddings for {len(chunk_texts)} chunks "
            f"using {embedding_service.get_model_id()}"
        )
        embeddings = await embedding_service.embed_texts(chunk_texts)
        for chunk_record, embedding in zip(chunk_records, embeddings):
            chunk_record.embedding = embedding

        logger.info("Storing chunks in database")
        await db_client.create_chunks_batch(chunk_records)

        await db_client.update_document_status(
            document_id,
            "completed",
            total_chunks=len(chunk_records),
            docling_metadata=docling_metadata,
        )

        logger.info(
            f"Successfully processed knowledge base document {document_id}. "
            f"Total chunks: {len(chunk_records)}"
        )

    except Exception as e:
        # Use positional-arg formatting to avoid loguru calling .format() on a
        # message that may contain dict-literal curly braces (e.g. SQLAlchemy
        # error messages with parameter lists), which would cause a secondary
        # KeyError and prevent the status from being set to "failed".
        logger.error(
            "Error processing knowledge base document {}: {}",
            document_id,
            type(e).__name__,
            exc_info=True,
        )
        await db_client.update_document_status(
            document_id, "failed", error_message=str(e)
        )
        raise

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                logger.debug(f"Cleaned up temp file: {temp_file_path}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp file {temp_file_path}: {e}")
