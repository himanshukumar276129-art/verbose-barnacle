"""
Authentication router – handles registration, login, logout, token retrieval,
and API‑key lifecycle.

Key improvements (deployment‑blocking fixes):
  • Every endpoint is wrapped in try/except with structured logging.
  • Registration & login return meaningful 4xx/5xx with detail strings.
  • Null‑safe access on wallet, subscription, and referral results.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.db.session import get_session
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.referral_service import ReferralService
from app.services.supabase_service import SupabaseService, SupabaseAuthError
from app.services.token_service import TokenService
from app.services.subscription_service import SubscriptionService
from app.schemas.auth import UserRegister, UserLogin, AuthResponse

logger = logging.getLogger("auth.router")

router = APIRouter(prefix="/auth", tags=["Authentication"])
security = HTTPBearer(auto_error=False)


# ────────────────────────────────────────────────────────────
# Dependency – current authenticated user
# ────────────────────────────────────────────────────────────
async def get_current_user_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_session),
) -> User:
    return await AuthService.get_authenticated_user(request, credentials, session)


# ────────────────────────────────────────────────────────────
# API‑Key CRUD
# ────────────────────────────────────────────────────────────
@router.post("/api-key/generate")
async def generate_api_key(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session),
):
    try:
        new_key = f"va_live_{uuid.uuid4().hex}{uuid.uuid4().hex}"
        user.api_key = new_key
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info("API key generated for user_id=%s", user.id)
        return {"success": True, "api_key": new_key}
    except Exception as e:
        logger.exception("API key generation failed for user_id=%s", getattr(user, "id", "?"))
        raise HTTPException(status_code=500, detail=f"API key generation failed: {e}") from e


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
    try:
        user.api_key = None
        session.add(user)
        session.commit()
        logger.info("API key revoked for user_id=%s", user.id)
        return {"success": True, "message": "API key revoked successfully."}
    except Exception as e:
        logger.exception("API key revocation failed for user_id=%s", user.id)
        raise HTTPException(status_code=500, detail=f"API key revocation failed: {e}") from e


# ────────────────────────────────────────────────────────────
# POST /register
# ────────────────────────────────────────────────────────────
@router.post("/register", response_model=AuthResponse)
async def register(body: UserRegister, request: Request, session: Session = Depends(get_session)):
    email = body.email.lower().strip()
    password = body.password
    full_name = body.full_name
    referral_code_input = body.referral_code

    logger.info("Registration attempt for email=%s", email)

    # ── Step 1: Supabase sign‑up ──
    try:
        auth_result = await SupabaseService.sign_up(
            email=email,
            password=password,
            metadata={"full_name": full_name} if full_name else None,
        )
        logger.info("Supabase sign_up succeeded for email=%s", email)
    except SupabaseAuthError as exc:
        logger.warning("Supabase sign_up error for email=%s: %s (status=%s)", email, exc.message, exc.status_code)
        # If it's a conflict or already exists, return 409
        status_code = exc.status_code
        if "already" in exc.message.lower() and status_code == 400:
            status_code = 409
        
        # Include full response data in log if possible for better debugging
        logger.debug("Supabase signup error details: %s", exc.message)

        raise HTTPException(
            status_code=status_code,
            detail=exc.message,
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected error during Supabase sign_up for email=%s", email)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ── Step 2: Local user + wallet creation ──
    try:
        user, created = SupabaseService.get_or_create_local_user(
            session,
            auth_result.get("user") or {},
            referral_code_input=referral_code_input,
            email_fallback=email,
        )
        logger.info("Local user %s (id=%s, created=%s)", email, user.id, created)
    except Exception as e:
        logger.exception("Failed to create/fetch local user for email=%s", email)
        raise HTTPException(status_code=500, detail=f"User creation failed: {e}") from e

    # ── Step 3: Wallet balance ──
    try:
        wallet = TokenService.get_balance(session, user.id)
    except ValueError:
        logger.warning("Wallet not found for user_id=%s — creating one", user.id)
        try:
            wallet = TokenService.create_wallet(session, user.id)
        except Exception as e:
            logger.exception("Wallet creation failed for user_id=%s", user.id)
            raise HTTPException(status_code=500, detail=f"Wallet setup failed: {e}") from e

    # ── Step 4: Referral processing ──
    referral_result = None
    if created and referral_code_input:
        try:
            referral_result = ReferralService.process_referral(
                session,
                user.id,
                referral_code_input,
                request.client.host if request.client else None,
            )
        except Exception as e:
            logger.warning("Referral processing failed (non‑fatal): %s", e)

    # ── Step 5: Build response ──
    token = auth_result.get("access_token")
    refresh_token = auth_result.get("refresh_token")

    try:
        subscription_summary = SubscriptionService.get_subscription_summary(session, user.id)
    except Exception as e:
        logger.warning("Subscription summary failed (non‑fatal): %s", e)
        subscription_summary = {"plan": "Free", "status": "active"}

    logger.info("Registration complete for email=%s user_id=%s", email, user.id)

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
                "fullName": user.full_name,
                "referralCode": user.referral_code,
                "role": user.role,
                "plan": user.plan,
                "isPro": user.is_pro,
                "subscriptionStart": user.subscription_start.isoformat() if user.subscription_start else None,
                "subscriptionEnd": user.subscription_end.isoformat() if user.subscription_end else None,
            },
            "token": token,
            "refreshToken": refresh_token,
            "requiresEmailConfirmation": token is None,
            "wallet": {"balance": wallet.balance},
            "subscription": subscription_summary,
            "referral": referral_result,
        },
    }


# ────────────────────────────────────────────────────────────
# POST /login
# ────────────────────────────────────────────────────────────
@router.post("/login", response_model=AuthResponse)
async def login(body: UserLogin, session: Session = Depends(get_session)):
    email = body.email.lower().strip()
    password = body.password

    logger.info("Login attempt for email=%s", email)

    # ── Supabase authenticate ──
    try:
        auth_result = await SupabaseService.sign_in_with_password(email=email, password=password)
        logger.info("Supabase sign_in succeeded for email=%s", email)
    except SupabaseAuthError as exc:
        logger.warning("Login failed for email=%s: %s (status=%s)", email, exc.message, exc.status_code)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except Exception as exc:
        logger.error("Supabase unavailable during login for email=%s: %s", email, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # ── Local user ──
    try:
        user, _ = SupabaseService.get_or_create_local_user(
            session, 
            auth_result.get("user") or {},
            email_fallback=email
        )
    except Exception as e:
        logger.exception("Local user sync failed during login for email=%s", email)
        raise HTTPException(status_code=500, detail=f"User sync failed: {e}") from e

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated. Contact support.")

    # ── Wallet ──
    try:
        wallet = TokenService.get_balance(session, user.id)
    except ValueError:
        logger.warning("Wallet missing for user_id=%s on login — creating", user.id)
        try:
            wallet = TokenService.create_wallet(session, user.id)
        except Exception as e:
            logger.exception("Wallet creation failed on login for user_id=%s", user.id)
            raise HTTPException(status_code=500, detail=f"Wallet setup failed: {e}") from e

    # ── Subscription ──
    try:
        subscription_summary = SubscriptionService.get_subscription_summary(session, user.id)
    except Exception as e:
        logger.warning("Subscription summary failed (non‑fatal): %s", e)
        subscription_summary = {"plan": "Free", "status": "active"}

    logger.info("Login complete for email=%s user_id=%s", email, user.id)

    return {
        "success": True,
        "message": "Login successful!",
        "data": {
            "user": {
                "id": user.id,
                "email": user.email,
                "fullName": user.full_name,
                "referralCode": user.referral_code,
                "role": user.role,
                "plan": user.plan,
                "isPro": user.is_pro,
                "subscriptionStart": user.subscription_start.isoformat() if user.subscription_start else None,
                "subscriptionEnd": user.subscription_end.isoformat() if user.subscription_end else None,
            },
            "token": auth_result.get("access_token"),
            "refreshToken": auth_result.get("refresh_token"),
            "wallet": {"balance": wallet.balance},
            "subscription": subscription_summary,
        },
    }



# ────────────────────────────────────────────────────────────
# POST /logout
# ────────────────────────────────────────────────────────────
@router.post("/logout")
async def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    user: User = Depends(get_current_user_auth),
):
    del user
    try:
        if credentials and not credentials.credentials.startswith("va_"):
            await SupabaseService.sign_out(credentials.credentials)
    except Exception as e:
        logger.warning("Supabase sign_out failed (non‑fatal): %s", e)

    request.state.user_id = None
    return {"success": True, "message": "Logged out successfully."}


# ────────────────────────────────────────────────────────────
# GET /me
# ────────────────────────────────────────────────────────────
@router.get("/me")
async def get_me(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session),
):
    try:
        wallet = TokenService.get_balance(session, user.id)
    except ValueError:
        logger.warning("Wallet missing for /me user_id=%s — creating", user.id)
        wallet = TokenService.create_wallet(session, user.id)

    try:
        subscription_summary = SubscriptionService.get_subscription_summary(session, user.id)
    except Exception as e:
        logger.warning("Subscription summary failed for /me: %s", e)
        subscription_summary = {"plan": "Free", "status": "active"}

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
            "subscription": subscription_summary,
        },
    }
