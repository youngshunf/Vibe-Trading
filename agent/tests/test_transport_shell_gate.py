"""传输方式 × shell 工具闸的守卫测试 —— 唤星 fork 新增（非上游代码）。

**这是一条安全边界，不是普通回归。** 唤星把本引擎作为 daemon 内建的本地 MCP
服务托管，服务对象是 swarm 内部那个我们不控制的 LLM。若 ``bash`` /
``background_run`` 被暴露给它，等于让 LLM 在主人电脑上执行任意命令——而本模块
没有 computer-use 那套审批 + 黑名单 + 归因闸。

旧上游曾按传输类型决定是否开放 shell：

    _include_shell_tools = True if args.transport == "stdio" else _env_shell_tools_enabled()

上游现已通过 GHSA-6wjh 修复该问题：stdio、sse 和 http 都默认关闭 shell，只有
``--enable-shell-tools`` 或 ``VIBE_TRADING_ENABLE_SHELL_TOOLS=1`` 才显式开启；模块级
默认也改为 fail-closed。这里继续**真调 main()**、真走判定，而不是读代码断言。
唯一被替换的是最后阻塞服务进程的 ``mcp.run()`` / ``uvicorn.run()``，判定逻辑
本身一行没绕过，也不会让单元测试真的占用 8900 端口。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# 关闸后必须消失的进程控制工具。
SHELL_TOOLS = ("bash", "background_run", "cancel_background")


@pytest.fixture
def run_main(monkeypatch: pytest.MonkeyPatch):
    """调 mcp_server.main()，拦住阻塞的 run()，返回判定出的 shell 闸状态。"""
    import mcp_server
    import uvicorn
    from src.config.accessor import reset_env_config

    def _run(*argv: str, shell_env: str | None = None) -> bool:
        if shell_env is None:
            monkeypatch.delenv("VIBE_TRADING_ENABLE_SHELL_TOOLS", raising=False)
        else:
            monkeypatch.setenv("VIBE_TRADING_ENABLE_SHELL_TOOLS", shell_env)
        reset_env_config()  # env config 带缓存，改完 environ 必须重置

        monkeypatch.setattr("sys.argv", ["mcp_server.py", *argv])
        monkeypatch.setattr(mcp_server.mcp, "run", lambda **kwargs: None)
        monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
        # pre-warm 会真建 79 工具的注册表；判定与它无关，跳过省几秒
        monkeypatch.setattr(mcp_server, "_get_registry", lambda: None)

        mcp_server.main()
        return mcp_server._include_shell_tools

    yield _run

    # main() 写的是模块级 global，复原以免污染同进程内的后续测试
    mcp_server._include_shell_tools = False
    mcp_server._registry = None
    reset_env_config()


class TestNetworkTransportsFailClosed:
    """网络传输默认关闸，只有显式环境变量才能开启。"""

    def test_http_without_env_disables_shell_tools(self, run_main) -> None:
        assert run_main("--transport", "http") is False

    def test_http_env_off_disables_shell_tools(self, run_main) -> None:
        assert run_main("--transport", "http", shell_env="0") is False

    def test_http_env_on_enables_shell_tools(self, run_main) -> None:
        """反向：闸必须真受 env 控制。

        若这条变绿不了，说明 http 分支已不读 env（比如被硬编码成 False）——
        那样「关着」只是巧合，不是我们控制的结果，选项 A 的前提就塌了。
        """
        assert run_main("--transport", "http", shell_env="1") is True

    def test_sse_also_honours_env(self, run_main) -> None:
        assert run_main("--transport", "sse") is False


class TestStdioIsFailClosed:
    """stdio 与默认传输同样必须显式授权 shell，不能因本地管道自动放行。"""

    def test_stdio_env_off_disables_shell(self, run_main) -> None:
        assert run_main("--transport", "stdio", shell_env="0") is False

    def test_default_transport_is_also_safe(self, run_main) -> None:
        """不带 ``--transport`` 时也不能绕过 shell 闸。"""
        assert run_main(shell_env="0") is False


class TestRegistryHonoursTheGate:
    """闸位真作用到注册表：关闸后 shell 工具从工具面消失，且只移除它俩。"""

    def test_disabled_removes_exactly_the_shell_tools(self) -> None:
        from src.tools import build_registry

        with_shell = set(build_registry(include_shell_tools=True).tool_names)
        without_shell = set(build_registry(include_shell_tools=False).tool_names)

        assert with_shell - without_shell == set(SHELL_TOOLS)
        assert not without_shell & set(SHELL_TOOLS)

    def test_build_registry_defaults_to_no_shell(self) -> None:
        """默认即安全——调用方忘了传参也不会开后门。"""
        assert not set(build_registry_names()) & set(SHELL_TOOLS)


def build_registry_names() -> list[str]:
    from src.tools import build_registry

    return build_registry().tool_names
