"""The one tool that changes state - and therefore the one that needs a human.

`request_sensitive_action` is deliberately the only write in the agent's
toolbox, and it cannot execute on the model's say-so:

1. The registry refuses it unless the run carries an explicit approval
   (`ToolContext.approved_actions`).
2. The approval is granted by a *different person* than the requester in the
   normal case, through `POST /agent/runs/{id}/approve`, and is recorded in
   `approval_requests` with who decided and when.
3. Even with approval, the action must be on the allowlist below, and the
   principal must independently hold the permission that action requires.

Only `archive_document` performs a real state change, and it is a reversible
soft delete. The other actions are recorded and audited but intentionally not
wired to any external system in a demo project - fabricating an
"export to an external partner" would be exactly the kind of fake complexity
worth avoiding.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth.permissions import Permission
from app.core.logging import get_logger
from app.models.document import Document
from app.tools.base import Tool, ToolContext, ToolResult

log = get_logger(__name__)

SensitiveAction = Literal["archive_document", "export_dataset", "share_externally"]

#: Action -> permission the principal must hold *in addition to* the approval.
ACTION_PERMISSIONS: dict[str, str] = {
    "archive_document": Permission.DOCUMENTS_DELETE,
    "export_dataset": Permission.TOOLS_SENSITIVE,
    "share_externally": Permission.TOOLS_SENSITIVE,
}


class SensitiveActionRequest(BaseModel):
    action: SensitiveAction = Field(description="The state-changing operation requested")
    target: str = Field(
        min_length=1, max_length=255, description="Document id/title or dataset name"
    )
    justification: str = Field(
        min_length=3, max_length=1000, description="Why this action is being requested"
    )


class RequestSensitiveActionTool(Tool):
    name = "request_sensitive_action"
    description = (
        "Request a state-changing action (archiving a document, exporting or "
        "externally sharing a dataset). This PAUSES the run for human approval "
        "and never performs the action by itself. Use it only when the user "
        "explicitly asks to modify, remove or share data."
    )
    input_model = SensitiveActionRequest
    required_permission = Permission.AGENT_RUN
    sensitive = True  # the registry gate keys off this
    cacheable = False
    timeout_seconds = 15.0

    async def run(self, args: SensitiveActionRequest, ctx: ToolContext) -> ToolResult:
        # Reaching here means the registry already confirmed an approval exists.
        required = ACTION_PERMISSIONS.get(args.action)
        if required and not ctx.principal.has_permission(required):
            # Approval is not a substitute for permission. A senior researcher
            # approving does not grant the *requester* rights they lack.
            log.warning(
                "sensitive.permission_missing",
                action=args.action,
                required=required,
                user_id=ctx.principal.user_id,
            )
            return ToolResult.failure(
                self.name,
                "authorization_failed",
                f"Approved, but you lack the {required} permission for {args.action}",
            )

        if args.action == "archive_document":
            return await self._archive_document(args, ctx)

        log.info(
            "sensitive.recorded_not_executed",
            action=args.action,
            target=args.target,
            user_id=ctx.principal.user_id,
        )
        return ToolResult(
            tool=self.name,
            ok=True,
            summary=(
                f"Approved and recorded: {args.action} on {args.target!r}. "
                "No external system is connected in this deployment, so nothing "
                "was transmitted; the approval and intent are in the audit log."
            ),
            data={"action": args.action, "target": args.target, "executed": False},
            count=1,
        )

    async def _archive_document(self, args: SensitiveActionRequest, ctx: ToolContext) -> ToolResult:
        """Reversible soft delete, scoped to documents the caller may read."""
        async with ctx.session_factory() as session:
            stmt = select(Document).where(Document.deleted_at.is_(None))
            try:
                stmt = stmt.where(Document.id == uuid.UUID(args.target))
            except (ValueError, AttributeError):
                stmt = stmt.where(Document.title.ilike(f"%{args.target.strip()}%"))

            document = (await session.execute(stmt.limit(2))).unique().scalars().all()
            if not document:
                return ToolResult(
                    tool=self.name,
                    ok=True,
                    summary=f"No live document matches {args.target!r}; nothing archived.",
                    data={"action": args.action, "target": args.target, "executed": False},
                    count=0,
                )
            if len(document) > 1:
                return ToolResult.failure(
                    self.name,
                    "ambiguous_target",
                    f"{args.target!r} matches more than one document; be specific.",
                )

            target = document[0]
            if not ctx.principal.can_read_access_level(target.access_level):
                return ToolResult.failure(
                    self.name,
                    "authorization_failed",
                    "You may not modify a document you cannot read",
                )

            target.deleted_at = dt.datetime.now(dt.UTC)
            await session.commit()

            log.warning(
                "sensitive.executed",
                action="archive_document",
                document_id=str(target.id),
                user_id=ctx.principal.user_id,
            )
            return ToolResult(
                tool=self.name,
                ok=True,
                summary=(
                    f"Archived document {target.title!r} (soft delete, reversible by "
                    f"clearing deleted_at)."
                ),
                data={
                    "action": args.action,
                    "document_id": str(target.id),
                    "title": target.title,
                    "executed": True,
                    "reversible": True,
                },
                count=1,
            )
