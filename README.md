# Symotus Auth Service

Symotus 平台的認證與權限中心，使用 FastAPI + PostgreSQL，部署在 Render。

## 技術棧

- **Framework**: FastAPI (Python)
- **Database**: PostgreSQL (Railway)
- **Deploy**: Render（自動 deploy，push to main 即上線）
- **Container**: Docker

## 架構職責

Auth Service 是**唯一的權限控制中心**：
- 所有 Camera Backend API 都必須透過 Auth Service 的 proxy 存取
- Camera Backend 本身不做權限控制
- 相機存取權限由 `camera_access` 表管理

## Router 說明

| Router | 路徑 | 說明 |
|--------|------|------|
| auth | `/auth/*` | 登入、登出、OAuth（Google/LINE）|
| cameras | `/cameras/*` | 相機 API proxy（權限控制）|
| jobs | `/jobs/*` | 縮時任務、Google Drive 縮時 |
| invites | `/invites/*` | 邀請連結管理 |
| reseller | `/reseller/*` | Reseller 用戶管理 |
| admin | `/admin/*` | 平台管理（service key 保護）|
| support | `/support/*` | 技術支援授權 |

## 重要設定

### 環境變數（在 Render 設定）
```
DATABASE_URL          PostgreSQL 連線字串
JWT_SECRET            JWT 簽名密鑰
JWT_ALGORITHM         HS256
LINE_CLIENT_ID        LINE Login Channel ID
LINE_CLIENT_SECRET    LINE Login Channel Secret
LINE_REDIRECT_URI     https://symotus-auth.onrender.com/auth/line/callback
GOOGLE_CLIENT_ID      Google OAuth Client ID
GOOGLE_CLIENT_SECRET  Google OAuth Client Secret
GOOGLE_REDIRECT_URI   https://symotus-auth.onrender.com/auth/google/callback
FRONTEND_URL          https://admin.symotus.com
```

### Camera Backend 連接
```python
CAMERA_BACKEND_URL = "https://user.symotus.com"
CAMERA_SERVICE_KEY = "9ad3343a32508c209152a450f601b990176fa4d41c94c27330e448b1a86826c2"
```

## 用戶角色

```
symotus_admin  →  平台管理員（最高權限）
reseller       →  二房東（有自己的相機，可管理 end_user）
end_user       →  終端用戶（只能看被授權的相機）
```

## DB 重要欄位

**users 表**
- `camera_email`: 對應 Camera Backend 的帳號 email
- `camera_user_id`: Camera Backend 的 user id（固定傳 0 讓後端用 email 查）
- `line_id`: LINE OAuth ID
- `google_id`: Google OAuth ID

**camera_access 表**
- `camera_id`: Camera Backend 的相機 ID
- `user_id`: Auth Service 的用戶 ID
- `granted_by`: 授權者的 user ID

## 本地開發

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8001
```

## API 文件

https://symotus-auth.onrender.com/docs

## 相關 Repo

- 前端: https://github.com/dondon2200/symotus-frontend
