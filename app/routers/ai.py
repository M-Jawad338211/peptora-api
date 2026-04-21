import anthropic
from fastapi import APIRouter, Depends, Request
from app.schemas import AIAssistantRequest, AIAssistantResponse, StackCheckRequest, StackCheckResponse
from app.middleware.auth import get_current_pro_user
from app.middleware.rate_limit import limiter
from app.models import User
from app.config import settings

router = APIRouter(prefix="/ai", tags=["ai"])

_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are a peptide research assistant for Peptora. You provide educational
information about peptide research protocols based on published scientific literature.

Rules you always follow:
- You never give medical advice or recommend specific vendors
- You always remind users that peptides are for research purposes only
- You only discuss information available in peer-reviewed research
- You do not recommend dosages for human use
- You end responses with a brief disclaimer when discussing dosing or protocols

You are knowledgeable about: BPC-157, TB-500, GHK-Cu, Ipamorelin, CJC-1295, GHRP-2, GHRP-6,
Sermorelin, Tesamorelin, Semaglutide, Tirzepatide, Semax, Selank, Epitalon, Thymalin, MOTS-C,
SS-31, Thymosin Alpha-1, Retatrutide, AOD-9604, and other research peptides."""


@router.post("/assistant", response_model=AIAssistantResponse)
@limiter.limit("20/minute")
async def ai_assistant(
    request: Request,
    body: AIAssistantRequest,
    user: User = Depends(get_current_pro_user),
):
    messages = [
        {"role": m.role, "content": m.content}
        for m in body.conversation_history[-10:]  # last 10 messages for context
    ]
    messages.append({"role": "user", "content": body.message})

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return AIAssistantResponse(reply=response.content[0].text)


@router.post("/stack-check", response_model=StackCheckResponse)
@limiter.limit("10/minute")
async def stack_check(
    request: Request,
    body: StackCheckRequest,
    user: User = Depends(get_current_pro_user),
):
    if len(body.peptides) < 2 or len(body.peptides) > 5:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Provide 2–5 peptides to check")

    peptide_list = ", ".join(body.peptides)
    prompt = f"""Analyze the research compatibility of this peptide stack: {peptide_list}

Provide a structured analysis with:
1. Overall compatibility (Compatible / Caution / Incompatible)
2. Detailed analysis of potential interactions
3. Timing recommendations (if compatible)
4. Known conflicts or concerns

Base your answer on published research only. This is for research purposes."""

    response = _client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text
    return StackCheckResponse(
        compatibility="See analysis",
        analysis=text,
        timing_recommendations="See analysis above",
        known_conflicts=[],
    )
