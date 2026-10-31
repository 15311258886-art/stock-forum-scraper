# Git 历史重写记录（2026-10-31）

> **本文是重写前的 commit hash 到重写后的唯一对照表。**
> 任何旧笔记、旧报告、对话里出现的旧 hash，均可在此查到对应的新 hash；
> 需要查旧 hash 的原始内容时，用备份 bundle（见文末"如何查旧历史"）。

## 一、为什么重写

仓库 `.git` 膨胀到 **869 MB**，而 HEAD 里受版本控制的文件只有 **153.8 MB**。
差额来自历史上堆积的运行时数据：

| 类别 | 历史中未压缩占用 | 说明 |
|---|---|---|
| `viewer/app.db.bak-*`、`archive/db_backups/*` | **1650 MB** / 35 个 blob | 每次改库前复制的整库备份，最大的单个 83 MB |
| `data/*.gz`、`*_kcache.json`、`fund_link_result.json`、`data/bili_scan_*` | **375 MB** / 20 个 blob | 抓取中间包与磁盘 TTL 缓存，每天变化 |
| `.browser-profile/`、`shots/`、`artifacts/` | 18 MB | 浏览器指纹目录与截图产物 |

这些内容**不承载任何决策信息**，但会让每次 clone 传几百 MB、每次 `git status` 变慢。
本仓库还有一个特殊性：2026-09-22 之前 `viewer/app.db`（生产库）曾被跟踪，
因此历史里留下了 20 多个 40~83 MB 的整库快照。

## 二、做了什么

用 `git filter-branch --index-filter` 对**全部 64 个提交**做索引级重写，
从每个提交的树里剔除上述路径，**保留全部提交、提交信息、作者时间、分支与 tag**。

- **没有** `--prune-empty`：即使某提交过滤后变成空提交，也原样保留，时间线不断点
- **没有**改动任何工作区文件内容（只在索引层操作）
- 剔除的运行时文件已从提交前的备份还原回工作区，**磁盘上的文件一个没少**

## 三、结果

| 指标 | 重写前 | 重写后 | 变化 |
|---|---|---|---|
| `.git` 体积 | 869 MB | **67 MB** | **-92.3%** |
| pack 体积 | 857.3 MB | 65.4 MB | -92.4% |
| git 对象数 | 10222 | 4701 | -54% |
| 提交数 | 64 | 64 | 不变 |
| tag | 2 | 2 | 不变 |
| 历史中最大 blob | 83.3 MB | 2.8 MB | -96.6% |

## 四、决策记录的可追溯性（重点）

`DECISIONS.md` 里有两处直接引用 commit hash 做复核。重写后已更新，并**逐条实测通过**：

| 原引用 | 新引用 | 提交主题 | 实测结果 |
|---|---|---|---|
| `ea292b6` | `137fd4a` | refactor(retire): 论坛/博主链路整体退役 | `git show 137fd4a^:viewer/static/app.js` 与 `git show 137fd4a:viewer/static/app.js` 分别为 17290 / 17278 行，差异存在，D20 的复核方法仍有效 |
| `826986d` | `8e9802a` | chore: 清理前存档点 | `git log -S"api/blogger/facets" -- viewer/server.py` 仍指向该提交，DECISIONS.md「这三组路由不是在 ea292b6 里删的，而是在清理前存档点删的」这一**归因结论重写后依然成立** |

也就是说：重写改变的是 hash 值，**不改变任何提交的内容、顺序与因果关系**。
决策记录里用 `git log -S` / `git show` 得出的论证，在新历史下可复现同样的结论。

## 五、完整映射表（64 条）

`改` = hash 发生变化；`—` = hash 未变（该提交本身不含被剔除的文件，其祖先也未变）。

