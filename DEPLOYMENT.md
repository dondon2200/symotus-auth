# Symotus-Auth 部署文件（自架機器 + GitHub CI/CD）

> 將 Auth Service（FastAPI + PostgreSQL）部署到自己的伺服器，並以 GitHub Actions 自動建置、推送映像、部署上線。
> 本文件為「執行說明」，照做即可。
> 建立日期：2026-06-02 ｜ 加入 CI/CD：2026-06-02

---

## 1. 服務組成

| 容器 | 內容 | 對外 port |
|---|---|---|
| `auth-service` | FastAPI（uvicorn）認證／權限中心 | **8001** |
| `auth-db` | PostgreSQL 16 | 5432（建議僅綁 localhost，不對外） |

- 啟動時會**自動建表**（`Base.metadata.create_all`）並補欄位，無需手動 migration。
- 健康檢查端點：`GET /health` → `{"status":"ok","service":"symotus-auth"}`
- API 文件：`/docs`

---

## 2. 部署架構與 CI/CD 總覽

程式碼推到 GitHub `main` 後，GitHub Actions 會自動建置映像、推到 GitHub Container Registry（GHCR），再透過 SSH 到自架伺服器拉新映像並重啟。伺服器端**不再 build**，只負責 `docker compose pull && up`。

```
  開發者 push / merge → main
            │
            ▼
   ┌─────────────────────── GitHub Actions（.github/workflows/ci-cd.yml）───────────────────────┐
   │  ① lint        ruff check + format（PR 與 main 都跑）                                        │
   │  ② smoke-test  docker build → 起 service+db → 打 /health（PR 與 main 都跑）                  │
   │  ③ release     CI 過了才跑：build → 推映像到 ghcr.io/dondon2200/symotus-auth:{sha,latest}    │
   │  ④ deploy      SSH 到伺服器：docker compose pull && up -d                                    │
   └────────────────────────────────────────────────────────────────────────────────────────────┘
            │ (僅 main / 手動觸發)
            ▼
   自架伺服器：docker compose（image-based）+ .env  →  auth-service:8001 + auth-db
            ▲
            │ nginx 反代 + HTTPS
       https://auth.symotus.com
```

- **PR**：只跑 ①② 兩個檢查，不會部署。
- **push 到 `main`** 或在 Actions 頁手動 **Run workflow**：①②③④ 全跑，自動部署上線。
- 映像 tag：每次 build 會同時打 `latest` 與該 commit 的短 sha（如 `a1b2c3d`），回滾時可指定 sha。

---

## 3. 前置需求

- 伺服器已安裝 **Docker** 與 **Docker Compose**。
- 一個對外網域（OAuth 與前端 CORS 都需要 HTTPS），例如 `https://auth.symotus.com`。
- Google / LINE OAuth 應用憑證（若要啟用社群登入）。
- Camera Backend 的 service key（與後端約定的 `x-service-key`）。
- **GitHub repo**：`dondon2200/symotus-auth`（CI/CD 與 GHCR 映像都掛在此）。
- 伺服器可被 GitHub Actions 以 **SSH** 連入（開放 SSH port、放入部署用公鑰）。

---

## 4. 上機前必須注意的兩個設定點

部署到自架機器時，以下兩點若沒設對，服務會「起得來但無法運作」，請務必照本文件的 compose 設定：

1. **PORT 必須設為 8001**
   程式預設監聽 `PORT`（預設值 8000）。對外要用 8001，因此 **必須在環境變數明確設定 `PORT=8001`**，讓容器內外一致（repo 的 `docker-compose.yml` 已設定）。

2. **LINE 變數名稱必須是 `LINE_CHANNEL_ID` / `LINE_CHANNEL_SECRET`**
   程式讀取的是 `LINE_CHANNEL_ID` / `LINE_CHANNEL_SECRET`（`LINE_CLIENT_ID` 只是唯讀別名，用環境變數設它無效）。repo 的 `docker-compose.yml` 已用正確名稱。

---

## 5. 環境變數清單

放在伺服器部署目錄的 `.env`（與 `docker-compose.yml` 同層，**不要 commit**）：

```dotenv
# ── 資料庫 ──
POSTGRES_PASSWORD=請改成強密碼

# ── JWT ──
JWT_SECRET=請用 openssl rand -hex 32 產生
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=1440
REFRESH_TOKEN_EXPIRE_DAYS=30

# ── 對外網址（OAuth redirect / CORS 都靠這些）──
FRONTEND_URL=https://admin.symotus.com           # 前端網域（CORS 白名單 + OAuth 完成後導回）
CAMERA_BACKEND_URL=https://user.symotus.com/api  # Camera Backend

# ── Google OAuth（選用）──
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=https://auth.symotus.com/auth/google/callback

# ── LINE OAuth（選用，注意是 CHANNEL 不是 CLIENT）──
LINE_CHANNEL_ID=
LINE_CHANNEL_SECRET=
LINE_REDIRECT_URI=https://auth.symotus.com/auth/line/callback

# ── 部署映像（選用，回滾時覆寫）──
# AUTH_IMAGE=ghcr.io/dondon2200/symotus-auth:latest
```

