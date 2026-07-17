"""传输方式 × shell 工具闸的守卫测试 —— 唤星 fork 新增（非上游代码）。

**这是一条安全边界，不是普通回归。** 唤星把本引擎作为 daemon 内建的本地 MCP
服务托管，服务对象是 swarm 内部那个我们不控制的 LLM。若 ``bash`` /
``background_run`` 被暴露给它，等于让 LLM 在主人电脑上执行任意命令——而本模块
没有 computer-use 那套审批 + 黑名单 + 归因闸。

上游 ``mcp_server.py:1920`` 的判定：

    _include_shell_tools = True if args.transport == "stdio" else _env_shell_tools_enabled()

读法：**stdio 无条件开 shell，env 闸对它完全无效**；只有 sse/http 才读 env。
唤星据此选定「选项 A」= ``--transport http`` + ``VIBE_TRADING_ENABLE_SHELL_TOOLS``
不开启，好处是**零改上游代码**就能真正关掉 shell（选项 B 要在 fork 里打安全补丁，
每次同步上游都得重打，漏一次就静默开后门）。

模块级默认 ``_include_shell_tools = True``（``mcp_server.py:84``）是 **fail-open**：
只有走完 ``main()`` 才会被收紧。整条安全结论就挂在这根细线上，所以这里**真调
main()**、真走判定，而不是读代码断言。唯一被替换的是最后那句阻塞的
``mcp.run()``——拦住它是为了让测试能返回，判定逻辑本身一行没绕过。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# 关闸后必须消失的工具（P0-闸2 实测差集，见 03 设计文档 §2.3）
SHELL_TOOLS = ("bash", "background_run")


@pytest.fixture
def run_main(monkeypatch: pytest.MonkeyPatch):
    """调 mcp_server.main()，拦住阻塞的 run()，返回判定出的 shell 闸状态。"""
    import mcp_server
    from src.config.accessor import reset_env_config

    def _run(*argv: str, shell_env: str | None = None) -> bool:
        if shell_env is None:
            monkeypatch.delenv("VIBE_TRADING_ENABLE_SHELL_TOOLS", raising=False)
        else:
            monkeypatch.setenv("VIBE_TRADING_ENABLE_SHELL_TOOLS", shell_env)
        reset_env_config()  # env config 带缓存，改完 environ 必须重置

        monkeypatch.setattr("sys.argv", ["mcp_server.py", *argv])
        monkeypatch.setattr(mcp_server.mcp, "run", lambda **kwargs: None)
        # pre-warm 会真建 79 工具的注册表；判定与它无关，跳过省几秒
        monkeypatch.setattr(mcp_server, "_get_registry", lambda: None)

        mcp_server.main()
        return mcp_server._include_shell_tools

    yield _run

    # main() 写的是模块级 global，复原以免污染同进程内的后续测试
    mcp_server._include_shell_tools = True
    mcp_server._registry = None
    reset_env_config()


class TestOptionA:
    """选项 A：http 传输 + env 不开 → shell 真的关掉（零改上游）。"""

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


class TestStdioIsFailOpen:
    """记录并钉死「为什么不能用 stdio」——env 闸对它无效。

    这不是缺陷，是上游的信任假设（stdio = 本地开发者自己跑）。但唤星的托管场景
    里，进程另一端是 LLM 而不是开发者，所以这个假设不成立 → 必须走 http。
    哪天这条变红（stdio 开始认 env 了），说明上游改了模型，可重新评估 stdio 范式。
    """

    def test_stdio_ignores_env_and_enables_shell(self, run_main) -> None:
        assert run_main("--transport", "stdio", shell_env="0") is True

    def test_default_transport_is_stdio_hence_unsafe_for_us(self, run_main) -> None:
        """不带 --transport 就是 stdio → 唤星的 daemon 必须显式传 http。"""
        assert run_main(shell_env="0") is True


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
