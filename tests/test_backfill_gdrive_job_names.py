"""scripts/backfill_gdrive_job_names.py 的命名規則：要與前端 gdriveJobName() 一致。"""
from scripts.backfill_gdrive_job_names import is_count_label, job_name


def test_count_labels_detected():
    for s in ("1 個資料夾", "12 張照片", "2 個資料夾、30 張照片"):
        assert is_count_label(s)


def test_real_names_not_detected():
    for s in ("2024 台北工地", "IMG_1.JPG 等 2 張照片", "", None, "A、B"):
        assert not is_count_label(s)


def test_job_name_matches_frontend_rules():
    assert job_name(["2024 台北工地"], []) == "2024 台北工地"
    assert job_name(["A", "B"], []) == "A、B"
    assert job_name(["A", "B", "C"], []) == "A、B 等 3 個資料夾"
    assert job_name([], ["IMG_1.JPG"]) == "IMG_1.JPG"
    assert job_name([], ["IMG_1.JPG", "IMG_2.JPG"]) == "IMG_1.JPG 等 2 張照片"
    assert job_name(["A"], ["x.jpg", "y.jpg"]) == "A ＋ 2 張照片"
    assert job_name(["很" * 40], []) == "很" * 29 + "…"
    assert job_name([], []) == ""
