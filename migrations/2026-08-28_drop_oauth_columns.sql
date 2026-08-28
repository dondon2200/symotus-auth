-- Task 6：移除舊式單一 OAuth 綁定欄位（改用 user_line_accounts 多綁定表；Google 登入已停用）
ALTER TABLE users DROP COLUMN line_id;
ALTER TABLE users DROP COLUMN google_id;
