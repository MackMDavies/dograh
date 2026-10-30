"""Customer queries must not mix private QA executions with live calls."""

from sqlalchemy import cast, func, literal
from sqlalchemy.dialects.postgresql import JSONB

from api.db.models import WorkflowRunModel
from api.services.workflow.test_sandbox_mode import SANDBOX_ANNOTATION


def customer_visible_workflow_run():
    annotations = func.coalesce(
        cast(WorkflowRunModel.annotations, JSONB), literal({}, JSONB)
    )
    return ~annotations.contains({SANDBOX_ANNOTATION: {"enabled": True}})
