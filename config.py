# -*- coding: utf-8 -*-
"""集中配置与开关（P2-21 / P1-45）。

此前所有运行时开关散落在 server.py / desktop_app / 各处环境变量里，名称、默认值、含义
全靠口口相传；本模块把它们收口成一份单一声明，并提供：

  · AppConfig —— 所有环境变量开关的唯一真源（名称 / 默认 / 含义 / 生效时机）
  · load_config() —— 读环境变量 + config/user_config.json 覆盖，得到运行时配置
  · 随机 APIToken 生成 / 落盘 / 读取（P1-45 防御纵深）
  · DISCLAIMER_TEXT —— 全局免责声明（P2-36，钉死「个人研究工具，不外发」）

本模块**零业务依赖**：只 import 标准库，绝不 import server / db，避免任何启动副作用
（P0-05：import viewer.server 不得触发任何 IO 或线程）。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# 仓库根（config.py 位于仓库根）。所有相对解析都基于它，杜绝散落绝对路径（P2-20）。
ROOT = Path(__file__).resolve().parent

# 用户可写配置 / 运行产物目录（与代码分离，P2-18 起步）。
CONFIG_DIR = ROOT / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
USER_CONFIG_PATH = CONFIG_DIR / "user_config.json"
TOKEN_PATH = CONFIG_DIR / "viewer_token.txt"  # 落盘随机 token（P1-45），不进版本库


@dataclass
class AppConfig:
    """所有环境变量开关的单一声明。

    新增开关请在此登记：名称 = 环境变量名，默认值 = 安全默认，doc = 含义与生效时机。
    这样 README / 桌面壳 / CI 都从同一份定义生成文档，杜绝漂移。
    """

    # ── 网络 / 端口 ──
    port: int = 8765
    host: str = "127.0.0.1"

    # ── 数据库 ──
    db_path: str = ""  # 空 = 默认 viewer/app.db；可指向 %LOCALAPPDATA%/stockvue/app.db（P2-18）

    # ── 写接口防御（P1-45）──
    # 随机 VIEWER_API_TOKEN：默认「按启动方决定」。
    #   · 由 desktop_app / pywebview 拉起 → 自动生成并注入 → 启用（浏览器同源自动带 Cookie，非浏览器客户端全 401）
    #   · 直接 python viewer/server.py（含 pytest / CLI 工具）→ 保持关闭，避免破坏非浏览器客户端
    # 设为非空字符串则强制启用该 token；设为 "off" 强制关闭。
    api_token: str = ""
    api_token_auto: bool = True  # desktop_app 拉起时是否自动生成随机 token

    # ── 抓取 / 调度（P1-46 / P1-47）──
    refresh_interval: int = 600          # 后台自动抓取最小间隔（秒）
    no_takeover: bool = False            # 禁止 build_id 接管重启
    stale_seconds: int = 3600            # 数据陈旧判定阈值
    keep_console: bool = False           # 保留控制台窗口
    ingest_max_workers: int = 4
    scheduler_enabled: bool = True       # 每站独立开关在 meta 里（P1-46），这是总闸
    rate_limit_per_min: int = 15
    tls_verify_hosts: str = ""           # STOCK_TLS_VERIFY_HOSTS：白名单校验主流源（P1-47）；空 = 兼容代理/烂证书链

    # ── 知识库 / 备份 ──
    kb_dir: str = ""
    db_backup_remote_dir: str = ""

    doc: dict = field(default_factory=lambda: {
        "port": "监听端口（VIEWER_PORT）",
        "host": "监听地址（默认回环，绝不暴露公网）",
        "db_path": "app.db 路径（VIEWER_DB）；空=默认位置",
        "api_token": "写接口本地令牌；非空强制启用，'off' 强制关闭（VIEWER_API_TOKEN）",
        "api_token_auto": "desktop_app 拉起时是否自动生成随机 token",
        "refresh_interval": "后台自动抓取最小间隔秒（VIEWER_REFRESH_INTERVAL）",
        "no_takeover": "禁止 build_id 接管重启（NO_TAKEOVER）",
        "stale_seconds": "数据陈旧判定阈值（STALE_SECONDS）",
        "keep_console": "保留控制台窗口（KEEP_CONSOLE）",
        "ingest_max_workers": "并发抓取线程数（INGEST_MAX_WORKERS）",
        "scheduler_enabled": "后台自动抓取总闸（默认开；每站独立开关在 meta）",
        "rate_limit_per_min": "每站每分钟请求上限（P1-46 反爬）",
        "tls_verify_hosts": "TLS 证书校验白名单，逗号分隔主机名（STOCK_TLS_VERIFY_HOSTS，P1-47）",
        "kb_dir": "知识库目录（KB_*）",
        "db_backup_remote_dir": "数据库异地镜像目录（DB_BACKUP_REMOTE_DIR）",
    })

    def as_dict(self) -> dict:
        return asdict(self)


_ENV_MAP = {
    "port": ("VIEWER_PORT", int),
    "host": ("VIEWER_HOST", str),
    "db_path": ("VIEWER_DB", str),
    "api_token": ("VIEWER_API_TOKEN", str),
    "api_token_auto": ("VIEWER_API_TOKEN_AUTO", lambda v: str(v).lower() in ("1", "true", "yes")),
    "refresh_interval": ("VIEWER_REFRESH_INTERVAL", int),
    "no_takeover": ("NO_TAKEOVER", lambda v: str(v).lower() in ("1", "true", "yes")),
    "stale_seconds": ("STALE_SECONDS", int),
    "keep_console": ("KEEP_CONSOLE", lambda v: str(v).lower() in ("1", "true", "yes")),
    "ingest_max_workers": ("INGEST_MAX_WORKERS", int),
    "scheduler_enabled": ("SCHEDULER_ENABLED", lambda v: str(v).lower() in ("1", "true", "yes")),
    "rate_limit_per_min": ("RATE_LIMIT_PER_MIN", int),
    "tls_verify_hosts": ("STOCK_TLS_VERIFY_HOSTS", str),
    "kb_dir": ("KB_DIR", str),
    "db_backup_remote_dir": ("DB_BACKUP_REMOTE_DIR", str),
}


def load_config() -> AppConfig:
    """环境变量 → AppConfig；再叠加 config/user_config.json 的持久化覆盖。"""
    cfg = AppConfig()
    for attr, (env, caster) in _ENV_MAP.items():
        raw = os.environ.get(env)
        if raw is not None and raw != "":
            try:
                setattr(cfg, attr, caster(raw))
            except (ValueError, TypeError):
                pass  # 非法值回退默认，绝不因此启动失败
    # 持久化覆盖（用户在前端设置面板调过的项）
    try:
        if USER_CONFIG_PATH.exists():
            overrides = json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in overrides.items():
                if k in _ENV_MAP and isinstance(v, (str, int, bool)):
                    setattr(cfg, k, v)
    except Exception:
        pass
    return cfg


# ── 随机 APIToken（P1-45）──
_lock = threading.Lock()


def generate_token() -> str:
    """生成一个密码学随机 token（urlsafe，约 32 字节）。"""
    return secrets.token_urlsafe(32)


def save_token(token: str) -> None:
    TOKEN_PATH.write_text(token, encoding="utf-8")
    # Windows 上 chmod 仅映射只读位，见 P2-22：注明现实，不强求 0600。
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def load_token() -> str:
    """读取落盘 token；不存在则返回空串（= 关闭令牌校验）。"""
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resolve_api_token(cfg: AppConfig, *, launched_by_desktop: bool) -> str:
    """决定本次运行的写接口 token。

    返回非空 = 启用令牌校验；空 = 关闭（向后兼容 / 非浏览器客户端可用）。
    """
    # 显式 env / 覆盖优先
    if isinstance(cfg.api_token, str) and cfg.api_token and cfg.api_token != "off":
        # 落盘以便 desktop_app 注入给前端
        try:
            save_token(cfg.api_token)
        except OSError:
            pass
        return cfg.api_token
    if isinstance(cfg.api_token, str) and cfg.api_token == "off":
        return ""
    # 自动模式：仅当桌面壳拉起时生成随机 token
    if cfg.api_token_auto and launched_by_desktop:
        tok = load_token() or generate_token()
        save_token(tok)
        return tok
    return ""


# ── 全局免责声明（P2-36）──
DISCLAIMER_TEXT = (
    "本工具为个人研究用途，所有数据、信号与结论仅供学习参考，不构成任何投资建议。"
    "据此操作风险自负。请勿外发。"
)


def get_disclaimer() -> str:
    return DISCLAIMER_TEXT


def main() -> None:
    """CLI：打印当前生效配置与 token 状态（调试用）。"""
    cfg = load_config()
    print("== AppConfig ==")
    for k, v in cfg.as_dict().items():
        if k == "doc":
            continue
        print("  %-22s = %r" % (k, v))
    tok = resolve_api_token(cfg, launched_by_desktop=False)
    print("api_token enabled (non-desktop) :", bool(tok))


if __name__ == "__main__":
    main()
