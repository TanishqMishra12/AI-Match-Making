"""
Chat API Router
"""
import json
import logging
import os
from uuid import UUID

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from .database import db_manager
from .dependencies import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["chat"])

_redis_client = None


async def _get_redis():
    """Returns shared async Redis client, initializing once if needed."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_BROKER_URL", "redis://localhost:6379/0")
        _redis_client = await aioredis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )
    return _redis_client


class SendMessageRequest(BaseModel):
    receiver_id: str = Field(..., description="User ID of the receiver")
    content: str = Field(..., min_length=1, max_length=2000, description="Message content")


@router.post("/chat/send", status_code=status.HTTP_201_CREATED, summary="Send a direct message")
async def send_message(
    body: SendMessageRequest,
    current_user_id: str = Depends(get_current_user),
):
    """Send a DM. Karma outbound rule and inbox shield are enforced."""
    if current_user_id == body.receiver_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot send a message to yourself.",
        )

    eligibility = await db_manager.check_message_eligibility(current_user_id, body.receiver_id)
    if not eligibility["allowed"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=eligibility["reason"],
        )

    row = await db_manager.pg_pool.fetchrow(
        """
        INSERT INTO messages (sender_id, receiver_id, content)
        VALUES ($1, $2, $3)
        RETURNING id, sender_id, receiver_id, content, is_read, created_at
        """,
        current_user_id,
        body.receiver_id,
        body.content,
    )

    message = dict(row)
    message["id"] = str(message["id"])
    message["created_at"] = message["created_at"].isoformat()

    realtime_delivered = False
    try:
        rc = await _get_redis()
        await rc.publish(f"chat_{body.receiver_id}", json.dumps(message))
        realtime_delivered = True
    except Exception as e:
        logger.warning("Redis publish failed (message saved to DB): %s", e)

    return {
        "message_id": message["id"],
        "sender_id": current_user_id,
        "receiver_id": body.receiver_id,
        "content": body.content,
        "created_at": message["created_at"],
        "realtime_delivered": realtime_delivered,
    }


@router.get("/chat/conversations/{user_id}", summary="List conversations with latest message and unread count")
async def get_conversations(
    user_id: str = Path(..., description="Clerk user ID"),
    current_user_id: str = Depends(get_current_user),
):
    """Returns all conversations for the authenticated user."""
    if current_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own conversations.",
        )

    rows = await db_manager.pg_pool.fetch(
        """
        SELECT
            other_user,
            content        AS last_message,
            created_at     AS last_message_at,
            sender_id,
            unread_count
        FROM (
            SELECT
                CASE
                    WHEN sender_id = $1 THEN receiver_id
                    ELSE sender_id
                END AS other_user,
                content,
                created_at,
                sender_id,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        CASE WHEN sender_id = $1 THEN receiver_id ELSE sender_id END
                    ORDER BY created_at DESC
                ) AS rn,
                COUNT(*) FILTER (
                    WHERE receiver_id = $1 AND is_read = FALSE
                ) OVER (
                    PARTITION BY
                        CASE WHEN sender_id = $1 THEN receiver_id ELSE sender_id END
                ) AS unread_count
            FROM messages
            WHERE sender_id = $1 OR receiver_id = $1
        ) sub
        WHERE rn = 1
        ORDER BY last_message_at DESC
        """,
        user_id,
    )

    conversations = []
    for r in rows:
        c = dict(r)
        c["last_message_at"] = c["last_message_at"].isoformat()
        conversations.append(c)

    return {
        "user_id": user_id,
        "total_conversations": len(conversations),
        "conversations": conversations,
    }


@router.get("/chat/{user_id}/{other_user_id}", summary="Get conversation history between two users")
async def get_conversation(
    user_id: str = Path(..., description="Clerk user ID"),
    other_user_id: str = Path(..., description="Other user's Clerk user ID"),
    limit: int = Query(50, ge=1, le=200, description="Messages per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    current_user_id: str = Depends(get_current_user),
):
    """Returns paginated message history between two users."""
    if current_user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view your own conversations.",
        )

    rows = await db_manager.pg_pool.fetch(
        """
        SELECT id, sender_id, receiver_id, content, is_read, created_at
        FROM messages
        WHERE (sender_id = $1 AND receiver_id = $2)
           OR (sender_id = $2 AND receiver_id = $1)
        ORDER BY created_at ASC
        LIMIT $3 OFFSET $4
        """,
        user_id,
        other_user_id,
        limit,
        offset,
    )

    messages = []
    for r in rows:
        m = dict(r)
        m["id"] = str(m["id"])
        m["created_at"] = m["created_at"].isoformat()
        messages.append(m)

    return {
        "user_id": user_id,
        "other_user_id": other_user_id,
        "total": len(messages),
        "messages": messages,
    }


@router.patch("/chat/{message_id}/read", summary="Mark a message as read")
async def mark_message_read(
    message_id: UUID = Path(..., description="UUID of the message"),
    current_user_id: str = Depends(get_current_user),
):
    """Mark a message as read. Only the receiver can mark their own messages."""
    row = await db_manager.pg_pool.fetchrow(
        "SELECT receiver_id FROM messages WHERE id = $1",
        message_id,
    )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Message not found.",
        )

    if row["receiver_id"] != current_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only mark your own received messages as read.",
        )

    await db_manager.pg_pool.execute(
        "UPDATE messages SET is_read = TRUE WHERE id = $1",
        message_id,
    )

    return {
        "message_id": str(message_id),
        "is_read": True,
    }
