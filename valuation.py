"""EPS 估值與缺口相關性分析（純邏輯模組，不含 Streamlit UI）。

依 AGENTS.md 單一職責原則，本檔只負責：
  1. 由逐季 EPS 建立 TTM（近四季累計）EPS 與歷史本益比序列
  2. 推導合理價三線：合理價 / 估值下緣 / 估值上緣
  3. 缺口事件的價值偏差率 VGR、後續 20/60 日報酬率與三大情境標記
  4. Pearson / Spearman 相關係數與 P-value、最小平方趨勢擬合線
  5. Plotly 圖表組裝（全程無任何 st.* 呼叫）

UI 呈現（st.metric / st.plotly_chart / st.dataframe / st.download_button）
一律留在 app.py，以維持業務邏輯與 UI 解耦。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from plotly import graph_objects as go
from plotly.subplots import make_subplots

from market_data import (
    GAP_TYPE_BREAKAWAY,
    GAP_TYPE_COMMON,
    GAP_TYPE_EXHAUSTION,
    GAP_TYPE_RUNAWAY,
    GAP_UNFILLED,
    GAP_UP,
)

try:
    from scipy import stats as _scipy_stats
except ImportError:  # 防禦性降級：scipy 缺席時只算係數、不提供 p-value
    _scipy_stats = None

# --------------------------------------------------------------------------- #
# 常數：三大情境標籤、預測評級、門檻（規格定義，集中管理避免字串散落）
# --------------------------------------------------------------------------- #
SCENARIO_A = "A｜基本面+技術面雙重支撐"
SCENARIO_B = "B｜估值過高，缺口易遭貫穿"
SCENARIO_C = "C｜過度反應，易現 V 型反彈"
SCENARIO_NEUTRAL = "—｜中性"

# 預測評級代表該缺口的「看多可信度」：A 雙重支撐→高、C 超跌易反彈→高、
# B 估值過高→低、其餘→中
RATING_HIGH = "高"
RATING_MEDIUM = "中"
RATING_LOW = "低"

VGR_BAND_A = 5.0          # 情境 A：-5% <= VGR <= +5%
VGR_OVERVALUE = 20.0      # 情境 B：VGR > +20%
VGR_UNDERVALUE = -15.0    # 情境 C：VGR < -15%
SUPPORT_NEAR_PCT = 5.0    # ⭐ 亮點：缺口價距離估值下緣 ±5%

MIN_PE_POINTS = 30        # 計算合理價所需的最少有效本益比樣本數

# 缺口分類 → 散佈圖顏色（普通缺口用中性灰）
GAP_TYPE_COLORS = {
    GAP_TYPE_BREAKAWAY: "#27ae60",
    GAP_TYPE_EXHAUSTION: "#e74c3c",
    GAP_TYPE_RUNAWAY: "#2980b9",
    GAP_TYPE_COMMON: "#95a5a6",
}

# 缺口估值事件表的固定欄位
GAP_VALUATION_COLUMNS = [
    "日期",
    "缺口類型",
    "缺口價格",
    "EPS合理價",
    "VGR(%)",
    "情境標記",
    "缺口狀態",
    "後續20日報酬率(%)",
    "後續60日報酬率(%)",
    "預測評級",
]


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


def format_stat(value, digits: int = 3) -> str:
    """將統計數值格式化為字串；None / NaN 顯示 N/A。"""
    if value is None:
        return "N/A"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if not np.isfinite(num):
        return "N/A"
    return f"{num:.{digits}f}"


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


def compute_fair_value(pe_history: pd.Series, ttm_eps: pd.Series) -> dict | None:
    """由歷史本益比序列推導合理價三線；樣本不足或無有效 TTM EPS 時回傳 None。

    合理價   = TTM EPS × 歷史本益比中位數
    估值下緣 = TTM EPS × 歷史本益比低點
    估值上緣 = TTM EPS × 歷史本益比高點
    """
    pe = pd.to_numeric(pe_history, errors="coerce").dropna()
    if len(pe) < MIN_PE_POINTS:
        return None

    eps = pd.to_numeric(ttm_eps, errors="coerce").dropna()
    if eps.empty:
        return None
    ttm = float(eps.iloc[-1])
    if not np.isfinite(ttm) or ttm <= 0:
        return None

    pe_median = float(pe.median())
    pe_min = float(pe.min())
    pe_max = float(pe.max())
    return {
        "ttm_eps": ttm,
        "pe_median": pe_median,
        "pe_min": pe_min,
        "pe_max": pe_max,
        "pe_points": int(len(pe)),
        "fair_value": ttm * pe_median,
        "support": ttm * pe_min,
        "resistance": ttm * pe_max,
    }


# --------------------------------------------------------------------------- #
# 2. 價值偏差率 VGR 與情境標記
# --------------------------------------------------------------------------- #
def classify_scenario(vgr: float) -> tuple[str, str]:
    """依 VGR 判定三大情境，回傳 (情境標記, 預測評級)。

    情境 A：-5% <= VGR <= +5%  → 高
    情境 B：VGR > +20%         → 低（估值過高，缺口易遭貫穿）
    情境 C：VGR < -15%         → 高（過度反應，易現 V 型反彈）
    其餘：中性 → 中
    """
    if vgr is None or not np.isfinite(float(vgr)):
        return SCENARIO_NEUTRAL, RATING_MEDIUM
    vgr = float(vgr)
    if -VGR_BAND_A <= vgr <= VGR_BAND_A:
        return SCENARIO_A, RATING_HIGH
    if vgr > VGR_OVERVALUE:
        return SCENARIO_B, RATING_LOW
    if vgr < VGR_UNDERVALUE:
        return SCENARIO_C, RATING_HIGH
    return SCENARIO_NEUTRAL, RATING_MEDIUM


def _forward_return(values: np.ndarray, pos: int, days: int) -> float:
    """缺口當日收盤 → 後續 days 個交易日的累積報酬率 %；資料不足回傳 NaN。"""
    end = pos + days
    if end >= len(values):
        return float("nan")
    base, future = values[pos], values[end]
    if not (np.isfinite(base) and np.isfinite(future)) or base == 0:
        return float("nan")
    return round((future / base - 1.0) * 100.0, 2)


def build_valuation_gap_table(
    gaps: pd.DataFrame,
    close: pd.Series,
    fair_value: float,
) -> pd.DataFrame:
    """計算每個缺口事件的 VGR、後續 20/60 日報酬率與情境評級。

    - 缺口價格取缺口區間中點 = (缺口底部 + 缺口頂部) / 2
    - VGR(%) = (缺口價格 − 合理價) ÷ 合理價 × 100
    - 報酬率以缺口當日收盤價為基準，往後 20 / 60 個交易日計算
    """
    empty = pd.DataFrame(columns=GAP_VALUATION_COLUMNS)
    if gaps is None or gaps.empty or close is None or len(close) == 0:
        return empty
    if fair_value is None or not np.isfinite(fair_value) or fair_value <= 0:
        return empty

    close_f = pd.to_numeric(close, errors="coerce").astype(float)
    target = pd.DatetimeIndex(pd.to_datetime(gaps["日期"], errors="coerce"))
    positions = close_f.index.get_indexer(target)
    values = close_f.to_numpy(dtype=float)

    records = []
    for (_, row), pos in zip(gaps.iterrows(), positions):
        if pos < 0:
            continue
        try:
            bottom = float(row["缺口底部"])
            top = float(row["缺口頂部"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (np.isfinite(bottom) and np.isfinite(top)):
            continue

        gap_price = (bottom + top) / 2.0
        vgr = (gap_price - fair_value) / fair_value * 100.0
        scenario, rating = classify_scenario(vgr)

        records.append(
            {
                "日期": str(row["日期"]),
                "缺口類型": str(row.get("缺口分類", "")),
                "缺口價格": round(gap_price, 2),
                "EPS合理價": round(fair_value, 2),
                "VGR(%)": round(vgr, 2),
                "情境標記": scenario,
                "缺口狀態": str(row.get("是否回補", "")),
                "後續20日報酬率(%)": _forward_return(values, int(pos), 20),
                "後續60日報酬率(%)": _forward_return(values, int(pos), 60),
                "預測評級": rating,
            }
        )

    if not records:
        return empty
    return pd.DataFrame(records, columns=GAP_VALUATION_COLUMNS)


def find_near_support_gaps(
    table: pd.DataFrame,
    support: float,
    near_pct: float = SUPPORT_NEAR_PCT,
) -> pd.DataFrame:
    """找出落在估值下緣附近（±near_pct%）的缺口，供 K 線圖標示 ⭐。"""
    if table is None or table.empty:
        return pd.DataFrame(columns=GAP_VALUATION_COLUMNS)
    if support is None or not np.isfinite(support) or support <= 0:
        return pd.DataFrame(columns=GAP_VALUATION_COLUMNS)

    prices = pd.to_numeric(table["缺口價格"], errors="coerce")
    distance = (prices - support).abs() / support * 100.0
    return table[distance <= near_pct]


# --------------------------------------------------------------------------- #
# 3. 統計相關性與趨勢擬合
# --------------------------------------------------------------------------- #
def compute_correlation(x, y) -> dict:
    """Pearson / Spearman 相關係數與 P-value（成對刪除 NaN）。

    回傳 {"n", "pearson_r", "pearson_p", "spearman_r", "spearman_p"}。
    以下情況對應值為 None：有效樣本 < 3、任一邊無變異、scipy 未安裝。
    """
    result = {
        "n": 0,
        "pearson_r": None,
        "pearson_p": None,
        "spearman_r": None,
        "spearman_p": None,
    }

    xa = pd.to_numeric(pd.Series(x), errors="coerce").to_numpy(dtype=float)
    ya = pd.to_numeric(pd.Series(y), errors="coerce").to_numpy(dtype=float)
    n = min(len(xa), len(ya))
    xa, ya = xa[:n], ya[:n]
    mask = np.isfinite(xa) & np.isfinite(ya)
    xa, ya = xa[mask], ya[mask]
    result["n"] = int(len(xa))

    if len(xa) < 3 or np.ptp(xa) == 0 or np.ptp(ya) == 0:
        return result

    if _scipy_stats is not None:
        try:
            pr, pp = _scipy_stats.pearsonr(xa, ya)
            sr, sp = _scipy_stats.spearmanr(xa, ya)
            result.update(
                pearson_r=float(pr),
                pearson_p=float(pp),
                spearman_r=float(sr),
                spearman_p=float(sp),
            )
            return result
        except Exception:
            pass  # 檢定失敗 → 降級只算係數

    xs, ys = pd.Series(xa), pd.Series(ya)
    result["pearson_r"] = float(xs.corr(ys, method="pearson"))
    result["spearman_r"] = float(xs.corr(ys, method="spearman"))
    return result


def compute_win_rate(
    table: pd.DataFrame,
    horizon_col: str = "後續20日報酬率(%)",
) -> float | None:
    """情境 A（雙重支撐）的缺口中，後續報酬率為正的比例 %；無樣本回傳 None。"""
    if table is None or table.empty or "情境標記" not in table.columns:
        return None
    subset = table[table["情境標記"] == SCENARIO_A]
    if subset.empty:
        return None
    returns = pd.to_numeric(subset[horizon_col], errors="coerce").dropna()
    if returns.empty:
        return None
    return float((returns > 0).mean() * 100.0)


def fit_trendline(x, y) -> tuple[float, float] | None:
    """最小平方趨勢線 y = slope·x + intercept；樣本不足或無變異回傳 None。"""
    xa = pd.to_numeric(pd.Series(x), errors="coerce").to_numpy(dtype=float)
    ya = pd.to_numeric(pd.Series(y), errors="coerce").to_numpy(dtype=float)
    n = min(len(xa), len(ya))
    xa, ya = xa[:n], ya[:n]
    mask = np.isfinite(xa) & np.isfinite(ya)
    xa, ya = xa[mask], ya[mask]
    if len(xa) < 2 or np.ptp(xa) == 0:
        return None
    try:
        slope, intercept = np.polyfit(xa, ya, 1)
    except (TypeError, ValueError):
        return None
    if not (np.isfinite(slope) and np.isfinite(intercept)):
        return None
    return float(slope), float(intercept)


# --------------------------------------------------------------------------- #
# 4. Plotly 圖表組裝（不含任何 st.* 呼叫）
# --------------------------------------------------------------------------- #
def build_scatter_figure(table: pd.DataFrame, y_col: str, y_label: str) -> go.Figure:
    """VGR (X) vs 缺口後續報酬率 (Y) 散佈圖，依缺口分類著色並疊加趨勢擬合線。"""
    fig = go.Figure()

    if table is None or table.empty:
        plot_df = pd.DataFrame(columns=GAP_VALUATION_COLUMNS)
    else:
        plot_df = table.copy()
        plot_df["VGR(%)"] = pd.to_numeric(plot_df["VGR(%)"], errors="coerce")
        plot_df[y_col] = pd.to_numeric(plot_df[y_col], errors="coerce")
        plot_df = plot_df.dropna(subset=["VGR(%)", y_col])

    if plot_df.empty:
        fig.update_layout(
            title="沒有可繪製的缺口樣本",
            template="plotly_white",
            height=420,
        )
        return fig

    # 依缺口分類著色（未知分類一律用中性灰，避免資料遺失）
    for gap_type in plot_df["缺口類型"].unique():
        subset = plot_df[plot_df["缺口類型"] == gap_type]
        color = GAP_TYPE_COLORS.get(str(gap_type), "#95a5a6")
        fig.add_trace(
            go.Scatter(
                x=subset["VGR(%)"].to_numpy(dtype=float),
                y=subset[y_col].to_numpy(dtype=float),
                mode="markers",
                name=str(gap_type),
                customdata=np.stack(
                    [
                        subset["日期"].to_numpy(dtype=str),
                        subset["預測評級"].to_numpy(dtype=str),
                    ],
                    axis=-1,
                ),
                marker={"size": 9, "color": color, "line": {"width": 0.6, "color": "white"}},
                hovertemplate=(
                    "VGR %{x:.2f}%<br>"
                    + y_label
                    + " %{y:.2f}%<br>日期 %{customdata[0]}"
                    + "<br>預測評級 %{customdata[1]}<extra></extra>"
                ),
            )
        )

    # 趨勢擬合線（最小平方）
    trend = fit_trendline(plot_df["VGR(%)"], plot_df[y_col])
    if trend is not None:
        slope, intercept = trend
        x_min = float(plot_df["VGR(%)"].min())
        x_max = float(plot_df["VGR(%)"].max())
        if x_min == x_max:
            x_min, x_max = x_min - 1.0, x_max + 1.0
        fig.add_trace(
            go.Scatter(
                x=[x_min, x_max],
                y=[slope * x_min + intercept, slope * x_max + intercept],
                mode="lines",
                name="趨勢擬合線",
                line={"color": "#2c3e50", "width": 2, "dash": "dot"},
            )
        )

    # 0% 參考軸線
    fig.add_hline(y=0, line_color="#7f8c8d", line_dash="dash", line_width=1)
    fig.add_vline(x=0, line_color="#7f8c8d", line_dash="dash", line_width=1)

    fig.update_layout(
        title=f"價值偏差率 VGR 與缺口後續報酬率散布圖（{y_label}）",
        xaxis_title="價值偏差率 VGR (%)",
        yaxis_title=y_label,
        template="plotly_white",
        height=480,
        legend_title_text="缺口分類",
    )
    fig.update_xaxes(zeroline=False)
    fig.update_yaxes(zeroline=False)
    return fig


def build_valuation_river_figure(
    df: pd.DataFrame,
    pe_history: pd.Series,
    fair: dict,
    gaps: pd.DataFrame,
    star_gaps: pd.DataFrame,
    title: str = "",
) -> go.Figure:
    """上圖：K 線 + 未回補缺口色塊 + EPS 合理價三線 + ⭐ 亮點。
    下圖：歷史本益比走勢與中位數／低點／高點參考線。
    """
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        row_heights=[0.64, 0.36],
        subplot_titles=("K 線與 EPS 合理價河流", "歷史本益比 (P/E) 走勢"),
    )

    # ---- 上圖：K 線 ----
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name="K線",
        ),
        row=1,
        col=1,
    )

    # ---- 上圖：未回補缺口色塊（畫出真實缺口價區間）----
    last_ts = pd.Timestamp(df.index[-1])
    if gaps is not None and not gaps.empty and "是否回補" in gaps.columns:
        unfilled = gaps[gaps["是否回補"] == GAP_UNFILLED]
        for _, row in unfilled.iterrows():
            try:
                gap_start = _naive_timestamp(row["日期"])
                y0 = float(row["缺口底部"])
                y1 = float(row["缺口頂部"])
            except Exception:  # 防禦性：單筆缺口資料異常不影響整張圖
                continue
            if pd.isna(gap_start) or not (np.isfinite(y0) and np.isfinite(y1)):
                continue
            gap_end = (
                last_ts if last_ts > gap_start else gap_start + pd.Timedelta(days=3)
            )
            is_up = str(row.get("類型", "")) == GAP_UP
            fig.add_shape(
                type="rect",
                x0=gap_start,
                x1=gap_end,
                y0=y0,
                y1=y1,
                fillcolor=(
                    "rgba(46, 204, 113, 0.16)"
                    if is_up
                    else "rgba(229, 72, 77, 0.16)"
                ),
                line_width=0,
                layer="below",
                row=1,
                col=1,
            )

    # ---- 上圖：EPS 合理價三線 ----
    x_start, x_end = df.index[0], df.index[-1]
    for key, label, color, dash in (
        ("support", f"估值下緣 P/E {fair['pe_min']:.1f}x", "#27ae60", "dot"),
        ("fair_value", f"EPS 合理價 P/E {fair['pe_median']:.1f}x", "#f1c40f", "solid"),
        ("resistance", f"估值上緣 P/E {fair['pe_max']:.1f}x", "#e74c3c", "dot"),
    ):
        value = float(fair[key])
        fig.add_trace(
            go.Scatter(
                x=[x_start, x_end],
                y=[value, value],
                mode="lines",
                name=label,
                line={"color": color, "width": 1.5, "dash": dash},
            ),
            row=1,
            col=1,
        )

    # ---- 上圖：⭐ 估值下緣亮點 ----
    if star_gaps is not None and not star_gaps.empty:
        fig.add_trace(
            go.Scatter(
                x=pd.to_datetime(star_gaps["日期"], errors="coerce"),
                y=pd.to_numeric(star_gaps["缺口價格"], errors="coerce"),
                mode="markers+text",
                text=["⭐"] * len(star_gaps),
                textposition="top center",
                name="⭐ 估值下緣亮點",
                marker={"size": 11, "color": "#f1c40f", "symbol": "circle"},
            ),
            row=1,
            col=1,
        )

    # ---- 下圖：歷史本益比走勢與三線 ----
    pe_clean = pd.to_numeric(pe_history, errors="coerce").dropna()
    if not pe_clean.empty:
        fig.add_trace(
            go.Scatter(
                x=pe_clean.index,
                y=pe_clean.to_numpy(dtype=float),
                mode="lines",
                name="歷史本益比",
                line={"color": "#2980b9", "width": 1.6},
            ),
            row=2,
            col=1,
        )
        for key, label, color, dash in (
            ("pe_max", f"P/E 高點 {fair['pe_max']:.1f}x", "#e74c3c", "dot"),
            ("pe_median", f"P/E 中位數 {fair['pe_median']:.1f}x", "#f1c40f", "solid"),
            ("pe_min", f"P/E 低點 {fair['pe_min']:.1f}x", "#27ae60", "dot"),
        ):
            value = float(fair[key])
            fig.add_trace(
                go.Scatter(
                    x=[pe_clean.index[0], pe_clean.index[-1]],
                    y=[value, value],
                    mode="lines",
                    name=label,
                    line={"color": color, "width": 1.2, "dash": dash},
                ),
                row=2,
                col=1,
            )

    fig.update_layout(
        title=title or "K 線、EPS 合理價河流與本益比走勢",
        template="plotly_white",
        height=760,
        legend_title_text="圖例",
        hovermode="closest",
    )
    fig.update_xaxes(rangeslider_visible=False, showgrid=True)
    fig.update_yaxes(showgrid=True)
    return fig
