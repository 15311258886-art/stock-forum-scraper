# -*- coding: utf-8 -*-
"""`viewer/build_id.py::of_disk` 的回归测试 —— 代码指纹必须只认真内容。

为什么单独一个文件（2026-11-01）：
这把指纹的用途是回答「正在监听 8765 的后端，跑的是不是磁盘上这份代码」。答错的代价不对称：

  · **漏报**（代码换了、指纹没变）→ desktop_app 判定"代码没换"，继续复用跑着旧代码的后端，
    而用户以为自己在看新代码。这正是指纹机制被造出来要防的那件事，所以它自己必须先不出这个洞。
  · **误报**（内容没变、指纹变了）→ 天天喊人重启。人一旦习惯性忽略这个告警，漏报就更没人发现。

旧实现只取 `(mtime, size)`，两个方向都出过事：Windows 上 `st_mtime` 只到**秒**，同一秒内做一次
**等长**替换（sed / 批量替换 / 自动化脚本都是这么干的）指纹纹丝不动；反过来 `touch` 一下
内容没变指纹却变了。现在改成逐文件内容 sha1，所以下面两条断言必须都在：
「同内容不同 mtime → 同指纹」「同长度不同内容 → 不同指纹」。

所有用例都在 `tmp_path` 里造一个小号 KEY_FILES，不读真实项目的 2.5MB 源码。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from viewer import build_id as bid  # noqa: E402


def _make_tree(root: Path, files: dict) -> None:
    """在 root 下按 KEY_FILES 的相对路径造文件（内容按 dict 给，缺省用 "x"）。"""
    for rel in bid.KEY_FILES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(files.get(rel, "x").encode("utf-8"))


def test_of_disk_ignores_mtime(tmp_path):
    """同内容、不同 mtime → 指纹必须相同。

    这是"假 stale"那一半：`touch` 不该让任何人以为代码变了。
    """
    _make_tree(tmp_path, {})
    fp1 = bid.of_disk(tmp_path)

    # 把所有文件的时间戳改成一个明显不同的值（内容一个字节都没动）
    for rel in bid.KEY_FILES:
        p = tmp_path / rel
        os.utime(p, (1_000_000_000, 1_000_000_000))
    fp2 = bid.of_disk(tmp_path)

    assert fp1 == fp2, "只改了 mtime，指纹不该变（否则天天误报「代码换了，请重启」）"


def test_of_disk_sees_equal_length_edit(tmp_path):
    """同长度、不同内容 → 指纹必须不同。

    这是**旧实现漏掉的那个方向**：`(mtime, size)` 下，同一秒内把 `"0.5"` 改成 `"0.6"`
    或把 `True` 改成 `Fals`，size 一样、秒级 mtime 一样，指纹一模一样 ——
    于是"跑着旧代码"被判定成"代码没换"，desktop_app 继续复用旧后端。
    本用例在老实现下必须是红的，否则它就是一条没有牙的断言。
    """
    body = "x" * 64
    files_a = {"viewer/server.py": body}
    files_b = {"viewer/server.py": body[:-1] + "y"}
    assert len(files_a["viewer/server.py"]) == len(files_b["viewer/server.py"]), "前提：等长"

    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_tree(a, files_a)
    _make_tree(b, files_b)
    # 把两边 mtime 拉到完全相同 —— 旧实现唯一能看到的差异就没了
    for sub in (a, b):
        for rel in bid.KEY_FILES:
            os.utime(sub / rel, (1_700_000_000, 1_700_000_000))

    assert bid.of_disk(a) != bid.of_disk(b), (
        "等长内容替换没被识别 —— 这正是旧 (mtime, size) 实现的漏报洞："
        "同一秒内改一个字符，指纹不变，后端被当成「代码没换」继续跑旧代码")


def test_of_disk_missing_file_is_recorded_not_raised(tmp_path):
    """文件缺失 → 记 missing、不抛异常（指纹漂一点可以，启动挂掉不行）。"""
    _make_tree(tmp_path, {})
    full = bid.of_disk(tmp_path)
    (tmp_path / "viewer" / "quant.py").unlink()
    missing = bid.of_disk(tmp_path)
    assert isinstance(missing, str) and len(missing) == 12
    assert missing != full, "文件被删了，指纹应该变（否则删文件也能蒙混过关）"


def test_of_disk_shape_and_determinism(tmp_path):
    """12 位小写十六进制、同一份磁盘算两次必须一样（server 定格一次、desktop_app 现算一次要对得上）。"""
    _make_tree(tmp_path, {})
    fp = bid.of_disk(tmp_path)
    assert len(fp) == 12, "指纹长度是协议的一部分（前端/日志/交叉验证都按 12 位比）"
    assert all(c in "0123456789abcdef" for c in fp), "指纹必须是十六进制：%r" % (fp,)
    assert bid.of_disk(tmp_path) == fp, "同一份磁盘两次算不一致 —— 那就什么也证明不了"


def test_of_disk_matches_independent_recomputation(tmp_path):
    """独立按同一定义重算一遍，必须与本模块同值 —— 防的是"实现悄悄换了定义但没人发现"。

    定义（与 e2e 的 Node 实现严格一致）：对每个 KEY_FILES 记 `rel|size|内容sha1;`，
    按 KEY_FILES 顺序拼起来，整体取 sha1 前 12 位。
    """
    _make_tree(tmp_path, {"viewer/static/app.js": "console.log(1);", "board_flow.py": "x = 1"})
    h = hashlib.sha1()
    for rel in bid.KEY_FILES:
        p = tmp_path / rel
        raw = p.read_bytes()
        h.update(("%s|%d|%s;" % (rel, len(raw), hashlib.sha1(raw).hexdigest())).encode("utf-8"))
    assert bid.of_disk(tmp_path) == h.hexdigest()[:12]


def test_key_files_cover_the_files_that_need_a_restart():
    """指纹覆盖面：这几个文件改了不重启就不生效，少一个就等于给那条路留个洞。"""
    need = {
        "viewer/server.py",       # 路由与接口全在这里
        "viewer/static/app.js",   # 前端主脚本（静态资源虽每次现读，但 build 要能反映它变过）
        "viewer/rhythm.py",       # 取数节奏唯一真源（import 时生效）
        "board_flow.py",          # 板块分时定稿判定（import 时生效）
    }
    missing = need - set(bid.KEY_FILES)
    assert not missing, "这些文件改了必须重启才生效，却不在指纹里：%s" % (sorted(missing),)


def test_module_docstring_does_not_claim_it_only_stats():
    """头的注释不许再写「只 stat、不读内容、~1ms」—— 改成内容哈希之后那就是谎话。

    一个撒谎的实现注释，比没有注释更能骗人：下一个人会据此认为"改内容不加大小是安全的"。
    """
    src = (ROOT / "viewer" / "build_id.py").read_text(encoding="utf-8")
    for lie in ("不读内容", "只 stat 文件", "~1ms"):
        assert lie not in src, "build_id.py 的注释还写着 %r，与内容哈希的实现不符" % (lie,)
    assert "内容 sha1" in src or "内容sha1" in src, "注释必须说清指纹取的是内容哈希"
