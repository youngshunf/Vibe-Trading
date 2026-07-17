"""影子账户**工具层**测试 —— 唤星 fork 新增（对应验收 A9 · 流程 C）。

与 ``test_shadow_account.py`` 的分工：那份测**库函数**（``extract_shadow_profile``
等）；本份测**工具**（``extract_shadow_strategy``）——即分身真正能调到的那一层。

**为什么必须单独测工具层**：分身看不见库函数，它只看得见工具。工具层额外压着三样
库函数没有的东西，每一样单独坏掉都足以让流程 C 对分身不可用，而库函数测试全绿：

1. **自动发现**——工具得真的出现在 ``build_registry()`` 里，分身才调得到；
2. **路径信封**——``safe_user_path`` 只认显式 import 根（``VIBE_TRADING_ALLOWED_FILE_ROOTS``），
   不是整个 home。信封配错 = 每次调用都被拒；
3. **错误诚实度**——包装器把库的异常转成 JSON 信封。这里最容易「好心」把
   「盈利闭环不足 5 笔」压成一句无信息的 "unexpected error"，让主人无从知道
   到底该补什么。A9 明确要求这条报错诚实，故本文件重点钉它。

零 mock：真注册表、真工具实例、真 CSV、真落盘。唯一被替换的是**价格源**——
强制离线（复刻 ``test_shadow_account.py`` 的同名 fixture），否则单测会真联网。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.shadow_account.extractor import MIN_PROFITABLE_ROUNDTRIPS
from src.tools import build_registry

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _offline_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制离线价格源——单测绝不联网。

    与 ``test_shadow_account.py`` 的同名 fixture 同理：``_compute_features`` 会去
    loader registry 取价格上下文，不拦住的话在联网环境里会真打 stooq/yahoo
    （它们免鉴权）。让 ``resolve_loader`` 直接抛错 = 复刻生产的离线降级路径。
    """
    from backtest.loaders.base import NoAvailableSourceError

    def _no_source(market: str):
        raise NoAvailableSourceError(f"offline test: no source for {market}")

    monkeypatch.setattr("backtest.loaders.registry.resolve_loader", _no_source)


def _write_journal(path: Path, trades: list[tuple[str, str, str, float, float]]) -> Path:
    """按同花顺导出格式写一份 CSV（``parse_tonghuashun`` 认的列名）。"""
    rows = []
    for dt_str, symbol, side, qty, price in trades:
        amount = qty * price
        rows.append({
            "成交时间": dt_str,
            "证券代码": symbol,
            "证券名称": f"标的{symbol}",
            "操作": "买入" if side == "buy" else "卖出",
            "成交数量": qty,
            "成交价格": price,
            "成交金额": round(amount, 2),
            "手续费": round(amount * 0.00025, 2),
            "印花税": round(amount * 0.001, 2) if side == "sell" else 0.0,
            "过户费": 0.0,
        })
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 HOME 与 import 根都圈进 tmp_path —— 落盘与读取都不碰真实用户目录。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_FILE_ROOTS", str(tmp_path))
    return tmp_path


@pytest.fixture
def profitable_journal(sandbox: Path) -> Path:
    """15 笔盈利闭环（5 标的 × 3 轮，每轮 +2%）——远超 5 笔下限。"""
    trades: list[tuple[str, str, str, float, float]] = []
    for sym in ("600519", "000001", "300750", "600036", "000858"):
        for i in range(3):
            buy_day = 1 + i * 4
            sell_day = buy_day + 2
            trades.append((f"2026-01-{buy_day:02d} 10:30:00", sym, "buy", 100.0, 10.0))
            trades.append((f"2026-01-{sell_day:02d} 14:15:00", sym, "sell", 100.0, 10.2))
    return _write_journal(sandbox / "journal_profitable.csv", trades)


@pytest.fixture
def thin_journal(sandbox: Path) -> Path:
    """只有 2 笔盈利闭环——低于 MIN_PROFITABLE_ROUNDTRIPS，必须诚实报错。"""
    trades: list[tuple[str, str, str, float, float]] = []
    for i, sym in enumerate(("600519", "000001")):
        buy_day = 1 + i * 4
        trades.append((f"2026-01-{buy_day:02d} 10:30:00", sym, "buy", 100.0, 10.0))
        trades.append((f"2026-01-{buy_day + 2:02d} 14:15:00", sym, "sell", 100.0, 10.2))
    return _write_journal(sandbox / "journal_thin.csv", trades)


