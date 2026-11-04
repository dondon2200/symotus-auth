-- 角色權限政策（reseller／end_user × 12 個動作）。main.py startup 的 create_all 會自動建表並 seed；
-- 此檔留作紀錄與手動套用。設計：symotus-frontend docs/superpowers/specs/2026-11-04-role-policies-design.md
CREATE TABLE IF NOT EXISTS role_policies (
  id SERIAL PRIMARY KEY,
  feature_key VARCHAR NOT NULL,
  role VARCHAR NOT NULL,
  allowed BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMP,
  updated_by INTEGER,
  CONSTRAINT uq_role_policies_key_role UNIQUE (feature_key, role)
);
CREATE INDEX IF NOT EXISTS ix_role_policies_feature_key ON role_policies (feature_key);
