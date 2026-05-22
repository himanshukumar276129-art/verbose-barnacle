from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Any
from jose import jwt, JWTError
from sqlmodel import Session, select
from datetime import datetime, time
import traceback

from ..schemas.ai_tools import (
    ImageGenerationRequest, VideoGenerationRequest, TextGenerationRequest,
    PromptGenerationRequest, ThreeDModelGenerationRequest, TTSRequest,
    WeddingCardRequest, LogoRequest, ImageEnhanceRequest, GenerationResponse,
    WordGenerationRequest, ExcelGenerationRequest, PPTGenerationRequest,
    MusicGenerationRequest, PDFGenerationRequest, AnimationGenerationRequest,
    CodeGenerationRequest, DesignGenerationRequest, AdsGenerationRequest,
    HomeDesignRequest, InteriorDesignRequest, HomeMapRequest,
    ColorSuggestionRequest, ThreeDEditRequest, Furniture3DGenerationRequest
)
from ..services.ai_service import AIToolsService
from ..db.session import get_session
from ..models.user import User, Subscription, Generation
from ..core.config import settings

router = APIRouter(
    prefix="/ai",
    tags=["AI Tools"],
    responses={404: {"description": "Not found"}},
)

security = HTTPBearer(auto_error=False)

async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_session)
) -> User:
    # 1. Check x-api-key header first
    api_key_header = request.headers.get("x-api-key")
    if api_key_header:
        user = session.exec(select(User).where(User.api_key == api_key_header)).first()
        if user:
            if not user.is_active:
                raise HTTPException(status_code=401, detail="Account deactivated.")
            return user

    # 2. Check Authorization header for api key
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in to generate content."
        )
    token = credentials.credentials
    if token.startswith("va_"):
        user = session.exec(select(User).where(User.api_key == token)).first()
        if user:
            if not user.is_active:
                raise HTTPException(status_code=401, detail="Account deactivated.")
            return user

    # 3. Standard JWT verification
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        subject = payload.get("sub")
        if not subject:
            raise HTTPException(status_code=401, detail="Invalid token.")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

    # Check session
    from app.models.token import UserSession
    db_session = session.exec(
        select(UserSession).where(
            UserSession.token == token,
            UserSession.expires_at > datetime.utcnow()
        )
    ).first()
    if not db_session:
        raise HTTPException(status_code=401, detail="Session expired. Please login again.")

    if str(subject).isdigit():
        user = session.get(User, int(subject))
    else:
        user = session.exec(select(User).where(User.email == subject)).first()

    if not user:
        raise HTTPException(status_code=401, detail="User not found.")
    return user

