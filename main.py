"""股票论坛 / 贴吧 实盘交易 + 资讯 采集程序（合规版）命令行入口。

┌─ 合规与红线（务必先读）──────────────────────────────────────────────┐
│ · 本程序【不】实现自动注册 / 批量建号 / 验证码破解 / 撞库。              │
│ · 账号由你本人在目标网站手动注册；程序仅用你的凭据做一次正常登录。        │
│ · 不采集任何用户行为 / 画像字段（在线状态、活跃度、互动量、粉丝数…）。     │
│ · 默认遵守 robots.txt 并限速，避免对目标站造成压力。                    │
│ · 仅抓取用户主动公开发布的内容（实盘贴、持仓贴、公开资讯）。              │
└────────────────────────────────────────────────────────────────────┘

用法示例：
  # 1) 手动注册后，把凭据交给我（本地加密保存）
  python main.py credentials add taoguba --username 你的账号 --password 你的密码

  # 2) 抓取实盘交易（需登录）
  python main.py run --site taoguba --mode trades --out ./data

  # 3) 抓取一手资讯
  python main.py run --site taoguba --mode news --out ./data

  # 4) 抓取两者
  python main.py run --site taoguba --mode all --out ./data --format csv

  # 5) 调试：把某个页面 HTML 落盘，便于你确认选择器
  python main.py inspect https://www.taoguba.com.cn/某实盘页
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from scraper import pipeline
from scraper.archive import ExcelArchive
from scraper.auth import print_register_hint
from scraper.config import get_site, load_config
from scraper.credentials import (
    delete_credential,
    describe,
    get_credential,
    list_sites,
    save_cookies,
    save_credential,
)
from scraper.session import RateLimitedSession

# 工程根目录（main.py 所在目录）
PROJECT_ROOT = Path(__file__).resolve().parent
# 默认累积存档路径
DEFAULT_ARCHIVE = PROJECT_ROOT / "archive" / "股票论坛抓取历史存档.xlsx"


def cmd_credentials(args):
    if args.action == "add":
        if not args.username or not args.password:
            print("用法：python main.py credentials add <site> --username <账号> --password <密码>")
            return
        save_credential(args.site, args.username, args.password)
        print(f"已加密保存 [{args.site}] 账号密码（本地，未上传）。")
    elif args.action == "add-cookie":
        if not args.file:
            print("用法：python main.py credentials add-cookie <site> --file cookies.json")
            print("  cookies.json 可由 tools/capture_cookies.js 生成，或从浏览器 DevTools 复制。")
            return
        src = Path(args.file)
        if not src.exists():
            print(f"文件不存在：{src}")
            return
        try:
            payload = json.loads(src.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"解析 JSON 失败：{e}")
            return
        cookies = payload.get("cookies") if isinstance(payload, dict) else payload
        meta = {}
        if isinstance(payload, dict):
            meta = {
                "source_url": payload.get("url", ""),
                "captured_at": payload.get("captured_at", ""),
                "user_agent": payload.get("user_agent", ""),
            }
        n = save_cookies(args.site, cookies, meta)
        if n:
            print(f"已加密保存 [{args.site}] Cookie {n} 条（本地，未上传）。")
            print("  提示：Cookie 会过期。失效后重新登录并再次执行本命令即可覆盖。")
        else:
            print("未从文件中解析出有效 Cookie（需含 name/value 字段）。")
    elif args.action == "list":
        if args.site:
            d = describe(args.site)
            print(f"{args.site}:")
            print(f"  账号密码：{'已保存（' + d['username'] + '）' if d['has_password'] else '未保存'}")
            print(f"  Cookie  ：{d['cookie_count']} 条"
                  + (f"，保存于 {d['cookie_saved_at']}" if d["cookie_saved_at"] else ""))
        else:
            sites = list_sites()
            if not sites:
                print("本机尚未保存任何站点的凭据。")
            for s in sites:
                d = describe(s)
                bits = []
                if d["has_password"]:
                    bits.append(f"账号密码({d['username']})")
                if d["cookie_count"]:
                    bits.append(f"Cookie {d['cookie_count']} 条")
                print(f"  {s}: " + ("、".join(bits) if bits else "（空条目）"))
    elif args.action == "delete":
        ok = delete_credential(args.site)
        print(f"[{args.site}] {'已删除' if ok else '不存在'}")


def cmd_run(args):
    total, new_t, new_n = pipeline.run(
        site_key=args.site,
        mode=args.mode,
        out_dir=args.out,
        config_path=args.config,
        fmt=args.format,
        respect_robots=not args.no_robots,
        no_login=args.no_login,
        archive_path=None if args.no_archive else args.archive,
    )
    print(f"完成：共 {total} 条记录 -> {args.out}")
    print(f"累积存档更新：新增交易 {new_t} 条、新增资讯 {new_n} 条 -> {args.archive}")


def cmd_archive(args):
    if args.action == "init":
        arc = ExcelArchive(args.path)
        arc.ensure_file()
        print(f"已初始化存档文件（含表头，尚无数据）：{arc.path}")
    elif args.action == "stats":
        arc = ExcelArchive(args.path)
        s = arc.stats()
        print(f"存档文件：{s['path']}")
        print(f"  实盘交易累计：{s['trades']} 条")
        print(f"  一手资讯累计：{s['news']} 条")
        print(f"  运行记录累计：{s['runs']} 条")
        print("  最近运行：")
        for r in arc.recent_runs(5):
            print(f"    {r['运行日期']} | {r['站点']} | {r['模式']} | "
                  f"新交易 {r['新增交易']} / 新资讯 {r['新增资讯']} | {r['状态']}")


def cmd_inspect(args):
    data = load_config(args.config)
    cfg = get_site(data, args.site)
    cfg["_key"] = args.site
    sess = RateLimitedSession(
        user_agent=cfg.get("user_agent", ""),
        respect_robots=not args.no_robots,
    )
    if cfg.get("requires_login") and not args.no_login:
        try:
            from scraper.auth import ensure_login

            ensure_login(sess, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 登录未完成（{e}）；将仅尝试匿名抓取。"
                  f"若页面需登录，请先 credentials add-cookie（扫码/验证码登录站点）或 credentials add。")
    r = sess.get(args.url)
    out = args.out or "inspect.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(r.text)
    print(f"已保存 {len(r.text)} 字节 -> {out}（请用浏览器打开，按真实 DOM 微调 config/sites.yaml 选择器）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="股票论坛实盘交易/资讯采集（合规版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    pc = sub.add_parser("credentials", help="管理本地加密凭据（账号密码 / Cookie）")
    pc.add_argument("action", choices=["add", "add-cookie", "list", "delete"],
                    help="add=存账号密码；add-cookie=存浏览器登录态；list=查看；delete=删除")
    pc.add_argument("site", nargs="?", help="站点键（如 taoguba）；list 时留空可列出全部")
    pc.add_argument("--username")
    pc.add_argument("--password")
    pc.add_argument("--file", help="add-cookie 用：cookies.json 路径（由 tools/capture_cookies.js 生成）")
    pc.set_defaults(func=cmd_credentials)

    pr = sub.add_parser("run", help="执行采集")
    pr.add_argument("--site", required=True)
    pr.add_argument("--mode", choices=["trades", "news", "all"], default="all")
    pr.add_argument("--out", default="./data")
    pr.add_argument("--config")
    pr.add_argument("--format", default="json", choices=["json", "csv", "sqlite"])
    pr.add_argument("--archive", default=str(DEFAULT_ARCHIVE),
                    help=f"累积存档 Excel 路径（默认 {DEFAULT_ARCHIVE}）")
    pr.add_argument("--no-archive", action="store_true", help="不写入累积存档")
    pr.add_argument("--no-robots", action="store_true", help="关闭 robots.txt 检查（请自担合规风险）")
    pr.add_argument("--no-login", action="store_true", help="跳过登录（仅抓公开页）")
    pr.set_defaults(func=cmd_run)

    pa = sub.add_parser("archive", help="管理累积存档 Excel（init / stats）")
    pa.add_argument("action", choices=["init", "stats"])
    pa.add_argument("--path", default=str(DEFAULT_ARCHIVE), help="存档文件路径")
    pa.set_defaults(func=cmd_archive)

    pi = sub.add_parser("inspect", help="抓取某页 HTML 落盘，用于确认选择器")
    pi.add_argument("url")
    pi.add_argument("--site", default="taoguba")
    pi.add_argument("--out")
    pi.add_argument("--config")
    pi.add_argument("--no-robots", action="store_true")
    pi.add_argument("--no-login", action="store_true")
    pi.set_defaults(func=cmd_inspect)

    return p


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "cmd", None):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
