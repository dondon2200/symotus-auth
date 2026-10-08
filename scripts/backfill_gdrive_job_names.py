"""補回 Google Drive 任務的標題（folder_name）。

2026-10-08 前，前端建立任務時送的 selection_name 是計數文字（「1 個資料夾」、「3 張照片」），
任務清單上多筆同名、辨識不出是哪批照片。前端已改送實際名稱（frontend 1d2f09c）；
這支腳本把舊任務補成同樣的格式：用任務存的 folder_ids 向 Google Drive 查資料夾名稱，
照片則直接用 job_params 裡已存的檔名。

命名規則與前端 src/lib/gdrive.ts 的 gdriveJobName() 一致。

用法（在 auth-service 容器內）：
    python scripts/backfill_gdrive_job_names.py            # dry-run，只列出將改成什麼
    python scripts/backfill_gdrive_job_names.py --apply    # 實際寫回 DB
"""
import argparse
import asyncio
import json
import pathlib
import re
import sys
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx

from database import SessionLocal
from models import GDriveJob, GoogleDriveCredential
from routers.jobs import GDRIVE_FILES_URL, _refresh_access_token

# 舊版前端 selectionLabel 的格式：「N 個資料夾」、「N 張照片」或兩者以「、」相接
COUNT_LABEL = re.compile(r"^(\d+ 個資料夾)?(、)?(\d+ 張照片)?$")
NAME_MAX = 30


def is_count_label(name: Optional[str]) -> bool:
    return bool(name) and bool(COUNT_LABEL.fullmatch(name.strip()))


def _clip(name: str) -> str:
    s = name.strip()
    return f"{s[:NAME_MAX - 1]}…" if len(s) > NAME_MAX else s


def job_name(folder_names: list[str], photo_names: list[str]) -> str:
    """與前端 gdriveJobName() 相同的規則。"""
    folders = [n for n in (_clip(f) for f in folder_names) if n]
    folder_part = ""
    if len(folders) == 1:
        folder_part = folders[0]
    elif len(folders) == 2:
        folder_part = "、".join(folders)
    elif len(folders) > 2:
        folder_part = f"{'、'.join(folders[:2])} 等 {len(folders)} 個資料夾"

    if not photo_names:
        return folder_part
    if folder_part:
        return f"{folder_part} ＋ {len(photo_names)} 張照片"
    first = _clip(photo_names[0])
    return first if len(photo_names) == 1 else f"{first} 等 {len(photo_names)} 張照片"


async def _folder_name(client: httpx.AsyncClient, access_token: str, folder_id: str) -> Optional[str]:
    r = await client.get(f"{GDRIVE_FILES_URL}/{folder_id}",
                         params={"fields": "name", "supportsAllDrives": "true"},
                         headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code != 200:
        return None
    return r.json().get("name")


async def main(apply: bool) -> int:
    db = SessionLocal()
    tokens: dict[str, str] = {}  # refresh token → access token，同一使用者只換發一次
    changed = skipped = 0
    try:
        jobs = [j for j in db.query(GDriveJob).order_by(GDriveJob.id).all() if is_count_label(j.folder_name)]
        print(f"標題是計數文字的任務：{len(jobs)} 筆")
        async with httpx.AsyncClient(timeout=30) as client:
            for job in jobs:
                params = json.loads(job.job_params) if job.job_params else {}
                folder_ids = params.get("folder_ids") or ([job.folder_id] if job.folder_id else [])
                photo_names = [f.get("name") or "" for f in (params.get("picked_files") or [])]

                folder_names: list[str] = []
                if folder_ids:
                    cred = db.query(GoogleDriveCredential).filter_by(user_id=job.user_id).first()
                    refresh = job.google_refresh_token or (cred.refresh_token if cred else None)
                    if not refresh:
                        print(f"  #{job.id} 跳過：沒有可用的 refresh token")
                        skipped += 1
                        continue
                    try:
                        if refresh not in tokens:
                            tokens[refresh] = (await _refresh_access_token(refresh))["access_token"]
                    except Exception as e:  # noqa: BLE001 — 單筆失敗不影響其他筆
                        print(f"  #{job.id} 跳過：換發 access token 失敗（{e}）")
                        skipped += 1
                        continue
                    for fid in folder_ids:
                        name = await _folder_name(client, tokens[refresh], fid)
                        if name:
                            folder_names.append(name)
                    if len(folder_names) != len(folder_ids):
                        print(f"  #{job.id} 跳過：{len(folder_ids)} 個資料夾只查到 {len(folder_names)} 個名稱"
                              "（可能已刪除或已撤銷授權）")
                        skipped += 1
                        continue

                new_name = job_name(folder_names, photo_names)
                if not new_name:
                    print(f"  #{job.id} 跳過：組不出名稱")
                    skipped += 1
                    continue
                print(f"  #{job.id} 「{job.folder_name}」→「{new_name}」")
                if apply:
                    job.folder_name = new_name
                changed += 1
        if apply:
            db.commit()
    finally:
        db.close()
    print(f"{'已更新' if apply else '將更新（dry-run，未寫入）'} {changed} 筆，跳過 {skipped} 筆")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="實際寫回 DB（預設只列出）")
    sys.exit(asyncio.run(main(ap.parse_args().apply)))
