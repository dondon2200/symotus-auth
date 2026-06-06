"""
LINE Messaging API Webhook
接收 LINE 訊息 → 比對用戶 → 呼叫 AI → 回覆
"""
import hmac, hashlib, base64, json, asyncio, os
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.orm import Session
import httpx

from database import get_db
from models import User
from auth import create_access_token, create_refresh_token
from models import RefreshToken
from config import settings

router = APIRouter(prefix="/webhook", tags=["line-webhook"])

# ── 常數 ──────────────────────────────────────────────────────────────────────
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_ACCESS_TOKEN   = os.environ.get("LINE_ACCESS_TOKEN", "")
LINE_REPLY_URL      = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL       = "https://api.line.me/v2/bot/message/push"
LINE_LOADING_URL    = "https://api.line.me/v2/bot/chat/loading/start"
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
AUTH_SERVICE_URL    = os.getenv("AUTH_SERVICE_URL", "http://auth-service:8001")
CAMERA_BACKEND_URL  = "https://user.symotus.com"
CAMERA_SERVICE_KEY  = os.environ.get("CAMERA_SERVICE_KEY", "")
SPARK_API_KEY       = os.environ.get("SPARK_API_KEY", "")

LINE_HEADERS = {
    "Authorization": f"Bearer {LINE_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

# ── LINE System Prompt ─────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是 Symotus 縮時攝影平台的 AI 助理，透過 LINE 提供服務。

你能做的事：
- 查詢相機列表和在線狀態
- 發送相機最新截圖
- 確認某日有沒有正常拍照（張數）
- 生成縮時影片（需確認）
- 查詢天氣

LINE 版限制（直接說「請開啟網頁操作」）：
- 相機設定（FTP/排程/網路等）
- 帳單管理

回應原則：
- 繁體中文，簡潔有力
- 純聊天一句話回應，不呼叫 tool
- 天氣可查詢
- 今天日期：""" + datetime.now().strftime("%Y-%m-%d")

# ── Tools ──────────────────────────────────────────────────────────────────────
LINE_TOOLS = [
    {"type":"function","function":{
        "name":"list_cameras","description":"查詢用戶所有相機列表和在線狀態",
        "parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{
        "name":"get_camera_status","description":"查詢特定相機狀態",
        "parameters":{"type":"object","properties":{"camera_id":{"type":"number"}},"required":["camera_id"]}}},
    {"type":"function","function":{
        "name":"get_snapshot","description":"取得相機最新截圖並發送給用戶。用戶說「讓我看相機」「看一下畫面」時使用",
        "parameters":{"type":"object","properties":{"camera_id":{"type":"number","description":"相機 ID（從 list_cameras 取得）"}},"required":["camera_id"]}}},
    {"type":"function","function":{
        "name":"get_recent_photos","description":"查詢相機近期照片數量",
        "parameters":{"type":"object","properties":{
            "camera_id":{"type":"number"},
            "date":{"type":"string","description":"YYYY-MM-DD，不填查近 7 天"}},"required":["camera_id"]}}},
    {"type":"function","function":{
        "name":"create_timelapse","description":"生成縮時影片，需用戶確認",
        "parameters":{"type":"object","properties":{
            "camera_id":{"type":"number"},
            "start_date":{"type":"string"},
            "end_date":{"type":"string"},
            "confirmed":{"type":"boolean"}},"required":["camera_id","start_date","end_date","confirmed"]}}},
    {"type":"function","function":{
        "name":"get_weather","description":"查詢天氣",
        "parameters":{"type":"object","properties":{
            "location":{"type":"string"}},"required":["location"]}}},
]

# ── 簽名驗證 ──────────────────────────────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    h = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256)
    return hmac.compare_digest(base64.b64encode(h.digest()).decode(), signature)

# ── LINE 回覆 ─────────────────────────────────────────────────────────────────
async def line_reply(reply_token: str, messages: list):
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(LINE_REPLY_URL, headers=LINE_HEADERS,
                     json={"replyToken": reply_token, "messages": messages})

async def line_push(user_id: str, messages: list):
    async with httpx.AsyncClient(timeout=10) as c:
        await c.post(LINE_PUSH_URL, headers=LINE_HEADERS,
                     json={"to": user_id, "messages": messages})

async def line_loading(user_id: str):
    async with httpx.AsyncClient(timeout=5) as c:
        await c.post(LINE_LOADING_URL, headers=LINE_HEADERS,
                     json={"chatId": user_id, "loadingSeconds": 20})

# ── Tool 執行 ─────────────────────────────────────────────────────────────────
async def execute_tool(name: str, args: dict, auth_token: str, line_user_id: str) -> dict:
    h = {"Authorization": f"Bearer {auth_token}"}

    if name == "list_cameras":
        r = await (await httpx.AsyncClient(timeout=15).__aenter__()).get(f"{AUTH_SERVICE_URL}/cameras", headers=h)
        if not r.is_success: return {"result": "無法取得相機列表"}
        cams = [(c.get("id"), c.get("name"), c.get("online_status")) for c in r.json().get("cameras", [])]
        return {"result": cams}

    if name == "get_camera_status":
        r = await (await httpx.AsyncClient(timeout=10).__aenter__()).get(f"{AUTH_SERVICE_URL}/cameras/{args['camera_id']}", headers=h)
        if not r.is_success: return {"result": "無法取得"}
        d = r.json(); info = d.get("basic_info", d)
        return {"result": {"name": info.get("name"), "online": info.get("online_status"), "last_seen": info.get("last_seen")}}

    if name == "get_snapshot":
        return {"result": "snapshot", "snapshot_camera_id": args["camera_id"], "auth_token": auth_token}

    if name == "get_recent_photos":
        cam_id = args["camera_id"]; date = args.get("date", "")
        from datetime import timedelta
        end = date or datetime.now().strftime("%Y-%m-%d")
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
        qs = f"camera_id={cam_id}&limit=1&offset=0&start_time={start}T00:00:00&end_time={end}T23:59:59"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{AUTH_SERVICE_URL}/cameras/nas/images?{qs}", headers=h)
        if not r.is_success: return {"result": "無法查詢"}
        d = r.json()
        return {"result": {"total": d.get("data", {}).get("total", 0), "dates": d.get("debug", {}).get("date_folders_found", []), "period": f"{start}~{end}"}}

    if name == "create_timelapse":
        if not args.get("confirmed"):
            return {"result": f"請確認：為相機 {args['camera_id']} 生成 {args['start_date']} 至 {args['end_date']} 的縮時影片？回覆「確認」後我會送出任務。"}
        async with httpx.AsyncClient(timeout=15) as c:
            cr = await c.get(f"{AUTH_SERVICE_URL}/cameras/{args['camera_id']}", headers=h)
        if not cr.is_success: return {"result": "找不到相機"}
        cd = cr.json(); serial = cd.get("basic_info", cd).get("device_serial_id")
        cam_name = cd.get("basic_info", cd).get("name", f"相機 {args['camera_id']}")
        if not serial: return {"result": "找不到相機序號"}
        async with httpx.AsyncClient(timeout=30) as c:
            sr = await c.post("https://user.symotus.com/spark/jobs/nas",
                headers={"Content-Type":"application/json","x-api-key": SPARK_API_KEY},
                json={"nas_path": serial, "callback_url": f"{os.getenv('FRONTEND_URL', 'https://reseller.symotus.com:9443')}/api/spark-callback",
                      "fps": 30, "resolution": "1920x1080",
                      "rain_fog_detection": True, "darkness_detection": True,
                      "image_recovery": False, "stabilization": False,
                      "start_date": args["start_date"], "end_date": args["end_date"]})
        if not sr.is_success: return {"result": f"Spark 失敗：{sr.status_code}"}
        job_id = sr.json().get("job_id")
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{AUTH_SERVICE_URL}/jobs", headers={**h, "Content-Type":"application/json"},
                json={"job_id": job_id, "camera_id": args["camera_id"], "camera_name": cam_name,
                      "serial_id": serial, "start_date": args["start_date"], "end_date": args["end_date"],
                      "fps": 30, "resolution": "1920x1080"})
        return {"result": f"✅ 縮時任務已送出！相機：{cam_name}，{args['start_date']} 至 {args['end_date']}。可到網頁 /jobs 查看進度。"}

    if name == "get_weather":
        loc = args["location"].replace(" ", "+")
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://wttr.in/{loc}?format=j1", headers={"User-Agent":"symotus/1.0"})
        if not r.is_success: return {"result": "無法取得天氣"}
        d = r.json(); c2 = d.get("current_condition", [{}])[0]
        area = d.get("nearest_area", [{}])[0]
        return {"result": {
            "location": area.get("areaName", [{}])[0].get("value"),
            "temp_c": c2.get("temp_C"), "humidity": c2.get("humidity"),
            "desc": c2.get("weatherDesc", [{}])[0].get("value"),
            "wind_kmph": c2.get("windspeedKmph")}}

    return {"result": "未知工具"}

# ── AI 呼叫 ───────────────────────────────────────────────────────────────────
async def call_ai_line(text: str, auth_token: str, line_user_id: str) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}]
    snapshot_action = None

    for _ in range(4):
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                         "Content-Type": "application/json",
                         "HTTP-Referer": os.getenv("FRONTEND_URL", "https://reseller.symotus.com:9443"), "X-Title": "Symotus LINE Bot"},
                json={"model": "openai/gpt-4o-mini", "messages": messages,
                      "tools": LINE_TOOLS, "tool_choice": "auto",
                      "max_tokens": 600, "temperature": 0.3})
        if not resp.is_success:
            return {"text": "AI 服務暫時無法使用，請稍後再試。"}

        msg = resp.json().get("choices", [{}])[0].get("message", {})
        if not msg.get("tool_calls"):
            return {"text": msg.get("content", "好的。"), "snapshot": snapshot_action}

        messages.append(msg)
        for tc in msg.get("tool_calls", []):
            args = json.loads(tc["function"]["arguments"] or "{}")
            result = await execute_tool(tc["function"]["name"], args, auth_token, line_user_id)
            # 截圖是特殊處理
            if result.get("result") == "snapshot":
                snapshot_action = result
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                  "content": "截圖取得中，即將發送圖片給用戶"})
            else:
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                  "content": json.dumps(result["result"], ensure_ascii=False)})

    return {"text": "操作完成。", "snapshot": snapshot_action}

# ── 截圖處理 ─────────────────────────────────────────────────────────────────
async def get_and_push_snapshot(line_user_id: str, camera_id: int, auth_token: str):
    """取最新照片 → 存入臨時快取 → 推送 LINE 圖片訊息"""
    from routers.public_camera import _store_temp_image, _temp_image_cache

    h = {"Authorization": f"Bearer {auth_token}"}
    # 1. 查 NAS 最新照片
    async with httpx.AsyncClient(timeout=30) as cl:
        r = await cl.get(f"{AUTH_SERVICE_URL}/cameras/nas/images?camera_id={camera_id}&limit=1&offset=0", headers=h)
    if not r.is_success:
        await line_push(line_user_id, [{"type":"text","text":"找不到照片，請確認相機是否有拍照紀錄。"}])
        return
    files = r.json().get("data", {}).get("files", [])
    if not files:
        await line_push(line_user_id, [{"type":"text","text":"目前沒有照片紀錄。"}])
        return

    # 2. 取完整圖片 URL（不帶 thumbnail）
    image_url = files[0].get("image_url", "")
    taken_at = files[0].get("date", "")
    full_path = image_url.replace("&thumbnail=true", "").replace("thumbnail=true&", "").replace("?thumbnail=true", "")
    full_url = f"{CAMERA_BACKEND_URL}{full_path}"

    # 3. 取 Camera Backend token 下載圖片
    async with httpx.AsyncClient(timeout=10) as cl:
        tok_r = await cl.post(f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"})
    if not tok_r.is_success:
        await line_push(line_user_id, [{"type":"text","text":"無法取得相機授權。"}])
        return
    cam_token = tok_r.json().get("access_token", "")

    # 4. 下載圖片 bytes
    async with httpx.AsyncClient(timeout=30) as cl:
        img_r = await cl.get(full_url, headers={"Authorization": f"Bearer {cam_token}"})
    if not img_r.is_success:
        await line_push(line_user_id, [{"type":"text","text":"無法下載圖片。"}])
        return
    img_bytes = img_r.content
    content_type = img_r.headers.get("content-type", "image/jpeg")

    # 5. 存入臨時快取取得公開 token
    token = await _store_temp_image(img_bytes, content_type)
    FRONTEND_URL = os.getenv("FRONTEND_URL", "https://reseller.symotus.com:9443")
    public_url = f"{FRONTEND_URL}/auth-api/cameras/temp-image/{token}"

    # 6. 推送 LINE 圖片訊息
    await line_push(line_user_id, [
        {"type": "image", "originalContentUrl": public_url, "previewImageUrl": public_url},
        {"type": "text", "text": f"📷 最新照片 · {taken_at}"}
    ])

# ── Webhook endpoint ──────────────────────────────────────────────────────────
@router.post("/line")
async def line_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    sig  = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, sig):
        raise HTTPException(400, "Invalid signature")

    data = json.loads(body)
    for event in data.get("events", []):
        if event.get("type") != "message": continue
        if event["message"]["type"] != "text": continue

        line_user_id = event["source"]["userId"]
        reply_token  = event["replyToken"]
        text         = event["message"]["text"]

        # 找 Symotus 用戶
        user = db.query(User).filter(User.line_id == line_user_id).first()
        if not user:
            await line_reply(reply_token, [{"type": "text",
                "text": f"您好！請先到 {os.getenv('FRONTEND_URL', 'https://reseller.symotus.com:9443')} 用 LINE 登入，才能使用 AI 助理功能"}])
            continue

        # 特殊指令：取消相機通知（從 Flex Message 按鈕觸發）
        if text.startswith("取消相機通知 "):
            try:
                cam_id = int(text.split(" ")[1])
                access = db.query(CameraAccess).filter(
                    CameraAccess.camera_id == cam_id,
                    CameraAccess.user_id == user.id,
                ).first()
                if access:
                    access.notify_on_online = False
                    db.commit()
                    from models import CameraAccess as CA
                await line_reply(reply_token, [{"type": "text",
                    "text": f"✅ 已取消相機 #{cam_id} 的開機通知。如需重新訂閱，請至網頁開機通知設定。"}])
            except Exception:
                await line_reply(reply_token, [{"type": "text", "text": "取消通知失敗，請稍後再試"}])
            continue

        # 顯示載入動畫
        await line_loading(line_user_id)

        # 產生 auth token
        auth_token = create_access_token(user, db)

        # 呼叫 AI
        result = await call_ai_line(text, auth_token, line_user_id)

        # 回覆文字
        await line_reply(reply_token, [{"type": "text", "text": result["text"]}])

        # 若有截圖需求，另外 push 圖片
        if result.get("snapshot"):
            snap = result["snapshot"]
            asyncio.create_task(get_and_push_snapshot(
                line_user_id, snap["snapshot_camera_id"], snap["auth_token"]))

    return {"status": "ok"}


# ── 截圖公開端點（LINE 用）────────────────────────────────────────────────────
@router.get("/snapshot/{camera_id}")
async def line_snapshot(camera_id: int, t: int, sig: str):
    """LINE 用的臨時公開圖片端點（5 分鐘有效）"""
    import time
    if abs(time.time() - t) > 300:
        raise HTTPException(400, "Token expired")
    expected = hashlib.md5(f"{camera_id}:{t}:{LINE_CHANNEL_SECRET}".encode()).hexdigest()[:12]
    if sig != expected:
        raise HTTPException(403, "Invalid signature")

    # 取最新照片 URL
    async with httpx.AsyncClient(timeout=10) as c:
        tok_r = await c.post(f"{CAMERA_BACKEND_URL}/internal/auth/token",
            headers={"x-service-key": CAMERA_SERVICE_KEY},
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "symotus_admin"})
    cam_token = tok_r.json().get("access_token", "") if tok_r.is_success else ""

    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{CAMERA_BACKEND_URL}/api/camera/nas/images?camera_id={camera_id}&limit=1&offset=0",
                        headers={"Authorization": f"Bearer {cam_token}"})
    if not r.is_success:
        raise HTTPException(404, "No image")
    files = r.json().get("data", {}).get("files", [])
    if not files:
        raise HTTPException(404, "No image")

    img_path = files[0].get("image_url", "").replace("&thumbnail=true","").replace("thumbnail=true&","")
    async with httpx.AsyncClient(timeout=20) as c:
        img_r = await c.get(f"{CAMERA_BACKEND_URL}{img_path}",
                            headers={"Authorization": f"Bearer {cam_token}"})

    from fastapi.responses import Response
    return Response(content=img_r.content,
                    media_type=img_r.headers.get("content-type", "image/jpeg"))