> `DATABASE_URL` 不放這裡，由 compose 直接組出（指向 `auth-db` 服務）。
> 必填：`POSTGRES_PASSWORD`、`JWT_SECRET`、`FRONTEND_URL`。OAuth 不啟用時對應變數可留空。
> `AUTH_IMAGE` 不設時預設拉 `:latest`，回滾時才需指定特定 sha（見 §15）。

---

## 6. `docker-compose.yml`（正式環境版，已在 repo）

repo 內的 `docker-compose.yml` 已是正式環境版：`auth-service` 直接拉 GHCR 映像（不在伺服器 build），並已修正 PORT 與 LINE 變數名稱、加上資料持久化、healthcheck，並把 DB 綁 localhost。內容如下：

```yaml
services:
  auth-db:
    image: postgres:16-alpine
    container_name: symotus-auth-db
    restart: unless-stopped
    environment:
      POSTGRES_DB: symotus_auth
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - auth_db_data:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5433:5432"   # 僅本機可連，不對外
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d symotus_auth"]
      interval: 10s
      timeout: 5s
      retries: 10

  auth-service:
    # 預設拉 latest；回滾時用 AUTH_IMAGE=ghcr.io/dondon2200/symotus-auth:<sha> 覆寫
    image: ${AUTH_IMAGE:-ghcr.io/dondon2200/symotus-auth:latest}
    container_name: symotus-auth
    restart: unless-stopped
    ports:
      - "8001:8001"
    environment:
      PORT: 8001                # ← 關鍵：與對外 port 一致
      DATABASE_URL: postgresql://postgres:${POSTGRES_PASSWORD}@auth-db:5432/symotus_auth
      JWT_SECRET: ${JWT_SECRET}
      JWT_ALGORITHM: ${JWT_ALGORITHM:-HS256}
      JWT_EXPIRE_MINUTES: ${JWT_EXPIRE_MINUTES:-1440}
      REFRESH_TOKEN_EXPIRE_DAYS: ${REFRESH_TOKEN_EXPIRE_DAYS:-30}
      CAMERA_BACKEND_URL: ${CAMERA_BACKEND_URL:-https://user.symotus.com/api}
      FRONTEND_URL: ${FRONTEND_URL}
      GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID:-}
      GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET:-}
      GOOGLE_REDIRECT_URI: ${GOOGLE_REDIRECT_URI:-}
      LINE_CHANNEL_ID: ${LINE_CHANNEL_ID:-}
      LINE_CHANNEL_SECRET: ${LINE_CHANNEL_SECRET:-}
      LINE_REDIRECT_URI: ${LINE_REDIRECT_URI:-}
    depends_on:
      auth-db:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8001/health').status==200 else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s

volumes:
  auth_db_data:
```

> CI 另有一份 `docker-compose.ci.yml`，只給 GitHub Actions 煙霧測試用（拉本地剛 build 的 `symotus-auth:ci`），**勿用於正式環境**。

---

## 7. `Dockerfile`

repo 內現有的 Dockerfile 可直接使用（`python main.py` 會讀 `PORT` 環境變數），CI 的 `release` 階段就是 build 它：

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

> `main.py` 以 `uvicorn` 監聽 `0.0.0.0:$PORT`，故容器內會跑在 8001（由 compose 的 `PORT` 注入）。
> 另有 `.dockerignore` 排除 `.git`、`.env`、`__pycache__` 等，讓映像更小、build 更快。

---

## 8. CI/CD 設定（GitHub Actions）

流程定義在 [.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml)，四個 job：

| Job | 觸發時機 | 做什麼 |
|---|---|---|
| `lint` | PR、push main | `ruff check .` + `ruff format --check .`（規則見 [ruff.toml](ruff.toml)） |
| `smoke-test` | PR、push main | `docker build` → 用 `docker-compose.ci.yml` 起 service+db → 打 `/health` 等回 200 |
| `release` | 僅 main（CI 過才跑） | build 映像並推到 `ghcr.io/dondon2200/symotus-auth`，tag 為 `latest` + 短 sha |
| `deploy` | 僅 main（`release` 後） | SSH 到伺服器 `docker compose pull && up -d`，再 `docker image prune -f` |