async def check_and_log_generation(
    user: User,
    gen_type: str,
    log_prompt: str,
    session: Session,
    generation_func,
    *args,
    **kwargs
):
    from app.services.exhaustion_service import ExhaustionService

    # Check if server is currently crashed/offline due to exhausted API keys
    if ExhaustionService.is_crashed():
        raise HTTPException(
            status_code=503,
            detail="Couldn't reach server. Please try again later. / सर्वर तक नहीं पहुँचा जा सका। कृपया बाद में प्रयास करें।"
        )

    # 1. Determine if user has an active premium subscription (Pro, Max, Ultra)
    is_free = True
    if user.subscription and user.subscription.status == "active":
        plan_name = user.subscription.plan.upper()
        if plan_name in ["PRO", "MAX", "ULTRA"]:
            is_free = False

    # 2. Get generation cost
    type_map = {
        "image": "IMAGE",
        "video": "VIDEO",
        "text": "TEXT",
        "prompt": "TEXT",
        "3d": "MODEL_3D",
        "tts": "TTS",
        "logo": "IMAGE",
        "bg_removal": "BG_REMOVAL",
        "image_enhance": "IMAGE",
        "ppt": "PPT",
        "word": "TEXT",
        "excel": "TEXT",
        "pdf": "TEXT",
        "animation": "VIDEO",
        "code": "TEXT",
        "design": "IMAGE",
        "ads": "TEXT",
        "home_design": "IMAGE",
        "interior_design": "IMAGE",
        "home_map": "IMAGE",
        "color_suggestions": "TEXT",
        "edit_3d": "MODEL_3D"
    }
    cost_key = type_map.get(gen_type.lower(), "TEXT")
    
    from app.config.costs import GENERATION_COSTS
    cost = GENERATION_COSTS.get(cost_key, 1)

    from app.services.token_service import TokenService
    from app.models.token import AIGenerationHistory

    # 3. Check credits if user is Free or Check Daily Limits if Ultra
    if is_free:
        wallet = TokenService.get_balance(session, user.id)
        if wallet.balance < cost:
            raise HTTPException(
                status_code=402,
                detail={
                    "message": "Insufficient Credits",
                    "required": cost,
                    "available": wallet.balance,
                    "deficit": cost - wallet.balance,
                    "suggestion": "Please upgrade to Pro, Max, or Ultra for unlimited generation!"
                }
            )
    elif plan_name == "ULTRA":
        # Ultra Plan Limit Enforcement
        from app.services.usage_tracking_service import UsageTrackingService
        if not UsageTrackingService.check_limit(session, user.id, gen_type, plan_name):
            from app.config.api_limits import ULTRA_DAILY_LIMITS, TOOL_TYPE_TO_LIMIT_KEY
            limit_key = TOOL_TYPE_TO_LIMIT_KEY.get(gen_type.lower(), "text_to_text")
            max_limit = ULTRA_DAILY_LIMITS.get(limit_key, 0)
            raise HTTPException(
                status_code=429,
                detail={
                    "message": f"Daily API Limit Reached for {gen_type}",
                    "limit": max_limit,
                    "suggestion": "Your daily free quota for this tool has been exhausted. Limits reset every 24 hours."
                }
            )

    # 4. Perform the actual generation with error handling for API exhaustion
    try:
        result = await generation_func(*args, **kwargs)
        
        # Increment Ultra Plan usage on success
        if plan_name == "ULTRA":
            from app.services.usage_tracking_service import UsageTrackingService
            UsageTrackingService.increment_usage(session, user.id, gen_type)
            
    except Exception as e:
        err_msg = str(e).lower()
        is_exhaustion = any(word in err_msg for word in [
            "exhausted", "limit", "rate limit", "credits", "unauthorized", "api key",
            "payment", "429", "401", "402", "403", "quota"
        ])
        if is_exhaustion or "all platforms" in err_msg or "all tiers" in err_msg:
            ExhaustionService.mark_crashed()
            raise HTTPException(
                status_code=503,
                detail="Couldn't reach server. Please try again later. / सर्वर तक नहीं पहुँचा जा सका। कृपया बाद में प्रयास करें।"
            )
        raise e
    
    # 5. Extract output URL if present
    output_url = None
    if isinstance(result, list) and len(result) > 0:
        output_url = result[0]
    elif isinstance(result, str):
        output_url = result
    
    # 6. Log the generation history
    generation_log = Generation(
        user_id=user.id,
        type=gen_type,
        prompt=log_prompt,
        output_url=output_url,
        created_at=datetime.utcnow()
    )
    session.add(generation_log)

    # Save to Token System History table
    token_gen_log = AIGenerationHistory(
        user_id=user.id,
        type=cost_key,
        prompt=log_prompt,
        output_url=output_url,
        cost=cost if is_free else 0,
        status="SUCCESS"
    )
    session.add(token_gen_log)
    
    # 7. Deduct credits if user is Free
    if is_free:
        TokenService.deduct_credits(
            session, user.id, cost,
            tx_type="USAGE",
            description=f"{cost_key} generation: -{cost} credits"
        )
    else:
        session.commit()
    
    # 8. Trigger Webhook if Ultra user has one configured
    # Assuming User model has a webhook_url field (placeholder for now)
    if hasattr(user, "webhook_url") and user.webhook_url:
        from ..services.webhook_service import WebhookService
        import asyncio
        asyncio.create_task(
            WebhookService.broadcast_generation_completed(
                user.id, gen_type, output_url, user.webhook_url
            )
        )
    
    return result

