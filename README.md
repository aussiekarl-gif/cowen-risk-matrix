# cowen_risk — open-source replica of Benjamin Cowen's On-Chain Metrics Historical Risk

A transparent re-implementation of the "On-Chain Metrics Historical Risk" composite
from [Into The Cryptoverse](https://intothecryptoverse.com) (9 sliders + legend,
puell multiple / MVRV / MVRV Z-Score / transaction fees / terminal price / MCTC /
mCTC / RHODL / supply P&L).

Cowen's own normalization is private; this app replaces it with a transparent
recipe: **each metric's risk = its historical percentile among all its values**
(risk-up: fraction of history at/below the current value), then

```
Total On-Chain Risk = Σ w_i · r_i / Σ w_i      (w_i = slider weight, 0–1)
```

which keeps the 0–1 scale of Cowen's gauge. The **dynamic-DCA risk matrix** below
is Cowen's (per CryptoGates / his videos).

## Formulas (all verified from public sources)

| # | Metric | Formula (Cowen / public sources) |
|---|--------|----------------------------------|
| 1 | **Puell Multiple** | Daily issuance value of new BTC (USD) ÷ 365-day moving average of daily issuance value. (Glassnode, Fidelity Digital Assets) |
| 2 | **MVRV** | Market Cap ÷ Realized Cap (value of each coin at last move × amount; Glassnode, CoreCharts) |
| 3 | **MVRV Z-Score** | (Market Cap − Realized Cap) ÷ σ(Market Cap) (Glassnode, CoreCharts) |
| 4 | **Transaction Fees** | Average fee pressure on the network (USD value of fees per day, sat/vB or USD) vs historical norms; spikes = risk (user description + Galaxy/CoreCharts fee data) |
| 5 | **Terminal Price** | Transferred Price × 21,000,000, where Transferred Price = CDD ÷ (Existing Supply × Market Age). (Checkmate, quoted by Glassnode TXMC experiment; BlockHorizon writes it as Total CDD ÷ Real Supply × 21) |
| 6 | **MCTC** | Market Cap ÷ Thermocap. Thermocap = cumulative miner revenue = sum of daily miner revenue in USD (Cointelegraph, CoinMetrics `RevAllTimeUSD`) |
| 7 | **mCTC** | Miner Cap ÷ Thermocap. Miner Cap = value of coins held by miners (CoinMetrics: `SplyMiner0HopAllUSD` + `SplyMiner1HopAllUSD`) |
| 8 | **RHODL Ratio** | (Realized Value of 1-week coins ÷ Realized Value of 1y–2y coins) × Market Age in days (CoreCharts, TradingView, Philip Swift). Cowen sets weight 0 — "less reliable or overly persistent signals." |
| 9 | **Supply P/L** | % of circulating supply whose current price > the price when they last moved on-chain (CoreCharts). Cowen sets weight 0. |

## How the composite maps to 0–1

All nine metrics are "risk-up" (higher raw value = higher risk), so the
percentile recipe is `r_i = fraction of history ≤ current_i`. The total is
the **normalized weighted mean** `Σ w_i r_i / Σ w_i`:

- **0.0 – 0.1 (floor):** best accumulation zone (Cowen: "a bottom usually forms once his on-chain risk metric falls below 0.1").
- **0.1 – 0.2:** accumulation / early DCA-up.
- **0.2 – 0.3:** early-cycle — keep DCA base level.
- **0.3 – 0.5:** late-cycle warning — slow DCA / add to cash.
- **> 0.5:** high risk — sell or slow down.

## Dynamic DCA — the risk matrix (Cowen method, per CryptoGates)

| risk band | DCA multiplier | note |
|---|---|---|
| 0 – 0.1 | 3x | most aggressive buying |
| 0.1 – 0.2 | 2x | |
| 0.2 – 0.3 | 1x | base amount |
| > 0.3 | 0 (skip) | "no buy, keep the cash" |
| > 0.5 | SELL | "area to sell or slow down" |

## Importing raw series (CSV)

Put any of these columns in a CSV and upload it in the app (one row per date):

| column | meaning |
|---|---|
| `date` | timestamp (optional; pandas default index used otherwise) |
| `market_cap` | BTC market cap, USD — Glassnode Studio "Market Cap" |
| `realized_cap` | Realized cap, USD — Glassnode Studio "Realized Cap" |
| `puell_multiple` | or supply `daily_issuance_usd` → Puell derived in-app (issuance ÷ 365d MA) |
| `transaction_fees_usd` | daily USD value of fees paid |
| `terminal_price` | or `cdd_cumulative_usd` + `market_age_days` + `circulating_supply` → derived in-app: (CDD ÷ (supply × age)) × 21,000,000 |
| `thermocap_usd` | cumulative miner revenue, USD — CoinMetrics `ThermoCap` / `RevAllTimeUSD` |
| `miner_cap_usd` | coins held by miners (CoinMetrics `SplyMiner0HopAllUSD` + `SplyMiner1HopAllUSD`) |
| `rhodl_ratio` | RHODL Ratio — CoreCharts export / TradingView |
| `supply_in_profit_pct` | Supply in Profit % — CoreCharts export |
| `circulating_supply` | BTC circulating supply |

- **Glassnode Studio:** export the needed series (Market Cap, Realized Cap,
  Puell Multiple, RHODL Ratio, Thermocap, Thermocap Multiple, etc.) as CSV,
  concatenate/merge on `date`, upload.
- **CoinMetrics API:** fetch `MCTC`, `ThermoCap`, `MinerCap`, `RevAllTimeUSD`
  etc. into a CSV.
- **Coinglass Pro:** export Terminal Price / RHODL / Average Fee data.
- **CoreCharts:** export "Supply in Profit and Loss", "RHODL Ratio",
  "MVRV Z-Score", "Puell Multiple".

## Run the app

```bash
python -m venv .venv && source .venv/bin/activate   # or on Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Prior art / other implementations

- **`KannaKim/BitcoinQuant` (GitHub)** — risk-metric tools for trading Bitcoin,
  "calculated based on logarithmic regression" (a different model inspired by Cowen).
- **ABYSS Index** — open 0–1 cycle risk: `ln(price ÷ 2y-MA) × (days since 2012-08-16)^0.395`,
  min/max-normalized (0 = floor, 1 = top).

## Disclaimer

For educational purposes only — not financial advice. Cowen's risk gauge is a
macro-cycle timing tool, not a crystal ball; even his own track record has
drawdowns (a March 2021 flattening, November tops).
