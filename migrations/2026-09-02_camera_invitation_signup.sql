-- 相機分享自助建帳（spec 2026-09-02）
-- 既有列：invitee_email/signup_limit 為 NULL、signup_count 為 0
-- → 行為等同「未指定對象、上限 10 人」，不需人工回填。
ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS invitee_email VARCHAR;
ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS signup_limit INTEGER;
ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS signup_count INTEGER NOT NULL DEFAULT 0;

-- 最終審查修正 1（Critical）：既有連結不得追溯獲得自助建帳能力。
-- 分享連結預設永不過期，遷移前建立的每一條非公開連結，NULL 若被 _signup_limit()
-- 視為「10 人可建帳」，會讓部署當下平台上所有歷史連結同時變成可自助建帳的連結
-- （其中舊的 full 連結甚至讓任何持有者建帳取得該相機完整管理權）。
-- signup_limit 設 0（0 = 不允許自助建帳）：只有部署後新建的連結才會拿到
-- 1（指定對象）或 NULL→10（未指定對象，見 _signup_limit()）。
UPDATE camera_invitations SET signup_limit = 0 WHERE signup_limit IS NULL;
