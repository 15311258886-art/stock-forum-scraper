用例 | 期望 | 实际 | 判定 | 证据
---|---|---|---|---
G0 破坏性按钮存在但不点 | 在 DOM 中存在即视为已识别，全程不点击 | btnDemo=false btnOverseas=true btnFetch=true | PASS | 拦截器已装 | 已 abort: 0 条
N0 一级导航渲染 5 个大类 | 5 个 .nav-item | 5 个: 总览(1) / 知识获取(3) / 行情研判(5) / 实盘操作(5) / 跟我学习(2) | PASS | 
N1 导航 总览 › 汇总 | 面板 active + 有内容 + 无报错 | active=true htmlLen=4555 textLen=1495 | PASS | 
N1 导航 知识获取 › 数据源 | 面板 active + 有内容 + 无报错 | active=true htmlLen=17923 textLen=2060 | PASS | 
N1 导航 知识获取 › 原始存档 | 面板 active + 有内容 + 无报错 | active=true htmlLen=43721 textLen=7869 | PASS | 
N1 导航 知识获取 › 抓取与登录记录 | 面板 active + 有内容 + 无报错 | active=true htmlLen=1977 textLen=541 | PASS | 
N1 导航 行情研判 › 当前讨论股票 | 面板 active + 有内容 + 无报错 | active=true htmlLen=1968 textLen=359 | PASS | 
N1 导航 行情研判 › 舆论情绪 | 面板 active + 有内容 + 无报错 | active=true htmlLen=2539 textLen=507 | PASS | 
N1 导航 行情研判 › 近期热点 | 面板 active + 有内容 + 无报错 | active=true htmlLen=35205 textLen=6733 | PASS | 
N1 导航 行情研判 › 美股港股舆情 | 面板 active + 有内容 + 无报错 | active=true htmlLen=52256 textLen=7837 | PASS | 
N1 导航 行情研判 › 国外驱动热点 | 面板 active + 有内容 + 无报错 | active=true htmlLen=94976 textLen=16731 | PASS | 
N1 导航 实盘操作 › 正在操作股票 | 面板 active + 有内容 + 无报错 | active=true htmlLen=115369 textLen=15647 | PASS | 
N1 导航 实盘操作 › 实盘选手(渠道) | 面板 active + 有内容 + 无报错 | active=true htmlLen=45508 textLen=4348 | PASS | 
N1 导航 实盘操作 › 共同持仓共识 | 面板 active + 有内容 + 无报错 | active=true htmlLen=4922 textLen=647 | PASS | 
N1 导航 实盘操作 › 近5日共同买入 | 面板 active + 有内容 + 无报错 | active=true htmlLen=2375 textLen=771 | PASS | 
N1 导航 实盘操作 › 相关人物 | 面板 active + 有内容 + 无报错 | active=true htmlLen=27475 textLen=3333 | PASS | 
N1 导航 跟我学习 › 中短线炒股 | 面板 active + 有内容 + 无报错 | active=true htmlLen=47738 textLen=11739 | PASS | 
N1 导航 跟我学习 › 学习轨道管理 | 面板 active + 有内容 + 无报错 | active=true htmlLen=1405 textLen=444 | PASS | 
N2 子类覆盖数 | >= 16 个子类全部可点 | 共遍历 16 个子类: 汇总,数据源,原始存档,抓取与登录记录,当前讨论股票,舆论情绪,近期热点,美股港股舆情,国外驱动热点,正在操作股票,实盘选手(渠道),共同持仓共识,近5日共同买入,相关人物,中短线炒股,学习轨道管理 | PASS | 
C1 共同持仓共识：positions=0 时仍渲染 3 只股 | 3 行，无 undefined/NaN | 行数=3 含undefined=false 含NaN=false | PASS | 表头 | hint-present
C2 共识股票代码与人数正确 | 000001/4人, 300750/4人, 601318/2人 | 000001:4 人 | 300750:4 人 | 601318:2 人 | PASS | 
C3 共识行字段齐全（名称/公司/层级/平台/热度/来源非空） | 每行关键列非空 | [000001] 名称=平安银行
000001 平安银行股份有限公司 加权=加权 1.8 层级=D 人气榜 渠道= 热度=讨论热度 119
平台 1 ‖ [300750] 名称=宁德时代
300750 宁德时代新能源科技股份有限公司 加权=加权 1.8 层级=D 人气榜 渠道= 热度=讨论热度 57
平台 1 ‖ [601318] 名称=中国平安
601318 中国平安保险(集团)股份有限公司 加权=加权 0.9 层级=D 人气榜 渠道= 热度=讨论热度 44
平台 1 | PASS | 空单元格数 | 3
C6 共识「覆盖渠道」列：渠道缺失时给出占位而非留空白格 | 真空白单元格 = 0，且缺失行有占位（— / 暂无 / 渠道未标注） | 覆盖渠道列真空白行数 = 3/3，占位行数 = 0 | FAIL | 该列取值 | 000001:"" | 300750:"" | 601318:""
C4 共识「持仓选手明细」渠道缺失时不留空角标 | 不应出现无文字的空 badge | 空 badge 数 = 10（明细内 badge 总数 20） | FAIL | 修法 | app.js:962-968 把渠道角标改为「channel 为空则整段不渲染」，非空时仍输出紧凑字母角标
C5 近5日共同买入：空窗口不崩、有明确空态 | 无 undefined/NaN，有空态或窗口条 | html 文本长=771 空态=true 窗口条=true | PASS | 摘要 | 按「窗口内 ≥2 位选手同向操作同一标的」聚合，共买（红）与共卖（绿）分两组列出。 与左侧「共同持仓共识」口径不同：共识看的是现在还持有（持仓快照），本板块看的是最近 5 天谁同时动手（动作发生时点）—— 所以本板块能露出「窗口内已卖出」这
P1 operating 面板：空 positions 下无 undefined/NaN/null | 无异常字样 | 文本长=15647 行=200 undefined=false NaN=false null=false | PASS | 摘要 | 真实抓取 200 条 / 示例 0 条（来源：taoguba, eastmoney） 每个选手均标注其来源渠道：平台（淘股吧/东方财富/雪球/同花顺） + 所属层级（A~F）。标的列格式为「股票简称 
P1 players 面板：空 positions 下无 undefined/NaN/null | 无异常字样 | 文本长=4348 行=68 undefined=false NaN=false null=false | PASS | 摘要 | 真实抓取 68 条 / 示例 0 条 多层级人物覆盖：在原有 A/B/C/D 渠道基础上新增 E 主播大V（热度不高但影响力突出）与 F 长尾观察 两层。低层级权重递减，但一律露出（is_visibl
P1 stocks 面板：空 positions 下无 undefined/NaN/null | 无异常字样 | 文本长=359 行=0 undefined=false NaN=false null=false | PASS | 摘要 | 卡片第一行是股票简称，第二行是股票代码 + 公司全称，点击任意卡片可查看该股的持仓选手（持仓起始日期 / 买入价 / 当前股价）、相关实盘交易与讨论帖。当前有 2 个代码未识别名称，点下方「补全股票名
P1 summary 面板：空 positions 下无 undefined/NaN/null | 无异常字样 | 文本长=1495 行=20 undefined=false NaN=false null=false | PASS | 摘要 | 最后抓取：2026-09-25 02:22:18 ｜ 接入站点：taoguba、eastmoney、xueqiu、ths 登录态检测 淘股吧 已登录 登录态可用（Cookie）：命中已登录标志「个人中
E1 summary 示例角标误标检查（按文本） | 真实数据 → 文本为「示例」的角标数 = 0 | chip-demo 总数=0（文本「示例」=0、其他=0）；「真实」角标=0（.chip-real=0，误用 .chip-demo=0） | PASS | 横幅 | 无
E1 stocks 示例角标误标检查（按文本） | 真实数据 → 文本为「示例」的角标数 = 0 | chip-demo 总数=0（文本「示例」=0、其他=0）；「真实」角标=0（.chip-real=0，误用 .chip-demo=0） | PASS | 横幅 | 无
E1 players 示例角标误标检查（按文本） | 真实数据 → 文本为「示例」的角标数 = 0 | chip-demo 总数=0（文本「示例」=0、其他=0）；「真实」角标=0（.chip-real=0，误用 .chip-demo=0） | PASS | 横幅 | 真实抓取 68 条 / 示例 0 条
E1 operating 示例角标误标检查（按文本） | 真实数据 → 文本为「示例」的角标数 = 0 | chip-demo 总数=200（文本「示例」=0、其他=200）；「真实」角标=200（.chip-real=0，误用 .chip-demo=200） | PASS | 横幅 | 真实抓取 200 条 / 示例 0 条（来源：taoguba, eastmoney）
E1 consensus 示例角标误标检查（按文本） | 真实数据 → 文本为「示例」的角标数 = 0 | chip-demo 总数=0（文本「示例」=0、其他=0）；「真实」角标=0（.chip-real=0，误用 .chip-demo=0） | PASS | 横幅 | 无
S1 「真实」角标不套用「示例」样式类（应为 .chip-real，非琥珀虚底） | 带 .chip-demo 的「真实」角标 = 0，且「真实」全部使用 .chip-real | 「真实」角标 200 个：.chip-real=0，误用 .chip-demo=200 | FAIL | 实渲样式 | 真实 cls="chip-s chip-demo" bg=rgb(254, 243, 199) color=rgb(146, 64, 14) border=1px dashed rgb(251, 191, 36)口径 | 示例 = 琥珀底+虚线框(.chip-demo)；真实 = 弱绿实底+实线框(.chip-real)
T0 主题选择器含 4 套方案 | 4 个 chip | 4 个: light-tech(A 浅色科技风) / dark-dashboard(B 深色数据大屏) / minimal-report(C 简约报表风) / card-warm(D 卡片暖色) | PASS | 可用性备注 | 色块无文字标签，仅靠 title 提示（hover 才可见），弱可用性但非 BUG
T1 主题 light-tech 生效+可读+无重叠 | theme-link 切换 + 对比度>=2.5 + 无重叠 + 无报错 | href=static/themes/light-tech.css bg=rgb(250, 247, 240) 低对比元素=无 顶栏∩侧栏=false 侧栏∩内容=false 报错=0 | PASS | 对比度采样 | 顶栏标题=17.4 一级导航文字=5.36 子导航当前项=5.36 说明文字=4.1 表格表头=4.83 卡片/正文=4.1
T1 主题 dark-dashboard 生效+可读+无重叠 | theme-link 切换 + 对比度>=2.5 + 无重叠 + 无报错 | href=static/themes/dark-dashboard.css bg=rgb(11, 14, 20) 低对比元素=无 顶栏∩侧栏=false 侧栏∩内容=false 报错=0 | PASS | 对比度采样 | 顶栏标题=14.74 一级导航文字=10.05 子导航当前项=10.05 说明文字=5.27 表格表头=6.03 卡片/正文=5.27
T1 主题 minimal-report 生效+可读+无重叠 | theme-link 切换 + 对比度>=2.5 + 无重叠 + 无报错 | href=static/themes/minimal-report.css bg=rgb(255, 255, 255) 低对比元素=无 顶栏∩侧栏=false 侧栏∩内容=false 报错=0 | PASS | 对比度采样 | 顶栏标题=13.98 一级导航文字=10.35 子导航当前项=10.35 说明文字=4.34 表格表头=4.55 卡片/正文=4.34
T1 主题 card-warm 生效+可读+无重叠 | theme-link 切换 + 对比度>=2.5 + 无重叠 + 无报错 | href=static/themes/card-warm.css bg=rgb(255, 247, 237) 低对比元素=无 顶栏∩侧栏=false 侧栏∩内容=false 报错=0 | PASS | 对比度采样 | 顶栏标题=12.69 一级导航文字=4.56 子导航当前项=4.56 说明文字=3.75 表格表头=4.04 卡片/正文=3.75
D1 点击共识行弹出抽屉且内容完整 | shown + 有内容 + 无 undefined/NaN | shown=true 文本长=464 undefined=false NaN=false | PASS | 摘要 | 平安银行 000001 公司全称 平安银行股份有限公司 上市板块 深市主板 共同持仓人数 4 人 覆盖渠道 — 讨论热度 119 来源网址 https://guba.eastmoney.com/news,000001,1776681769.
D2 Esc 关闭抽屉 | closed | true | PASS | 
X1 全程无未捕获异常 / console.error | 0 条 | console.error=0 pageerror=0 | PASS | 无
X2 本轮未发出任何破坏性请求 | 0 条 blocked | 被拦截(即尝试发出但已 abort)=0: 无 | PASS | confirm 被调用次数 | 0

总计 44 用例，PASS 41，FAIL 3
失败用例：C6 共识「覆盖渠道」列：渠道缺失时给出占位而非留空白格 | C4 共识「持仓选手明细」渠道缺失时不留空角标 | S1 「真实」角标不套用「示例」样式类（应为 .chip-real，非琥珀虚底）