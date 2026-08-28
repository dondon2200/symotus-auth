-- Task 1: 多 LINE 綁定資料表
-- users.line_id 先保留（Task 6 全部引用清除後再 DROP）

CREATE TABLE user_line_accounts (
  id SERIAL PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  line_user_id TEXT NOT NULL UNIQUE,
  display_name TEXT, picture_url TEXT,
  created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX ix_user_line_accounts_user_id ON user_line_accounts(user_id);
INSERT INTO user_line_accounts (user_id, line_user_id)
  SELECT id, line_id FROM users WHERE line_id IS NOT NULL;
ALTER TABLE users ALTER COLUMN role SET DEFAULT 'end_user';
