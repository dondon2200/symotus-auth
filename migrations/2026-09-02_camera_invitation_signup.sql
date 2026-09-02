-- 相機分享自助建帳（spec 2026-09-02）
-- 既有列：invitee_email/signup_limit 為 NULL、signup_count 為 0
-- → 行為等同「未指定對象、上限 10 人」，不需人工回填。
ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS invitee_email VARCHAR;
ALTER TABLE camera_invitations ADD COLUMN IF NOT EXISTS signup_count INTEGER NOT NULL DEFAULT 0;

-- 最終審查修正 1（Critical）→ 複審修正 2（Minor）：既有連結不得追溯獲得自助建帳能力。
-- 分享連結預設永不過期，遷移前建立的每一條非公開連結，NULL 若被 _signup_limit()
-- 視為「10 人可建帳」，會讓部署當下平台上所有歷史連結同時變成可自助建帳的連結
-- （其中舊的 full 連結甚至讓任何持有者建帳取得該相機完整管理權）。
--
-- 複審指出：原本「ALTER 一句 + UPDATE 一句」只在「服務上線前手動跑這支檔案」的
-- 前提下安全。這支檔案放在 migrations/ 目錄，日後可能因換機、複製到 staging、
-- 或重放整個 migrations 目錄而在「服務已上線、signup_limit 已有正常非 NULL 值」的
-- 環境下被重新執行——屆時 UPDATE ... WHERE signup_limit IS NULL 撈到的就不是
-- 「遷移前的既有連結」，而是「部署後才新建、尚未指定對象、合法維持 NULL（視為
-- 10 人）的正常連結」，會把它們一併清成 0，功能靜默失效且無任何錯誤訊息。
--
-- 改成守衛式 DO block：只有在「signup_limit 欄位這次執行前就不存在」時才 ALTER
-- 新增並回填 0；欄位若已存在（代表非首次執行——不論是本 repo 的 main.py 已經加過，
-- 還是這支檔案先前已經跑過一次），就什麼都不做，直接略過 ALTER 與 UPDATE。
-- 語意與 main.py startup 內的單一交易遷移（同樣以「欄位是否本次才新增」為準）對齊。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'camera_invitations'
          AND column_name = 'signup_limit'
    ) THEN
        ALTER TABLE camera_invitations ADD COLUMN signup_limit INTEGER;
        UPDATE camera_invitations SET signup_limit = 0 WHERE signup_limit IS NULL;
    END IF;
END $$;
