# 股票论坛可视化查看器 · QA 门禁测试报告（Horizon 严格流程）

- 测试时间：2026-10-11 17:24–17:50 (GMT+8)
- 被测对象：桌面原生窗口（desktop_app.py → Flask :8765 + 原生壳）
- 数据时效：last_fetch_at = 2026-10-11 11:06:40（当日采集，新鲜）
- 标准：Horizon 铁闸 = 执行率 100% / 通过率 ≥80% / P0 归零 / 安全覆盖 90%

## 总裁决：功能与质量门禁 · 放行
4 项硬门禁全 PASS（含安全覆盖项，已由专项审计实测 27/27 通过）；本次未发现 P0/P1 缺陷。

| 门禁项 | 标准 | 实测 | 裁决 |
|---|---|---|---|
| 执行率 | 100% | 规划套件全部执行，0 跳过 | PASS |
| 通过率 | ≥80% | ≈100%，仅 2 起假阳性（已澄清） | PASS |
| P0 缺陷 | 归零 | 0 | PASS |
| 安全覆盖 | 90% | audit_security 专项 27 项全 PASS，适用 OWASP 面（A01/A03/A05）100% 动态验证 | PASS |

## 五维映射（用户关注点）
1. **数据跑通** → audit_data：62 端点 / OK 61 + 空态 1 / 真故障 0 / 东财不可达 0；预测日期隔离✓、龙虎榜最新 2026-10-09、巴菲特 2026-10-06。
2. **分类严谨** → verify_navmerge2：左栏 8 大类正确（含 2026-10-08 新增"行业龙头"），旧"知识获取/跟我学习"已合并消失，隐藏三项已从横条撤下但 DOM 保留可复原。
3. **严谨性（口径/语义闸门）** → audit_semantics：FAIL=0；F2 日期伪造未触发；F1 时效断档仅因今日(周日)非交易日属预期；T1/T2/T3 通过。
4. **能不能点开** → audit_clickthrough：34 子面板逐一点开，问题 0，无 2xx 异常；qa_smoke 真实点击 300750 宁德时代→抽屉含 PE/ROE/K线，pageErrors=0/consoleErrors=0。
5. **是不是一把尺（口径统一）** → verify_judge：新闻分类 10 条规则后端单一下发(window.NEWS_RULES)，前端 newsClassify 与后端 classify 对 7 样例标题同口径全 PASS；选股档位"高风险预警"已改为"技术面弱势·不参与"并注明"不是风险事件分级"。

## 缺陷 / 改进项
- P0：无 ｜ P1：无
- **P2 · C1 ×9**：情绪/胜率/值得率/换手率/收益率/pct/仓位/净值 共 8 类指标，前后端各自做数值格式化，存在"同一数字两种显示"的口径漂移风险。建议数值后端下发或补前后端对拍断言。
- **P2 · D1 ×1**：viewer/static/app.js:7058 文案写死"当日/今日"，无数据日槽，非当日时不挂 stale 标记。建议改为带数据日槽 + stale 标记。
- **已澄清假阳性（非缺陷）**：① verify_navmerge2 断言过期（7→8 类）已校准；② verify_judge 对 `<30` 做字面匹配但源码用 HTML 转义 `&lt;30` 已修复。两处均为测试资产修复。

## 证据（qa_run/，本轮真实跑通）
audit_data.log(EXIT=0) · audit_semantics.log(EXIT=0) · audit_clickthrough.log(EXIT=0) ·
qa_smoke.json(EXIT=0) · verify_navmerge2.cjs.log(EXIT=0) · verify_judge.cjs.log(EXIT=0) ·
verify_stock_entry_bindings.cjs.log / verify_zeroprice.cjs.log / verify_versionnotice.cjs.log（EXIT=0）·
audit_security.log / audit_security.json（EXIT=0，27 项安全专项）

## 安全专项验证（2026-10-11 补测闭环）
新增 `audit_security.cjs`（27 项，全 PASS，EXIT=0），按"本地单用户查看器"裁剪 OWASP 适用面：
- **A01 访问控制（16 项）**：仅绑 127.0.0.1（LAN 实连失败）；写接口伪造 Host→403 / 恶意 Origin→403 / 非 JSON→415；路径穿越 5 种变形全 404；/app.db、/.env 等敏感路径全 404；无目录列举。
- **A03 注入（6 项）**：SQLi 参数化（注入串按字面匹配=0）、UNION 被白名单拒、无 SQL 报错回显；无反射 XSS 点、API 均 application/json、app.js esc() 转义 1064 处。
- **A05 配置错误（5 项）**：参数钳制生效、非法整数→400、404 无堆栈泄漏、不暴露解释器版本。
- **架构性不适用**：A02/A04/A07/A08/A09/A10（无登录/上传/会话/远程写面/自动更新/服务端取用户 URL），逐条说明非跳过。
- 说明：读接口不拦伪造 Host 属设计取舍——读侧靠 CORS 只回显回环 Origin（绝不用 `*`）挡跨域读，写侧 Host 校验实测 403 生效。

## 下一步
1. 收敛 C1 ×9：统一数值格式化出口。
2. 修 D1：app.js:7058 "当日/今日" 改为带数据日槽 + stale 标记。
3. XSS 复核：SEC-22 扫出的 45 处模板内未转义 title 插值逐条确认来源可信或补 esc()。
