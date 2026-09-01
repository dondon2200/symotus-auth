"""_resolve_user active 優先/自動遞補；「切換帳號」指令。"""
from models import User, UserLineAccount
from routers.line_webhook import _resolve_user, _handle_switch_command


def _bind(db, name, line_uid, active=False):
    u = User(username=name, email=f"{name}@x.com", hashed_password="h", role="end_user")
    db.add(u); db.flush()
    db.add(UserLineAccount(user_id=u.id, line_user_id=line_uid, is_active=active))
    db.commit()
    return u


def test_resolve_prefers_active(db):
    a = _bind(db, "sw-a", "U-sw", active=False)
    b = _bind(db, "sw-b", "U-sw", active=True)
    assert _resolve_user(db, "U-sw").id == b.id


def test_resolve_promotes_first_when_no_active(db):
    a = _bind(db, "pr-a", "U-pr", active=False)
    b = _bind(db, "pr-b", "U-pr", active=False)
    assert _resolve_user(db, "U-pr").id == a.id  # id 序第一列
    rows = db.query(UserLineAccount).filter_by(line_user_id="U-pr").all()
    assert [r.is_active for r in sorted(rows, key=lambda r: r.id)] == [True, False]


def test_resolve_unbound_still_none(db):
    assert _resolve_user(db, "U-none") is None


def test_switch_non_command_returns_none(db):
    _bind(db, "nc-a", "U-nc", active=True)
    assert _handle_switch_command(db, "U-nc", "你好") is None
    assert _handle_switch_command(db, "U-nc", "123456") is None


def test_switch_lists_accounts(db):
    a = _bind(db, "ls-a", "U-ls", active=True)
    b = _bind(db, "ls-b", "U-ls", active=False)
    reply = _handle_switch_command(db, "U-ls", "切換帳號")
    assert "1. ls-a" in reply and "2. ls-b" in reply
    assert "作用中" in reply and "切換帳號 " in reply


def test_switch_by_number(db):
    a = _bind(db, "n-a", "U-n", active=True)
    b = _bind(db, "n-b", "U-n", active=False)
    reply = _handle_switch_command(db, "U-n", "切換帳號 2")
    assert "n-b" in reply and "已切換" in reply
    assert _resolve_user(db, "U-n").id == b.id


def test_switch_out_of_range(db):
    _bind(db, "r-a", "U-r", active=True)
    reply = _handle_switch_command(db, "U-r", "切換帳號 5")
    assert "超出範圍" in reply


def test_switch_clears_chat_history(db):
    """FIX 4：成功切換帳號後，AI 對話歷史要清空——否則模型仍握著舊帳號
    的相機名稱/id，即使工具呼叫會用新 token 正確 403，助理仍會誤答舊帳號的資料。"""
    from routers.line_webhook import _get_history, _save_history

    _bind(db, "hist-a", "U-hist", active=True)
    _bind(db, "hist-b", "U-hist", active=False)

    _save_history("U-hist", [{"role": "user", "content": "帳號 A 的相機"}])
    assert _get_history("U-hist")  # 切換前確實有歷史

    _handle_switch_command(db, "U-hist", "切換帳號 2")

    assert _get_history("U-hist") == []


def test_switch_listing_does_not_clear_history(db):
    """只是列清單（沒帶編號），並未真的切換帳號，歷史不該被清掉。"""
    from routers.line_webhook import _get_history, _save_history

    _bind(db, "list-a", "U-list", active=True)
    _bind(db, "list-b", "U-list", active=False)

    _save_history("U-list", [{"role": "user", "content": "hi"}])
    _handle_switch_command(db, "U-list", "切換帳號")

    assert _get_history("U-list") != []