def _extract_tool():
    """从**真实注册表**取工具——顺带就证明了「自动发现」这条。"""
    return build_registry().get("extract_shadow_strategy")


class TestAutoDiscovery:
    """A9①：分身只调得到注册表里的工具，进不了注册表 = 对分身不存在。"""

    def test_tool_is_discovered_by_registry(self) -> None:
        assert _extract_tool() is not None, "extract_shadow_strategy 未被 build_registry 自动发现"

    def test_shell_tools_stay_off_by_default(self) -> None:
        """安全选项 A 的默认：不显式开启就不该暴露 shell 类工具。"""
        default_names = set(build_registry().tool_names)

        assert "bash" not in default_names
        assert "background_run" not in default_names

    def test_declares_journal_path_as_required(self) -> None:
        """入参契约：收的是**路径**，不是字节块（遵守禁 base64 铁律）。"""
        params = _extract_tool().parameters

        assert params["required"] == ["journal_path"]
        assert params["properties"]["journal_path"]["type"] == "string"


class TestExecutionAndPersistence:
    """A9②③：执行 + 持久化。"""

    def test_execute_returns_ok_envelope_with_rules(self, profitable_journal: Path) -> None:
        result = json.loads(_extract_tool().execute(journal_path=str(profitable_journal)))

        assert result["status"] == "ok", result
        assert result["shadow_id"]
        assert result["profitable_roundtrips"] >= MIN_PROFITABLE_ROUNDTRIPS
        assert result["rules"], "盈利闭环够却没提炼出任何规则"
        # 规则得是人能读的——影子账户的整个卖点就是「把隐性习惯说出来」
        assert all(r["human_text"] for r in result["rules"])

    def test_execute_persists_profile_to_disk(self, profitable_journal: Path, sandbox: Path) -> None:
        result = json.loads(_extract_tool().execute(journal_path=str(profitable_journal)))

        saved = list((sandbox / ".vibe-trading" / "shadow_accounts").glob("*.json"))
        assert saved, "profile 没落盘——工具说成功了但下次读不到"
        assert any(result["shadow_id"] in p.read_text(encoding="utf-8") for p in saved)

    def test_persisted_profile_is_reloadable(self, profitable_journal: Path) -> None:
        """落盘不是终点——存了必须还能读回来，否则等于没存。"""
        from src.shadow_account import load_profile

        result = json.loads(_extract_tool().execute(journal_path=str(profitable_journal)))
        loaded = load_profile(result["shadow_id"])

        assert loaded.shadow_id == result["shadow_id"]
        assert len(loaded.rules) == len(result["rules"])


class TestHonestErrors:
    """A9④：报错必须诚实——说清「差什么」，不许糊成 unexpected error。"""

    def test_insufficient_roundtrips_says_what_is_missing(self, thin_journal: Path) -> None:
        result = json.loads(_extract_tool().execute(journal_path=str(thin_journal)))

        assert result["status"] == "error"
        # 原文透传：包装器的 `except (FileNotFoundError, ValueError)` 分支必须把
        # 库的诊断原样带出，而不是落进 `unexpected error: ...` 的兜底分支。
        assert "Insufficient profitable roundtrips" in result["error"], result
        assert "unexpected error" not in result["error"]

    def test_missing_file_is_reported_not_raised(self, sandbox: Path) -> None:
        """文件不存在是**主人**的输入问题，要如实说，不该把工具调用炸掉。"""
        result = json.loads(_extract_tool().execute(journal_path=str(sandbox / "nope.csv")))

        assert result["status"] == "error"
        assert result["error"]

    def test_missing_journal_path_is_rejected(self) -> None:
        result = json.loads(_extract_tool().execute())

        assert result["status"] == "error"
        assert "journal_path" in result["error"]

    def test_path_outside_import_roots_is_rejected(self, sandbox: Path, tmp_path_factory) -> None:
        """路径信封守卫：import 根之外的文件必须拒，且拒得有话说。

        这条同时是**安全**回归钉子——``safe_user_path`` 的存在意义就是不让分身
        用一个 journal_path 把任意用户文件读进上下文。
        """
        outside = tmp_path_factory.mktemp("outside") / "elsewhere.csv"
        _write_journal(outside, [("2026-01-01 10:30:00", "600519", "buy", 100.0, 10.0)])

        result = json.loads(_extract_tool().execute(journal_path=str(outside)))

        assert result["status"] == "error"
        assert result["error"]
