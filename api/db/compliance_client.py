"""Append-only writes and reads for compliance acknowledgements.

There is deliberately no update method. A change of mind is a new row, so the
sequence of rows is the history — the property the previous
`orchestrator_metadata` timestamp could not provide, because an update
overwrote it.
"""

from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import desc
from sqlalchemy.future import select

from api.db.base_client import BaseDBClient
from api.db.models import ComplianceAcknowledgementModel


class ComplianceDBClient(BaseDBClient):
    async def record_acknowledgement(
        self,
        organization_id: int,
        user_id: int,
        acknowledgement_type: str,
        statement_text: str,
        statement_version: str,
        campaign_id: Optional[int] = None,
        campaign_name: Optional[str] = None,
        client_statement_text: Optional[str] = None,
        acknowledged_at=None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ComplianceAcknowledgementModel]:
        """Append one acknowledgement. Never raises into the caller.

        A failure to write the audit row must not block the campaign the user
        is trying to create — enforcement already happened upstream, and losing
        the business action because the evidence write failed would be a worse
        outcome than a logged gap. The error is logged loudly so the gap is
        visible rather than silent.
        """
        try:
            async with self.async_session() as session:
                row = ComplianceAcknowledgementModel(
                    organization_id=organization_id,
                    user_id=user_id,
                    campaign_id=campaign_id,
                    campaign_name=campaign_name,
                    acknowledgement_type=acknowledgement_type,
                    statement_text=statement_text,
                    statement_version=statement_version,
                    client_statement_text=client_statement_text,
                    context=context or {},
                )
                if acknowledged_at is not None:
                    row.acknowledged_at = acknowledged_at
                session.add(row)
                await session.commit()
                await session.refresh(row)
                logger.info(
                    f"[compliance] recorded {acknowledgement_type} by user "
                    f"{user_id} for campaign {campaign_id} (row {row.id})"
                )
                return row
        except Exception as e:
            logger.error(
                f"[compliance] FAILED to record {acknowledgement_type} by user "
                f"{user_id} for campaign {campaign_id}: {e}"
            )
            return None

    async def list_acknowledgements(
        self,
        organization_id: int,
        campaign_id: Optional[int] = None,
        acknowledgement_type: Optional[str] = None,
        limit: int = 200,
    ) -> List[ComplianceAcknowledgementModel]:
        """Newest first — a dispute starts from the most recent state."""
        async with self.async_session() as session:
            stmt = select(ComplianceAcknowledgementModel).where(
                ComplianceAcknowledgementModel.organization_id == organization_id
            )
            if campaign_id is not None:
                stmt = stmt.where(
                    ComplianceAcknowledgementModel.campaign_id == campaign_id
                )
            if acknowledgement_type is not None:
                stmt = stmt.where(
                    ComplianceAcknowledgementModel.acknowledgement_type
                    == acknowledgement_type
                )
            stmt = stmt.order_by(
                desc(ComplianceAcknowledgementModel.acknowledged_at)
            ).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())
