from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session, select

from ..models.user import User
from .api_key_service import APIKeyService
from .supabase_service import SupabaseService


class AuthService:
    @staticmethod
    def _get_api_key_user(session: Session, api_key: str | None) -> User | None:
        if not api_key:
            return None

        user = session.exec(select(User).where(User.api_key == api_key)).first()
        if user and not user.is_active:
            raise HTTPException(status_code=401, detail="Account deactivated.")
        return user

    @staticmethod
    def get_user_from_any_api_key(
        session: Session,
        x_api_key: str | None = None,
        bearer_token: str | None = None,
    ) -> User | None:
        for candidate in (x_api_key, bearer_token):
            if not candidate:
                continue

            user = APIKeyService.validate_api_key(session, candidate)
            if not user:
                user = AuthService._get_api_key_user(session, candidate)

            if user:
                if not user.is_active:
                    raise HTTPException(status_code=401, detail="Account deactivated.")
                return user

        return None

    @staticmethod
    async def get_authenticated_user(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None,
        session: Session,
    ) -> User:
        bearer_token = credentials.credentials if credentials else None
        user = AuthService.get_user_from_any_api_key(
            session,
            x_api_key=request.headers.get("x-api-key"),
            bearer_token=bearer_token,
        )

        if user:
            request.state.user_id = user.id
            return user

        if not credentials:
            raise HTTPException(
                status_code=401,
                detail="Authentication required. Supply a Supabase Bearer token or x-api-key.",
            )

        try:
            supabase_user = await SupabaseService.verify_access_token(credentials.credentials)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if not supabase_user:
            raise HTTPException(status_code=401, detail="Invalid or expired Supabase token.")

        user, _ = SupabaseService.get_or_create_local_user(session, supabase_user)
        if not user.is_active:
            raise HTTPException(status_code=401, detail="Account deactivated.")

        request.state.user_id = user.id
        return user
