-- chat_sessions memory refactor
-- context_state(JSONB) -> summary_text(TEXT), recent_chat(TEXT)

ALTER TABLE chat_sessions
DROP COLUMN IF EXISTS context_state;

ALTER TABLE chat_sessions
ADD COLUMN IF NOT EXISTS summary_text TEXT DEFAULT '',
ADD COLUMN IF NOT EXISTS recent_chat TEXT DEFAULT '';
