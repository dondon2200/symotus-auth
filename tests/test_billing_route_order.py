"""billing router 內部路由順序防護。

MIGRATION_PLAN §9-C1 的教訓：cameras.py 的 GET /{camera_id}(int) 先註冊，
把 /cameras/projects 整條路由變成死碼並回 422。

billing 與 cameras 的路徑前綴（/billing、/cameras）互不相交，FastAPI/Starlette
以完整路徑比對，兩者不可能互相攔截，因此 include_router 的先後順序對它們之間
沒有影響。真正同型的風險在 billing router **內部**：/billing/invoices/my
必須先於 /billing/invoices/{invoice_id} 註冊，否則 "my" 會被當成
invoice_id 吃掉，重演 §9-C1 的事故。本檔用測試把這個順序釘住。
"""
from main import app


def _paths():
    return [r.path for r in app.routes if hasattr(r, "path")]


def test_billing路由已掛載():
    assert "/billing/subscriptions/my" in _paths()
    assert "/billing/admin/plans" in _paths()


def test_invoices_my在id路由之前():
    paths = _paths()
    assert paths.index("/billing/invoices/my") < paths.index("/billing/invoices/{invoice_id}")
