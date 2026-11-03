# pages/2_🚨_每日異動警示.py
"""🚨 每日異動警示：量能 Z-Score 異動偵測與相對強度健檢。

依 agents.md 規範：
- 業務邏輯（Z-Score / 相對強度 / 評分）集中於 market_data.py，本檔僅負責 UI（SRP）
- 外部 API 一律 try-except 與判空，資料不足時以 st.warning / st.error 優雅降級
- 登入驗證沿用全域 st.session_state["authenticated"]（auth_gate.py）
- 評分維持 0 ~ 100 分量化機制，80 分以上為健康區間
"""

from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf

from auth_gate import check_password
from gsheets_store import DEFAULT_WATCHLIST, load_watchlist_stocks
from market_data import DEFAULT_BENCHMARK, build_alert_table, get_symbol_frame

# 頁面配置必須是本頁第一個 Streamlit 指令
st.set_page_config(page_title="每日異動警示", page_icon="🚨", layout="wide")

# 登入驗證（密碼存於 .streamlit/secrets.toml，程式碼不出現明文）
if not check_password():
    st.stop()  # 未驗證成功則中斷本頁後續程式碼

# 頁眉與登出按鈕（與主頁一致）
top_col1, top_col2 = st.columns([6, 1])
with top_col1:
    st.title("🚨 每日異動警示")
with top_col2:
    if st.button("🔒 安全登出"):
        st.session_state["authenticated"] = False
        st.rerun()

st.caption(
    f"量能 Z-Score 異動偵測 · 相對強度（基準 {DEFAULT_BENCHMARK}）· "
    "0 ~ 100 分量化評分，80 分以上為健康區間"
)

LOOKBACK_DAYS = 180  # 回溯天數，需涵蓋 20MA 與相對強度比較天數