@router.post("/generate/image", response_model=GenerationResponse)
async def generate_image(
    request: ImageGenerationRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        result = await check_and_log_generation(
            user=current_user,
            gen_type="image",
            log_prompt=request.prompt,
            session=session,
            generation_func=AIToolsService.generate_image,
            prompt=request.prompt,
            aspect_ratio=request.aspect_ratio,
            num_outputs=request.num_outputs,
            tier=request.tier,
            provider=request.provider
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/video", response_model=GenerationResponse)
async def generate_video(
    request: VideoGenerationRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        result = await check_and_log_generation(
            user=current_user,
            gen_type="video",
            log_prompt=request.prompt,
            session=session,
            generation_func=AIToolsService.generate_video,
            prompt=request.prompt,
            tier=request.tier,
            provider=request.provider,
            avatar_id=request.avatar_id,
            voice_id=request.voice_id
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/text", response_model=GenerationResponse)
async def generate_text(
    request: TextGenerationRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        provider = getattr(request, "provider", "replicate")
        result = await check_and_log_generation(
            user=current_user,
            gen_type="text",
            log_prompt=request.prompt,
            session=session,
            generation_func=AIToolsService.generate_text,
            prompt=request.prompt,
            system_prompt=request.system_prompt,
            tier=request.tier,
            provider=provider
        )
        # Replicate text models often return a list of tokens
        if isinstance(result, list):
            result = "".join(result)
        # Handle OpenAI format (free.ai)
        elif isinstance(result, dict) and "choices" in result:
            result = result["choices"][0]["message"]["content"]
            
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/prompt", response_model=GenerationResponse)
async def generate_prompt(
    request: PromptGenerationRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        result = await check_and_log_generation(
            user=current_user,
            gen_type="prompt",
            log_prompt=request.base_concept,
            session=session,
            generation_func=AIToolsService.generate_prompt,
            base_concept=request.base_concept
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/3d", response_model=GenerationResponse)
async def generate_3d_model(
    request: ThreeDModelGenerationRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        result = await check_and_log_generation(
            user=current_user,
            gen_type="3d",
            log_prompt=request.prompt,
            session=session,
            generation_func=AIToolsService.generate_3d_model,
            prompt=request.prompt,
            tier=request.tier,
            provider=request.provider
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/tts", response_model=GenerationResponse)
async def generate_tts(
    request: TTSRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        result = await check_and_log_generation(
            user=current_user,
            gen_type="tts",
            log_prompt=request.text,
            session=session,
            generation_func=AIToolsService.generate_tts,
            text=request.text,
            voice=request.voice,
            tier=request.tier,
            provider=request.provider
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/wedding-card", response_model=GenerationResponse)
async def generate_wedding_card(
    request: WeddingCardRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        log_prompt = f"Wedding card: {request.bride_name} & {request.groom_name}"
        result = await check_and_log_generation(
            user=current_user,
            gen_type="image",
            log_prompt=log_prompt,
            session=session,
            generation_func=AIToolsService.generate_wedding_card,
            groom_name=request.groom_name,
            bride_name=request.bride_name,
            date=request.date,
            venue=request.venue,
            theme=request.theme_prompt,
            tier=request.tier
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/logo", response_model=GenerationResponse)
async def generate_logo(
    request: LogoRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        log_prompt = f"Logo: {request.brand_name} ({request.niche})"
        result = await check_and_log_generation(
            user=current_user,
            gen_type="image",
            log_prompt=log_prompt,
            session=session,
            generation_func=AIToolsService.generate_logo,
            brand_name=request.brand_name,
            niche=request.niche,
            style=request.style_prompt,
            tier=request.tier,
            provider=request.provider
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/enhance/image", response_model=GenerationResponse)
async def enhance_image(
    request: ImageEnhanceRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        result = await check_and_log_generation(
            user=current_user,
            gen_type="image",
            log_prompt=request.prompt or "Image enhance",
            session=session,
            generation_func=AIToolsService.enhance_image,
            image_url=request.image_url,
            prompt=request.prompt,
            tier=request.tier
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/ppt", response_model=GenerationResponse)
async def generate_ppt(
    request: PPTGenerationRequest,
    req: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    provider = getattr(request, "provider", "2slides")
    
    if provider == "2slides":
        from app.services.providers.twoslides_provider import TwoslidesProvider
        try:
            result = await check_and_log_generation(
                user=current_user,
                gen_type="ppt",
                log_prompt=request.prompt,
                session=session,
                generation_func=TwoslidesProvider.generate_ppt,
                prompt=request.prompt,
                starting_tier=request.tier
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif provider == "presenton":
        from app.services.providers.presenton_provider import PresentonProvider
        try:
            result = await check_and_log_generation(
                user=current_user,
                gen_type="ppt",
                log_prompt=request.prompt,
                session=session,
                generation_func=PresentonProvider.generate_ppt,
                prompt=request.prompt,
                starting_tier=request.tier
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif provider == "skillboss":
        from app.services.providers.skillboss_provider import SkillbossProvider
        try:
            result = await check_and_log_generation(
                user=current_user,
                gen_type="ppt",
                log_prompt=request.prompt,
                session=session,
                generation_func=SkillbossProvider.generate_ppt,
                prompt=request.prompt,
                starting_tier=request.tier
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    elif provider in ["groq", "ollama"]:
        from app.services.document_service import DocumentService
        try:
            result = await check_and_log_generation(
                user=current_user,
                gen_type="ppt",
                log_prompt=request.prompt,
                session=session,
                generation_func=DocumentService.generate_ppt,
                prompt=request.prompt,
                base_url=str(req.base_url),
                provider=provider,
                tier=request.tier
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        from app.core.config import settings
        if not settings.DOCUMENT_COMPILER_KEY:
            raise HTTPException(
                status_code=501,
                detail="Document compilation feature is currently inactive. Please configure DOCUMENT_COMPILER_KEY in your .env file to activate."
            )
        try:
            from app.services.document_service import DocumentService
            result = await check_and_log_generation(
                user=current_user,
                gen_type="ppt",
                log_prompt=request.prompt,
                session=session,
                generation_func=DocumentService.generate_ppt,
                prompt=request.prompt,
                base_url=str(req.base_url)
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
@router.post("/generate/word", response_model=GenerationResponse)
async def generate_word(
    request: WordGenerationRequest,
    req: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    provider = getattr(request, "provider", "document_compiler")

    if provider in ["groq", "ollama"]:
        from app.services.document_service import DocumentService
        try:
            result = await check_and_log_generation(
                user=current_user,
                gen_type="word",
                log_prompt=request.prompt,
                session=session,
                generation_func=DocumentService.generate_word,
                prompt=request.prompt,
                base_url=str(req.base_url),
                provider=provider,
                tier=request.tier
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        from app.core.config import settings
        if not settings.DOCUMENT_COMPILER_KEY:
            raise HTTPException(
                status_code=501,
                detail="Document compilation feature is currently inactive. Please configure DOCUMENT_COMPILER_KEY in your .env file to activate."
            )
        try:
            from app.services.document_service import DocumentService
            result = await check_and_log_generation(
                user=current_user,
                gen_type="word",
                log_prompt=request.prompt,
                session=session,
                generation_func=DocumentService.generate_word,
                prompt=request.prompt,
                base_url=str(req.base_url)
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate/excel", response_model=GenerationResponse)
async def generate_excel(
    request: ExcelGenerationRequest,
    req: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    provider = getattr(request, "provider", "document_compiler")

    if provider in ["groq", "ollama"]:
        from app.services.document_service import DocumentService
        try:
            result = await check_and_log_generation(
                user=current_user,
                gen_type="excel",
                log_prompt=request.prompt,
                session=session,
                generation_func=DocumentService.generate_excel,
                prompt=request.prompt,
                base_url=str(req.base_url),
                provider=provider,
                tier=request.tier
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        from app.core.config import settings
        if not settings.DOCUMENT_COMPILER_KEY:
            raise HTTPException(
                status_code=501,
                detail="Document compilation feature is currently inactive. Please configure DOCUMENT_COMPILER_KEY in your .env file to activate."
            )
        try:
            from app.services.document_service import DocumentService
            result = await check_and_log_generation(
                user=current_user,
                gen_type="excel",
                log_prompt=request.prompt,
                session=session,
                generation_func=DocumentService.generate_excel,
                prompt=request.prompt,
                base_url=str(req.base_url)
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

# =============================================
#   MUSIC GENERATION (Suno / Udio via PiAPI)
# =============================================
@router.post("/generate/music", response_model=GenerationResponse)
async def generate_music(
    request: MusicGenerationRequest,
    req: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    try:
        result = await check_and_log_generation(
            user=current_user,
            gen_type="music",
            log_prompt=request.prompt,
            session=session,
            generation_func=AIToolsService.generate_music,
            prompt=request.prompt,
            tier=request.tier,
            provider=request.provider
        )
        return GenerationResponse(status="success", result=result)
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =============================================
#   PDF DOCUMENT GENERATION (Groq / Ollama)
# =============================================
@router.post("/generate/pdf", response_model=GenerationResponse)
async def generate_pdf(
    request: PDFGenerationRequest,
    req: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    provider = getattr(request, "provider", "document_compiler")

    if provider in ["groq", "ollama"]:
        from app.services.document_service import DocumentService
        try:
            result = await check_and_log_generation(
                user=current_user,
                gen_type="pdf",
                log_prompt=request.prompt,
                session=session,
                generation_func=DocumentService.generate_pdf,
                prompt=request.prompt,
                base_url=str(req.base_url),
                provider=provider,
                tier=request.tier
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        from app.core.config import settings
        if not settings.DOCUMENT_COMPILER_KEY:
            raise HTTPException(
                status_code=501,
                detail="Document compilation feature is currently inactive. Please configure DOCUMENT_COMPILER_KEY in your .env file to activate."
            )
        try:
            from app.services.document_service import DocumentService
            result = await check_and_log_generation(
                user=current_user,
                gen_type="pdf",
                log_prompt=request.prompt,
                session=session,
                generation_func=DocumentService.generate_pdf,
                prompt=request.prompt,
                base_url=str(req.base_url)
            )
            return GenerationResponse(status="success", result=result)
        except HTTPException as he:
            raise he
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
