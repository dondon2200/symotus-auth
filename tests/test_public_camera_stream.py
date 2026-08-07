"""go2rtc stream 名稱解析（公開分享連結用）。

回歸來源：公網 IP 相機（110.25.96.15）被硬推成 cam96，go2rtc 無此串流 → 分享連結永遠播不出畫面。
"""
from routers.public_camera import _pick_stream_name

# 取自線上 go2rtc /api/config：公網 IP 相機是流水號、LAN 相機才等於第三段
PAIRS = [
    ("cam1", "110.25.96.4"),
    ("cam2", "110.25.96.15"),
    ("cam103", "192.168.103.1"),
    ("cam104", "192.168.104.1"),
]


def test_public_ip_camera_resolves_by_exact_host():
    assert _pick_stream_name("110.25.96.15", PAIRS) == "cam2"
    assert _pick_stream_name("110.25.96.4", PAIRS) == "cam1"


def test_lan_camera_resolves_by_exact_host():
    assert _pick_stream_name("192.168.103.1", PAIRS) == "cam103"


def test_lan_device_resolves_to_unique_subnet_match():
    """裝置 .100 對到同 /24 的 NVR .1"""
    assert _pick_stream_name("192.168.104.100", PAIRS) == "cam104"


def test_unknown_ip_falls_back_to_third_octet():
    assert _pick_stream_name("192.168.199.50", PAIRS) == "cam199"


def test_empty_config_falls_back_to_third_octet():
    assert _pick_stream_name("192.168.103.1", []) == "cam103"


def test_blank_and_malformed_ip_return_empty():
    assert _pick_stream_name("", PAIRS) == ""
    assert _pick_stream_name("10.1", PAIRS) == ""
