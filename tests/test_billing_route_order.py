"""路由註冊順序防護。

MIGRATION_PLAN §9-C1 的教訓：cameras.py 的 GET /{camera_id}(int) 先註冊，
把 /cameras/projects 整條路由變成死碼並回 422。billing 有同型風險，
用測試把順序釘住，避免日後有人調整 include_router 的位置。
"""
from main import app


def _paths():
    return [r.path for r in app.routes if hasattr(r, "path")]


def test_billing路由已掛載():
    assert "/billing/subscriptions/my" in _paths()
    assert "/billing/admin/plans" in _paths()


def test_billing註冊在cameras之前():
    paths = _paths()
    billing_idx = min(i for i, p in enumerate(paths) if p.startswith("/billing"))
    camera_idx = min(i for i, p in enumerate(paths) if p.startswith("/cameras"))
    assert billing_idx < camera_idx


def test_invoices_my在id路由之前():
    paths = _paths()
    assert paths.index("/billing/invoices/my") < paths.index("/billing/invoices/{invoice_id}")
