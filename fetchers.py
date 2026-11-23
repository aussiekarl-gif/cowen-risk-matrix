"""
Free live data fetchers — anonymous/free tiers only (Nov 2026).

Primary source — BGeometrics (bitcoin-data.com):
  * "Free plan: No registration needed — just call the API directly."
  * Endpoints used here (all GET https://bitcoin-data.com/v1/<endpoint>):
      - market-cap, mvrv, mvrv-zscore, puell-multiple, terminal-price,
        thermo-cap, profit-loss (fraction in [0,1]), realized-cap-hodl-waves
        (per-band realized cap for the RHODL approximation).
  * Limit: 10 requests / hour (hourly counter on your IP).

Optional source — CryptoQuant (api.cryptoquant.com):
  * Free-account community endpoints, enabled by setting the CQ_API_KEY env
    var. Market-indicator metrics on CryptoQuant require Professional+ —
    those are NOT used here.

Streamlit caches the live bundle (st.cache_data, TTL=1800s) so the
BGeometrics 10-requests-per-hour limit is respected across app reruns.
"""

from __future__ import annotations

import concurrent.futures
import os
from typing import Optional

import numpy as np
import pandas as pd
import requests
import streamlit as st

BASE = "https://bitcoin-data.com/v1"

# Canonical columns produced by fetch_live_df() — consumed by app.derive_series().
# endpoint: canonical column emitted.
BG_SERIES = {
    "mvrv": "mvrv",
    "mvrv-zscore": "mvrv_zscore",
    "puell-multiple": "puell_multiple",
    "terminal-price": "terminal_price",
    "thermo-cap": "thermo_cap",
    "profit-loss": "profit_loss",
    "market-cap": "market_cap",
}

# Candidate field names accepted when parsing BGeometrics responses
# (the API uses e.g. mvrvZscore / marketCap; different endpoints name
# values slightly differently).
BG_CANDIDATES = {
    "mvrv": ["mvrv"],
    "mvrv-zscore": ["mvrvZscore", "mvrv_zscore", "mvrvZScore", "mvrv_z", "zscore"],
    "puell-multiple": ["puellMultiple", "puell_multiple", "puell"],
    "terminal-price": ["terminalPrice", "terminal_price", "terminalprice"],
    "thermo-cap": ["thermoCap", "thermocap", "thermo_cap"],
    "profit-loss": ["profitLoss", "profit_loss"],
    "market-cap": ["marketCap", "market_cap"],
}

BTC_GENESIS = pd.Timestamp("2009-01-03")

# RHODL approximation: (RealizedValue(1-week band) / RealizedValue(1y–2y band))
# × market age in days.  "1-week band" is proxied by the band that starts at
# 1 week (age_1w_1m); the "1y–2y band" is age_1y_2y.
RHODL_RECENT_BAND = "age_1w_1m"
RHODL_MATURE_BAND = "age_1y_2y"

# CryptoQuant community enrichment: canonical column -> path under
# /v2/community/ when CQ_API_KEY is set.  Fills only what BGeometrics lacks.
CQ_COMMUNITY_FILL = {
    "mvrv_zscore": "bitcoin-mvrv-z-score",
}


# ---------------------------------------------------------------------------
# JSON / record parsing
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: int = 20, headers: dict | None = None):
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        if r.status_code != 200:
            return None  # includes 429 (rate-limited)
        return r.json()
    except Exception:
        return None


def _parse_records_df(payload) -> Optional[pd.DataFrame]:
    """Parse BGeometrics responses of the form [{d, <value fields>}, ...]
    into a DataFrame indexed by date."""
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("result") or payload.get("body")
    if not isinstance(payload, list):
        return None
    records = [r for r in payload
               if isinstance(r, dict) and any(k in r for k in ("d", "date", "timestamp", "unixTs"))]
    if not records:
        return None
    df = pd.DataFrame(records)
    date_col = next((c for c in ("d", "date", "timestamp") if c in df.columns), None)
    if date_col is None:
        return None
    df = df.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    if df.empty:
        return None
    return df.set_index("date")


def _series_from_records(payload, candidates: list[str]) -> Optional[pd.Series]:
    df = _parse_records_df(payload)
    if df is None:
        return None
    col = next((c for c in candidates if c in df.columns), None)
    if col is None:
        numeric = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
        col = numeric[0] if numeric else None
    if col is None:
        return None
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    s.name = "value"
    return s


def _fetch_bgeometrics_series(endpoint: str, candidates: list[str]) -> Optional[pd.Series]:
    return _series_from_records(_get_json(f"{BASE}/{endpoint}"), candidates)


