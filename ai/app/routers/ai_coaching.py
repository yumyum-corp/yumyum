from fastapi import APIRouter
from app.schemas.coaching import WeeklyCoachingRequest, WeeklyCoachingResponse
from app.services.coaching_service import run_coaching_chain

router = APIRouter(tags=["AI Coaching"])


@router.post("/ai/coaching/weekly", response_model=WeeklyCoachingResponse)
async def weekly_coaching(req: WeeklyCoachingRequest):
    """
    F704 - Multi-Agent 주간 코칭
    기록 상태에 따라 영양·운동·목표 Agent를 선택하고 Synthesis로 통합 코칭 반환.
    Spring @Scheduled 배치에서 호출되며, 결과는 WeeklyReport에 저장된다.
    """
    return await run_coaching_chain(req)
