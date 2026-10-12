# -*- coding: utf-8 -*-
"""从 Windows 剪贴板读取雪球 cookie 文本，校验含 xq_a_token 后写入工程根目录 cookies.txt。

用途：王总只需在对话里复制那条雪球 cookie 消息（Ctrl+C），由助手直接读剪贴板落盘，
     免去他找文件、双击 bat 的操作。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from save_cookie_tool import get_clipboard_text  # noqa: E402


def main() -> int:
    txt = (get_clipboard_text() or "").strip()
    if not txt:
        print("CLIP_EMPTY: 剪贴板为空，请先复制雪球 cookie 文本")
        return 1
    if "xq_a_token" not in txt:
        print("NO_XQ_TOKEN: 剪贴板内容里没有 xq_a_token（可能复制了别的）")
        print("HEAD:", txt[:120].replace("\n", " "))
        return 2
    out = os.path.join(HERE, "cookies.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(txt)
        f.write("\n")
    print("COOKIE_SAVED:", out, os.path.getsize(out), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
