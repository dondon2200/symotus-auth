-- 相機分享自助建帳（spec 2026-09-02）
-- 既有列：invitee_email/signup_limit 為 NULL、signup_count 為 0
-- → 行為等同「未指定對象、上限 10 人」，不需人工回填。
ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS invitee_email VARCHAR;
ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS signup_limit INTEGER;
ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS signup_count INTEGER NOT NULL DEFAULT 0;
