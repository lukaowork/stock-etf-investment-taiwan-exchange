"""EPS 估值與合理價推導（純邏輯模組，不含 Streamlit UI）。

依 AGENTS.md 單一職責原則，本檔只負責：
  1. 由逐季 EPS 建立 TTM（近四季累計）EPS 與歷史本益比序列
  2. 動態 EPS 基準計算（TTM / 單季季化 / 月營收動態加權）
  3. 由歷史本益比 × 使用者選定基準推導合理價三線

UI 呈現（st.metric / st.plotly_chart / st.dataframe / st.download_button）
一律留在 app.py，以維持業務邏輯與 UI 解耦。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 常數：EPS 基準選項與門檻（規格定義，集中管理避免字串散落）
# --------------------------------------------------------------------------- #
MIN_PE_POINTS = 30        # 計算合理價所需的最少有效本益比樣本數

# ---- EPS 動態基準切換器（Sidebar 下拉選單選項）----
BASELINE_TTM = "1. 靜態 TTM 基準 (TTM EPS)"
BASELINE_RUNRATE = "2. 單季季化基準 (Run-rate EPS)"
BASELINE_REVENUE = "3. 月營收動態加權基準 (Monthly Revenue Dynamic EPS)"
BASELINE_OPTIONS = (BASELINE_TTM, BASELINE_RUNRATE, BASELINE_REVENUE)

# 經營槓桿 Alpha（僅月營收動態加權基準使用）
ALPHA_MIN = 0.5
ALPHA_MAX = 2.0
ALPHA_DEFAULT = 1.0


# --------------------------------------------------------------------------- #
# 工具函式
# --------------------------------------------------------------------------- #
def _naive_timestamp(value) -> pd.Timestamp:
    """統一轉為 tz-naive 的 Timestamp，避免時區索引比對失敗。"""
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return pd.NaT
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


def _to_naive_index(index) -> pd.DatetimeIndex:
    """將任意日期索引統一為 tz-naive 的 DatetimeIndex。"""
    di = pd.DatetimeIndex(index)
    if di.tz is not None:
        # .values 一律回傳 UTC 的 datetime64，必定可轉為 naive
        di = pd.DatetimeIndex(di.values)
    return di


# --------------------------------------------------------------------------- #
# 1. TTM EPS 與歷史本益比
# --------------------------------------------------------------------------- #
def build_ttm_eps_series(price_index, quarterly_eps) -> pd.Series:
    """將逐季 EPS 換算成每個交易日的 TTM（近四季累計）EPS。

    Parameters
    ----------
    price_index : array-like
        交易日索引（K 線日期）。
    quarterly_eps : dict
        {財報公告日字串: 該季 EPS}。以「公告日」而非「財期末」計算，
        只使用當下市場已知的資訊，避免前視偏差。

    Returns
    -------
    pd.Series
        與 price_index 對齊的 TTM EPS；尚未累積滿 4 季的日期為 NaN。
        任一日期若無資料或樣本不足 4 季，一律回傳 NaN（該日不納入本益比）。
    """
    idx = _to_naive_index(price_index)
    out = pd.Series(np.nan, index=idx, dtype=float)
    if not quarterly_eps:
        return out

    rows = []
    for raw_date, raw_eps in quarterly_eps.items():
        try:
            ts = _naive_timestamp(raw_date)
            val = float(raw_eps)
        except Exception:  # 防禦性：外部財報日期格式異常時略過該筆
            continue
        if pd.isna(ts) or not np.isfinite(val):
            continue
        rows.append((ts, val))

    if len(rows) < 4:
        return out

    rows.sort(key=lambda item: item[0])
    report_index = pd.DatetimeIndex([item[0] for item in rows])
    eps_values = np.array([item[1] for item in rows], dtype=float)
    cum = np.cumsum(eps_values)

    # 每個交易日取「該日（含）之前已公告的最近 4 季」
    pos = report_index.searchsorted(idx, side="right") - 1
    ttm = np.full(len(idx), np.nan, dtype=float)
    ok = pos >= 3  # 至少要有 4 筆（索引 0~3）
    if ok.any():
        p = pos[ok]
        # p >= 4 時減去前 4 季的累計；p == 3 時直接取前 4 季總和
        prior = np.where(p >= 4, cum[np.clip(p - 4, 0, None)], 0.0)
        ttm[ok] = cum[p] - prior
    out.iloc[:] = ttm
    return out


def compute_pe_history(close: pd.Series, ttm_eps: pd.Series) -> pd.Series:
    """歷史本益比序列 = 收盤價 ÷ 當日 TTM EPS（僅保留 TTM EPS > 0 的交易日）。"""
    close_f = pd.to_numeric(close, errors="coerce").astype(float)
    eps_f = pd.to_numeric(
        ttm_eps.reindex(close_f.index), errors="coerce"
    ).astype(float)

    pe = pd.Series(np.nan, index=close_f.index, dtype=float)
    valid = eps_f.notna() & (eps_f > 0) & close_f.notna() & (close_f > 0)
    if valid.any():
        pe.loc[valid] = close_f.loc[valid] / eps_f.loc[valid]
    return pe


def summarize_quarterly_eps(quarterly_eps) -> dict:
    """整理逐季 EPS，回傳最新單季與近 4 季累積（TTM）資訊。

    回傳 {"latest_eps", "latest_date", "ttm_eps", "quarters"}；
    資料不足時對應值為 None / 0。
    """
    result = {
        "latest_eps": None,
        "latest_date": "",
        "ttm_eps": None,
        "quarters": 0,
    }
    if not quarterly_eps:
        return result

    rows = []
    for raw_date, raw_eps in quarterly_eps.items():
        try:
            ts = _naive_timestamp(raw_date)
            val = float(raw_eps)
        except Exception:  # 防禦性：外部財報日期格式異常時略過該筆
            continue
        if pd.isna(ts) or not np.isfinite(val):
            continue
        rows.append((ts, val))
    if not rows:
        return result

    rows.sort(key=lambda item: item[0])
    values = [item[1] for item in rows]
    result["quarters"] = len(values)
    result["latest_eps"] = values[-1]
    result["latest_date"] = rows[-1][0].strftime("%Y-%m-%d")
    if len(values) >= 4:
        result["ttm_eps"] = float(sum(values[-4:]))
    return result


def compute_baseline_eps(
    quarterly_eps,
    baseline: str,
    revenue_yoy_pct=None,
    alpha: float = ALPHA_DEFAULT,
) -> dict:
    """依使用者選定的基準計算「動態 EPS (Calculated EPS)」。

    1. TTM 基準       ：近四季累積已發布 EPS 總和
    2. 單季季化基準   ：最新單季 EPS × 4
    3. 月營收動態基準 ：最新單季 EPS × 4 × (1 + 近3個月營收YoY × Alpha)

    說明：yfinance 未提供台股月營收，故以「最近一季（＝近 3 個月）營收 YoY」
    作為動能係數；revenue_yoy_pct 傳入的是百分數（15.2 代表 15.2%）。

    回傳 {"eps", "label", "display", "detail", "warning", "alpha"}；
    無法計算時 eps 為 None。
    """
    stats = summarize_quarterly_eps(quarterly_eps)
    latest = stats["latest_eps"]
    label = str(baseline)
    warning = ""
    display = label

    if baseline == BASELINE_TTM:
        eps = stats["ttm_eps"]
        display = "靜態 TTM 基準"
        detail = (
            f"近 4 季累積 EPS（樣本 {stats['quarters']} 季）"
            if eps is not None
            else "已發布季報不足 4 季，無法計算 TTM"
        )

    elif baseline == BASELINE_RUNRATE:
        display = "單季季化基準 (Run-rate)"
        if latest is None:
            eps, detail = None, "查無已發布的單季 EPS"
        else:
            eps = latest * 4.0
            detail = f"最新單季 EPS {latest:.2f} × 4 = {eps:.2f}"

    elif baseline == BASELINE_REVENUE:
        display = f"月營收動態加權 (Alpha={float(alpha):.1f})"
        if latest is None:
            eps, detail = None, "查無已發布的單季 EPS"
        else:
            run_rate = latest * 4.0
            if revenue_yoy_pct is None:
                yoy = 0.0
                warning = (
                    "⚠️ 未取得營收 YoY，暫以 0% 計算，"
                    "動態加權效果等同單季季化基準。"
                )
            else:
                yoy = float(revenue_yoy_pct) / 100.0
            factor = 1.0 + yoy * float(alpha)
            eps = run_rate * factor
            detail = (
                f"單季年化 {run_rate:.2f} × (1 + {yoy:+.2%} × {float(alpha):.2f}) "
                f"= {eps:.2f}"
            )

    else:
        eps, detail = None, "未知的 EPS 基準選項"

    # 負值或非有限值的 EPS 無法定價，一律視為無法計算
    if eps is not None and (not np.isfinite(eps) or eps <= 0):
        eps = None
        warning = f"{warning} ⚠️ 計算出的 EPS 非正值，無法進行估值。".strip()

    return {
        "eps": eps,
        "label": label,
        "display": display,
        "detail": detail,
        "warning": warning,
        "alpha": float(alpha),
        "revenue_yoy_pct": revenue_yoy_pct,
        "quarters": stats["quarters"],
        "latest_eps": latest,
        "ttm_eps": stats["ttm_eps"],
    }


# --------------------------------------------------------------------------- #
# 2. 合理價推導（供四層健檢顯示參考價位，不與缺口做任何比對）
# --------------------------------------------------------------------------- #
def compute_fair_value(pe_history: pd.Series, eps_value: float) -> dict | None:
    """由歷史本益比序列 × 使用者選定的 EPS 基準推導合理價三線。

    合理價   = Calculated EPS × 歷史本益比中位數
    估值下緣 = Calculated EPS × 歷史本益比低點
    估值上緣 = Calculated EPS × 歷史本益比高點

    歷史本益比序列固定以「實際已發布的 TTM EPS」換算（反映市場真實給值）；
    切換基準只替換分子的 EPS，符合規格「動態合理股價 = Calculated EPS × P/E Median」。
    """
    pe = pd.to_numeric(pe_history, errors="coerce").dropna()
    if len(pe) < MIN_PE_POINTS:
        return None

    try:
        eps = float(eps_value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(eps) or eps <= 0:
        return None

    pe_median = float(pe.median())
    pe_min = float(pe.min())
    pe_max = float(pe.max())
    return {
        "eps": eps,
        "pe_median": pe_median,
        "pe_min": pe_min,
        "pe_max": pe_max,
        "pe_points": int(len(pe)),
        "fair_value": eps * pe_median,
        "support": eps * pe_min,
        "resistance": eps * pe_max,
    }
