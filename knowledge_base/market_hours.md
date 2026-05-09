# Futures Market Hours

For the instruments in this repo, treat the futures market as operating on the
standard CME/Globex cycle in **America/New_York time**:

- Weekly open: **Sunday 6:00 p.m. ET**
- Weekly close: **Friday 5:00 p.m. ET**
- Daily maintenance break: **5:00 p.m. ET to 6:00 p.m. ET** Monday through Thursday

Operational rules for this repo:

- Do not open new trades when the market is closed.
- Do not open new trades near the daily close.
- Flatten open positions before the daily close cut-off.
- The live trading crew only evaluates entries during the configured London and
  NY windows, but the broader futures market itself is open outside those
  windows on Globex.

Prop-firm-safe trading rules for the live TradingCrew:

- Primary entry window: **9:30 a.m. ET to 11:30 a.m. ET**
- Secondary entry window: **2:00 p.m. ET to 3:30 p.m. ET**
- Hard flat deadline: **4:45 p.m. ET**
- Daily reset reference: **5:00 p.m. ET**

The ResearchCrew can run any day and any time because it does not place trades.

Important distinction:

- **Market open** means the exchange is available for futures trading.
- **Strategy session window** means this repo is allowed to enter trades.
- Both conditions must be true to allow a new trade.