def fetch_bgeometrics_hodl_waves() -> Optional[pd.DataFrame]:
    """Realized cap by HODL bands -> for the RHODL approximation."""
    return _parse_records_df(_get_json(f"{BASE}/realized-cap-hodl-waves"))


def _rhodl_from_hodl(hodl: pd.DataFrame) -> Optional[pd.Series]:
    if RHODL_RECENT_BAND not in hodl.columns or RHODL_MATURE_BAND not in hodl.columns:
        return None
    recent = pd.to_numeric(hodl[RHODL_RECENT_BAND], errors="coerce")
    mature = pd.to_numeric(hodl[RHODL_MATURE_BAND], errors="coerce")
    age_days = (hodl.index - BTC_GENESIS).days.astype(float)
    rhodl = pd.Series(
        np.where(mature > 0, (recent * age_days) / mature, np.nan),
        index=hodl.index,
    )
    return rhodl.dropna()


# ---------------------------------------------------------------------------
# CryptoQuant community (optional, free-account) enrichment
# ---------------------------------------------------------------------------

def fetch_cryptoquant_series(path: str) -> Optional[pd.Series]:
    """Fetch a CryptoQuant community endpoint (requires CQ_API_KEY env var)
    and extract the first date/value series found in the response."""
    key = os.environ.get("CQ_API_KEY")
    if not key:
        return None
    url = f"https://api.cryptoquant.com/v2/community/{path}"
    payload = _get_json(url, headers={"Authorization": f"Bearer {key}"})
    if payload is None:
        return None

    def extract(obj, depth=0):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            date_keys = ("date", "datetime", "day", "d")
            has_date = any(k in obj for k in date_keys)
            if has_date and any(isinstance(v, (int, float)) and not isinstance(v, bool)
                                for k, v in obj.items() if k not in date_keys):
                return obj
            for v in obj.values():
                r = extract(v, depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
            date_keys = ("date", "datetime", "day", "d")
            if any(any(k in r for k in date_keys) for r in obj[:50]):
                recs = [r for r in obj if isinstance(r, dict)]
                dates, vals = [], []
                for r in recs:
                    d = next((r[k] for k in date_keys if k in r), None)
                    v = next((v for k, v in r.items()
                              if k not in date_keys and isinstance(v, (int, float))
                              and not isinstance(v, bool)), None)
                    if d is not None and v is not None:
                        dates.append(d)
                        vals.append(v)
                if dates:
                    idx = pd.to_datetime(pd.Series(dates), errors="coerce").dropna()
                    df = pd.DataFrame({"v": vals}, index=pd.to_datetime(dates, errors="coerce"))
                    df = df.loc[~df.index.duplicated(keep="first")].sort_index()
                    return df["v"]
            for v in obj[:50]:
                r = extract(v, depth + 1)
                if r is not None:
                    return r
        return None

    s = extract(payload)
    if isinstance(s, pd.Series):
        return s.astype(float)
    if isinstance(s, pd.DataFrame) and "v" in s.columns:
        return s["v"].astype(float)
    return None


# ---------------------------------------------------------------------------
# Live bundle (cached)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_live_df() -> Optional[pd.DataFrame]:
    """Fetch the canonical live DataFrame. Returns None when nothing
    could be fetched (e.g. hourly rate limit hit)."""
    out: dict[str, pd.Series] = {}
    endpoints = dict(BG_SERIES)

    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as ex:
        futs = {
            ep: ex.submit(_fetch_bgeometrics_series, ep, BG_CANDIDATES[ep])
            for ep in endpoints
        }
        futs["realized-cap-hodl-waves"] = ex.submit(fetch_bgeometrics_hodl_waves)
        for ep, fut in futs.items():
            try:
                s = fut.result()
            except Exception:
                s = None
            if ep == "realized-cap-hodl-waves":
                if s is not None:
                    rhodl = _rhodl_from_hodl(s)
                    if rhodl is not None and not rhodl.empty:
                        out["rhodl_ratio"] = rhodl
                continue
            col = endpoints[ep]
            if s is not None and not s.empty:
                out[col] = s

    # Optional CryptoQuant community enrichment (needs CQ_API_KEY).
    for col, cq_path in CQ_COMMUNITY_FILL.items():
        if col not in out:
            s = fetch_cryptoquant_series(cq_path)
            if s is not None and not s.empty:
                out[col] = s

    df = pd.DataFrame(out)
    if "supply_in_profit_pct" not in df.columns and "profit_loss" in df.columns:
        df["supply_in_profit_pct"] = df["profit_loss"] * 100.0
    df = df.dropna(how="all", axis=1)
    if df.empty:
        return None
    return df
