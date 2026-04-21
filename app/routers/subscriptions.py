import stripe
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.database import get_db
from app.models import User, Subscription, AuditLog
from app.schemas import CreateCheckoutRequest, CheckoutResponse, PortalResponse, SubscriptionStatusResponse
from app.middleware.auth import get_current_user
from app.config import settings

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

stripe.api_key = settings.STRIPE_SECRET_KEY

PRICE_MAP = {
    "monthly": settings.STRIPE_MONTHLY_PRICE_ID,
    "annual": settings.STRIPE_ANNUAL_PRICE_ID,
}


@router.post("/create-checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CreateCheckoutRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.plan not in PRICE_MAP:
        raise HTTPException(status_code=400, detail="Invalid plan")

    if not user.stripe_customer_id:
        customer = stripe.Customer.create(email=user.email, metadata={"user_id": str(user.id)})
        await db.execute(update(User).where(User.id == user.id).values(stripe_customer_id=customer.id))
        customer_id = customer.id
    else:
        customer_id = user.stripe_customer_id

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": PRICE_MAP[body.plan], "quantity": 1}],
        mode="subscription",
        success_url=f"{settings.FRONTEND_URL}/dashboard?payment=success",
        cancel_url=f"{settings.FRONTEND_URL}/pricing",
        metadata={"user_id": str(user.id)},
    )
    return CheckoutResponse(checkout_url=session.url)


@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, settings.STRIPE_WEBHOOK_SECRET)
    except Exception:
        return Response(status_code=400)

    data = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = data.get("metadata", {}).get("user_id")
        sub_id = data.get("subscription")
        if user_id and sub_id:
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                await db.execute(
                    update(User).where(User.id == user.id)
                    .values(plan="pro", stripe_subscription_id=sub_id)
                )
                stripe_sub = stripe.Subscription.retrieve(sub_id)
                sub = Subscription(
                    user_id=user.id,
                    stripe_subscription_id=sub_id,
                    stripe_price_id=stripe_sub["items"]["data"][0]["price"]["id"],
                    plan_name="pro",
                    status=stripe_sub["status"],
                    current_period_start=datetime.fromtimestamp(stripe_sub["current_period_start"], tz=timezone.utc),
                    current_period_end=datetime.fromtimestamp(stripe_sub["current_period_end"], tz=timezone.utc),
                )
                db.add(sub)
                db.add(AuditLog(user_id=user.id, action="subscribe"))
                try:
                    from app.utils.email import send_pro_welcome_email
                    await send_pro_welcome_email(user.email, user.full_name)
                except Exception:
                    pass

    elif event["type"] == "customer.subscription.updated":
        sub_id = data["id"]
        result = await db.execute(select(Subscription).where(Subscription.stripe_subscription_id == sub_id))
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = data["status"]
            sub.cancel_at_period_end = data.get("cancel_at_period_end", False)
            sub.current_period_end = datetime.fromtimestamp(data["current_period_end"], tz=timezone.utc)
            if data["status"] == "active":
                await db.execute(update(User).where(User.id == sub.user_id).values(plan="pro"))

    elif event["type"] == "customer.subscription.deleted":
        sub_id = data["id"]
        result = await db.execute(select(Subscription).where(Subscription.stripe_subscription_id == sub_id))
        sub = result.scalar_one_or_none()
        if sub:
            sub.status = "cancelled"
            await db.execute(update(User).where(User.id == sub.user_id).values(plan="free"))
            db.add(AuditLog(user_id=sub.user_id, action="subscription_cancelled"))
            user_result = await db.execute(select(User).where(User.id == sub.user_id))
            user = user_result.scalar_one_or_none()
            if user:
                try:
                    from app.utils.email import send_cancellation_email
                    await send_cancellation_email(user.email)
                except Exception:
                    pass

    elif event["type"] == "invoice.payment_failed":
        customer_id = data.get("customer")
        if customer_id:
            result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
            user = result.scalar_one_or_none()
            if user:
                sub_result = await db.execute(
                    select(Subscription).where(Subscription.user_id == user.id, Subscription.status == "active")
                )
                sub = sub_result.scalar_one_or_none()
                if sub:
                    sub.status = "past_due"
                try:
                    from app.utils.email import send_payment_failed_email
                    await send_payment_failed_email(user.email)
                except Exception:
                    pass

    return Response(status_code=200)


@router.get("/portal", response_model=PortalResponse)
async def billing_portal(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No billing account found")
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/dashboard",
    )
    return PortalResponse(portal_url=session.url)


@router.get("/status", response_model=SubscriptionStatusResponse)
async def subscription_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
    )
    sub = result.scalar_one_or_none()
    return SubscriptionStatusResponse(
        plan=user.plan,
        status=sub.status if sub else None,
        current_period_end=sub.current_period_end if sub else None,
        cancel_at_period_end=sub.cancel_at_period_end if sub else False,
    )


@router.post("/cancel")
async def cancel_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.plan != "pro" or not user.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")
    stripe.Subscription.modify(user.stripe_subscription_id, cancel_at_period_end=True)
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == user.stripe_subscription_id)
    )
    sub = result.scalar_one_or_none()
    if sub:
        sub.cancel_at_period_end = True
    db.add(AuditLog(user_id=user.id, action="cancel_subscription"))
    return {"message": "Subscription will cancel at end of billing period"}
