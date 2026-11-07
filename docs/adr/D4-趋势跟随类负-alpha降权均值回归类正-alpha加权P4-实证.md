## D4 趋势跟随类负 alpha，降权；均值回归类正 alpha，加权（P4 实证）
- **背景**：`signal_backtest` 全量 51 行显示 均值回归 / 超卖类（RSI6超卖、跌破BOLL下轨）正 alpha（sharpe 1.1+），趋势跟随类（Supertrend上、放量、MACD金叉）全负（放量 sharpe −1.2）。
- **结论**：信号不等权。信号权重由 `signal_backtest` 胜率 / 超额驱动（见 `viewer/config.py` 的 `SIGNAL_WEIGHTS`），定期（季度）重算；趋势跟随类默认降权 / 反用。
- **影响模块**：漏斗加权、策略模板启停（B 阶段消费 `config.SIGNAL_WEIGHTS`）。
- **推翻条件**：换市场阶段（如强趋势市）重算后 alpha 反转，则调整权重。
