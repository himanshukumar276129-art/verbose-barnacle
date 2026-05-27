import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.db.session import init_db

# Import existing routers
from app.routers.auth import router as auth_router
from app.routers.ai_tools import router as ai_tools_router
from app.routers.admin import router as admin_router
from app.routers.generation import router as generation_router
from app.routers.promo import router as promo_router
from app.routers.subscriptions import router as subscription_router
from app.routers.wallet import router as wallet_router
from app.routers.api_keys import router as api_keys_router
from app.routers.payments import router as payments_router

# Import new media processing routers
from app.routes.media import router as media_router
from app.routes.admin import router as admin_media_router
from app.routes.processor import router as processor_router
from app.core.config import settings

from app.middleware.api_logger import APILoggerMiddleware

# Initialize FastAPI App
app = FastAPI(
    title="VedaCLI Media & Core API Hub",
    description="SaaS AI Media Processing Backend with Token-Based Billing and Queue Monitoring.",
    version="1.0.0"
)

# Add Middlewares
app.add_middleware(APILoggerMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.MEDIA_ALLOWED_ORIGINS.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Startup DB initializations
@app.on_event("startup")
def on_startup():
    logger = open_logger()
    logger.info("Initializing SQLModel Database Tables...")
    try:
        init_db()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database tables: {e}")

def open_logger():
    import logging
    # Setup directory for persistent backend log streams
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(log_dir, "backend.log")),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger("media_backend")

# Mount Static Files to serve local media downloads seamlessly
uploads_dir = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(uploads_dir, exist_ok=True)
app.mount("/api/v1/media/download", StaticFiles(directory=uploads_dir), name="media_downloads")

# Register core pre-existing routers
app.include_router(auth_router, prefix="/api/v1")
app.include_router(ai_tools_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(generation_router, prefix="/api/v1")
app.include_router(promo_router, prefix="/api/v1")
app.include_router(subscription_router, prefix="/api/v1")
app.include_router(wallet_router, prefix="/api/v1")
app.include_router(api_keys_router, prefix="/api/v1")
app.include_router(payments_router, prefix="/api/v1")

# Register new advanced media routes
app.include_router(media_router, prefix="/api/v1")
app.include_router(admin_media_router, prefix="/api/v1")
app.include_router(processor_router, prefix="/api/v1")

@app.get("/health")
async def health():
    return {"status": "ok", "service": "vedaapex-python-media"}


@app.get("/ready")
async def ready():
    return {"status": "ready", "service": "vedaapex-python-media"}

@app.get("/")
async def root():
    return {
        "message": "VedaApex Universal AI Media Processing SaaS API Hub is active.",
        "docs_url": "/docs",
        "api_v1_base": "/api/v1"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
