-- 官網方案上架（2026-11-16，修改清單 #11）：半年約（term='semiannual'，VARCHAR 不需改型別）、
-- 一次性費用（採購訂閱的相機採購款）、方案內容。spec：symotus-frontend/docs/superpowers/specs/2026-11-16-official-plans-design.md
-- main.py startup 會執行相同內容；本檔留作紀錄，可重跑。
ALTER TABLE billing_plans_v2 ADD COLUMN IF NOT EXISTS one_time_fee INTEGER;
ALTER TABLE billing_plans_v2 ADD COLUMN IF NOT EXISTS one_time_label VARCHAR(50);
ALTER TABLE billing_plans_v2 ADD COLUMN IF NOT EXISTS features TEXT;
ALTER TABLE billing_subscriptions_v2 ADD COLUMN IF NOT EXISTS one_time_fee INTEGER;
ALTER TABLE billing_subscriptions_v2 ADD COLUMN IF NOT EXISTS one_time_label VARCHAR(50);
ALTER TABLE billing_bills ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'period';
ALTER TABLE billing_bills ADD COLUMN IF NOT EXISTS label VARCHAR(50);
-- 一次性帳單與第一期同一天，舊唯一鍵 (subscription_id, period_start) 會撞
ALTER TABLE billing_bills DROP CONSTRAINT IF EXISTS uq_billing_bill_sub_period;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_billing_bill_sub_kind_period') THEN
        ALTER TABLE billing_bills ADD CONSTRAINT uq_billing_bill_sub_kind_period UNIQUE (subscription_id, kind, period_start);
    END IF;
END $$;
