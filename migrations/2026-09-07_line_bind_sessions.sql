-- LINE Login 一鍵綁定：一次性綁定 session
CREATE TABLE line_bind_sessions (
  id SERIAL PRIMARY KEY,
  sid TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TIMESTAMP NOT NULL,
  used_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX ix_line_bind_sessions_user_id ON line_bind_sessions(user_id);
