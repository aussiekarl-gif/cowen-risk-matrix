"""
cowen_risk — open-source, transparent re-implementation of Benjamin Cowen's
"On-Chain Metrics Historical Risk" composite (Into The Cryptoverse).

Pipeline: nine metrics -> per-metric 0–1 risk score (historical percentile of
the current value) -> weighted composite "Total On-Chain Risk" (0–1) ->
dynamic-DCA risk matrix.

Import raw series as CSV with any of these columns (see README.md for the
export instructions per source):

    date, market_cap, realized_cap,
    puell_multiple            # or supply: daily_issuance_usd (derived below)
    transaction_fees_usd      # daily USD value of fees paid
    terminal_price            # or: cdd_cumulative_usd + market_age_days
                              #     + circulating_supply (derived below)
    thermocap_usd             # cumulative miner revenue (USD) — CoinMetrics "ThermoCap"
    miner_cap_usd             # value of coins held by miners
    rhodl_ratio               # RHODL Ratio — CoreCharts/Glassnode export
    supply_in_profit_pct      # Supply in Profit % — CoreCharts export
    circulating_supply

Run:    pip install -r requirements.txt && streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------
# All nine metrics are "risk-up": a higher raw value means higher risk.
# (Cowen's private 0–1 mapping is approximated below by the historical
# percentile recipe.)

METRICS: dict[str, dict] = {
    "PuellMultiple": {
        "label": "Puell Multiple",
        "formula": "issuance_usd / rolling(365).mean(issuance_usd)",
        "default_weight": 1.0,
    },
    "MVRV": {
        "label": "MVRV",
        "formula": "market_cap / realized_cap",
        "default_weight": 1.0,
    },
    "MVRVZScore": {
        "label": "MVRV Z-Score",
        "formula": "(market_cap − realized_cap) / std(market_cap)",
        "default_weight": 1.0,
    },
    "TransactionFees": {
        "label": "Transaction Fees",
        "formula": "transaction_fees_usd  (fee spikes vs historical norms)",
        "default_weight": 1.0,
    },
    "TerminalPrice": {
        "label": "Terminal Price",
        "formula": "terminal_price | (cdd_cumulative_usd / (supply × age)) × 21e6",
        "default_weight": 1.0,
    },
    "MCTC": {
        "label": "MCTC",
        "formula": "market_cap / thermocap_usd",
        "default_weight": 1.0,
    },
    "mCTC": {
        "label": "mCTC",
        "formula": "miner_cap_usd / thermocap_usd",
        "default_weight": 1.0,
    },
    "RHODL": {
        "label": "RHODL Ratio",
        "formula": "rhodl_ratio  (realized value of 1-week coins ÷ 1y–2y coins × market age)",
        "default_weight": 0.0,   # Cowen: 0 weight (less reliable signal)
    },
    "SupplyP/L": {
        "label": "Supply P/L",
        "formula": "supply_in_profit_pct",
        "default_weight": 0.0,   # Cowen: 0 weight (less reliable signal)
    },
}

# Interpretation notes — one per metric (direction: higher raw value = higher risk, all nine).
METRIC_NOTES: dict[str, str] = {
    "PuellMultiple": (
        "Miner-profitability oscillator: daily issuance value (USD) ÷ 365-day moving average. "
        "High (≥ ~2× normal) = miners' revenue running above yearly average — historically precedes macro tops; "
        "Low (< ~1) = miners losing money — bottoms."
    ),
    "MVRV": (
        "Market Cap ÷ Realized Cap — valuation gap above the supply's average entry basis. "
        "High (> ~2.5) = late-cycle stretch; low (~1–1.2) = near bottoms."
    ),
    "MVRVZScore": (
        "(Market Cap − Realized Cap) ÷ σ(Market Cap) — same gap in standardized dollar terms; "
        "sharper regime-boundary signal than the ratio."
    ),
    "TransactionFees": (
        "Average fee pressure (USD value of fees per day, or sat/vB) vs historical norms; "
        "spikes during bull phases raise risk (fees are part of miner revenue)."
    ),
    "TerminalPrice": (
        "Terminal Price = Transferred Price × 21,000,000 (Transferred Price = CDD ÷ (supply × market age)). "
        "When terminal price ≈ spot price, tops have formed."
    ),
    "MCTC": (
        "Market Cap ÷ Thermocap (cumulative miner revenue). Historically > ~10 is top-like; "
        "dropping below ~2 can mark bottoms (CoinMetrics)."
    ),
    "mCTC": (
        "Miner Cap ÷ Thermocap — value of coins held by miners vs total thermocap. "
        "High = miners hold unusually large value; low = distributed/sold."
    ),
    "RHODL": (
        "Realized Value of 1-week coins ÷ 1y–2y coins × market age; late-cycle heat = recent "
        "turnover dominating mature held value. Cowen sets weight 0 (less reliable signal)."
    ),
    "SupplyP/L": (
        "% of circulating supply priced above last-moved price; high = most coins above cost basis "
        "(late cycle). Cowen sets weight 0."
    ),
}

# Risk tiers: risk band → DCA multiplier → note (Cowen method, CryptoGates/Cryptonews).
RISK_TIERS: list[tuple[str, str, str]] = [
    ("0 – 0.1", "3x", "Floor / accumulation zone — most aggressive DCA (Cowen: bottoms often form once risk < 0.1)"),
    ("0.1 – 0.2", "2x", "Accumulation — DCA 2x"),
    ("0.2 – 0.3", "1x", "Early-cycle — DCA base level"),
    ("> 0.3", "0 (skip)", "Late-cycle — no new buys; keep cash"),
    ("> 0.5", "SELL", "High risk — sell or slow down"),
]


# ---------------------------------------------------------------------------
# Series derivation — transcribed from public sources (see README.md)
# ---------------------------------------------------------------------------

def derive_series(df: pd.DataFrame) -> dict[str, pd.Series]:
    s: dict[str, pd.Series] = {}

    # 1) Puell Multiple: Daily issuance value (USD) ÷ 365-day MA of it.
    if "puell_multiple" in df:
        s["PuellMultiple"] = pd.to_numeric(df["puell_multiple"])
    elif "daily_issuance_usd" in df:
        v = pd.to_numeric(df["daily_issuance_usd"])
        s["PuellMultiple"] = v / v.rolling(365, min_periods=365).mean()

    # 2)–3) MVRV and MVRV Z-Score (Glassnode/CoreCharts):
    #    MVRV = Market Cap / Realized Cap
    #    MVRVZScore = (Market Cap − Realized Cap) / σ(Market Cap)
    if {"market_cap", "realized_cap"}.issubset(df):
        mc = pd.to_numeric(df["market_cap"])
        rc = pd.to_numeric(df["realized_cap"])
        s["MVRV"] = mc / rc
        sd = mc.std()
        if sd is not None and sd > 0:
            s["MVRVZScore"] = (mc - rc) / sd

    # 4) Transaction Fees: average fee pressure (USD value of fees per day).
    if "transaction_fees_usd" in df:
        s["TransactionFees"] = pd.to_numeric(df["transaction_fees_usd"])

    # 5) Terminal Price (Checkmate/BlockHorizon):
    #    Transferred Price = CDD / (Existing Supply × Market Age)
    #    Terminal Price    = Transferred Price × 21,000,000
    if "terminal_price" in df:
        s["TerminalPrice"] = pd.to_numeric(df["terminal_price"])
    elif (
        {"cdd_cumulative_usd", "market_age_days"}.issubset(df)
        and "circulating_supply" in df
    ):
        cdd = pd.to_numeric(df["cdd_cumulative_usd"])
        supply = pd.to_numeric(df["circulating_supply"])
        age = pd.to_numeric(df["market_age_days"])
        s["TerminalPrice"] = (cdd / (supply * age)) * 21_000_000

    # 6) MCTC: Market Cap ÷ Thermocap (Cointelegraph/CoinMetrics RevAllTimeUSD).
    if {"market_cap", "thermocap_usd"}.issubset(df):
        s["MCTC"] = pd.to_numeric(df["market_cap"]) / pd.to_numeric(df["thermocap_usd"])

    # 7) mCTC: Miner Cap ÷ Thermocap. Miner Cap = value of coins held by miners
    #    (CoinMetrics SplyMiner0HopAllUSD + SplyMiner1HopAllUSD).
    if {"miner_cap_usd", "thermocap_usd"}.issubset(df):
        s["mCTC"] = pd.to_numeric(df["miner_cap_usd"]) / pd.to_numeric(df["thermocap_usd"])

    # 8) RHODL Ratio: (Realized Value of 1-week coins ÷ 1y–2y coins) × market age (days).
    if "rhodl_ratio" in df:
        s["RHODL"] = pd.to_numeric(df["rhodl_ratio"])

    # 9) Supply P/L: % of circulating supply whose current price > last-moved price.
    if "supply_in_profit_pct" in df:
        s["SupplyP/L"] = pd.to_numeric(df["supply_in_profit_pct"])

    return s


# ---------------------------------------------------------------------------
# Risk scoring and compositing
# ---------------------------------------------------------------------------

def risk_score(series: pd.Series) -> float:
    """0–1 risk for a risk-up metric: the historical percentile of the
    current value (fraction of all history at/below it)."""
    s = series.dropna()
    if s.size < 2:
        return 0.5
    return float((s <= s.iloc[-1]).mean())


def band_label(risk: float) -> str:
    if risk < 0.1:
        return "Floor — best accumulation zone (Cowen: bottoms often sit below 0.1)"
    if risk < 0.2:
        return "Accumulation"
    if risk < 0.3:
        return "Early-cycle — continue DCA at base"
    if risk < 0.5:
        return "Late-cycle — slow DCA, build cash"
    return "High risk — sell or slow down"


def dca_action(risk: float) -> tuple[str, str]:
    """Dynamic-DCA tiers (Cowen method, CryptoGates example)."""
    if risk > 0.5:
        return "Sell / slow down", "risk > 0.5 — trim or halt buys, take profits."
    if risk > 0.3:
        return "Skip buys", "risk > 0.3 — no new DCA this period; keep cash."
    if risk > 0.2:
        return "Buy 1x", "risk in (0.2, 0.3] — base DCA amount."
    if risk > 0.1:
        return "Buy 2x", "risk in (0.1, 0.2] — double the buy."
    return "Buy 3x", "risk ≤ 0.1 — triple the buy (accumulation zone)."


def dca_matrix_table() -> pd.DataFrame:
    rows = [
        ("0 – 0.1", "3x", "floor/accumulation — most aggressive buying (Cowen: bottoms often form once risk < 0.1)"),
        ("0.1 – 0.2", "2x", "risk 0.1–0.2: DCA 2x (cryptogates example)"),
        ("0.2 – 0.3", "1x", "risk 0.2–0.3: DCA 1x — base amount"),
        ("> 0.3", "0", "risk > 0.3: skip buys — keep cash"),
        ("> 0.5", "SELL", "risk above 0.5: 'area to sell or slow down' (CryptoGates)"),
    ]
    return pd.DataFrame(
        rows,
        columns=["risk band", "DCA multiplier", "note"],
    )


def fmt(x) -> str:
    if pd.isna(x):
        return "n/a"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    return f"{x:.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Streamlit app
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Cowen On-Chain Risk (transparent replica)", page_icon="₿")
    st.title("Cowen On-Chain Metrics Historical Risk")
    st.caption("Open-source replica of the risk composite — formulas & sources in README.md")

    uploaded = st.file_uploader(
        "Import raw series as CSV (export from Glassnode Studio / CoinMetrics / Coinglass Pro / CoreCharts, see README)",
        type=["csv"],
    )
    if uploaded is None:
        st.info(
            "Import a CSV of raw on-chain series to start. See README.md for the "
            "expected columns and export instructions."
        )
        return

    df = pd.read_csv(uploaded)
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"], errors="coerce").sort_values()
        df = df.set_index("date")

    series = derive_series(df)
    available = [k for k, s in series.items() if s.dropna().size >= 2]
    if not available:
        st.warning(
            "None of the expected columns were found. Expected at least one of: "
            "market_cap+realized_cap, puell_multiple|daily_issuance_usd, rhodl_ratio, ..."
        )
        return

    st.success(
        "Computed: " + ", ".join(METRICS[k]["label"] for k in available)
    )

    # ---- weights: Cowen's sliders (weight 0 = excluded) ------------------
    st.subheader("Weights (screenshot sliders — 0 = excluded)")
    weights: dict[str, float] = {}
    for k in available:
        weights[k] = st.slider(
            METRICS[k]["label"], 0.0, 1.0, METRICS[k]["default_weight"],
            step=0.05, key=f"w_{k}",
        )
    for k in METRICS:
        if k not in weights:
            weights[k] = 0.0

    # ---- per-metric risk -------------------------------------------------
    risks = {k: risk_score(series[k]) for k in available}
    active = [k for k in available if weights[k] > 0]
    if not active:
        st.warning("All weights are 0 — drag at least one slider above 0 to compute risk.")
        return
    den = sum(weights[k] for k in active)
    total = sum(weights[k] * risks[k] for k in active) / den

    st.subheader("Per-metric risk (historical percentile of the current value)")
    rows = []
    for k in available:
        cur = series[k].dropna().iloc[-1]
        rows.append({
            "metric": METRICS[k]["label"],
            "formula": METRICS[k]["formula"],
            "current": fmt(cur),
            "risk": f"{risks[k]:.3f}",
            "weight": f"{weights[k]:.2f}",
            "contribution": (
                f"{weights[k] * risks[k] / den:.3f}" if k in active else "—"
            ),
        })
    st.table(pd.DataFrame(rows))

    # ---- total risk -------------------------------------------------------
    st.subheader("Total On-Chain Risk")
    st.metric("Total Risk (0 = floor, 1 = top)", f"{total:.3f}")
    st.caption(band_label(total))

    # ---- dynamic DCA matrix + interpretation ------------------------------
    st.subheader("Dynamic DCA (risk matrix)")
    amount, note = dca_action(total)
    st.markdown(f"**Current risk: {total:.3f} → {amount}** — {note}")
    st.table(dca_matrix_table())

    st.subheader("Risk interpretation & per-metric notes")
    st.markdown(f"**Current tier:** {band_label(total)}")
    st.table(pd.DataFrame(RISK_TIERS, columns=["risk band", "DCA multiplier", "meaning"]))
    with st.expander("Per-metric notes (all 9)"):
        st.table(pd.DataFrame(
            [{"metric": METRICS[k]["label"], "note": METRIC_NOTES[k]} for k in METRICS],
            columns=["metric", "note"],
        ))


if __name__ == "__main__":
    main()
