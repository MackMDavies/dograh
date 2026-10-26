"""Per-request API log (Developers console): batched writes, org-scoped reads, retention."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, insert

from api.db.base_client import BaseDBClient
from api.db.models import ApiRequestLogModel

SURFACES = ("rest", "public", "mcp")


class ApiRequestLogClient(BaseDBClient):
    async def insert_api_request_logs(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        async with self.async_session() as session:
            await session.execute(insert(ApiRequestLogModel), rows)
            await session.commit()

    async def list_api_request_logs(
        self,
        organization_id: int,
        surface: Optional[str] = None,
        errors_only: bool = False,
        after_id: Optional[int] = None,
        limit: int = 60,
    ) -> List[Dict[str, Any]]:
        """Newest first. ``after_id`` returns only rows newer than that id (live polling)."""
        where = ["l.organization_id = :org"]
        params: Dict[str, Any] = {"org": organization_id, "limit": max(1, min(limit, 200))}
        if surface in SURFACES:
            where.append("l.surface = :surface")
            params["surface"] = surface
        if errors_only:
            where.append("l.status >= 400")
        if after_id:
            where.append("l.id > :after_id")
            params["after_id"] = after_id
        query = f"""
            SELECT l.id, l.created_at, l.surface, l.method, l.path, l.mcp_method,
                   l.status, l.latency_ms, l.user_agent, l.api_key_id,
                   k.name AS key_name, k.key_prefix
              FROM api_request_logs l
              LEFT JOIN api_keys k ON k.id = l.api_key_id
             WHERE {' AND '.join(where)}
             ORDER BY l.id DESC
             LIMIT :limit
        """
        return await self.execute_raw_query(query, params)

    async def api_request_stats(self, organization_id: int, days: int = 30) -> Dict[str, Any]:
        """Aggregates for the Developers overview, all computed from real rows."""
        days = max(1, min(days, 90))
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_start = (month_start - timedelta(days=1)).replace(day=1)
        key_since = (now - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
        p = {"org": organization_id, "since": since, "month": month_start,
             "last_month": last_month_start, "key_since": key_since}

        daily = await self.execute_raw_query(
            """
            SELECT to_char(date_trunc('day', created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD') AS day,
                   surface, count(*)::int AS n
              FROM api_request_logs
             WHERE organization_id = :org AND created_at >= :since
             GROUP BY 1, 2
            """,
            p,
        )
        month = await self.execute_raw_query(
            """
            SELECT surface, count(*)::int AS n,
                   count(*) FILTER (WHERE status >= 400)::int AS errors
              FROM api_request_logs
             WHERE organization_id = :org AND created_at >= :month
             GROUP BY 1
            """,
            p,
        )
        lat = await self.execute_raw_query(
            """
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95,
                   count(*)::int AS n,
                   count(*) FILTER (WHERE status >= 400)::int AS errors
              FROM api_request_logs
             WHERE organization_id = :org AND created_at >= :since
            """,
            p,
        )
        top_err = await self.execute_raw_query(
            """
            SELECT status, count(*)::int AS n
              FROM api_request_logs
             WHERE organization_id = :org AND created_at >= :since AND status >= 400
             GROUP BY 1 ORDER BY 2 DESC LIMIT 1
            """,
            p,
        )
        keys = await self.execute_raw_query(
            """
            SELECT api_key_id,
                   to_char(date_trunc('day', created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD') AS day,
                   count(*)::int AS n
              FROM api_request_logs
             WHERE organization_id = :org AND created_at >= :key_since AND api_key_id IS NOT NULL
             GROUP BY 1, 2
            """,
            p,
        )
        minutes = await self.execute_raw_query(
            """
            SELECT
              COALESCE(sum((wr.cost_info->>'call_duration_seconds')::float)
                FILTER (WHERE wr.created_at >= :month), 0) / 60.0 AS this_month,
              COALESCE(sum((wr.cost_info->>'call_duration_seconds')::float)
                FILTER (WHERE wr.created_at >= :last_month AND wr.created_at < :month), 0) / 60.0 AS last_month
              FROM workflow_runs wr
              JOIN api_keys k ON k.id = wr.api_key_id
             WHERE k.organization_id = :org AND wr.created_at >= :last_month
            """,
            p,
        )

        day_keys = [(since + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        series = {d: {"date": d, "rest": 0, "public": 0, "mcp": 0} for d in day_keys}
        for r in daily:
            if r["day"] in series and r["surface"] in SURFACES:
                series[r["day"]][r["surface"]] = r["n"]

        month_tot = {s: 0 for s in SURFACES}
        month_err = 0
        for r in month:
            if r["surface"] in SURFACES:
                month_tot[r["surface"]] = r["n"]
                month_err += r["errors"]

        key_days = [(key_since + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
        per_key: Dict[int, List[int]] = {}
        for r in keys:
            arr = per_key.setdefault(r["api_key_id"], [0] * 14)
            if r["day"] in key_days:
                arr[key_days.index(r["day"])] = r["n"]

        l0 = lat[0] if lat else {}
        m0 = minutes[0] if minutes else {}
        return {
            "days": [series[d] for d in day_keys],
            "month": {**month_tot, "total": sum(month_tot.values()), "errors": month_err},
            "latency_ms": {
                "p50": round(l0.get("p50") or 0),
                "p95": round(l0.get("p95") or 0),
            },
            "window": {
                "requests": l0.get("n") or 0,
                "errors": l0.get("errors") or 0,
                "top_error_status": top_err[0]["status"] if top_err else None,
            },
            "minutes": {
                "this_month": round(float(m0.get("this_month") or 0), 1),
                "last_month": round(float(m0.get("last_month") or 0), 1),
            },
            "keys": [{"api_key_id": k, "daily": v} for k, v in per_key.items()],
        }

    async def purge_api_request_logs(self, older_than_days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        async with self.async_session() as session:
            result = await session.execute(
                delete(ApiRequestLogModel).where(ApiRequestLogModel.created_at < cutoff)
            )
            await session.commit()
            return result.rowcount or 0

