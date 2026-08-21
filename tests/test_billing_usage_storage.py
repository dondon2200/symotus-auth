"""NAS 儲存用量。用 tmp_path 建假目錄樹，不碰真實 NAS。"""
from services.billing_usage import dir_size_bytes, collect_storage_gb


def _write(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_加總目錄下所有檔案大小(tmp_path):
    _write(tmp_path / "2026-08-19" / "a.jpg", 1000)
    _write(tmp_path / "2026-08-19" / "b.jpg", 2000)
    _write(tmp_path / "2026-08-20" / "c.jpg", 500)
    assert dir_size_bytes(str(tmp_path)) == 3500


def test_目錄不存在回零(tmp_path):
    assert dir_size_bytes(str(tmp_path / "不存在")) == 0


def test_空目錄回零(tmp_path):
    (tmp_path / "empty").mkdir()
    assert dir_size_bytes(str(tmp_path / "empty")) == 0


def test_換算成GB(tmp_path):
    serial = "SN12345"
    _write(tmp_path / serial / "2026-08-19" / "a.jpg", 1024 ** 3)
    assert collect_storage_gb(serial, base=str(tmp_path)) == 1.0


def test_相機沒有NAS目錄時回零而非拋錯(tmp_path):
    # 新相機、或還沒上傳過任何照片
    assert collect_storage_gb("SN_NOT_EXIST", base=str(tmp_path)) == 0.0


def test_serial為空時回零(tmp_path):
    assert collect_storage_gb("", base=str(tmp_path)) == 0.0
