# -*- coding: utf-8 -*-
"""凭据读取链路的回归：DPAPI 密文优先 / 明文回退 / WARNING 限频（2026-11-01，P1-5）。

## 为什么要有这个文件

`viewer/secrets_dpapi.load()` 是三个抓取入口（雪球渲染抓取、集思录 ETF、东财跟踪误差）
现在共用的凭据唯一入口。它一旦"太聪明"（比如解不开就返回 None），三条采集链会同时静默停摆；
一旦"太啰嗦"（每读一次打一条 WARNING），迁移提醒会被日志噪音淹掉、没人再当回事。
这两件事都不会报错、都很难从现象反推原因 —— 所以必须锁死：

1. **密文优先**：`.dpapi` 在、能解开 → 用密文，明文就算躺着也不用；
2. **明文回退**：密文坏/缺 → 回退明文（且只在同一进程同一天打**一条** WARNING）；
3. **限频**：连读 3 次只 1 条 WARNING；`reset_warn_throttle()` 之后才会再打；
4. **都没有 → None**（调用方自己决定"跳过本轮"，不是抛异常）；
5. **三个读取点的源码里必须同时存在"密文入口"与"明文回退"** —— 这条是防日后有人
   "顺手清理"掉回退分支：一旦删掉，`.dpapi` 还没生成的那段时间采集就整条断掉。

全部在 tmp 目录里跑，**不读也不碰真实凭据**（`plain_path`/`cipher_path` 依赖模块级
`ROOT`，这里 monkeypatch 到 tmp_path）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer import secrets_dpapi as M  # noqa: E402

NAME = "jisilu_cookie.txt"
PLAIN = ".jisilu.cn\tTRUE\t/\tTRUE\t1999999999\tsession\tPLAINVALUE\n"


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    """把凭据根目录指到 tmp，并清空限频状态（否则用例之间会互相吞掉 WARNING）。"""
    monkeypatch.setattr(M, "ROOT", tmp_path)
    M.reset_warn_throttle()
    yield tmp_path
    M.reset_warn_throttle()


def _warnings(caplog) -> list:
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and r.name == M.__name__]


# ------------------------------------------------------- 1. 密文优先（明文躺着也不用）
def test_ciphertext_wins_over_plaintext(sandbox, monkeypatch):
    (sandbox / NAME).write_text(PLAIN, encoding="utf-8")
    (sandbox / (NAME + M._SUFFIX)).write_bytes(b"fake-cipher")
    # 用桩替掉真 DPAPI：本用例验的是"选哪个来源"，不是加密本身（加密另有端到端自测）
    monkeypatch.setattr(M, "unprotect_bytes", lambda b: b"FROM_CIPHER")

    assert M.load(NAME) == "FROM_CIPHER", "密文存在且可解时，绝不能去读明文"


# ------------------------------------------------------- 2. 密文坏 → 回退明文 + WARNING
def test_corrupt_cipher_falls_back_to_plaintext(sandbox, monkeypatch, caplog):
    (sandbox / NAME).write_text(PLAIN, encoding="utf-8")
    (sandbox / (NAME + M._SUFFIX)).write_bytes(b"corrupt")

    def boom(_b):
        raise M.SecretsUnavailable("模拟解密失败")

    monkeypatch.setattr(M, "unprotect_bytes", boom)

    with caplog.at_level(logging.WARNING, logger=M.__name__):
        assert M.load(NAME) == PLAIN, "密文坏了必须能回退明文，否则采集当场停摆"
    msgs = [r.getMessage() for r in _warnings(caplog)]
    # 这条路径上应当是**两条**、且原因不同 —— 它们是两件不同的待办，不能合并成一句：
    #   ①「密文解不开」= 加密文件坏了/换了机器，需要重新 --protect；
    #   ②「明文还在盘上」= 迁移没做完。
    # 合成一条会丢掉其中一个信息，排查时就得靠猜。两者都受 `_warn_once` 限频。
    assert len(msgs) == 2, "回退路径应留 2 条 WARNING（解密失败 + 明文仍在），实际 %r" % (msgs,)
    assert any("解密失败" in m for m in msgs), msgs
    assert any("仍是**明文**" in m for m in msgs), msgs


# ------------------------------------------------------- 3. 限频：同进程同一天只一条
def test_plaintext_warning_is_throttled_to_one_per_day(sandbox, caplog):
    (sandbox / NAME).write_text(PLAIN, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=M.__name__):
        for _ in range(3):
            assert M.load(NAME) == PLAIN
        after_three = len(_warnings(caplog))
        M.reset_warn_throttle()          # 模拟"第二天"（或显式盘点）
        assert M.load(NAME) == PLAIN
        after_reset = len(_warnings(caplog))

    assert after_three == 1, "连读 3 次只应留 1 条 WARNING，实际 %d 条" % after_three
    assert after_reset == 2, "清空限频后应重新提醒一次，实际累计 %d 条" % after_reset


# ------------------------------------------------------- 4. 什么都没有 → None（不抛）
def test_missing_secret_returns_none(sandbox):
    assert M.load("cookies.txt") is None
    assert M.load(NAME) is None


# ------------------------------------------------------- 5. 读取点源码必须保留双档
@pytest.mark.parametrize("rel, name", [
    ("scraper/browser_fetch.py", "cookies.txt"),
    ("etf_jisilu.py", "jisilu_cookie.txt"),
    ("etf_track_error.py", "eastmoney_cookie.txt"),
])
def test_read_point_keeps_cipher_and_plaintext_fallback(rel, name):
    """每个读取点：入口调 `_secrets.load(<名>)`，且明文回退分支还在。

    这条是"防回归的防回归"：改造的目标是**加密**，不是**删功能**。
    若有人日后为了"去掉那条烦人的 WARNING"把回退分支删掉，
    `.dpapi` 尚未生成/解不开的那段时间，采集会直接断 —— 而这条测试会先红。
    """
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "_secrets.load(" in src, "%s 没有走 secrets_dpapi.load 入口" % rel
    assert name in src, "%s 里找不到受管凭据名 %s" % (rel, name)
    # 明文回退必须还在（两个读取点用 COOKIE_FILE 常量，雪球用候选路径列表）
    assert ("COOKIE_FILE" in src) or ("cookies.json" in src), (
        "%s 的明文回退路径被删了 —— 加密改造不能顺手把可用性改坏" % rel)
    assert "_secrets = None" in src, (
        "%s 缺少 import 失败兜底（_secrets = None）—— sys.path 一变就会 ImportError" % rel)
