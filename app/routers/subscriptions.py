import json
from fastapi import APIRouter, HTTPException, Depends, Request
from sqlmodel import Session, select
from datetime import datetime, timedelta

from app.db.session import get_session
from app.models.user import User
from app.models.token import SubscriptionPlan, UserSubscription
from app.routers.auth import get_current_user_auth
from app.services.token_service import TokenService

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])


# ─── Get All Plans ───────────────────────────────────────
@router.get("/plans")
async def get_plans(session: Session = Depends(get_session)):
    plans = session.exec(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.is_active == True)
        .order_by(SubscriptionPlan.sort_order)
    ).all()

    formatted = []
    for p in plans:
        formatted.append({
            "id": p.id, "name": p.name, "slug": p.slug,
            "price": p.price, "currency": p.currency,
            "token_allocation": p.token_allocation,
            "daily_credits": p.daily_credits,
            "features": json.loads(p.features) if p.features else [],
            "billing_cycle": p.billing_cycle
        })

    return {"success": True, "data": formatted}


# ─── Get Current Subscription ────────────────────────────
@router.get("/current")
async def get_current_subscription(
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session)
):
    sub = session.exec(
        select(UserSubscription).where(UserSubscription.user_id == user.id)
    ).first()

    if not sub:
        return {
            "success": True,
            "data": {
                "plan": "Free", "status": "active",
                "features": ["100 credits on signup", "10 daily free credits", "Basic features"]
            }
        }

    plan = session.get(SubscriptionPlan, sub.plan_id)
    return {
        "success": True,
        "data": {
            "plan": plan.name, "status": sub.status,
            "token_allocation": plan.token_allocation,
            "daily_credits": plan.daily_credits,
            "features": json.loads(plan.features) if plan.features else [],
            "current_period_end": sub.current_period_end.isoformat()
        }
    }


# ─── Subscribe (payment-ready) ───────────────────────────
@router.post("/subscribe")
async def subscribe(
    request: Request,
    user: User = Depends(get_current_user_auth),
    session: Session = Depends(get_session)
):
    body = await request.json()
    plan_slug = body.get("plan_slug")
    payment_id = body.get("payment_id")

    plan = session.exec(
        select(SubscriptionPlan).where(SubscriptionPlan.slug == plan_slug)
    ).first()

    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found.")

    period_end = datetime.utcnow() + timedelta(days=30)

    # Check existing subscription
    existing = session.exec(
        select(UserSubscription).where(UserSubscription.user_id == user.id)
    ).first()

    if existing:
        existing.plan_id = plan.id
        existing.status = "active"
        existing.current_period_start = datetime.utcnow()
        existing.current_period_end = period_end
        existing.payment_id = payment_id
        session.add(existing)
    else:
        sub = UserSubscription(
            user_id=user.id, plan_id=plan.id, status="active",
            current_period_start=datetime.utcnow(),
            current_period_end=period_end, payment_id=payment_id
        )
        session.add(sub)

    session.commit()

    # Add token allocation
    TokenService.add_credits(
        session, user.id, plan.token_allocation,
        tx_type="PURCHASE",
        description=f"{plan.name} plan subscription: +{plan.token_allocation} credits",
        ip_address=request.client.host,
        metadata={"plan_id": plan.id, "payment_id": payment_id}
    )

    return {
        "success": True,
        "message": f"Subscribed to {plan.name} plan!",
        "data": {
            "plan": plan.name,
            "credits_added": plan.token_allocation,
            "expires_at": period_end.isoformat()
        }
    }
