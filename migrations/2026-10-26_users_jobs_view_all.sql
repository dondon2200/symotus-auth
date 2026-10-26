-- 帳號旗標：可讀取全部縮時影片（任務頁看全部、可播放／下載／分享，不能改刪別人的）
-- main.py startup 也會自動補；此檔留作紀錄與手動套用。
ALTER TABLE users ADD COLUMN IF NOT EXISTS jobs_view_all BOOLEAN DEFAULT FALSE NOT NULL;
