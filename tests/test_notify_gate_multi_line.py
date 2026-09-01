"""FIX 1 回歸測試：好友判定不能只看 line_accounts[0]。

遷移期常見情境：使用者舊 OAuth 流程綁的 LINE-A 從沒加官方帳號好友，
後來用新的綁定碼流程加好友、綁定 LINE-B。只要「任一」已綁定 LINE 帳號
是好友，開機通知的好友門檻就該放行——不能因為無序集合裡剛好排到
LINE-A 就一直被擋。
"""
import asyncio

import routers.cameras as cameras_mod
from routers.cameras import _is_following_oa_any


class _Acc:
    def __init__(self, line_user_id):
        self.line_user_id = line_user_id


def test_passes_when_any_account_is_following(monkeypatch):
    calls = []

    async def fake_is_following(line_id):
        calls.append(line_id)
        return line_id == "LINE-B"  # 只有 LINE-B 是好友

    monkeypatch.setattr(cameras_mod, "_is_following_oa", fake_is_following)

    accounts = [_Acc("LINE-A"), _Acc("LINE-B")]
    result = asyncio.run(_is_following_oa_any(accounts))

    assert result is True


def test_fails_when_no_account_is_following(monkeypatch):
    async def fake_is_following(line_id):
        return False

    monkeypatch.setattr(cameras_mod, "_is_following_oa", fake_is_following)

    accounts = [_Acc("LINE-A"), _Acc("LINE-C")]
    result = asyncio.run(_is_following_oa_any(accounts))

    assert result is False


def test_short_circuits_on_first_match(monkeypatch):
    """第一支就是好友時，不該再對其餘帳號打 LINE API。"""
    calls = []

    async def fake_is_following(line_id):
        calls.append(line_id)
        return line_id == "LINE-A"

    monkeypatch.setattr(cameras_mod, "_is_following_oa", fake_is_following)

    accounts = [_Acc("LINE-A"), _Acc("LINE-B"), _Acc("LINE-C")]
    result = asyncio.run(_is_following_oa_any(accounts))

    assert result is True
    assert calls == ["LINE-A"]
