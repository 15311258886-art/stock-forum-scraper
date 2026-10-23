"""P2-03：请求级数据源错误采集（与 viewer.server 解耦，便于单元测试且不触发 server 导入）。

设计：
  · 每个 HTTP 请求一个线程（ThreadingHTTPServer），用 threading.local() 隔离请求级错误列表，
    避免跨请求串扰。
  · 各静默 except 分支在返回空集合的同时，调用 record_source_error(source, error) 把
    「哪个数据源、什么错」记下来；do_GET/do_POST 在每个请求入口调用 reset_source_errors() 清空。
  · _send_json 在写出 JSON 前调用 attach_source_errors(obj)，仅当存在错误时注入
    "source_errors": [...] 键，前端据此区分「空态」与「错误态」。

注意：本文件名不带 `_` 前缀——仓库 .gitignore 将 `_*` 视为一次性探针脚本（不入库），
而本模块是 server 运行期活代码，必须正式命名以入库。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List


# 请求级错误列表（threading.local：每线程独立，天然隔离并发请求）。
_REQ_ERRORS = threading.local()


def record_source_error(source: str, error: str) -> None:
    """记录一个数据源查询失败。source=数据源名（如 'stock_names'），error=异常信息。"""
    errs = getattr(_REQ_ERRORS, "value", None)
    if errs is None:
        errs = []
        _REQ_ERRORS.value = errs
    errs.append({"source": str(source), "error": str(error)})


def get_source_errors() -> List[Dict[str, str]]:
    """返回当前请求已记录的数据源错误列表（副本）。"""
    errs = getattr(_REQ_ERRORS, "value", None)
    return list(errs) if errs else []


def reset_source_errors() -> None:
    """每个请求入口清空（do_GET / do_POST 调用）。"""
    _REQ_ERRORS.value = []


def attach_source_errors(obj: Any) -> Any:
    """若当前请求存在数据源错误，且 obj 是 dict，则注入 'source_errors' 键；否则原样返回。

    不改动调用方的原对象（返回拷贝），避免污染缓存/上层引用。
    """
    errs = getattr(_REQ_ERRORS, "value", None)
    if not errs:
        return obj
    if isinstance(obj, dict):
        obj = dict(obj)
        obj["source_errors"] = list(errs)
    return obj