### 8.1 需要設定的 GitHub Secrets

在 GitHub repo → **Settings → Secrets and variables → Actions → New repository secret** 建立：

| Secret | 說明 |
|---|---|
| `DEPLOY_HOST` | 伺服器 IP 或網域（如 `auth.symotus.com`） |
| `DEPLOY_USER` | SSH 登入帳號（需有權限跑 `docker compose`） |
| `DEPLOY_SSH_KEY` | 部署用 SSH **私鑰**（對應公鑰放在伺服器 `~/.ssh/authorized_keys`） |
| `DEPLOY_PORT` | SSH port（一般是 `22`） |
| `DEPLOY_PATH` | 伺服器上放 `docker-compose.yml` 與 `.env` 的目錄（如 `/home/deploy/symotus-auth`） |
| `GHCR_TOKEN` | 伺服器登入 GHCR 拉**私有**映像用的 PAT（權限 `read:packages`）。若把套件設公開可省略此步 |

> 推映像用的權限由內建的 `GITHUB_TOKEN` + workflow 的 `packages: write` 自動取得，**不需**額外 secret。

### 8.2 ruff 設定

lint 規則見 repo 的 `ruff.toml`。已刻意忽略下列規則（屬本專案正確寫法，非錯誤）：

- `E711` / `E712`：SQLAlchemy filter 需要 `== None` / `== True` 來產生 SQL `IS NULL` / `= TRUE`。
- `E402`：部分模組刻意在區塊註解後才 import。
- `routers/__init__.py` 的 `F401`：匯入各 router 供 `from routers import ...` re-export。

本機要先檢查（避免 CI 卡關）：

```bash
pip install ruff==0.15.15
ruff check .
ruff format .        # 自動排版；CI 用 --check 不會改檔，本機請先跑這行排好
```

---

## 9. 伺服器一次性設定（讓 CI 能部署）

CI 的 `deploy` 階段是 SSH 進伺服器執行 `docker compose pull && up`，所以伺服器上要先備好「compose + .env + 能登入 GHCR + 收得到 SSH」：

```bash
# 1. 在伺服器建立部署目錄並 clone（路徑要與 DEPLOY_PATH 一致）
git clone https://github.com/dondon2200/symotus-auth.git ~/symotus-auth
cd ~/symotus-auth

# 2. 建立 .env（內容見 §5）
nano .env

# 3. 產生部署用 SSH 金鑰（在本機或伺服器擇一產生），把公鑰加到伺服器
#    把「私鑰」內容貼到 GitHub secret DEPLOY_SSH_KEY
ssh-keygen -t ed25519 -C "github-deploy" -f ~/deploy_key
cat ~/deploy_key.pub >> ~/.ssh/authorized_keys   # 在伺服器上執行

# 4. （私有映像才需要）讓伺服器能登入 GHCR
#    用一個有 read:packages 的 PAT 登入，CI 每次部署也會再 login 一次
echo "<GHCR_PAT>" | docker login ghcr.io -u dondon2200 --password-stdin
```

> 伺服器上的 repo 只需要 `docker-compose.yml` 與 `.env`；映像由 GHCR 拉，程式碼不必在伺服器 build。
> 若不想讓伺服器 clone 整個 repo，也可只放 `docker-compose.yml` 與 `.env` 兩個檔在 `DEPLOY_PATH`。

---

## 10. 首次部署步驟

```bash
# 1. 先讓 CI 推出第一版映像：
#    把程式碼 push 到 main，或到 GitHub Actions 頁手動 Run workflow（ci-cd.yml）。
#    確認 release job 成功，GHCR 已有 ghcr.io/dondon2200/symotus-auth:latest。

# 2. 伺服器已完成 §9 一次性設定後，CI 的 deploy job 會自動 SSH 上來 pull & up。
#    若要在伺服器手動起一次：
cd ~/symotus-auth
docker compose pull
docker compose up -d

# 3. 確認兩個容器健康
docker compose ps
docker compose logs -f auth-service   # 應看到 "DB connected and tables created!"
```

> 之後每次 push 到 `main` 都會自動走完 build → push → deploy，不需手動上機。

---

## 11. 建立第一個管理員帳號

DB 表建立後，執行一次性腳本建立 `symotus_admin`：

```bash
docker compose exec auth-service python init_admin.py
# 輸出： Admin created: symotus_admin / change-this-password
```

> **立即改密碼**：預設密碼為 `change-this-password`，登入後（或直接改 `init_admin.py` 後重跑）務必更換。

---

## 12. OAuth 設定（啟用 Google / LINE 才需要）

在各 OAuth 後台把 **redirect URI** 設成與 `.env` 完全一致的網址：

