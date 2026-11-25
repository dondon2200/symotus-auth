"""照片打包下載（需求台帳 R-015）：依日期範圍逐張向 Camera Backend 抓照片，邊抓邊串流成 zip。

三個刻意的限制，都是為了 auth 容器的 2 GiB 記憶體上限：
- 不壓縮（JPEG 再壓沒有意義）、不落地、不把整包放進記憶體；記憶體裡同時最多 FETCH_CONCURRENCY 張照片。
- 單次範圍最多 MAX_DAYS 天。
- 同時進行的打包數量由呼叫端（router）限制。

這個模組不碰 HTTP 與資料庫：列目錄與抓照片由呼叫端注入，測試不必打網路。
"""
import asyncio
import re
import zipfile
from datetime import date, timedelta
from typing import AsyncIterator, Awaitable, Callable, Optional

MAX_DAYS = 31
FETCH_CONCURRENCY = 4

FAILED_LIST_NAME = "下載失敗清單.txt"
EMPTY_NOTE_NAME = "這段範圍沒有照片.txt"

# 相機照片檔名：2026_11_24_19_30_38_140.jpg（年_月_日_時_分_秒_毫秒）
_NAME_TIME = re.compile(r"^(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})")


class ArchiveRangeError(ValueError):
    """日期範圍不合法；code 給前端判斷用。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_range(start: str, end: str) -> list[str]:
    """回傳由舊到新的日期字串清單（YYYY-MM-DD）。格式錯、起訖顛倒、超過 MAX_DAYS 天都丟 ArchiveRangeError。"""
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except (TypeError, ValueError):
        raise ArchiveRangeError("ARCHIVE_BAD_DATE", "日期格式要是 YYYY-MM-DD")
    if e < s:
        raise ArchiveRangeError("ARCHIVE_BAD_DATE", "結束日期不能早於開始日期")
    days = (e - s).days + 1
    if days > MAX_DAYS:
        raise ArchiveRangeError("ARCHIVE_RANGE_TOO_LONG", f"一次最多下載 {MAX_DAYS} 天的照片，請分次下載")
    return [(s + timedelta(days=i)).isoformat() for i in range(days)]


class _Sink:
    """給 zipfile 用的只寫、不可 seek 的輸出端。沒有 seek，zipfile 會改用 data descriptor，
    每寫完一個檔就能把已產生的位元組交出去。"""

    def __init__(self):
        self._chunks: list[bytes] = []
        self._pos = 0

    def write(self, data) -> int:
        b = bytes(data)
        self._chunks.append(b)
        self._pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks = []
        return out


def _safe_name(name: str) -> str:
    """只留檔名本身：後端回來的名稱不可信，不能讓它帶路徑進壓縮檔。"""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1].strip()
    return base if base and base not in (".", "..") else "photo.jpg"


def _zip_info(day: str, name: str) -> zipfile.ZipInfo:
    m = _NAME_TIME.match(name)
    if m:
        y, mo, d, hh, mm, ss = (int(x) for x in m.groups())
    else:
        y, mo, d = (int(x) for x in day.split("-"))
        hh = mm = ss = 0
    try:
        info = zipfile.ZipInfo(f"{day}/{name}", date_time=(y, mo, d, hh, mm, ss))
    except ValueError:
        info = zipfile.ZipInfo(f"{day}/{name}")
    info.compress_type = zipfile.ZIP_STORED
    return info


async def stream_zip(
    days: list[str],
    list_day: Callable[[str], Awaitable[list[dict]]],
    fetch_photo: Callable[[str], Awaitable[Optional[bytes]]],
) -> AsyncIterator[bytes]:
    """逐日列出照片、分批抓回來寫進 zip，每批寫完就把位元組交出去。

    - list_day(day) 回傳 [{"name": …, "path": …}, …]；列不出來就回空清單。
    - fetch_photo(path) 回傳照片內容；抓不到回 None，該張記進壓縮檔裡的「下載失敗清單.txt」，不中斷整包。
    """
    sink = _Sink()
    zf = zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True)
    failed: list[str] = []
    written = 0
    try:
        for day in days:
            files = sorted(await list_day(day), key=lambda f: str(f.get("name", "")))
            seen: set[str] = set()
            for i in range(0, len(files), FETCH_CONCURRENCY):
                batch = [f for f in files[i:i + FETCH_CONCURRENCY] if f.get("path")]
                datas = await asyncio.gather(*[fetch_photo(f["path"]) for f in batch])
                for f, data in zip(batch, datas):
                    name = _safe_name(f.get("name") or f["path"])
                    if name in seen:
                        continue
                    seen.add(name)
                    if not data:
                        failed.append(f"{day}/{name}")
                        continue
                    zf.writestr(_zip_info(day, name), data)
                    written += 1
                chunk = sink.drain()
                if chunk:
                    yield chunk
        if failed:
            zf.writestr(FAILED_LIST_NAME, "以下照片沒有抓到，可以稍後再下載一次：\r\n" + "\r\n".join(failed) + "\r\n")
        elif written == 0:
            zf.writestr(EMPTY_NOTE_NAME, f"{days[0]} 到 {days[-1]} 沒有照片。\r\n")
    finally:
        zf.close()
    yield sink.drain()
