import time
from fastapi import Request, HTTPException, Depends
from sqlmodel import Session, select
from datetime import datetime
from jose import jwt, JWTError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.db.session import get_session
from app.models.user import User
from app.models.token import UserSession
from app.core.config import settings

security = HTTPBearer(auto_error=False)

async def authenticate_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: Session = Depends(get_session)
) -> User:
    """
    SaaS authentication dependency.
    Validates API key (from header 'x-api-key' or Authorization Bearer 'va_live_...')
    or Standard JWT Bearer token.
    """
    # 1. API Key validation via Header
    api_key_header = request.headers.get("x-api-key")
    if api_key_header:
        user = session.exec(select(User).where(User.api_key == api_key_header)).first()
        if user:
            if not user.is_active:
                raise HTTPException(status_code=401, detail="Account deactivated.")
            return user

    # 2. Token / Bearer checks
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authentication credentials not provided. Log in or supply an x-api-key."
        )
        
    token = credentials.credentials
    
    # 3. API Key validation via Bearer token (va_...)
    if token.startswith("va_"):
        user = session.exec(select(User).where(User.api_key == token)).first()
        if user:
            if not user.is_active:
                raise HTTPException(status_code=401, detail="Account deactivated.")
            return user

    # 4. Standard JWT token decoding or Firebase Fallback
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        subject = payload.get("sub")
        if not subject:
            raise HTTPException(status_code=401, detail="Invalid token payload.")
        
        # 5. Session validation in DB (only for local JWT)
        db_session = session.exec(
            select(UserSession).where(
                UserSession.token == token,
                UserSession.expires_at > datetime.utcnow()
            )
        ).first()
        if not db_session:
            raise HTTPException(status_code=401, detail="Session expired. Please log in again.")

        # 6. Fetch user
        if str(subject).isdigit():
            user = session.get(User, int(subject))
        else:
            user = session.exec(select(User).where(User.email == subject)).first()

    except JWTError:
        # Fallback to Firebase verification
        from ..services.firebase_service import FirebaseService
        decoded_token = FirebaseService.verify_id_token(token)
        if not decoded_token:
            raise HTTPException(status_code=401, detail="Invalid or expired authorization token.")
        
        user = FirebaseService.get_or_create_user(session, decoded_token)

    if not user:
        raise HTTPException(status_code=401, detail="User account not found.")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="User account is deactivated.")
        
    return user

async def authenticate_admin(
    current_user: User = Depends(authenticate_user)
) -> User:
    """
    Access control dependency that ensures the authenticated user is an administrator.
    """
    if current_user.role.upper() != "ADMIN" and not current_user.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="Forbidden. Administrative privileges required."
        )
    return current_user