| 供應商 | 後台 | 需登記的 Redirect URI |
|---|---|---|
| Google | Google Cloud Console → OAuth 用戶端 | `https://auth.symotus.com/auth/google/callback` |
| LINE | LINE Developers → Channel → LINE Login | `https://auth.symotus.com/auth/line/callback` |

- 程式會用 `FRONTEND_URL` 在 OAuth 完成後把使用者導回前端（`/auth/callback`），故 `FRONTEND_URL` 必須是實際前端網域。
- `FRONTEND_URL` 同時是 CORS 白名單來源；前端網域若不符，瀏覽器會擋下 API 請求。

---

## 13. 反向代理 + HTTPS（必要）

OAuth redirect 與前端 CORS 都要求 HTTPS。在主機放一層 nginx，將網域反代到 `127.0.0.1:8001`：

```nginx
# /etc/nginx/conf.d/symotus-auth.conf
server {
    listen 80;
    server_name auth.symotus.com;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name auth.symotus.com;

    ssl_certificate     /etc/nginx/ssl/auth.cert.pem;
    ssl_certificate_key /etc/nginx/ssl/auth.key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

> 取得憑證可用 certbot：`certbot certonly --webroot -w /var/www/certbot -d auth.symotus.com`，再把憑證路徑填入上方。

---

## 14. 驗證

```bash
# 服務健康
curl -s https://auth.symotus.com/health
# → {"status":"ok","service":"symotus-auth"}

# API 文件可開
#   瀏覽器開 https://auth.symotus.com/docs

# 登入（用 init_admin 建立的帳號）
curl -s -X POST https://auth.symotus.com/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"symotus_admin","password":"change-this-password"}'
# → 應回傳 access_token / refresh_token
```

前端串接：把前端的 `AUTH_API` / `AUTH_CAMERAS_API` 指向 `https://auth.symotus.com`，並確認此網域已在本服務 `FRONTEND_URL` 對應的 CORS 白名單可互通。

---

## 15. 升級與回滾

**升級（日常）**：直接 push / merge 到 `main`，CI/CD 會自動 build → push → 部署。要手動重跑可到 GitHub Actions 頁 **Run workflow**。

**回滾**：用特定 commit 的映像 sha 重啟（不必改程式碼）。

```bash
# 在 GHCR 或 GitHub Actions 紀錄找到要回滾的 commit 短 sha（如 a1b2c3d）
cd ~/symotus-auth

# 方式 A：暫時指定映像 tag 起一次
AUTH_IMAGE=ghcr.io/dondon2200/symotus-auth:a1b2c3d docker compose up -d

# 方式 B：寫進 .env 持久化（下次 deploy 前記得改回 latest 或移除）
echo "AUTH_IMAGE=ghcr.io/dondon2200/symotus-auth:a1b2c3d" >> .env
docker compose up -d
```

> 資料存於 `auth_db_data` volume，重建容器不會遺失。**升級前建議備份**（見 §16）。
> 緊急時仍可在伺服器手動 `git checkout <commit> && docker build`，但正常情況一律走映像 tag 回滾。

---

## 16. 維運注意事項

1. **資料庫備份**（重要）：
   ```bash
   docker compose exec auth-db pg_dump -U postgres symotus_auth > backup_$(date +%F).sql
   ```
   還原：`cat backup.sql | docker compose exec -T auth-db psql -U postgres symotus_auth`

2. **寫死的 Camera service key**：`routers/auth.py`、`routers/cameras.py`、`routers/jobs.py`、`routers/admin.py` 內 `CAMERA_SERVICE_KEY` 為明碼，且須與 Camera Backend 約定值一致。
   - 上機前確認此 key 與後端相符，否則 `/cameras/*`、`/jobs/*` proxy 會失敗。
   - 建議後續改為讀取環境變數（目前為硬編碼）。

3. **JWT_SECRET**：務必使用高強度隨機值；更換後所有既有 token 會失效（使用者需重新登入）。

4. **DB 不對外**：compose 已將 PostgreSQL 綁 `127.0.0.1`，請勿改成對公網開放。

5. **首次啟動較慢**：`auth-service` 會等 `auth-db` healthy 後才建表，`start_period` 已設 20s，屬正常。

6. **GHCR 套件權限**：私有套件需伺服器 `docker login ghcr.io`（§9）；若改設公開則伺服器免登入，但映像會公開可見，請斟酌。

7. **部署 secrets 安全**：`DEPLOY_SSH_KEY`、`GHCR_TOKEN` 等只存在 GitHub Secrets，切勿寫進 repo；金鑰外洩時立即在 GitHub 與伺服器各自撤換。
