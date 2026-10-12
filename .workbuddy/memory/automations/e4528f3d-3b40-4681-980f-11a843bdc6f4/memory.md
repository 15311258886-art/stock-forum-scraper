# 自动化执行记录 · 股票论坛查看器数据自动更新

任务：运行 auto_update.py → 读 auto_update_status.json → 中文简报。

## 执行历史

### 2026-09-28 16:30:46（首次记录）
- 命令：`.venv/Scripts/python.exe auto_update.py`（Bash 前台，超时 600s 后自动转后台 task jMVceW）
- 结果：ok=true，returncode=0，seconds=1003（约 16分45秒，明显长于预期的 5-8 分钟，下次可把预估口径放宽到 10-17 分钟）
- delta 非零：players +3、trades +5、news +16、posts +8、lhb_stocks +40、lhb_seats +253、lhb_depts +204、lhb_inst +32、stock_holders +662、stock_holder_num +136、lhb_sector_focus +4
- delta 为 0：positions、stock_names
- after：players 249 / positions 271 / trades 107 / news 1155 / posts 1716
- 无 _err，error=null，wal_checkpoint=true
- 日志预期噪声（非故障）：雪球代理不可达 127.0.0.1:53269 多次降级直连；eastmoney/news=0；xueqiu players/positions=0（未配置 players 目标页 + player_page_tpl，属已知跳过）
- 运行注意：脚本写 stdout 最后一行才输出汇总 JSON；status.json 与 stdout 内容一致，读文件即可

### 2026-09-30 16:30:42
- 命令：`.venv/Scripts/python.exe auto_update.py`（Bash 直接 run_in_background，task UE3fga）
- 结果：ok=true，returncode=0，seconds=1027（17分07秒；连续两次实测 1003s/1027s，可把预估口径固定为 17 分钟）
- delta 非零：players +1、trades +5、news +13、posts +19、lhb_stocks +148、lhb_seats +1157、lhb_depts +997、lhb_inst +71、stock_holders +2267、stock_holder_num +459
- delta 为 0：positions、stock_names、lhb_sector_focus
- after：players 280 / positions 271 / trades 222 / news 1852 / posts 2807
- 无 _err，error=null，wal_checkpoint=true；lhb returncode=0，库内最新交易日 2026-09-30
- 预期噪声照旧：雪球代理 127.0.0.1:53269 不可达降级直连多次；eastmoney news=0；xueqiu players/positions=0

### 2026-10-03 00:27:43
- 命令：`.venv/Scripts/python.exe auto_update.py`（Bash run_in_background，task DICoKY）
- 结果：ok=true，returncode=0，seconds=1028（17分08秒；三次实测 1003/1027/1028s，预估口径稳定为 ~17 分钟）
- delta 非零：players +5、trades +11、news +142、posts +312、lhb_stocks +35、lhb_seats +316、lhb_depts +192、lhb_inst +11、stock_holders +858、stock_holder_num +180
- delta 为 0：positions、stock_names、lhb_sector_focus
- after：players 286 / positions 271 / trades 253 / news 2200 / posts 3379
- 无 _err，error=null，wal_checkpoint=true；lhb returncode=0，库内最新交易日 2026-09-30
- 预期噪声照旧：雪球代理 127.0.0.1:53269 不可达降级直连多次（本次日志显示"已加载雪球 cookie 18 条"，非故障）；xueqiu players/positions=0（未配置目标页）；taoguba 正常返回
- 阻塞等待节奏：两次 TaskOutput(block=true,timeout=600000) 即拿到结果，无需第三次

### 2026-10-07 16:31:04
- 命令：`.venv/Scripts/python.exe auto_update.py`（Bash run_in_background，task ZfTjDO）
- 结果：ok=true，returncode=0，seconds=994（16分34秒；四次实测 1003/1027/1028/994s，口径固定 ~17 分钟）
- delta 非零：players +1、trades +1、news +15、posts +26；其余全 0（positions、lhb_*、stock_holders 等）
- after：players 311 / positions 271 / trades 287 / news 2673 / posts 4238
- 无 _err，error=null，wal_checkpoint=true；lhb returncode=0，但"目标 2026-10-02~10-07 命中交易日 0 个、库内最新交易日仍 = 2026-09-30"
- 预期噪声照旧：雪球代理 127.0.0.1:53269 不可达降级直连 + 加载 cookie 18 条
- 本次增量明显偏小（前几次 posts +312），国庆假期非交易日是主因；news 有 2026-10-07 12:51 的publish_time 属正常（新闻不随休市停更）
- **语义闸门新增 FAIL**：F2 `rate_snap.trade_date` 库内最大 2026-10-06 晚于基准最近交易日 2026-09-30
  - 只读复核：rate_snap 三个分组 trade_date == created_at（10-06/10-05 各 30 行、10-02 有 17 行），即 trade_date 被写成**抓取运行日**而非真实交易日；且 lhb 判定 10-02~10-07 命中交易日 0 个 → 这几天是国庆休市日，属真·日期戳伪造
  - 修法建议（未动代码）：写入层加交易日闸门前置拦截，或 trade_date 改由底层 rate 数据源的真实日期派生，不要用 now()
- WARN=9：T2 服务未起跳过 payload 实测 ×1（环境性，非代码问题）；C1 前后端各自格式化「两把尺」×8（情绪/胜率/值得率/换手率/收益率/pct/仓位/净值）
- 待办（等王总确认再动）：F2 交易日期戳闸门、C1 八项数值口径收敛

### 2026-10-09 16:31:05
- 命令：`.venv/Scripts/python.exe auto_update.py`（Bash run_in_background，task mY8TvH）
- 结果：ok=false，崩溃，未落盘 returncode（NameError），seconds≈1062（17分42秒）
- 崩溃点：auto_update.py 第183行 `"sector_chain": chain` 引用未定义变量 `chain`（疑似编辑删/改名残留），收尾拼状态字典时 NameError；**未写出 auto_update_status.json**（文件仍 stale 为 10-07），抓取数据已落库（_wal_checkpoint 先于崩溃执行）
- 库内当前条数（只读复核，因 status.json stale）：players 326 / positions 271 / trades 348 / news 3458 / posts 5204（较 10-07 after ≈ +15 / +0 / +61 / +785 / +966）
- 语义闸门审计：FAIL=3（均 F1 时效断档，今日 2026-10-09 被审为交易日故不降级）：board_flow_daily.trade_date→10-08(滞后1天)、rate_snap.trade_date→10-06(滞后2天)、sector_chain.trade_date→10-08(滞后1天)；WARN=9（C1 两把尺×8：情绪/胜率/值得率/换手率/收益率/pct/仓位/净值；D1 app.js:6864 写死「当日/今日」）
- 待办（须先修代码再跑）：① auto_update.py 删除或补全 `chain` 引用；② F1 三表补采或确认数据源未披露后加免检名单；③ C1/D1 口径收敛

## 可复用要点
- Bash 跑 Windows exe 用正斜杠路径 + 双引号，勿用 cd（本机 cd/ls 已损坏，但 ls 带绝对路径实测可用）
- 前台 600s 不够，必然转后台 → 直接用 TaskOutput(block=true, timeout=600000) 等通知，不要 sleep 轮询
- 实测经验（2026-09-30）：即使 run_in_background=true 直接起后台，仍需连做两次 600s 阻塞等待（17 分钟 > 单次 600s 上限）；期间系统会提示"不要轮询"，但对无人值守自动化必须阻塞等到结果，属必要等待而非无效轮询
- 无需 present_files（不产出交付文件）
