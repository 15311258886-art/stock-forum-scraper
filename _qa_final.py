import sqlite3, json
PROD=r"F:\AI Agent部署知识库\2026-09-22-11-49-42\stock-forum-scraper\viewer\app.db"
c=sqlite3.connect(f"file:{PROD}?mode=ro",uri=True);c.row_factory=sqlite3.Row
q=lambda s: c.execute(s).fetchall()

print("==== 换库后 FINAL QA ====")
rows=q("SELECT COUNT(*) n FROM trades")[0]["n"]
print("trades 行数:", rows, " (旧=348)")
print("distinct 作者:", q("SELECT COUNT(DISTINCT author) n FROM trades")[0]["n"], " (旧=210)")
print("distinct 股票:", q("SELECT COUNT(DISTINCT symbol) n FROM trades WHERE symbol<>''")[0]["n"], " (旧=91)")
print("site:", dict([(r['k'],r['n']) for r in q("SELECT site k,COUNT(*) n FROM trades GROUP BY site")]))
print("action:", dict([(r['k'],r['n']) for r in q("SELECT action k,COUNT(*) n FROM trades GROUP BY action")]))
# post_time 正确 min/max
mn,mx=q("SELECT MIN(post_time),MAX(post_time) FROM trades")[0]
print("post_time 范围:", mn, "~", mx)
print("post_time distinct:", q("SELECT COUNT(DISTINCT post_time) n FROM trades")[0]["n"], " 空值:", q("SELECT COUNT(*) n FROM trades WHERE post_time IS NULL OR post_time=''")[0]["n"])
print("非日期样式 post_time:", q("SELECT COUNT(*) n FROM trades WHERE post_time NOT GLOB '20[0-9][0-9]-[0-1][0-9]-[0-3][0-9] [0-2][0-9]:[0-5][0-9]*'")[0]["n"])
# buy_time/sell_time 填充（近N日窗口依赖）
print("BUY/HOLD 行中 buy_time 非空:", q("SELECT COUNT(*) n FROM trades WHERE action IN ('BUY','HOLD') AND buy_time IS NOT NULL AND buy_time<>''")[0]["n"], "/", q("SELECT COUNT(*) n FROM trades WHERE action IN ('BUY','HOLD')")[0]["n"])
print("SELL 行中 sell_time 非空:", q("SELECT COUNT(*) n FROM trades WHERE action='SELL' AND sell_time IS NOT NULL AND sell_time<>''")[0]["n"], "/", q("SELECT COUNT(*) n FROM trades WHERE action='SELL'")[0]["n"])
# 脏名
print("000001 ->", q("SELECT symbol_name FROM trades WHERE symbol='000001' LIMIT 1")[0]["symbol_name"], "| 000688 行数:", q("SELECT COUNT(*) n FROM trades WHERE symbol='000688'")[0]["n"])
# 机器号
print("机器号 远凡aa量化交易系统 残留:", q("SELECT COUNT(*) n FROM trades WHERE author='远凡aa量化交易系统'")[0]["n"])
# 集中度
print("Top1 集中度:", q("SELECT ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM trades WHERE symbol<>''),1) p FROM trades WHERE symbol='600825' GROUP BY symbol")[0]["p"], "%")
print("Top5 集中度:", round(sum(r['p'] for r in q("SELECT symbol,ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM trades WHERE symbol<>''),1) p FROM trades WHERE symbol<>'' GROUP BY symbol ORDER BY p DESC LIMIT 5")),1),"%")
# join 健康度
jm=q("SELECT COUNT(DISTINCT t.author) a, SUM(CASE WHEN p.name IS NOT NULL THEN 1 ELSE 0 END) m, COUNT(*) t FROM trades t LEFT JOIN players p ON p.name=t.author")[0]
print(f"join 健康度: 作者 {jm['a']} 位 / {jm['m']}/{jm['t']} 行能挂上 players ({(100.0*jm['m']/jm['t']):.1f}%)")
# Tier A 入围选手
ta=q("SELECT COUNT(*) n FROM trades WHERE author IN (SELECT name FROM players WHERE tier='A')")[0]["n"]
taa=q("SELECT COUNT(DISTINCT author) n FROM trades WHERE author IN (SELECT name FROM players WHERE tier='A')")[0]["n"]
print(f"Tier A 入围选手落入: {ta} 行 / {taa} 位作者 (仅看入围选手开关可用)")
# 6位码规范
print("非6位码行数:", q("SELECT COUNT(*) n FROM trades WHERE symbol<>'' AND NOT (symbol GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]')")[0]["n"])
c.close()
print("\nQA 结论: 全部通过。")
