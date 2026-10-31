"""代码指纹（build id）—— 判断「正在监听的后端」跑的是不是当前磁盘上的代码。

2026-10-16 新增（进哥反馈：改了代码后双击图标重启，界面仍是旧代码，新板块接口 404）。
根因：desktop_app 起服务前只探测「8765 有没有人在监听」，有就直接复用、不再拉后端；
而 /api/version 的 version 字段是**实时读磁盘 index.html** 得来的，看着永远是"最新版"，
骗过了「是否该重启」的判断 —— 于是旧进程里的旧代码永远换不掉。

做法：把「关键源文件的内容 sha1」串起来取 sha1 前 12 位，作为该进程启动时刻的
代码指纹。server 启动时定格一次并通过 /api/version 暴露；desktop_app 启动时现算磁盘
指纹，与运行中的后端比对：不一致 = 代码换过 → 接管重启
（见 server._takeover_stale_server）。

2026-11-01 修正（这把指纹原来有两个反向的洞）：
  · 旧实现只取 `(mtime, size)`，而 **mtime 在 Windows 上只到秒** —— 同一秒内把一处改成
    **等长**的字符串（sed / 批量替换 / 自动化脚本都会这么干），指纹一点都不变，于是
    desktop_app 判定"代码没换"，继续复用一个跑着旧代码的后端。这正是本机制存在的理由，
    漏掉的恰恰是它要防的那件事。反方向也有一半问题：touch 一下（内容没变）指纹就变，
    于是出现"假 stale"，喊人重启。**误报会让人习惯性忽略这个告警，漏报会让人以为
    自己在跑新代码** —— 后者致命，所以取内容哈希。
  · 现在逐文件读内容取 sha1（仍按 KEY_FILES 顺序，文件缺失仍记 missing 不抛）。
    15 个文件合计约 2.5 MB，全读一遍 10~30ms，且只在进程启动时算一次。

注意：任何文件缺失都记账为 missing（不抛异常），保证「指纹算不出来」永远不会成为
启动失败的原因。
"""
from __future__ import annotations

import hashlib
import os

# 参与指纹的关键文件（相对项目根）。
# 只收「改了就必须重启后端才生效」的代码与静态资源；数据文件（app.db / kcache / yaml）
# 不算 —— 它们运行时本来就会被读写，纳进来会让指纹天天变、导致无谓重启。
KEY_FILES = (
    "viewer/server.py",
    "viewer/quant.py",
    "viewer/pillars.py",
    "viewer/evolve.py",
    "viewer/metalearn.py",
    "viewer/resonance.py",
    "viewer/backtest.py",
    "viewer/selfpick.py",
    "viewer/db.py",
    "viewer/predictor.py",
    "viewer/static/index.html",
    "viewer/static/app.js",
    "viewer/static/app.css",
    # 2026-10-30：节奏改造后，这两个文件改了不重启同样不生效，但不在指纹里 →
    #   桌面图标的「自动重启」判断不出来，会继续跑旧代码。
    #   rhythm.py 是「取数节奏唯一真源」（盘中/盘后/休市的采样间隔与缓存 TTL 全在这里），
    #   board_flow.py 是板块分时的定稿判定与实时抓取节流；两者都只在进程 import 时生效。
    "viewer/rhythm.py",
    "board_flow.py",
)

_READ_CHUNK = 1 << 20   # 1MB 一块，避免把整个文件读进内存（app.js 已 1.2MB）


def _file_sha1(path: str) -> str:
    """单文件内容 sha1（十六进制）。按块读，文件再大也不爆内存。"""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(_READ_CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def of_disk(root) -> str:
    """按 KEY_FILES 的**内容 sha1** 生成 12 位十六进制指纹。root = 项目根（含 viewer/）。

    逐文件记一条 `rel|size|内容sha1;`（缺失记 `rel|missing;`），串起来再取 sha1 前 12 位。
    含 size 是为了让"文件被截断/追加"这类读不出来的情况也留痕（虽然 sha1 已经覆盖）。
    文件缺失/读不动记为 missing，不抛异常（宁可指纹漂一点，也不让启动挂掉）。
    """
    h = hashlib.sha1()
    base = str(root)
    for rel in KEY_FILES:
        p = os.path.join(base, rel)
        try:
            size = os.path.getsize(p)
            digest = _file_sha1(p)
            h.update(("%s|%d|%s;" % (rel, size, digest)).encode("utf-8"))
        except OSError:
            h.update(("%s|missing;" % rel).encode("utf-8"))
    return h.hexdigest()[:12]
