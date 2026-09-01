"""LINE 綁定的舊 OAuth 授權連結流程已停用，改用官方帳號綁定碼流程
（見 routers/auth.py 的 /line/bind-url、/line/callback 與 tests/test_line_webhook_bind_code.py）。
本檔僅保留「端點已停用」的斷言；逐筆解綁/多帳號列表的行為測試已搬到 test_line_accounts.py。"""


def test_line_bind_url_disabled(client, make_user, auth_headers):
    user = make_user("linebindurl", "linebindurl@example.com", password="oldpassword")
    r = client.get("/auth/line/bind-url", headers=auth_headers(user))
    assert r.status_code == 410


def test_line_callback_disabled(client):
    r = client.get("/auth/line/callback?code=fakecode&state=whatever",
                    follow_redirects=False)
    assert r.status_code == 410
