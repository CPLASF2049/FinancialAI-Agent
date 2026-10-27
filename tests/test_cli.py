"""命令行入口的冒烟测试。

这里专门覆盖 ``main()`` 本身：之前「不带子命令直接启动」的分支把协程原样返回，
于是用户敲一个 ``finagent`` 就只看到一闪而过的退出——只有真的调用 ``main()``
才能发现这类问题。
"""

from __future__ import annotations

import io
import sys

import pytest

from finagent.cli import main


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "finagent" in capsys.readouterr().out


def test_no_subcommand_opens_the_web_ui(offline_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """不带子命令应当直接打开浏览器界面，而不是终端 REPL。"""

    calls: list[dict] = []

    def fake_serve(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("finagent.web.server.serve", fake_serve)
    code = main([])

    assert code == 0
    assert len(calls) == 1
    assert calls[0]["open_browser"] is True
    assert calls[0]["port"] == 8000


def test_web_command_honours_flags(offline_env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_serve(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr("finagent.web.server.serve", fake_serve)
    assert main(["web", "--no-browser", "--port", "8123"]) == 0

    assert calls[0]["open_browser"] is False
    assert calls[0]["port"] == 8123


def test_chat_command_returns_an_int(offline_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """终端 REPL 仍在，供 SSH / 无浏览器环境使用。"""

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # 立刻 EOF，模拟无输入
    code = main(["chat"])

    assert isinstance(code, int), f"main(['chat']) 返回了 {type(code)!r}"
    assert code == 0


def test_chat_command_exits_on_quit(offline_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("/exit\n"))
    assert main(["chat"]) == 0


def test_graph_command_prints_topology(offline_env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["graph"]) == 0
    out = capsys.readouterr().out
    assert "Supervisor + 专家拓扑" in out
    for agent in ("stock_picker", "analyst", "backtest", "portfolio", "alert"):
        assert agent in out


def test_tools_command_lists_the_mcp_catalogue(
    offline_env, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "41 个 MCP 工具" in out
    assert "add_position" in out
    assert "run_backtest" in out


def test_run_command_json_output(offline_env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "茅台现在多少钱", "--json"]) == 0
    out = capsys.readouterr().out
    assert "贵州茅台" in out
    assert '"agents"' in out


def test_unknown_subcommand_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["definitely-not-a-command"])
    assert excinfo.value.code == 2


def test_doctor_reports_readiness(offline_env, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Fin-Agent 自检" in out
    assert "MCP 正常" in out
