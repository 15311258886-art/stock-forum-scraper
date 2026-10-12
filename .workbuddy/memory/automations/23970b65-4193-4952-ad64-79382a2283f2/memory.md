# 股票论坛每日抓取与存档 - 自动化执行记录

## 2026-09-23（首跑）
- 执行：`main.py run --site taoguba --mode all --out data`
- 结果：run 报「站点淘股吧未配置可用的表单登录地址，本机无有效 Cookie」→ 受限于登录。
- 存档统计（archive stats）：实盘交易累计 0 / 一手资讯累计 0 / 运行记录 1；最近运行状态 `login_failed`（2026-09-23）。
- 处理：按合规边界，未索取/猜测密码；提示用户先注册淘股吧账号并运行 `credentials add` 或 `credentials add-cookie` 存入凭据后再跑。
- 注：命令需对含空格路径与 PYTHONPATH 加引号，否则 shell 解析会把路径截断。

## 2026-09-24
- 执行：同上 `run --site taoguba --mode all --out data`。
- 本次凭据注入成功：INFO Cookie 注入成功（11 条，命中「个人中心」已登录标志）→ 不再受登录限制（对比 9-23 的 login_failed）。
- run 结果：trades 解析 0 条；news 解析 4 条；存档新增交易 0、新增资讯 4；状态 `ok`，退出码 0。
- archive stats：实盘交易累计 0 / 一手资讯累计 4 / 运行记录累计 2；最近运行 2026-09-24 ok（新交易0/新资讯4），上一条 2026-09-23 login_failed。
- 数据质量告警：今日 4 条「资讯」实为 tgb.cn 首页页脚导航链接（新手上路/版权声明/广告合作/社区规则），非真实一手资讯。判断为 news 选择器命中页脚链接，需校准 config/sites.yaml 的 news targets 才能抓到真实资讯。状态仍为 ok（非空解析），故不触发 empty_parse 流程，但需向用户提示。

## 2026-09-25
- 执行：同上 `run --site taoguba --mode all --out data`。
- 凭据仍有效：INFO Cookie 注入成功（11 条，命中「个人中心」已登录标志）；未受登录限制。
- run 结果：trades 解析 5 条（去重后新增 4，1 条重复）；news 解析 5 条（新增 5）；状态 `ok`，退出码 0。
- archive stats：实盘交易累计 4 / 一手资讯累计 9 / 运行记录累计 3；最近运行 2026-09-25 ok（新交易4/新资讯5），前两条 09-24 ok、09-23 login_failed。
- 待跟进（沿用 09-24 告警）：news 选择器此前命中首页页脚导航链接，今日 5 条资讯真实性仍需校准 config/sites.yaml 的 news targets；状态 ok 不触发 empty_parse，故需在用户侧提示。交易侧本次有真实解析（talk/talkSeq/14088 实盘帖），质量优于资讯侧。

## 2026-09-26
- 执行：同上 `run --site taoguba --mode all --out data`。
- 凭据仍有效：INFO Cookie 过滤过期 8 条后注入成功（11 条，命中「个人中心」已登录标志）；未受登录限制。
- run 结果：trades 解析 4 条（去重后新增 1）；news 解析 5 条（新增 5）；状态 `ok`，退出码 0。
- archive stats：实盘交易累计 5 / 一手资讯累计 14 / 运行记录累计 4；最近运行 2026-09-26 ok（新交易1/新资讯5），前三条 09-25 ok、09-24 ok、09-23 login_failed。
- 待跟进（沿用 09-24 告警）：news 选择器命中首页页脚导航链接的隐患仍未校准，今日 5 条资讯真实性待用户侧核查 config/sites.yaml 的 news targets；状态 ok 不触发 empty_parse。交易侧今日有真实解析，质量优于资讯侧。

## 2026-09-27
- 执行：同上 `run --site taoguba --mode all --out data`。
- 凭据仍有效：INFO Cookie 过滤过期 8 条后注入成功（11 条，命中「个人中心」已登录标志）；未受登录限制。
- run 结果：trades 解析 4 条（去重后新增 1）；news 解析 5 条（新增 4）；状态 `ok`，退出码 0。
- archive stats：实盘交易累计 6 / 一手资讯累计 18 / 运行记录累计 5；最近运行 2026-09-27 ok（新交易1/新资讯4），前三为 09-26 ok、09-25 ok、09-24 ok。
- 待跟进（沿用 09-24 告警）：news 选择器命中首页页脚导航链接的隐患仍未校准，今日 4 条资讯真实性待用户侧核查 config/sites.yaml 的 news targets；状态 ok 不触发 empty_parse。交易侧今日有真实解析，质量优于资讯侧。

