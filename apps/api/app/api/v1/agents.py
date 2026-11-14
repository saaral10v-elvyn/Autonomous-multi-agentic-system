"""Agent-level human controls."""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.api.v1.deps import CurrentUser, DbSession
from app.api.v1.schemas import AgentResponse
from app.orchestrator.state import IllegalRunTransition
from app.services.run_controls import RunControlService

router = APIRouter(prefix="/agents")


@router.post("/{agent_id}/interrupt", response_model=AgentResponse)
async def interrupt_agent(agent_id: UUID, user: CurrentUser, session: DbSession) -> AgentResponse:
    """Interrupt one running agent belonging to the current user."""
    try:
        agent = await RunControlService(session).interrupt(user.id, agent_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Agent not found.") from None
    except IllegalRunTransition as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return AgentResponse.model_validate(agent)