| 重写前 | 重写后 | 变更 | 提交主题 |
|---|---|---|---|
| `5c6b03d474` | `5c6b03d474` | — | 股票论坛可视化查看器：阶段1~3 正确性体系 + CI 门禁 + 本地提交 |
| `e3a0d8d645` | `e3a0d8d645` | — | 调参：收紧 reasonableness_gate 经验阈值（total_return/sharpe）并同步单测与设计文档 |
| `451a4f3fd1` | `451a4f3fd1` | — | fix: 集合竞价向前补齐 + 收盘对账，长假后不再卡节前 |
| `6d943be7df` | `6d943be7df` | — | fix: 每日预测与 K 线强制对齐真实最近交易日，不用昨天/上月数据 |
| `7083cc89a3` | `7083cc89a3` | — | fix: 智能选股死按钮根治 + watch_list 超时熔断+新浪兜底 |
| `9386d1b0dc` | `9386d1b0dc` | — | feat: 个股快讯/公告代码可点开个股详情 |
| `db52af3f4a` | `db52af3f4a` | — | fix: 资金流主源切东财 datacenter（绕开 push2 风控）+ blogger_perf 禁代理 |
| `0848f3d680` | `0848f3d680` | — | fix(data): 龙虎榜/板块扩散/主线研判 接入每日自动刷新，补齐 9-30 冻结缺口 |
| `e377e7c365` | `e377e7c365` | — | feat(data): 查看器博主荐股直读源头Excel，摆脱对原App的依赖 + 每日自动刷新 |
| `10457b03ca` | `5a1a702a82` | 改 | chore: 清理前快照（工程垃圾清理前置检查点） |
| `400b0ba172` | `a7dc2e8a76` | 改 | chore: 工程垃圾大清理 — 回收约1.9G |
| `fd563ddee9` | `9c22413b4e` | 改 | fix(remed): 采纳《整改执行清单》8 项（真bug+安全+健壮性）+ 修 git 泄密面 |
| `7d35720a81` | `0a4f6fa01e` | 改 | feat(remed): #27 TLS 白名单 + #21 免责声明 + #16 T+1开盘口径（含 #06 端口裁决修正） |
| `977532f6b9` | `971517037e` | 改 | test(remed): 规格书 A4-2 配色门禁 + A7 导航契约测试 + 一键 pytest 入口 |
| `3a7f0eff60` | `2a65174bb2` | 改 | feat(D1): 本地库备份与完整性校验（本地部分） |
| `4d6b843096` | `864e9cb0b0` | 改 | feat(remed): 第二批 A6 缺失≠0 + D2 恢复提示（D2 主体本就已存在） |
| `1e5fa10a63` | `26ea8d1f24` | 改 | feat(remed): 第三批 A3-1 时点纪律守卫（防前视）+ A4 复核结论 |
| `83954c473b` | `2421e9b301` | 改 | docs: 顶配改造 V3 第二/三批执行进度报告 |
| `e41023767c` | `9618725596` | 改 | fix(ui): 交易日横排限量 + 日期查询（进哥反馈：不能无限排、不能拐行） |
| `d19f9e67ce` | `fb875af5be` | 改 | fix(ui): 日期横排限量推广到全站（牛散动向报告期）+ 修 JS 原型坑 |
| `4794204ae4` | `9e8d32437c` | 改 | fix(kline): 修复「缩到最小看不到上市至今」+ 新增「全历史」一键全览 |
| `b8c54bd230` | `455d5e4094` | 改 | 关注池整改：删无用列，加入手值得率/战法信号/博主提及标记 |
| `c83b454626` | `16a551a57c` | 改 | eng(infra): add shared infra package (fmt/httpclient/cal/cache) |
| `04da3cd8f4` | `a8c88fc174` | 改 | eng(server): backend consolidation - boot() fold-in, P1-45 token, HTTP |
| `07a09bc09d` | `250dc26db3` | 改 | eng(deps): requirements(+tzdata/pytest-timeout), pytest.ini timeout, . |
| `cf88a5abf2` | `7fcee3fb90` | 改 | feat(config): add config.py (AppConfig/token) and migrate.py (user_ver |
| `e2bc5c75d4` | `9347f3a455` | 改 | test: backend regression - boot no side-effects, route snapshot(192 ro |
| `1b8fea1dd9` | `a77ee6d025` | 改 | chore(viewer): fold-in viewer module updates, static frontend, drop st |
| `1cca8f8696` | `1aab66ecc4` | 改 | feat(handoff): wire A/C backend algo/data modules so server.py imports |
| `d073784816` | `cb93e843b8` | 改 | chore(scrapers): fold-in A/C scraper/tooling/data joint state |
| `2d3cdd9470` | `2ada6647eb` | 改 | chore: remaining joint-state tracked changes |
| `bf22ae73f6` | `9f462ab675` | 改 | backend hardening (P2) + A/C handoff: disclose_at, _js_restart_service |
| `58fda11596` | `4b80ab5438` | 改 | 做满 P2-03（source_errors 上抛）与 P2-24（GET 只读路由分类） |
| `60b25f63b5` | `4ebc9c8f2b` | 改 | docs: 修正 P2-03 注释中过时的模块名引用（_source_errors -> source_errors） |
| `fa15e69a1b` | `cfec93e5fb` | 改 | feat(frontend): api() 加 AbortController 超时与失败兜底，消除永久加载中 |
| `c1de9a5870` | `ceaf8aa081` | 改 | perf(backend): 上游抓取磁盘TTL缓存+请求合并+东财令牌桶，消除弹窗首开加载中 |
| `f20579b5b4` | `b4a0b30f67` | 改 | fix(ui): 版本自检正则容忍自闭合标签 + 前端版本号 bump 20261023-perf1；MCP 启动隐藏控制台窗口 |
| `ab8400280f` | `f3ae8cae81` | 改 | feat(bt): 历史回测收益格三色口径（涨但跑输/跌但跑赢=浅黄底黄框，字色仍随涨跌）+ 图例 + 版本号 bump |
| `d847bbd847` | `ff65b9b57e` | 改 | fix(bt): 三色判定加 bench 存在校验，避免无基准的涨格被误标黄框 |
| `826986d119` | `8e9802a616` | 改 | chore: 清理前存档点（论坛停抓/AI闭环四件套等既有改动） |
| `ea292b67c6` | `137fd4a7c8` | 改 | refactor(retire): 论坛/博主链路整体退役——清 18 万行死数据、删 10 张孤儿表、下 2 个空转任务、摘 10 个死面 |
| `cb11938dcb` | `ddb9dfc548` | 改 | feat(harvest): 新增「大势研判」板块——把「连板开板=派发」做成可测信号与实时陷阱名单 |
| `a395d4f23f` | `8cf4d405f4` | 改 | feat(oss): 开源情报模块 —— GitHub 规则在本系统严格口径下重测 |
| `5ca5d0c575` | `29e9d26bd6` | 改 | feat(selection): 选股方向 —— 短线证伪 + 中长期改判 |
| `fd477e3d3e` | `bf233649bf` | 改 | fix(coverage): 四处池子锁死一次修完 —— 候选池 594→1974，基本面 1123→5055 |
| `15c0396a96` | `406a444267` | 改 | fix(evidence): 因子历史回补到 2016 并重标定 —— 288 日样本会把真效应判死 |
| `41e77229f5` | `f650ea19bb` | 改 | feat(lhb): 龙虎榜历史回补到 2016 —— 最强信号被高估 5 倍 |
| `5aed8457b3` | `69fb7f0e9d` | 改 | fix(ui): 三个面板深链首屏全是死的 —— AVOID/QUANT/ETF 的 TDZ 地雷 |
| `e039f39f7a` | `9fb6df213e` | 改 | feat(oss): 指标表扩到 11 年后重跑 11 条开源规则 —— 首次出现长样本为正的一条 |
| `ab48ade6d7` | `4067aff3c5` | 改 | feat(retire): 停雪球+抖音语料，驳回两条被数据推翻的建议 |
| `db7185fcf4` | `dfeb272c1c` | 改 | fix(selfpick): 修复博主维度复活与缺失维度填50，扩展低位池批量预取 |
| `4adac89303` | `cec37ef6ef` | 改 | docs: 记录 selfpick 口径修复、公告月窗验证与 2010 回补边界 |
| `c32296f3a7` | `370b17063c` | 改 | fix(selfpick): 舆情分位分母只含有舆情样本，统计只认新口径 |
| `8f02aaab66` | `d6f602f2ce` | 改 | feat(coverage): 行业映射批量回补 377→5,356 只（7.5%→100%） |
| `aba119ba79` | `6ed0201ff6` | 改 | docs: 补记因子扩到2010完成与行业映射377→5356 |
| `0042b1ccbb` | `7554c5aa84` | 改 | feat(evidence): 龙虎榜第三次重标定到 2010 起 —— 幅度 -1.22%→-0.91%，方向仍一致 |
| `2a796206c3` | `95e6413e77` | 改 | fix(coverage): 行业回补补上证监会行业，覆盖率按干净A股宇宙核算 |
| `edebadd493` | `3d1089fdeb` | 改 | fix(ann): seen 只数窗口内公告行，避免月份被误标 done 后永久丢公告 |
| `62f37f3bbb` | `774a647733` | 改 | feat(lowbox): 低位池(低波池)选股加行业上限，回测定阈值20% |
| `4e5cfe75c6` | `e4bb459890` | 改 | fix(predictor): 机构结构前视+行序累加双修，顺带清4项pytest基线红灯 |
| `68ff539a9b` | `e1b279390e` | 改 | fix(sched): bili_scan 三处缺陷 + lhb_daily 闭环 + 测试写生产隔离 |
| `248c4aa445` | `1a58511cf4` | 改 | chore(repo): 核心源码纳入版本管理（基线快照） |
| `e9940c5510` | `65f8300a34` | 改 | chore(gitignore): 补 4 类漏网排除项 |
| `38e44e10a1` | `535aa9a5c9` | 改 | fix(isolation): 库路径全域接入 VIEWER_DB，堵住「测试写生产库」根因 |

## 六、如何查旧历史

重写前的完整仓库已打包保存（含全部提交与 tag）：

```
F://AI Agent部署知识库\2026-09-22-11-49-42\_repo_slim_backup_20261031\
  repo-before-slim-20261031.bundle     # 378 MB，全量历史
  old_bare.git/                        # 从 bundle 解出的只读仓库（362 MB）
  worktree_files/                      # 被移出跟踪的运行时文件（94.9 MB）
  old_head.txt                         # 重写前的 HEAD（38e44e10a183…）
```

查某个旧 hash 的内容：

```bash
git --git-dir="F:/AI Agent部署知识库/2026-09-22-11-49-42/_repo_slim_backup_20261031/old_bare.git" \
    show <旧hash>:<路径>
```

确认旧仓库可解压后（本文写作时已验证），bundle 可以单独长期保留，`old_bare.git` 可删。

## 七、回滚方法

若需完全回到重写前状态（**会丢弃重写后的新提交**）：

```bash
git bundle verify ../_repo_slim_backup_20261031/repo-before-slim-20261031.bundle
git fetch ../_repo_slim_backup_20261031/repo-before-slim-20261031.bundle "refs/*:refs/restore/*"
git reset --hard <old_head>   # 见 old_head.txt
```

## 八、注意事项

1. **旧 hash 在新仓库里查不到**（对象已被 `gc --prune=now` 清除），这是重写的预期结果，不是数据丢失。
2. 若某份旧文档里写了旧 hash 的**完整 40 位**，请按本表第五节换算；只有前 7~10 位时按本表前缀匹配即可。
3. 重写只针对本机仓库，**本仓库没有远程（`git remote -v` 为空）**，因此不存在与他人分支冲突的问题。
4. 后续若再发现 `.git` 异常膨胀，先跑
   `git rev-list --objects --all | git cat-file --batch-check='%(objecttype) %(objectname) %(objectsize) %(rest)' | awk '$1=="blob" && $3>5000000'`
   定位大 blob，再决定是否清理。

---
*生成于 2026-10-31，对应 DECISIONS.md D59。*
