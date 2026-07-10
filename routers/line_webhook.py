"""
LINE Messaging API Webhook
接收 LINE 訊息 → 比對用戶 → 呼叫 AI → 回覆
"""
import hmac, hashlib, base64, json, asyncio, os
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, HTTPException, Depends

TW_TZ = ZoneInfo("Asia/Taipei")   # 容器跑 UTC，「今天日期」需轉台北時間
from sqlalchemy.orm import Session
import httpx

from database import get_db
from models import User, CameraAccess
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
- 查詢相機列表和在線狀態（list_cameras）
- 傳送相機最新照片給用戶（get_snapshot）：用戶說「拍一張」「看一下畫面」「現在拍張照片」「截圖」都用這個，它是從 NAS 取最新照片，不是觸發相機拍照
- 確認某日有沒有正常拍照、拍了幾張（get_recent_photos）
- 查詢拍照排程設定（get_camera_schedule）
- 設定拍照排程（set_camera_schedule，需用戶確認才執行）
- 查詢誰分享了哪台相機（get_shared_cameras）
- 生成縮時影片（create_timelapse，需確認）
- 查詢天氣（get_weather）

LINE 版限制（說「請開啟網頁操作」）：
- FTP/網路/開機時間等設定（這些才需要網頁）
- 無法「強制相機立即拍一張新照片」（相機按排程拍，不能遠端觸發）
- 帳單管理