## 2026-09-28
- 执行：同上 `run --site taoguba --mode all --out data`。
- 凭据仍有效：INFO Cookie 过滤过期 8 条后注入成功（11 条，命中「个人中心」已登录标志）；未受登录限制。
- run 结果：trades 解析 15 条（去重后新增 9）；news 解析 5 条（新增 4）；状态 `ok`，退出码 0。
- archive stats：实盘交易累计 15 / 一手资讯累计 22 / 运行记录累计 6；最近运行 2026-09-28 ok（新交易9/新资讯4），前四条 09-27 ok、09-26 ok、09-25 ok、09-24 ok（09-23 login_failed 已不在最近5条窗口）。
- 待跟进（沿用 09-24 告警）：news 选择器命中首页页脚导航链接隐患仍未校准，今日 4 条资讯真实性待用户侧核查 config/sites.yaml 的 news targets；状态 ok 不触发 empty_parse。交易侧今日解析量显著放大（15 条，新增 9），质量优于资讯侧。

## 2026-09-29
- 执行：同上 `run --site taoguba --mode all --out data`。
- 凭据仍有效：INFO Cookie 过滤过期 9 条后注入成功（10 条，命中「个人中心」已登录标志）；未受登录限制。
- run 结果：trades 解析 11 条（去重后新增 8）；news 解析 5 条（新增 3）；状态 `ok`，退出码 0。
- archive stats：实盘交易累计 23 / 一手资讯累计 25 / 运行记录累计 7；最近运行 2026-09-29 ok（新交易8/新资讯3），前四条 09-28 ok、09-27 ok、09-26 ok、09-25 ok（09-23/09-24 已不在最近5条窗口）。
- 待跟进（沿用 09-24 告警）：news 选择器命中首页页脚导航链接隐患仍未校准，今日 5 条 news 解析量稳定（新增 3），真实性仍待用户侧核查 config/sites.yaml 的 news targets；状态 ok 不触发 empty_parse。交易侧今日解析 11 条（新增 8），质量优于资讯侧。

## 2026-09-30
- 执行：同上 `run --site taoguba --mode all --out data`。
- 凭据仍有效：INFO Cookie 过滤过期 9 条后注入成功（10 条，命中「个人中心」已登录标志）；未受登录限制。
- run 结果：trades 解析 9 条（去重后新增 5）；news 解析 5 条（新增 3）；状态 `ok`，退出码 0。
- archive stats：实盘交易累计 28 / 一手资讯累计 28 / 运行记录累计 8；最近运行 2026-09-30 ok（新交易5/新资讯3），前一至四为 09-29 ok、09-28 ok、09-27 ok、09-26 ok。
- 待跟进（沿用 09-24 告警）：news 选择器命中首页页脚导航链接隐患仍未校准，今日 5 条 news 解析量稳定（新增 3），真实性仍待用户侧核查 config/sites.yaml 的 news targets；状态 ok 不触发 empty_parse。交易侧今日解析 9 条（新增 5），质量优于资讯侧。

## 2026-10-01
- 执行：同上 `run --site taoguba --mode all --out data`。
- 凭据失效（本任务首次回归登录受限）：INFO Cookie 过滤：过期 11 条、域不匹配 0 条 → WARNING 校验未通过（页面仍出现未登录标志「扫码登录」）→ ERROR 登录失败。已存 Cookie 落盘时间 2026-09-24 16:17:52，有效期约 7 天后过期。
- run 结果：trades 解析 0、news 解析 0；存档新增交易 0、新增资讯 0；状态 `login_failed`，退出码 0（程序未异常退出，仅业务态失败）。
- archive stats：实盘交易累计 28 / 一手资讯累计 28 / 运行记录累计 9；最近运行 2026-10-01 login_failed（新交易0/新资讯0），前四条 09-30、09-29、09-28、09-27 均 ok。
- 处理：按合规边界未索取/未猜测密码。已提示用户重新登录淘股吧后执行 `credentials add-cookie taoguba`（用法：`main.py credentials add-cookie taoguba --file <tools/capture_cookies.js 生成的 cookies.json>`）刷新登录态；若改用账号密码则 `credentials add`。
- 沉淀经验：Cookie 注入模式约 7 天需要刷新一次，是此任务唯一的单点故障源；后续可考虑在 stats 里对 login_failed 做连续失败计数告警。

## 2026-10-03
- 执行：同上 `run --site taoguba --mode all --out data`。
- 凭据恢复有效：INFO Cookie 过滤过期 8 条后注入成功（11 条，命中「个人中心」已登录标志）→ 摆脱 10-01 的 login_failed。
- run 结果：trades 解析 9 条（去重后新增 3）；news 解析 5 条（新增 3）；状态 `ok`，退出码 0。
- archive stats：实盘交易累计 31 / 一手资讯累计 31 / 运行记录累计 10；最近运行 2026-10-03 ok（新交易3/新资讯3），前四条 10-01 login_failed、09-30 ok、09-29 ok、09-28 ok。
- 断档提示：运行记录中无 2026-10-02，即 10-02 当日未产生运行记录（可能未触发或未落盘），需留意。
- 待跟进（沿用 09-24 告警）：news 选择器命中首页页脚导航链接隐患仍未校准（今日 news 源仍为 https://www.tgb.cn/ 首页），真实性待用户侧核查 config/sites.yaml 的 news targets；状态 ok 不触发 empty_parse。交易侧今日解析 9 条（新增 3），质量优于资讯侧。
