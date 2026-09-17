"""LINE 版寫入型工具的確認流程：/api/assistant 回 confirm action 時提示回覆「確認」，
下一則「確認」才帶 confirm_action_id 執行；「取消」放棄；逾時失效。"""
from routers import line_webhook as lw


def setup_function():
    lw._pending_confirm.clear()


def test_format_result_with_confirm_action_adds_hint_and_remembers_id():
    data = {"message": "請確認", "actions": [{"type": "confirm", "id": "abc123", "summary": "對相機 1 執行自動對焦"}]}
    out = lw._format_assistant_result(data, "U-1")
    assert "對相機 1 執行自動對焦" in out["text"]
    assert "回覆「確認」" in out["text"]
    assert out["confirm_id"] == "abc123"
    assert lw._pending_confirm["U-1"][0] == "abc123"


def test_format_result_without_confirm_has_no_hint():
    data = {"message": "好的。", "actions": [{"type": "navigate", "path": "/jobs"}]}
    out = lw._format_assistant_result(data, "U-2")
    assert out["confirm_id"] is None
    assert "回覆「確認」" not in out["text"]
    assert out["nav_links"] == [{"path": "/jobs", "label": "查看進度"}]
    assert "U-2" not in lw._pending_confirm


def test_confirm_command_returns_pending_id_once():
    lw._remember_confirm("U-3", "id-3")
    assert lw._handle_confirm_command("U-3", "確認") == ("confirm", "id-3")
    assert lw._handle_confirm_command("U-3", "確認") is None


def test_cancel_command_clears_pending():
    lw._remember_confirm("U-4", "id-4")
    assert lw._handle_confirm_command("U-4", "取消") == ("cancel", None)
    assert "U-4" not in lw._pending_confirm


def test_confirm_without_pending_is_not_a_command():
    assert lw._handle_confirm_command("U-5", "確認") is None
    assert lw._handle_confirm_command("U-5", "你好") is None


def test_confirm_expires(monkeypatch):
    lw._remember_confirm("U-6", "id-6")
    remembered_at = lw._pending_confirm["U-6"][1]
    monkeypatch.setattr(lw._time, "time", lambda: remembered_at + lw._CONFIRM_TTL + 1)
    assert lw._handle_confirm_command("U-6", "確認") is None
