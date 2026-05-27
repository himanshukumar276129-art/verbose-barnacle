import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.db.session import get_session
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.referral_service import ReferralService
from app.services.supabase_service import SupabaseService
from app.services.token_service import TokenService
from app.services.subscription_service import SubscriptionService

router = APIRouter(prefix="/auth", tags=["Authentication"])
security = HTTPBearer(auto_error=False)


async def get_current_user_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_session),
) -> User:
    return await AuthService.get_authenticated_user(request, credentials, session)


@router.post("/api-key/generate")
async def generate_api_key(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session),
):
    new_key = f"va_live_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    user.api_key = new_key
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"success": True, "api_key": new_key}


@router.get("/api-key")
async def get_api_key(user: User = Depends(get_current_user_auth)):
    masked = None
    if user.api_key:
        masked = user.api_key[:12] + "..." + user.api_key[-4:]
    return {
        "success": True,
        "has_key": user.api_key is not None,
        "api_key": masked,
    }


@router.delete("/api-key/revoke")
async def revoke_api_key(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session),
):
    user.api_key = None
    session.add(user)
    session.commit()
    return {"success": True, "message": "API key revoked successfully."}


@router.post("/register")
async def register(request: Request, session: Session = Depends(get_session)):
    body = await request.json()
    email = body.get("email")
    password = body.get("password")
    full_name = body.get("full_name")
    referral_code_input = body.get("referral_code")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")

    try:
        auth_result = await SupabaseService.sign_up(
            email=email,
            password=password,
            metadata={"full_name": full_name} if full_name else None,
        )
    except ValueError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=409 if "already" in detail.lower() else 400,
            detail=detail,
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    user, created = SupabaseService.get_or_create_local_user(
        session,
        auth_result.get("user") or {},
        referral_code_input=referral_code_input,
    )
    wallet = TokenService.get_balance(session, user.id)

    referral_result = None
    if created and referral_code_input:
        referral_result = ReferralService.process_referral(
            session,
            user.id,
            referral_code_input,
            request.client.host if request.client else None,
        )

    token = auth_result.get("access_token")
    refresh_token = auth_result.get("refresh_token")

    return {
        "success": True,
        "message": (
            "Account created successfully!"
            if token
            else "Account created. Verify your email in Supabase before signing in."
        ),
        "data": {
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "referral_code": user.referral_code,
                "role": user.role,
                "plan": user.plan,
                "is_pro": user.is_pro,
                "subscription_start": user.subscription_start.isoformat() if user.subscription_start else None,
                "subscription_end": user.subscription_end.isoformat() if user.subscription_end else None,
            },
            "token": token,
            "refresh_token": refresh_token,
            "requires_email_confirmation": token is None,
            "wallet": {"balance": wallet.balance},
            "subscription": SubscriptionService.get_subscription_summary(session, user.id),
            "referral": referral_result,
        },
    }


@router.post("/login")
async def login(session_request: Request, session: Session = Depends(get_session)):
    body = await session_request.json()
    email = body.get("email")
    password = body.get("password")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")

    try:
        auth_result = await SupabaseService.sign_in_with_password(email=email, password=password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    user, _ = SupabaseService.get_or_create_local_user(session, auth_result.get("user") or {})
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated. Contact support.")

    wallet = TokenService.get_balance(session, user.id)

    return {
        "success": True,
        "message": "Login successful!",
        "data": {
            "user": {
                "id": user.id,
                "email": user.email,
                "full_name": user.full_name,
                "referral_code": user.referral_code,
                "role": user.role,
                "plan": user.plan,
                "is_pro": user.is_pro,
                "subscription_start": user.subscription_start.isoformat() if user.subscription_start else None,
                "subscription_end": user.subscription_end.isoformat() if user.subscription_end else None,
            },
            "token": auth_result.get("access_token"),
            "refresh_token": auth_result.get("refresh_token"),
            "wallet": {"balance": wallet.balance},
            "subscription": SubscriptionService.get_subscription_summary(session, user.id),
        },
    }


@router.post("/logout")
async def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    user: User = Depends(get_current_user_auth),
):
    del user
    if credentials and not credentials.credentials.startswith("va_"):
        await SupabaseService.sign_out(credentials.credentials)
    request.state.user_id = None
    return {"success": True, "message": "Logged out successfully."}


@router.get("/me")
async def get_me(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session),
):
    wallet = TokenService.get_balance(session, user.id)

    return {
        "success": True,
        "data": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "referral_code": user.referral_code,
            "role": user.role,
            "plan": user.plan,
            "is_pro": user.is_pro,
            "subscription_start": user.subscription_start.isoformat() if user.subscription_start else None,
            "subscription_end": user.subscription_end.isoformat() if user.subscription_end else None,
            "created_at": user.created_at.isoformat(),
            "wallet": {
                "balance": wallet.balance,
                "lifetime_earned": wallet.lifetime_earned,
                "lifetime_spent": wallet.lifetime_spent,
            },
            "subscription": SubscriptionService.get_subscription_summary(session, user.id),
        },
    }
