# Symotus Auth Service

權限管理微服務，負責登入、角色管理、邀請連結、相機存取授權。

## 快速啟動

### 1. 複製設定檔
```bash
cp .env.example .env
# 編輯 .env 填入實際值
```

### 2. 啟動服務
```bash
docker-compose up -d
```

### 3. 建立第一個 Admin 帳號
```bash
docker-compose exec auth-service python init_admin.py
```

### 4. 確認服務正常
```bash
curl http://localhost:8001/health
```

## API 文件
啟動後訪問：http://localhost:8001/docs

## Endpoints 總覽

### 認證
| Method | Path | 說明 |
|--------|------|------|
| POST | /auth/login | 帳密登入 |
| POST | /auth/refresh | 刷新 token |
| POST | /auth/logout | 登出 |
| GET  | /auth/me | 取得當前用戶 |
| GET  | /auth/google/url | 取得 Google OAuth URL |
| POST | /auth/google/token | Google OAuth 換 token |
| GET  | /auth/line/url | 取得 LINE OAuth URL |
| POST | /auth/line/token | LINE OAuth 換 token |

### 邀請管理（reseller）
| Method | Path | 說明 |
|--------|------|------|
| POST | /invites | 建立邀請連結 |
| GET  | /invites | 列出邀請記錄 |
| DELETE | /invites/{token} | 撤銷邀請 |
| GET  | /invites/preview/{token} | 查看邀請資訊（公開，不需登入）|

### 使用者管理（reseller）
| Method | Path | 說明 |
|--------|------|------|
| GET  | /reseller/users | 列出名下 end_user |
| PUT  | /reseller/users/{id} | 更新用戶（停用/啟用）|
| DELETE | /reseller/users/{id} | 移除 end_user |
| GET  | /reseller/cameras/{id}/access | 列出相機存取權 |
| POST | /reseller/cameras/{id}/access | 分配相機給用戶 |
| DELETE | /reseller/cameras/{id}/access/{user_id} | 撤銷存取 |

### 技術支援授權（reseller）
| Method | Path | 說明 |
|--------|------|------|
| POST | /support/grants | 授權 Symotus 技術支援 48hr |
| GET  | /support/grants | 查看有效授權 |
| DELETE | /support/grants/{id} | 提前撤銷 |

### 管理後台（symotus_admin）
| Method | Path | 說明 |
|--------|------|------|
| GET | /admin/resellers | 列出所有 reseller |
| GET | /admin/resellers/{id}/users | 查看 reseller 的 end_user |
| GET | /admin/support/grants | 查看所有有效技術支援授權 |

## Token 格式

JWT payload 包含：
```json
{
  "sub": 123,
  "role": "end_user",
  "reseller_id": 5,
  "camera_ids": [1, 2, 3],
  "exp": 1234567890
}
```

前端存在 localStorage，每次 request 帶在 Header：
```
Authorization: Bearer <token>
```

## NUC 部署說明

1. 確保 NUC 上已安裝 Docker 和 Docker Compose
2. clone 這個 repo 到 NUC
3. 設定 .env
4. `docker-compose up -d`
5. 在 nginx 設定反向代理：

```nginx
location /auth-api/ {
    proxy_pass http://localhost:8001/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## 前端需要的改動

1. 登入改打 Auth Service：`POST http://nuc-ip:8001/auth/login`
2. 登入後收到 JWT，存 localStorage
3. 打現有相機後端時帶同一個 JWT（現有後端只需驗 JWT 有效性）
4. 邀請頁面 `/invite/{token}` 先打 `/invites/preview/{token}` 顯示邀請資訊
