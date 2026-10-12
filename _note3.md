## 17:2x 名单口径已定 + players 真相（关键）

- 王总选定：第一批「自己抓」名单 = **股吧入围 200+ 选手**（不是抖音 21 博主、不是 Excel 5 位）。飞书授权他去确认位置，暂挂。
- **players 表真相（关键）**：326 行，全是**论坛博主**，分布 taoguba 140 / eastmoney 79 / sina 52 / xueqiu 34 / ths 18 / jisilu 3。
  字段：site,name,channel,rank_position,ranking_score,is_top_player,source_url(tgb.cn/blog/xxx),tier,tier_weight,influence,platform,followers,real_holding,axis。
  昵称样例：柯基吃柠檬 / 困困小柚子 / 小马总是我 / 三晋才子 / 骗我请注意次数 / 航城大道小陈。
- **这批人没有任何抖音/小红书/微博 账号或 ID**（platform 字段存的是论坛站名，不是社媒平台）。
  所以「按名单去各平台抓」= **跨平台身份定位**（昵称 → 账号 → 主页 ID），不是抓现成的号。规模 326 × 6 平台 ≈ 2000 次搜索，属消耗型。
- 相关：persons 表 2854（name/mention_count/role）；person_scores 739。
- 拟定方案：Step1 抽 ~20 人跑抖音「昵称→账号」定位试点，先报**命中率** → Step2 建 `player_accounts` 映射表 + 全量半自动定位 → Step3 抓取（抖音复用 CDP 链路，其他平台新建）→ Step4 回到 ABCD。
- 已问王总：试点是否现在开跑。