def resolve_watchlist() -> dict:
    """取得自選股清單：session_state（首頁已載入）→ Google Sheets → 內建預設。"""
    stocks = st.session_state.get("custom_stocks")
    if stocks:
        return dict(stocks)
    sheet_stocks = load_watchlist_stocks()
    if sheet_stocks:
        return sheet_stocks
    return dict(DEFAULT_WATCHLIST)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_frames(symbols: tuple, benchmark: str, start: str) -> dict:
    """批次抓取自選股與基準指數的 OHLCV，回傳 {代號: DataFrame}；失敗回傳空 dict。"""
    tickers = list(dict.fromkeys(list(symbols) + [benchmark]))
    try:
        raw = yf.download(
            tickers,
            start=start,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception:
        return {}

    if raw is None or len(raw) == 0:
        return {}

    frames = {}
    for ticker in tickers:
        try:
            frame = get_symbol_frame(raw, ticker)
            if not frame.empty:
                frames[ticker] = frame
        except Exception:
            continue
    return frames


# ---------------------------------------------------------
# 側邊欄：異動警示設定
# ---------------------------------------------------------
stocks = resolve_watchlist()
st.sidebar.header("🚨 異動警示設定")
st.sidebar.caption(f"共監控 {len(stocks)} 檔標的（來源：自選股清單）")

z_threshold = st.sidebar.slider(
    "量能 Z-Score 警戒門檻",
    min_value=1.0,
    max_value=4.0,
    value=2.0,
    step=0.1,
    help="當日成交量偏離 20 日均量達此標準差倍數即列為爆量異動。",
)
rs_window = st.sidebar.selectbox(
    "相對強度比較天數",
    options=[5, 10, 20],
    index=2,
    help="相對強度 = 個股報酬率 − 基準指數報酬率。",
)
only_alert = st.sidebar.checkbox("只顯示異動項目（評分 < 80 或爆量）", value=False)
st.sidebar.caption("📡 行情來源：yfinance，快取 1 小時")

# ---------------------------------------------------------
# 資料載入
# ---------------------------------------------------------
symbols = tuple(dict.fromkeys(stocks.values()))
start = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

with st.spinner("📡 正在抓取行情並計算異動指標..."):
    frames = fetch_frames(symbols, DEFAULT_BENCHMARK, start)

if not frames:
    st.error("⚠️ 無法取得任何行情資料，請確認網路連線後再試一次。")
    st.stop()

# 拆出基準指數後再交給計算函式
bench_close = pd.Series(dtype=float)
if DEFAULT_BENCHMARK in frames:
    bench_close = frames.pop(DEFAULT_BENCHMARK)["Close"].dropna()
else:
    st.warning(
        f"⚠️ 無法取得基準指數 {DEFAULT_BENCHMARK} 資料，"
        "本頁相對強度（RS）將不予計算，僅顯示量能與漲跌幅指標。"
    )

labels = {symbol: label for label, symbol in stocks.items()}
alert_df, skipped = build_alert_table(
    frames,
    labels,
    bench_close,
    rs_window=rs_window,
    z_threshold=z_threshold,
)

if skipped:
    st.warning(
        "⚠️ 以下標的交易資料不足（可能尚未上市或近期無成交），已略過："
        + "、".join(skipped)
    )

if alert_df.empty:
    st.info("目前沒有可分析的標的，請先於首頁新增自選股。")
    st.stop()

# ---------------------------------------------------------
# 摘要指標
# ---------------------------------------------------------
alert_count = int((alert_df["評分"] < 80).sum())
vol_count = int((alert_df["量能Z-Score"] >= z_threshold).sum())
avg_score = float(alert_df["評分"].mean())

col1, col2, col3, col4 = st.columns(4)
col1.metric("📊 監控檔數", f"{len(alert_df)} 檔")
col2.metric("🚨 警戒項目（評分 < 80）", f"{alert_count} 檔")
col3.metric(f"📈 爆量異動（Z ≥ {z_threshold}）", f"{vol_count} 檔")
col4.metric("💯 平均健康評分", f"{avg_score:.1f} 分", delta=f"{avg_score - 80:+.1f}")
st.caption("健康區間：80 分以上（delta 為與 80 分健康線的差距）")

# ---------------------------------------------------------
# 異動明細
# ---------------------------------------------------------
st.divider()
st.markdown("### 📋 異動明細")

display_df = alert_df
if only_alert:
    mask = (
        (alert_df["評分"] < 80)
        | (alert_df["量能Z-Score"].abs() >= z_threshold)
        | (alert_df["漲跌幅 %"].abs() >= 3)
    )
    display_df = alert_df[mask]

if display_df.empty:
    st.info("✅ 目前沒有符合條件的異動項目。")
else:
    # 由最警戒（評分最低）排序
    display_df = display_df.sort_values("評分").reset_index(drop=True)

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "標的": st.column_config.TextColumn("標的", width="medium"),
            "代號": st.column_config.TextColumn("代號", width="small"),
            "收盤價": st.column_config.NumberColumn("收盤價", format="%.2f"),
            "漲跌幅 %": st.column_config.NumberColumn("漲跌幅 %", format="%.2f"),
            "今日量": st.column_config.NumberColumn("今日成交量", format="%.0f"),
            "量能Z-Score": st.column_config.NumberColumn("量能 Z-Score", format="%.2f"),
            "量能倍數": st.column_config.NumberColumn("量能倍數", format="%.2f"),
            "RS %": st.column_config.NumberColumn("RS %", format="%.2f"),
            "評分": st.column_config.NumberColumn("評分", format="%.1f"),
            "燈號": st.column_config.TextColumn("燈號", width="small"),
            "警訊": st.column_config.TextColumn("警訊", width="large"),
        },
    )

    st.download_button(
        "⬇️ 下載異動明細 CSV",
        data=display_df.to_csv(index=False, encoding="utf-8-sig"),
        file_name=f"daily_alert_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )

    # ---------------------------------------------------------
    # 圖表：量能 Z-Score / 相對強度
    # ---------------------------------------------------------
    st.divider()
    st.markdown("### 📊 異動圖表")
    chart_tab1, chart_tab2 = st.tabs(["📊 量能 Z-Score", "📈 相對強度 RS"])

    with chart_tab1:
        z_df = display_df.dropna(subset=["量能Z-Score"]).sort_values("量能Z-Score")
        if z_df.empty:
            st.info("目前沒有可繪製的量能 Z-Score 資料。")
        else:
            fig = go.Figure()
            fig.add_trace(
                go.Bar(
                    x=z_df["量能Z-Score"],
                    y=z_df["標的"],
                    orientation="h",
                    marker_color=[
                        "#e5484d" if z >= z_threshold else "#3b82f6"
                        for z in z_df["量能Z-Score"]
                    ],
                    text=[f"{z:.2f}" for z in z_df["量能Z-Score"]],
                    textposition="outside",
                )
            )
            fig.add_vline(
                x=z_threshold,
                line_dash="dash",
                line_color="#e5484d",
                annotation_text=f"警戒門檻 {z_threshold}",
                annotation_position="top",
            )
            fig.add_vline(x=0, line_color="#9ca3af", line_width=1)
            fig.update_layout(
                height=max(320, 32 * len(z_df) + 140),
                xaxis_title="量能 Z-Score（當日量相對 20 日均量的標準差倍數）",
                yaxis_title="",
                showlegend=False,
                margin=dict(l=10, r=60, t=50, b=10),
            )
            st.plotly_chart(fig, use_container_width=True)

    with chart_tab2:
        rs_df = display_df.dropna(subset=["RS %"]).sort_values("RS %")
        if rs_df.empty:
            st.info("目前沒有可繪製的相對強度資料（基準指數可能取得失敗）。")
        else:
            fig = go.Figure()
            fig.add_trace(
                go.Bar(
                    x=rs_df["RS %"],
                    y=rs_df["標的"],
                    orientation="h",
                    marker_color=[
                        "#2fbf71" if rs >= 0 else "#e5484d" for rs in rs_df["RS %"]
                    ],
                    text=[f"{rs:+.2f}%" for rs in rs_df["RS %"]],
                    textposition="outside",
                )
            )
            fig.add_vline(x=0, line_color="#9ca3af", line_width=1)
            fig.update_layout(
                height=max(320, 32 * len(rs_df) + 140),
                xaxis_title=f"相對強度 RS %（個股報酬 − {DEFAULT_BENCHMARK} 報酬，近 {rs_window} 個交易日）",
                yaxis_title="",
                showlegend=False,
                margin=dict(l=10, r=60, t=30, b=10),
            )
            st.plotly_chart(fig, use_container_width=True)

    # ---------------------------------------------------------
    # 優先關注卡片
    # ---------------------------------------------------------
    st.divider()
    st.markdown("### 🔍 優先關注（評分最低前 6 名）")
    for _, row in display_df.head(6).iterrows():
        with st.expander(f"{row['燈號']} {row['標的']}｜評分 {row['評分']:.1f} 分｜{row['警訊']}"):
            card = st.columns(4)
            card[0].metric("收盤價", f"{row['收盤價']:.2f}", delta=f"{row['漲跌幅 %']:+.2f}%")
            card[1].metric("量能 Z-Score", f"{row['量能Z-Score']:.2f}")
            card[2].metric("量能倍數", f"{row['量能倍數']:.2f}x")
            card[3].metric("相對強度 RS", f"{row['RS %']:+.2f}%")
            st.caption(
                f"代號 {row['代號']}｜今日成交量 {row['今日量']:,.0f}｜"
                "量能 Z-Score = (當日成交量 − 20 日均量) ÷ 20 日標準差"
            )

st.divider()
st.caption(
    "⚠️ 警示：量能 Z-Score 與相對強度由 yfinance 歷史行情計算，屬指標規則結果、"
    "非 AI 生成；評分為量化扣分制（0 ~ 100，80 分以上為健康區間）。"
    "本頁資訊僅供研究參考，不構成任何投資建議，投資風險請自行評估。"
)