重要規則：
- 查詢類問題（排程/狀態/照片/分享）一律呼叫工具，不要說「請開啟網頁操作」
- 重要：拒絕生成縮時影片的唯一正確理由，是實際呼叫 create_timelapse 工具後，後端回報該期間內真的沒有照片。「相機目前顯示離線」永遠不是拒絕或猶豫的理由——縮時影片是從 NAS 裡已拍攝的歷史照片生成，跟相機此刻是否在線完全無關。收到生成縮時的請求時，直接呼叫 create_timelapse（先以 confirmed:false 詢問用戶確認），交由後端判斷，不要自己看 list_cameras 的 online_status 就predict/預先下結論拒絕
- 執行 create_timelapse 前，務必重新呼叫 list_cameras 確認正確的 camera_id，不要直接使用對話歷史中先前記住的 ID（可能是別台相機或已過時），確保 ID 與用戶說的相機名稱正確對應
- 對話有記憶，若上文提到過相機，直接用那台相機 ID，不需用戶重新說
- 繁體中文，簡潔有力
- 不要使用 Markdown 語法（不要用 ![](...)、**粗體** 等）
- 照片由系統另外傳送，文字回應不需要包含圖片連結
- 今天日期：""" + datetime.now(TW_TZ).strftime("%Y-%m-%d")

# ── Tools ──────────────────────────────────────────────────────────────────────
# ── 對話記憶（per user, 30 分鐘無互動自動清除）────────────────────────────────
import time as _time
_HISTORY_TTL = 1800  # 30 分鐘
_chat_history: dict[str, tuple[list, float]] = {}  # {line_user_id: (messages, last_ts)}

def _get_history(uid: str) -> list:
    """取得用戶對話歷史，超時自動清除"""
    now = _time.time()
    if uid in _chat_history:
        msgs, ts = _chat_history[uid]
        if now - ts < _HISTORY_TTL:
            return msgs
    return []

def _save_history(uid: str, msgs: list):
    """儲存對話歷史（最多保留最近 20 則）"""
    _chat_history[uid] = (msgs[-20:], _time.time())

def _clear_history(uid: str):
    _chat_history.pop(uid, None)


LINE_TOOLS = [
    {"type":"function","function":{
        "name":"list_cameras","description":"查詢用戶所有相機列表和在線狀態",
        "parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{
        "name":"get_camera_status","description":"查詢特定相機狀態",
        "parameters":{"type":"object","properties":{"camera_id":{"type":"number"}},"required":["camera_id"]}}},
    {"type":"function","function":{
        "name":"get_snapshot","description":"取得相機截圖並發送給用戶。用戶說「讓我看相機」「看一下畫面」時不填 date（取最新）；用戶指定日期（例如「7/1的照片」）時務必帶上 date 參數，會從 NAS 抓該日最新一張",
        "parameters":{"type":"object","properties":{
            "camera_id":{"type":"number","description":"相機 ID（從 list_cameras 取得）"},
            "date":{"type":"string","description":"YYYY-MM-DD，指定日期時填寫；不填則取目前最新即時畫面"}},"required":["camera_id"]}}},
    {"type":"function","function":{
        "name":"get_recent_photos","description":"查詢相機近期照片數量",
        "parameters":{"type":"object","properties":{
            "camera_id":{"type":"number"},
            "date":{"type":"string","description":"YYYY-MM-DD，不填查近 7 天"}},"required":["camera_id"]}}},
    {"type":"function","function":{
        "name":"create_timelapse","description":"生成縮時影片，需用戶確認。若用戶要求限制影片長度（例如「30秒的縮時」），務必帶上 target_duration_secs",
        "parameters":{"type":"object","properties":{
            "camera_id":{"type":"number"},
            "start_date":{"type":"string"},
            "end_date":{"type":"string"},
            "target_duration_secs":{"type":"number","description":"影片目標秒數，若用戶沒指定則不填（不限制，用全部照片）"},
            "confirmed":{"type":"boolean"}},"required":["camera_id","start_date","end_date","confirmed"]}}},
    {"type":"function","function":{
        "name":"get_shared_cameras","description":"查詢有哪些相機被分享給我，或我分享給別人的相機",
        "parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{
        "name":"get_camera_schedule","description":"查詢相機的拍照排程（開機時間、結束時間、拍照間隔）",
        "parameters":{"type":"object","properties":{"camera_id":{"type":"number"}},"required":["camera_id"]}}},
    {"type":"function","function":{
        "name":"set_camera_schedule","description":"設定相機的拍照排程（開機時間、結束時間、拍照間隔）。用戶確認後才執行。",
        "parameters":{"type":"object","properties":{
            "camera_id":{"type":"number","description":"相機 ID"},
            "start_time":{"type":"string","description":"開始時間 HH:MM，例如 08:00"},
            "end_time":{"type":"string","description":"結束時間 HH:MM，例如 18:00"},
            "interval_minutes":{"type":"number","description":"拍照間隔（分鐘），例如 15"},
            "confirmed":{"type":"boolean","description":"用戶是否已確認"}
        },"required":["camera_id","start_time","end_time","interval_minutes","confirmed"]}}},
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
        return {"result": "snapshot", "snapshot_camera_id": args["camera_id"], "snapshot_date": args.get("date") or "", "auth_token": auth_token}

    if name == "get_recent_photos":
        cam_id = args["camera_id"]; date = args.get("date", "")
        from datetime import timedelta
        end = date or datetime.now(TW_TZ).strftime("%Y-%m-%d")
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
        qs = f"camera_id={cam_id}&limit=1&offset=0&start_time={start}T00:00:00&end_time={end}T23:59:59"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{AUTH_SERVICE_URL}/cameras/nas/images?{qs}", headers=h)
        if not r.is_success: return {"result": "無法查詢"}
        d = r.json()
        return {"result": {"total": d.get("data", {}).get("total", 0), "dates": d.get("debug", {}).get("date_folders_found", []), "period": f"{start}~{end}"}}

    if name == "create_timelapse":
        target_secs = args.get("target_duration_secs") or 0
        if not args.get("confirmed"):
            dur_txt = f"，限制長度 {target_secs} 秒" if target_secs else ""
            return {"result": f"請確認：為相機 {args['camera_id']} 生成 {args['start_date']} 至 {args['end_date']} 的縮時影片{dur_txt}？回覆「確認」後我會送出任務。"}
        cam_url = f"{AUTH_SERVICE_URL}/cameras/{args['camera_id']}"
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                cr = await c.get(cam_url, headers=h)
        except Exception as e:
            return {"result": f"[DEBUG] 連線失敗 url={cam_url} error={type(e).__name__}: {e}"}
        if not cr.is_success:
            return {"result": f"[DEBUG] 相機查詢失敗 url={cam_url} status={cr.status_code} body={cr.text[:200]}"}
        cd = cr.json(); serial = cd.get("basic_info", cd).get("device_serial_id")
        cam_name = cd.get("basic_info", cd).get("name", f"相機 {args['camera_id']}")
        if not serial: return {"result": f"[DEBUG] 找不到序號 raw_response={json.dumps(cd)[:300]}"}

        nas_path = serial
        # 若有指定時長，先呼叫 prepare-timelapse 做每天均勻抽片（跟網頁邏輯一致）
        if target_secs > 0:
            prep_url = f"{AUTH_SERVICE_URL}/cameras/{args['camera_id']}/prepare-timelapse"
            try:
                async with httpx.AsyncClient(timeout=60) as c:
                    pr = await c.post(prep_url, headers={**h, "Content-Type": "application/json"},
                        json={"serial_id": serial, "start_date": args["start_date"], "end_date": args["end_date"],
                              "target_duration_secs": target_secs, "fps": 30})
                if pr.is_success:
                    nas_path = pr.json().get("nas_folder", serial)
                else:
                    return {"result": f"[DEBUG] 抽片失敗 status={pr.status_code} body={pr.text[:200]}"}
            except Exception as e:
                return {"result": f"[DEBUG] 抽片連線失敗 error={type(e).__name__}: {e}"}

        spark_body = {"nas_path": nas_path, "callback_url": f"{os.getenv('FRONTEND_URL', 'https://user.symotus.com')}/api/spark-callback",
                      "fps": 30, "resolution": "1920x1080",
                      "rain_fog_detection": True, "darkness_detection": True,
                      "image_recovery": False, "stabilization": False,
                      **({"start_date": args["start_date"], "end_date": args["end_date"]} if target_secs <= 0 else {})}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                sr = await c.post("https://user.symotus.com/spark/jobs/nas",
                    headers={"Content-Type":"application/json","x-api-key": SPARK_API_KEY},
                    json=spark_body)
        except Exception as e:
            return {"result": f"[DEBUG] Spark 連線失敗 error={type(e).__name__}: {e} body={json.dumps(spark_body, ensure_ascii=False)}"}
        if not sr.is_success:
            return {"result": f"[DEBUG] Spark 失敗 status={sr.status_code} body={sr.text[:300]} sent={json.dumps(spark_body, ensure_ascii=False)}"}
        job_id = sr.json().get("job_id")
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{AUTH_SERVICE_URL}/jobs", headers={**h, "Content-Type":"application/json"},
                json={"job_id": job_id, "camera_id": args["camera_id"], "camera_name": cam_name,
                      "serial_id": serial, "start_date": args["start_date"], "end_date": args["end_date"],
                      "fps": 30, "resolution": "1920x1080"})
        return {"result": f"✅ 縮時任務已送出！相機：{cam_name}，{args['start_date']} 至 {args['end_date']}。可到網頁 /jobs 查看進度。"}

    if name == "get_shared_cameras":
        async with httpx.AsyncClient(timeout=15) as cl:
            r = await cl.get(f"{AUTH_SERVICE_URL}/cameras", headers=h)
        if not r.is_success: return {"result": "無法取得相機列表"}
        cams = r.json().get("cameras", [])
        # 查 granter 名稱：用 camera_access 的 granted_by user
        async with httpx.AsyncClient(timeout=10) as cl:
            ra = await cl.get(f"{AUTH_SERVICE_URL}/admin/users",
                headers={"x-service-key": CAMERA_SERVICE_KEY})
        user_map = {}
        if ra.is_success:
            for u in ra.json():
                user_map[u["id"]] = u.get("full_name") or u.get("username") or u.get("email","?")
        shared = [cam for cam in cams if cam.get("is_shared")]
        owned = [cam for cam in cams if not cam.get("is_shared")]
        parts = []
        if owned:
            names = ", ".join(cam.get("name","?") for cam in owned)
            parts.append("你擁有：" + names)
        if shared:
            items = []
            for cam in shared:
                granter_id = cam.get("granted_by") or cam.get("granter_id")
                granter = user_map.get(granter_id, "管理員") if granter_id else "管理員"
                items.append(f"{cam.get('name','?')}（由 {granter} 分享）")
            parts.append("分享給你：" + "、".join(items))
        return {"result": "；".join(parts) if parts else "目前沒有任何相機"}

    if name == "get_camera_schedule":
        r = await (await httpx.AsyncClient(timeout=10).__aenter__()).get(f"{AUTH_SERVICE_URL}/cameras/{args['camera_id']}/timesnap", headers=h)
        if not r.is_success: return {"result": "無法取得排程設定"}
        d = r.json()
        enabled = str(d.get("enable","0")) in ("1","True","true")
        if not enabled: return {"result": "此相機目前未啟用拍照排程"}
        # interval 單位是秒
        interval_secs = int(d.get("interval", 900))
        interval_mins = interval_secs // 60
        # 解析 DAY7.T0.timeSeg（全天設定）: "enable/HH:MM-HH:MM"
        time_seg = d.get("DAY7.T0.timeSeg", "0/0:0-23:59")
        seg_enabled = time_seg.startswith("1/")
        if seg_enabled:
            time_range = time_seg[2:]  # 去掉 "1/"
            start_raw, end_raw = time_range.split("-") if "-" in time_range else ("0:0","23:59")
            def fmt(t): parts=t.split(":"); return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
            start_str = fmt(start_raw); end_str = fmt(end_raw)
        else:
            start_str = "全天"; end_str = "全天"
        mins_per_day = (int(end_raw.split(":")[0])*60+int(end_raw.split(":")[1])) - (int(start_raw.split(":")[0])*60+int(start_raw.split(":")[1])) if seg_enabled else 1440
        shots = mins_per_day // interval_mins if interval_mins > 0 else "?"
        return {"result": "拍照排程已啟用\n• 時間：" + start_str + " ～ " + end_str + "\n• 間隔：每 " + str(interval_mins) + " 分鐘\n• 每天約 " + str(shots) + " 張"}

    if name == "set_camera_schedule":
        if not args.get("confirmed"):
            start = args.get("start_time","?")
            end = args.get("end_time","?")
            interval = args.get("interval_minutes","?")
            mins = (int(end.split(":")[0])*60+int(end.split(":")[1])) - (int(start.split(":")[0])*60+int(start.split(":")[1]))
            shots = mins // int(interval) if mins > 0 and int(interval) > 0 else "?"
            return {"result": f"確認設定：每天 {start}～{end}，每 {interval} 分鐘拍一張，每天約 {shots} 張。回覆「確認」執行。"}
        # 組 timesnap payload（Camera Backend 格式）
        # Camera Backend timesnap：interval 單位是秒，時間用 DAY7.T0.timeSeg
        start = args["start_time"].replace(":", "").zfill(4)  # "08:00" → "0800"
        end = args["end_time"].replace(":", "").zfill(4)
        start_fmt = f"{int(start[:2])}:{int(start[2:])}"  # "8:0"
        end_fmt = f"{int(end[:2])}:{int(end[2:])}"
        payload = {
            "enable": True,
            "interval": int(args["interval_minutes"]) * 60,  # 轉為秒
            "ftp": 1,
            "DAY7.T0.timeSeg": f"1/{start_fmt}-{end_fmt}",  # 全週時間設定
        }
        async with httpx.AsyncClient(timeout=15) as cl:
            r = await cl.put(f"{AUTH_SERVICE_URL}/cameras/{args['camera_id']}/timesnap",
                headers={**h, "Content-Type": "application/json"},
                json=payload)
        if r.is_success:
            return {"result": f"✅ 排程已設定！相機 {args['camera_id']}：{args['start_time']}～{args['end_time']}，每 {args['interval_minutes']} 分鐘一張。"}
        return {"result": f"設定失敗（{r.status_code}），請確認相機是否在線。"}

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
    """改為呼叫網頁版共用的 /api/assistant，兩邊共用同一套 prompt/工具/邏輯，
    修一次 bug 兩邊都生效，不用再各自維護一份。"""
    history = _get_history(line_user_id)
    history.append({"role": "user", "content": text})

    assistant_url = f"{os.getenv('FRONTEND_URL', 'https://admin.symotus.com')}/api/assistant"
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            resp = await c.post(assistant_url,
                headers={"Content-Type": "application/json"},
                json={"messages": history, "auth_token": auth_token})
    except Exception as e:
        return {"text": f"[DEBUG] 呼叫共用 AI 服務失敗 error={type(e).__name__}: {e}", "snapshot": None}

    if not resp.is_success:
        return {"text": f"[DEBUG] 共用 AI 服務錯誤 status={resp.status_code} body={resp.text[:300]}", "snapshot": None}

    data = resp.json()
    ai_text = data.get("message", "好的。")
    actions = data.get("actions", []) or []

    photo_url = None
    nav_links: list[dict] = []
    NAV_LABELS = {
        "/jobs": "查看進度", "/dashboard": "前往儀表板", "/gdrive": "Google Drive 縮時",
        "/support": "技術支援", "/admin/users": "用戶管理", "/admin/support": "技術支援管理",
    }
    for act in actions:
        a_type = act.get("type")
        path = act.get("path") or ""
        if a_type == "photo" and path:
            photo_url = path
        elif a_type in ("navigate", "spotlight") and path:
            label = NAV_LABELS.get(path.split("?")[0])
            if not label:
                label = "查看相機" if path.startswith("/camera/") else "開啟頁面"
            nav_links.append({"path": path, "label": label[:20]})

    history.append({"role": "assistant", "content": ai_text})
    _save_history(line_user_id, history)

    snapshot_action = {"photo_url": photo_url} if photo_url else None
    return {"text": ai_text, "snapshot": snapshot_action, "nav_links": nav_links}

# ── 截圖處理 ─────────────────────────────────────────────────────────────────
GO2RTC_BASE = "https://user.symotus.com/go2rtc"

async def get_and_push_snapshot(line_user_id: str, camera_id: int, auth_token: str, date: str = ""):
    """取相機即時截圖（go2rtc）或指定日期/最新 NAS 照片 → 推送 LINE 圖片訊息"""
    from routers.public_camera import _store_temp_image, _temp_image_cache

    h = {"Authorization": f"Bearer {auth_token}"}

    # 指定日期時，一定要查 NAS 歷史照片，不能用即時串流（跳過 live-frame）
    if date:
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.get(
                f"{AUTH_SERVICE_URL}/cameras/nas/images?camera_id={camera_id}&limit=1&offset=0"
                f"&start_time={date}T00:00:00&end_time={date}T23:59:59",
                headers=h,
            )
        if not r.is_success:
            await line_push(line_user_id, [{"type":"text","text":f"查詢 {date} 照片失敗，請稍後再試。"}])
            return
        files = r.json().get("data", {}).get("files", [])
        if not files:
            await line_push(line_user_id, [{"type":"text","text":f"{date} 沒有拍攝到照片紀錄。"}])
            return
        image_url = files[0].get("image_url", "")
        full_path = image_url.replace("&thumbnail=true", "").replace("thumbnail=true&", "").replace("?thumbnail=true", "")
        photo_url = f"https://user.symotus.com{full_path}" if full_path.startswith("/") else full_path
        await line_push(line_user_id, [
            {"type": "image", "originalContentUrl": photo_url, "previewImageUrl": photo_url}
        ])
        return

    # 沒指定日期：先查 camera 的 ip 以取得 stream_name（即時串流）
    stream_name = None
    try:
        async with httpx.AsyncClient(timeout=8) as cl:
            cr = await cl.get(f"{AUTH_SERVICE_URL}/cameras/{camera_id}", headers=h)
        if cr.is_success:
            info = cr.json(); basic = info.get("basic_info", info)
            ip = basic.get("ip_address","")
            if ip and len(ip.split(".")) >= 3:
                stream_name = f"cam{ip.split('.')[2]}"
    except Exception:
        pass

    # 用 live-frame proxy 端點（直接 proxy go2rtc，不用 temp cache）
    if stream_name:
        FRONTEND_URL = os.getenv("FRONTEND_URL", "https://user.symotus.com")
        # F-4：以簽章 URL 提供 live-frame（30 分鐘有效），LINE 可抓圖但外部無法以 camera_id 枚舉
        from routers.public_camera import _live_frame_sig
        import time as _t
        _exp = int(_t.time()) + 1800
        _sig = _live_frame_sig(camera_id, _exp)
        public_url = f"{FRONTEND_URL}/auth-api/cameras/public/live-frame/{camera_id}?exp={_exp}&sig={_sig}"
        try:
            # 先驗證 go2rtc 有串流才送 LINE
            async with httpx.AsyncClient(timeout=8) as cl:
                test = await cl.get(f"{GO2RTC_BASE}/api/frame.jpeg?src={stream_name}")
            if test.status_code == 200 and test.content:
                await line_push(line_user_id, [
                    {"type": "image", "originalContentUrl": public_url, "previewImageUrl": public_url}
                ])
                return
        except Exception:
            pass  # fallback to NAS

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
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "admin"})
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
    FRONTEND_URL = os.getenv("FRONTEND_URL", "https://user.symotus.com")
    public_url = f"{FRONTEND_URL}/auth-api/cameras/public/temp-image/{token}"

    # 6. 推送 LINE 圖片訊息
    await line_push(line_user_id, [
        {"type": "image", "originalContentUrl": public_url, "previewImageUrl": public_url}
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
                "text": f"您好！請先到 {os.getenv('FRONTEND_URL', 'https://user.symotus.com')} 用 LINE 登入，才能使用 AI 助理功能"}])
            continue

        # 特殊指令：取消相機通知（從 Flex Message 按鈕觸發）
        if text.startswith("取消相機通知 "):
            try:
                cam_id = int(text.split(" ")[1])
                # 0-c：關閉所有符合列；0-d：若無存取列（admin 全域收通知）建一列作退訂標記
                rows = db.query(CameraAccess).filter(
                    CameraAccess.camera_id == cam_id,
                    CameraAccess.user_id == user.id,
                ).all()
                if rows:
                    for access in rows:
                        access.notify_on_online = False
                else:
                    db.add(CameraAccess(
                        camera_id=cam_id, user_id=user.id,
                        granted_by=user.id, permission_level="stream_only",
                        notify_on_online=False,
                    ))
                db.commit()
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

        # 若共用 AI 回傳了 photo action，直接推送圖片給用戶
        if result.get("snapshot") and result["snapshot"].get("photo_url"):
            await line_push(line_user_id, [{
                "type": "image",
                "originalContentUrl": result["snapshot"]["photo_url"],
                "previewImageUrl": result["snapshot"]["photo_url"],
            }])

        # 若有導頁需求，推送可點擊按鈕（LINE Buttons Template），最多一則（LINE 限制）
        nav_links = result.get("nav_links") or []
        if nav_links:
            link = nav_links[0]
            full_url = f"https://admin.symotus.com{link['path']}"
            await line_push(line_user_id, [{
                "type": "template",
                "altText": link["label"],
                "template": {
                    "type": "buttons",
                    "text": link["label"],
                    "actions": [
                        {"type": "uri", "label": link["label"][:20], "uri": full_url}
                    ]
                }
            }])

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
            json={"user_id": 0, "email": "admin@timelapse.com", "role": "admin"})
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
