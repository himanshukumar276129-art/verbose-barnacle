from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlmodel import Session, select
from datetime import datetime, timedelta
from jose import jwt, JWTError
import uuid

from app.db.session import get_session
from app.models.user import User
from app.models.token import UserSession
from app.core.config import settings
from app.core.security import create_access_token, verify_password, get_password_hash
from app.services.token_service import TokenService
from app.services.referral_service import ReferralService

router = APIRouter(prefix="/auth", tags=["Authentication"])
security = HTTPBearer(auto_error=False)


async def get_current_user_auth(
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
        raise HTTPException(status_code=401, detail="Authentication required.")
    
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

    # Verify session exists
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

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Account not found or deactivated.")

    return user


async def get_current_firebase_user(
    request: Request,
    session: Session = Depends(get_session)
) -> User:
    """FastAPI dependency to verify Firebase ID token and return/create local user."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Firebase ID token required.")
    
    id_token = auth_header.split(" ")[1]
    from ..services.firebase_service import FirebaseService
    
    decoded_token = FirebaseService.verify_id_token(id_token)
    if not decoded_token:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase token.")
        
    return FirebaseService.get_or_create_user(session, decoded_token)


# ─── API Key Management ───────────────────────────────────
@router.post("/api-key/generate")
async def generate_api_key(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session)
):
    new_key = f"va_live_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    user.api_key = new_key
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"success": True, "api_key": new_key}


@router.get("/api-key")
async def get_api_key(
    user: User = Depends(get_current_user_auth)
):
    masked = None
    if user.api_key:
        masked = user.api_key[:12] + "..." + user.api_key[-4:]
    return {
        "success": True,
        "has_key": user.api_key is not None,
        "api_key": masked
    }


@router.delete("/api-key/revoke")
async def revoke_api_key(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session)
):
    user.api_key = None
    session.add(user)
    session.commit()
    return {"success": True, "message": "API key revoked successfully."}


# ─── Register ────────────────────────────────────────────
@router.post("/register")
async def register(request: Request, session: Session = Depends(get_session)):
    body = await request.json()
    email = body.get("email")
    password = body.get("password")
    full_name = body.get("full_name")
    referral_code_input = body.get("referral_code")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")

    existing = session.exec(select(User).where(User.email == email)).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered.")

    user_referral_code = f"VEDA{uuid.uuid4().hex[:8].upper()}"

    user = User(
        email=email,
        hashed_password=get_password_hash(password),
        full_name=full_name,
        referral_code=user_referral_code,
        referred_by=referral_code_input,
        role="USER"
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    # Create wallet with signup bonus
    wallet = TokenService.create_wallet(session, user.id)

    # Process referral
    referral_result = None
    if referral_code_input:
        referral_result = ReferralService.process_referral(
            session, user.id, referral_code_input, request.client.host
        )

    # Create token + session
    token = create_access_token(subject=user.id)
    expires_at = datetime.utcnow() + timedelta(days=7)
    db_session = UserSession(
        user_id=user.id, token=token,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
        expires_at=expires_at
    )
    session.add(db_session)
    session.commit()

    return {
        "success": True,
        "message": "Account created successfully!",
        "data": {
            "user": {
                "id": user.id, "email": user.email,
                "full_name": user.full_name,
                "referral_code": user.referral_code, "role": user.role
            },
            "token": token,
            "wallet": {"balance": wallet.balance},
            "referral": referral_result
        }
    }


# ─── Login ───────────────────────────────────────────────
@router.post("/login")
async def login(request: Request, session: Session = Depends(get_session)):
    body = await request.json()
    email = body.get("email")
    password = body.get("password")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password are required.")

    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account deactivated. Contact support.")

    token = create_access_token(subject=user.id)
    expires_at = datetime.utcnow() + timedelta(days=7)
    db_session = UserSession(
        user_id=user.id, token=token,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
        expires_at=expires_at
    )
    session.add(db_session)

    user.last_login_at = datetime.utcnow()
    session.add(user)
    session.commit()

    wallet = TokenService.get_balance(session, user.id)

    return {
        "success": True,
        "message": "Login successful!",
        "data": {
            "user": {
                "id": user.id, "email": user.email,
                "full_name": user.full_name,
                "referral_code": user.referral_code, "role": user.role
            },
            "token": token,
            "wallet": {"balance": wallet.balance}
        }
    }


# ─── Logout ──────────────────────────────────────────────
@router.post("/logout")
async def logout(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session)
):
    if credentials:
        sessions = session.exec(
            select(UserSession).where(
                UserSession.user_id == user.id,
                UserSession.token == credentials.credentials
            )
        ).all()
        for s in sessions:
            session.delete(s)
        session.commit()

    return {"success": True, "message": "Logged out successfully."}


# ─── Get Current User ────────────────────────────────────
@router.get("/me")
async def get_me(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session)
):
    wallet = TokenService.get_balance(session, user.id)

    return {
        "success": True,
        "data": {
            "id": user.id, "email": user.email,
            "full_name": user.full_name,
            "referral_code": user.referral_code,
            "role": user.role, "created_at": user.created_at.isoformat(),
            "wallet": {
                "balance": wallet.balance,
                "lifetime_earned": wallet.lifetime_earned,
                "lifetime_spent": wallet.lifetime_spent
            }
        }
    }
