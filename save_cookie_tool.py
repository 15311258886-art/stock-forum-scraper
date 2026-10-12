# -*- coding: utf-8 -*-
"""
雪球 Cookie 一键保存工具（给王总用）
用法：双击「第2步-保存雪球Cookie.bat」，按提示复制文本后按任意键即可。
逻辑：读取 Windows 剪贴板 -> 校验含 xq_a_token -> 写入本目录 cookies.txt
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

CF_UNICODETEXT = 13
GMEM_HANDLE = 0x0040


def get_clipboard_text():
    """读取 Windows 剪贴板文本（标准库 ctypes，无需装包）"""
    if not ctypes.windll.user32.OpenClipboard(0):
        return ""
    try:
        if not ctypes.windll.user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        h = ctypes.windll.user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = ctypes.windll.kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            # 注意：GetClipboardData 返回的句柄归剪贴板所有，不要 GlobalFree
            return ctypes.c_wchar_p(p).value or ""
        finally:
            ctypes.windll.kernel32.GlobalUnlock(h)
    finally:
        ctypes.windll.user32.CloseClipboard()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "cookies.txt")

    print("=" * 52)
    print("  第2步：保存雪球 Cookie（复制粘贴即可，不用登录）")
    print("=" * 52)
    print()
    print("请按下面顺序操作：")
    print()
    print("  1. 打开你和 AI 助手的对话窗口")
    print("  2. 往上翻，找到你发雪球 Cookie 的那条消息")
    print("     （特征：一大段英文数字，里面有 xq_a_token 这个词）")
    print("  3. 把那一整段文本全选、复制（Ctrl+C）")
    print("  4. 回到这个黑色窗口，按任意键")
    print()
    input("复制好之后，按回车键继续...")
    print()

    text = get_clipboard_text().strip()
    if not text:
        print("[失败] 剪贴板是空的。请先复制 cookie 文本，再重新双击本工具。")
        input("按回车关闭...")
        sys.exit(1)

    if "xq_a_token" not in text:
        print("[失败] 复制的内容里没找到 xq_a_token，可能复制错了内容。")
        print("       请确认复制的是那条雪球 Cookie 消息（一大段英文数字）。")
        input("按回车关闭...")
        sys.exit(1)

    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
        f.write("\n")

    size = os.path.getsize(out)
    print("[成功] 已保存到：" + out)
    print("       文件大小：%d 字节，已确认包含雪球登录凭证。" % size)
    print()
    print("现在回到对话里告诉 AI「cookie 存好了」，")
    print("然后双击「第1步-开始回填.bat」即可。")
    input("按回车关闭...")


if __name__ == "__main__":
    main()
