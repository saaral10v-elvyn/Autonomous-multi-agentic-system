"""Approval decision endpoint."""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.v1.deps import CurrentUser, DbSession
from app.api.v1.schemas import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalResponse,
)
from app.services.approvals import (
    ApprovalConflict,
    ApprovalService,
    enqueue_approval_resume,
)

router = APIRouter(prefix="/approvals")


@router.post("/{approval_id}/decide", response_model=ApprovalDecisionResponse)
async def decide_approval(
    approval_id: UUID,
    payload: ApprovalDecisionRequest,
    user: CurrentUser,
    session: DbSession,
) -> ApprovalDecisionResponse:
    """Resolve a pending approval, then enqueue its durable resume job."""
    try:
        approval, feedback = await ApprovalService(session).decide(
            user.id,
            approval_id,
            payload.decision.value,
            edited_payload=payload.edited_payload,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Approval not found.") from None
    except ApprovalConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    await enqueue_approval_resume(user.id, approval.run_id, approval.id)
    return ApprovalDecisionResponse(
        approval=ApprovalResponse.model_validate(approval), feedback=feedback
    )
