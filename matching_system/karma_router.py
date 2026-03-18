"""
Karma Points System — API Routes
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from .dependencies import get_current_user
from .database import db_manager
from .karma_models import (
    KarmaAwardRequest,
    KarmaProfileResponse,
    InboxShieldUpdate,
    MessageEligibilityResponse,
    KarmaLedgerEntry,
    compute_tier,
    get_next_tier_threshold,
    get_tier_level,
    TIER_CONFIG,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["karma"])


@router.post("/karma/award", response_model=KarmaProfileResponse, status_code=201)
async def award_karma(
    payload: KarmaAwardRequest,
    current_user_id: str = Depends(get_current_user),
):
    """Append a karma ledger row and return the updated profile."""
    await db_manager.insert_karma_entry(
        user_id=payload.user_id,
        action_type=payload.action_type.value,
        point_delta=payload.point_delta,
        reference_id=payload.reference_id,
    )

    score = await db_manager.get_karma_score(payload.user_id)
    tier = compute_tier(score)

    logger.info(
        "Karma awarded: user=%s action=%s delta=%+d new_score=%d tier=%s",
        payload.user_id, payload.action_type.value, payload.point_delta, score,
        tier.value if tier else "none",
    )

    return KarmaProfileResponse(
        user_id=payload.user_id,
        karma_score=score,
        tier=tier,
        tier_label=TIER_CONFIG[tier]["label"] if tier else None,
        tier_level=get_tier_level(tier),
        next_tier_threshold=get_next_tier_threshold(score),
        inbox_shield_threshold=0,
    )


@router.get("/users/{user_id}/karma", response_model=KarmaProfileResponse)
async def get_karma_profile(
    user_id: str = Path(..., description="Clerk user ID"),
    include_ledger: bool = Query(False, description="Include recent ledger entries"),
    limit: int = Query(20, ge=1, le=100, description="Ledger entries per page"),
    offset: int = Query(0, ge=0, description="Ledger pagination offset"),
    current_user_id: str = Depends(get_current_user),
):
    """Return the user's karma score, tier, and optionally their ledger history."""
    score = await db_manager.get_karma_score(user_id)
    if score is None:
        raise HTTPException(status_code=404, detail="User not found")

    shield = await db_manager.get_inbox_shield(user_id)
    tier = compute_tier(score)

    ledger = None
    if include_ledger:
        rows = await db_manager.get_karma_ledger(user_id, limit=limit, offset=offset)
        ledger = [
            KarmaLedgerEntry(
                id=str(row["id"]),
                action_type=row["action_type"],
                point_delta=row["point_delta"],
                reference_id=row.get("reference_id"),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    return KarmaProfileResponse(
        user_id=user_id,
        karma_score=score,
        tier=tier,
        tier_label=TIER_CONFIG[tier]["label"] if tier else None,
        tier_level=get_tier_level(tier),
        next_tier_threshold=get_next_tier_threshold(score),
        inbox_shield_threshold=shield,
        ledger=ledger,
    )


@router.patch("/users/{user_id}/inbox-shield", status_code=200)
async def update_inbox_shield(
    user_id: str = Path(..., description="Clerk user ID"),
    payload: InboxShieldUpdate = None,
    current_user_id: str = Depends(get_current_user),
):
    """Set the user's inbox shield threshold. Self-only (403 otherwise)."""
    if current_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update your own inbox shield.",
        )

    await db_manager.update_inbox_shield(user_id, payload.threshold)
    logger.info("Inbox shield updated: user=%s threshold=%d", user_id, payload.threshold)

    return {
        "user_id": user_id,
        "inbox_shield_threshold": payload.threshold,
        "message": "Inbox shield updated successfully.",
    }


@router.get(
    "/users/{user_id}/karma/can-message/{target_user_id}",
    response_model=MessageEligibilityResponse,
)
async def check_message_eligibility(
    user_id: str = Path(..., description="Sender Clerk user ID"),
    target_user_id: str = Path(..., description="Recipient Clerk user ID"),
    current_user_id: str = Depends(get_current_user),
):
    """
    Check whether the sender can initiate a DM with the target.
    Applies the outbound rule and inbox shield in order.
    """
    if current_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only check your own messaging eligibility.",
        )

    result = await db_manager.check_message_eligibility(user_id, target_user_id)
    return MessageEligibilityResponse(**result)
