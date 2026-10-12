# -*- coding: utf-8 -*-
"""统一出网 TLS 上下文（2026-10-12 整改 #27）。

背景：本仓多个采集模块过去直接关掉 TLS 校验（`create_default_context()` 后
`check_hostname = False` + `verify_mode = ssl.CERT_NONE`），原因有二：
  · 国内部分金融数据源（东财 / 新浪等）证书链异常，严格校验会直接连不上；
  · 本机 / 企业代理会做 TLS 中间人，证书非公信 CA 签发。
代价：关校验＝对旁路篡改不设防（中间人可改数据，属供应链风险）。

本模块把口径**收拢成可配置**，且**默认与整改前完全一致（关校验）**，
以确保数据源不断；需要加固时按需开启（环境变量）：

    STOCK_TLS_VERIFY=1                    全局开启严格校验（对所有主机）
    STOCK_TLS_VERIFY_HOSTS=a.com,b.com     仅对列出的主机开启校验（白名单，逗号/分号分隔）
    STOCK_TLS_CA=D:\\path\\ca.pem          追加信任的 CA 包（自签 / 企业代理场景下仍可校验）

用法：
    from netctx import https_context
    ctx = https_context()                      # 与旧行为一致（按环境变量决定）
    ctx = https_context("fund.eastmoney.com")  # 传主机，白名单模式才能按主机命中
"""
from __future__ import annotations

import os
import ssl

_TRUE = {"1", "true", "yes", "on"}


def _parse_hosts(raw: str) -> set:
    return {h.strip().lower() for h in (raw or "").replace(";", ",").split(",") if h.strip()}


def tls_mode() -> str:
    """当前口径：'strict' | 'whitelist' | 'off'（供日志 / 自检展示）。"""
    if os.environ.get("STOCK_TLS_VERIFY", "").strip().lower() in _TRUE:
        return "strict"
    if _parse_hosts(os.environ.get("STOCK_TLS_VERIFY_HOSTS")):
        return "whitelist"
    return "off"


def _verify_enabled(host) -> bool:
    if os.environ.get("STOCK_TLS_VERIFY", "").strip().lower() in _TRUE:
        return True
    wl = _parse_hosts(os.environ.get("STOCK_TLS_VERIFY_HOSTS"))
    return bool(wl) and bool(host) and str(host).strip().lower() in wl


def https_context(host=None, *, cafile=None) -> ssl.SSLContext:
    """构造 https 上下文。

    默认**不校验**（保持历史行为，避免国内源 / 代理场景连不上）；
    按环境变量可开启严格校验或白名单校验。传入 host 时白名单按主机命中。

    cafile 显式给出，或读 STOCK_TLS_CA；加载后即便处于关校验模式也无害。
    """
    ctx = ssl.create_default_context()
    ca = cafile or os.environ.get("STOCK_TLS_CA") or None
    if ca:
        try:
            ctx.load_verify_locations(cafile=ca)
        except Exception:  # noqa: BLE001 —— CA 包坏了不该阻断采集，降级为不校验
            pass
    if not _verify_enabled(host):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx
