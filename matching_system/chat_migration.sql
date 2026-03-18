-- Chat System Migration
-- Run ONCE after karma_migration.sql:
--   psql -d community_matching -f matching_system/chat_migration.sql

BEGIN;

CREATE TABLE IF NOT EXISTS messages (
    id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_id       TEXT            NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    receiver_id     TEXT            NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    content         TEXT            NOT NULL CHECK (char_length(content) BETWEEN 1 AND 2000),
    is_read         BOOLEAN         DEFAULT FALSE,
    created_at      TIMESTAMPTZ     DEFAULT NOW(),
    CONSTRAINT no_self_message CHECK (sender_id <> receiver_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_sender
    ON messages (sender_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_receiver
    ON messages (receiver_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages (
        LEAST(sender_id, receiver_id),
        GREATEST(sender_id, receiver_id),
        created_at DESC
    );

CREATE INDEX IF NOT EXISTS idx_messages_unread
    ON messages (receiver_id)
    WHERE is_read = FALSE;

COMMIT;

-- Verify
SELECT column_name, data_type, udt_name, is_nullable
  FROM information_schema.columns
 WHERE table_name = 'messages'
 ORDER BY ordinal_position;
