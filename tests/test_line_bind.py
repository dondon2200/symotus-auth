"""LINE 綁定的舊 OAuth 授權連結流程（/line/bind-url）已於官方帳號綁定碼流程時代停用；
現已被 Task「LINE 一鍵綁定」的 /me/line/bind-session + /line/bind-start + /line/callback
取代，callback 不再是 410 stub。本檔僅保留「舊端點已移除」的斷言；callback 的完整行為測試
已搬到 test_line_bind_session.py；逐筆解綁/多帳號列表的行為測試在 test_line_accounts.py。"""


def test_line_bind_url_removed(client, make_user, auth_headers):
    """舊 /line/bind-url（曾是 410 stub）已隨一鍵綁定上線整支移除，改用 POST /me/line/bind-session。"""
    user = make_user("linebindurl", "linebindurl@example.com", password="oldpassword")
    r = client.get("/auth/line/bind-url", headers=auth_headers(user))
    assert r.status_code == 404
