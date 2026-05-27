import uuid
from datetime import datetime
from typing import Any, Optional

import httpx
from sqlmodel import Session, select

from ..core.config import settings
from ..core.security import get_password_hash
from ..models.user import User
from .token_service import TokenService


class SupabaseService:
    @staticmethod
    def is_configured() -> bool:
        return bool(settings.SUPABASE_URL and (settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_KEY))

    @staticmethod
    def _base_url() -> str:
        if not settings.SUPABASE_URL:
            raise RuntimeError("Supabase auth is not configured.")
        return settings.SUPABASE_URL.rstrip("/")

    @staticmethod
    def _headers(access_token: Optional[str] = None) -> dict[str, str]:
        api_key = settings.SUPABASE_SERVICE_ROLE_KEY or settings.SUPABASE_KEY
        if not api_key:
            raise RuntimeError("Supabase auth is not configured.")

        headers = {
            "apikey": api_key,
            "Content-Type": "application/json",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    @staticmethod
    def _extract_error(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("msg", "message", "error_description", "error"):
                value = payload.get(key)
                if value:
                    return str(value)
        return "Supabase request failed."

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"message": response.text or "Supabase request failed."}

    @staticmethod
    async def verify_access_token(access_token: str) -> Optional[dict[str, Any]]:
        if not SupabaseService.is_configured():
            raise RuntimeError("Supabase auth is not configured.")

        try:
            async with httpx.AsyncClient(timeout=settings.SUPABASE_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{SupabaseService._base_url()}/auth/v1/user",
                    headers=SupabaseService._headers(access_token),
                )
        except httpx.HTTPError as exc:
            raise RuntimeError("Supabase auth is unavailable.") from exc

        if response.status_code == 200:
            return SupabaseService._safe_json(response)
        if response.status_code in {401, 403}:
            return None

        raise RuntimeError(SupabaseService._extract_error(SupabaseService._safe_json(response)))

    @staticmethod
    async def sign_up(email: str, password: str, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not SupabaseService.is_configured():
            raise RuntimeError("Supabase auth is not configured.")

        payload: dict[str, Any] = {"email": email, "password": password}
        if metadata:
            payload["data"] = metadata

        try:
            async with httpx.AsyncClient(timeout=settings.SUPABASE_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{SupabaseService._base_url()}/auth/v1/signup",
                    headers=SupabaseService._headers(),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise RuntimeError("Supabase auth is unavailable.") from exc

        data = SupabaseService._safe_json(response)
        if response.status_code not in {200, 201}:
            raise ValueError(SupabaseService._extract_error(data))
        return data

    @staticmethod
    async def sign_in_with_password(email: str, password: str) -> dict[str, Any]:
        if not SupabaseService.is_configured():
            raise RuntimeError("Supabase auth is not configured.")

        try:
            async with httpx.AsyncClient(timeout=settings.SUPABASE_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{SupabaseService._base_url()}/auth/v1/token?grant_type=password",
                    headers=SupabaseService._headers(),
                    json={"email": email, "password": password},
                )
        except httpx.HTTPError as exc:
            raise RuntimeError("Supabase auth is unavailable.") from exc

        data = SupabaseService._safe_json(response)
        if response.status_code != 200:
            raise ValueError(SupabaseService._extract_error(data))
        return data

    @staticmethod
    async def sign_out(access_token: str) -> None:
        if not SupabaseService.is_configured():
            return

        try:
            async with httpx.AsyncClient(timeout=settings.SUPABASE_TIMEOUT_SECONDS) as client:
                await client.post(
                    f"{SupabaseService._base_url()}/auth/v1/logout",
                    headers=SupabaseService._headers(access_token),
                )
        except httpx.HTTPError:
            return

    @staticmethod
    def get_or_create_local_user(
        session: Session,
        supabase_user: dict[str, Any],
        referral_code_input: Optional[str] = None,
    ) -> tuple[User, bool]:
        email = supabase_user.get("email") or f"{supabase_user.get('id', uuid.uuid4().hex)}@supabase.local"
        metadata = supabase_user.get("user_metadata") or {}
        full_name = metadata.get("full_name") or metadata.get("name") or supabase_user.get("phone")

        user = session.exec(select(User).where(User.email == email)).first()
        created = False

        if not user:
            created = True
            user = User(
                email=email,
                hashed_password=get_password_hash(uuid.uuid4().hex),
                full_name=full_name,
                referral_code=f"VEDA{uuid.uuid4().hex[:8].upper()}",
                referred_by=referral_code_input,
                role="USER",
                last_login_at=datetime.utcnow(),
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            TokenService.create_wallet(session, user.id)
        else:
            if full_name and user.full_name != full_name:
                user.full_name = full_name
            user.last_login_at = datetime.utcnow()
            session.add(user)
            session.commit()
            session.refresh(user)

        return user, created
