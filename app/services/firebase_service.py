import json
import firebase_admin
from firebase_admin import credentials, auth
from sqlmodel import Session, select
from typing import Optional
import uuid

from ..core.config import settings
from ..models.user import User
from .token_service import TokenService

class FirebaseService:
    _initialized = False

    @staticmethod
    def initialize():
        if not FirebaseService._initialized:
            if settings.FIREBASE_SERVICE_ACCOUNT_JSON:
                try:
                    cert_dict = json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON)
                    cred = credentials.Certificate(cert_dict)
                    firebase_admin.initialize_app(cred)
                    FirebaseService._initialized = True
                    print("[Firebase] Admin SDK initialized successfully.")
                except Exception as e:
                    print(f"[Firebase] Initialization error: {e}")
            else:
                print("[Firebase Warning] FIREBASE_SERVICE_ACCOUNT_JSON not found in environment.")

    @staticmethod
    def verify_id_token(id_token: str) -> Optional[dict]:
        """Verify the Firebase ID token and return decoded claims."""
        FirebaseService.initialize()
        if not FirebaseService._initialized:
            return None
        try:
            return auth.verify_id_token(id_token)
        except Exception as e:
            print(f"[Firebase] Token verification failed: {e}")
            return None

    @staticmethod
    def get_or_create_user(session: Session, decoded_token: dict) -> User:
        """Get existing user or create a new one based on Firebase claims."""
        firebase_uid = decoded_token.get("uid")
        email = decoded_token.get("email")
        name = decoded_token.get("name", "Firebase User")

        # 1. Try to find by firebase_uid
        user = session.exec(select(User).where(User.firebase_uid == firebase_uid)).first()
        if user:
            return user

        # 2. Try to find by email and link
        if email:
            user = session.exec(select(User).where(User.email == email)).first()
            if user:
                user.firebase_uid = firebase_uid
                session.add(user)
                session.commit()
                session.refresh(user)
                return user

        # 3. Create new user if not found
        from ..core.security import get_password_hash
        random_password = uuid.uuid4().hex
        referral_code = f"VEDA{uuid.uuid4().hex[:8].upper()}"

        user = User(
            email=email or f"{firebase_uid}@firebase.veda",
            full_name=name,
            firebase_uid=firebase_uid,
            hashed_password=get_password_hash(random_password),
            referral_code=referral_code,
            role="USER"
        )
        session.add(user)
        session.commit()
        session.refresh(user)

        # Initialize wallet for new user
        TokenService.create_wallet(session, user.id)
        
        print(f"[Firebase] Created new user: {user.email}")
        return user
