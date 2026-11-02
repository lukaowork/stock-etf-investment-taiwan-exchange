import streamlit as st
import yfinance as yf
import plotly.graph_objects as go
import pandas as pd
from datetime import datetime, timedelta

# 1. 頁面配置
st.set_page_config(
    page_title="台股與 ETF 專屬投資研報系統",
    page_icon="📈",
    layout="wide"
)

# 2. 登入驗證機制（密碼存放於 .streamlit/secrets.toml，程式碼中不出現明文）
def get_configured_password() -> str:
    """從 Streamlit secrets 讀取存取密碼；未設定時回傳空字串。"""
    try:
        pwd = st.secrets["app_password"]
        return str(pwd)
    except Exception:
        return ""


def check_password():
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if st.session_state["authenticated"]:
        return True

    configured_password = get_configured_password()
    if not configured_password:
        st.error(
            "⚠️ 未設定存取密碼。請在 `.streamlit/secrets.toml` 中加入：\n\n"
            "```toml\napp_password = \"你的密碼\"\n```"
        )
        return False

    st.markdown("<h1 style='text-align: center;'>🔒 登入個人投資分析系統</h1>", unsafe_allow_html=True)
    st.markdown("---")

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        password_input = st.text_input("請輸入系統存取密碼：", type="password")
        if st.button("登入系統", use_container_width=True):
            if password_input == configured_password:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("❌ 密碼錯誤！請重新輸入。")
    return False

if not check_password():
    st.stop()  # 未驗證成功則中斷後續程式碼執行

# ---------------------------------------------------------
# 3. 登入成功後的主畫面
# ---------------------------------------------------------

# 頁眉與登出按鈕
top_col1, top_col2 = st.columns([6, 1])
with top_col1:
    st.title("📈 台股與 ETF 觀察 Dashboard")
with top_col2:
    if st.button("🔒 安全登出"):
        st.session_state["authenticated"] = False
        st.rerun()

# ---------------------------------------------------------
# 側邊欄：動態新增自選股
# ---------------------------------------------------------
st.sidebar.header("⚙️ 控制面板")

# 1. 使用 session_state 儲存股票選單字典，避免重新整理時消失
if "custom_stocks" not in st.session_state:
    st.session_state["custom_stocks"] = {
        "台積電 (2330)": "2330.TW",
        "鴻海 (2317)": "2317.TW",
        "聯發科 (2454)": "2454.TW",
        "元大台灣50 (0050)": "0050.TW",
        "國泰永續高股息 (00878)": "00878.TW",
        "群益台灣精選高息 (00919)": "00919.TW",
        "復華台灣科技優息 (00929)": "00929.TW"
    }

# 2. 手動新增股票的功能區
with st.sidebar.expander("➕ 新增自選股票/ETF"):
    new_name = st.text_input("顯示名稱（例如：廣達）", key="input_name")
    new_symbol = st.text_input("台股代號（例如：2382）", key="input_symbol")

    if st.button("加入選單", use_container_width=True):
        name_input = new_name.strip()
        symbol_input = new_symbol.strip().upper()

        if not name_input or not symbol_input:
            st.warning("⚠️ 請完整填寫顯示名稱與代號。")
        else:
            # 拆出純代號與附檔名（支援 2382 / 2382.TW / 2382.TWO 三種輸入）
            if symbol_input.endswith(".TWO"):
                code_part, clean_symbol = symbol_input[:-4], symbol_input
            elif symbol_input.endswith(".TW"):
                code_part, clean_symbol = symbol_input[:-3], symbol_input
            else:
                code_part, clean_symbol = symbol_input, symbol_input + ".TW"

            if not code_part.isdigit():
                st.warning("⚠️ 代號請輸入 4~6 位數字（例如：2382，上櫃可加 .TWO）。")
            else:
                formatted_label = f"{name_input} ({code_part})"

                if formatted_label in st.session_state["custom_stocks"]:
                    st.warning(f"⚠️ {formatted_label} 已存在於選單中。")
                else:
                    st.session_state["custom_stocks"][formatted_label] = clean_symbol
                    st.session_state["pending_select"] = formatted_label
                    # 不呼叫 st.rerun()，讓下方 multiselect 同一次執行就帶入新選項，
                    # 同時 st.success 的提示也能正常顯示
                    st.success(f"✅ 已成功新增 {formatted_label} → {clean_symbol}！")

# 下方對照的標的字典改為調用 session_state
STOCK_DICT = st.session_state["custom_stocks"]

# 3. 供使用者選擇要追蹤的股票
# 預設值寫入 session_state（用 key 綁定），新增的股票可自動勾選
if "watchlist" not in st.session_state:
    st.session_state["watchlist"] = ["台積電 (2330)", "元大台灣50 (0050)"]

# 剛加入選單的股票自動加入勾選（此段在 multiselect 建立之前執行，符合 Streamlit 規則）
if "pending_select" in st.session_state:
    pending = st.session_state.pop("pending_select")
    if pending in STOCK_DICT and pending not in st.session_state["watchlist"]:
        st.session_state["watchlist"].append(pending)

selected_labels = st.sidebar.multiselect(
    "選擇要追蹤的股票 / ETF：",
    options=list(STOCK_DICT.keys()),
    key="watchlist",
)

# 選擇觀察時間區間
time_frame = st.sidebar.selectbox("資料時間範圍：", ["近 3 個月", "近 6 個月", "近 1 年", "近 3 年"], index=2)
days_map = {"近 3 個月": 90, "近 6 個月": 180, "近 1 年": 365, "近 3 年": 1095}
start_date = datetime.now() - timedelta(days=days_map[time_frame])


def get_symbol_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """從 yf.download 的回傳值取出單一標的的 OHLCV 資料。

    相容各種回傳結構：
    - 單一標的、一般欄位（Open/High/Low/Close/Volume）
    - 多標的、MultiIndex 欄位（第一層為價格欄或代號，皆可處理）
    """
    if isinstance(raw.columns, pd.MultiIndex):
        level_values = list(raw.columns.get_level_values(-1))
        if symbol in level_values:
            df = raw.xs(symbol, axis=1, level=-1)
        else:
            level_values0 = list(raw.columns.get_level_values(0))
            if symbol not in level_values0:
                return pd.DataFrame()
            df = raw.xs(symbol, axis=1, level=0)
    else:
        df = raw

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        return pd.DataFrame()
    return df[required].dropna(how="all")


if not selected_labels:
    st.warning("👈 請於左側邊欄選擇至少一檔股票或 ETF 開始載入數據。")
else:
    symbols = [STOCK_DICT[label] for label in selected_labels]

    @st.cache_data(ttl=3600)  # 快取資料 1 小時以提升載入速度
    def load_data(symbol_list, start):
        data = yf.download(
            symbol_list,
            start=start,
            progress=False,
            group_by="ticker",
            threads=True,
        )
        return data

    data_raw = load_data(symbols, start_date)

    # 資料載入失敗（斷線、代號無資料等）的統一檢查
    if data_raw is None or len(data_raw) == 0:
        st.error("⚠️ 無法取得任何行情資料，請確認網路連線後再試一次。")
        st.stop()

    # 預先取出各標的的乾淨 OHLCV，供後續分頁共用
    frames = {}
    for label in selected_labels:
        sym = STOCK_DICT[label]
        frames[label] = get_symbol_frame(data_raw, sym)

    # 主畫面 4 大分頁
    tab1, tab2, tab3, tab4 = st.tabs(["📊 核心觀察", "📈 K線與技術分析", "⚖️ 多標的走勢對比", "📋 歷史數據明細"])

    # -----------------------------------------------------
    # Tab 1: 核心觀察 (Metrics)
    # -----------------------------------------------------
    with tab1:
        st.subheader("📌 自選標的即時概況")
        cols = st.columns(len(selected_labels))

        for idx, label in enumerate(selected_labels):
            with cols[idx]:
                df_sym = frames[label]
                close_series = df_sym["Close"].dropna() if not df_sym.empty else pd.Series(dtype=float)
                if len(close_series) >= 2:
                    latest_p = float(close_series.iloc[-1])
                    prev_p = float(close_series.iloc[-2])
                    diff = latest_p - prev_p
                    pct = (diff / prev_p) * 100
                    st.metric(
                        label=label,
                        value=f"{latest_p:.2f} 元",
                        delta=f"{diff:+.2f} 元 ({pct:+.2f}%)",
                    )
                else:
                    st.error(f"{label} 資料不足或載入異常")

    # -----------------------------------------------------
    # Tab 2: K線與技術分析 (K-Line Chart)
    # -----------------------------------------------------
    with tab2:
        focus_label = st.selectbox("選擇要詳細分析的標的：", selected_labels)
        df_single = frames[focus_label]

        if df_single.empty or len(df_single) < 2:
            st.error(f"⚠️ {focus_label} 無法取得足夠的 K 線資料。")
        else:
            # 計算均線
            df_single = df_single.copy()
            df_single["20MA"] = df_single["Close"].rolling(20).mean()
            df_single["60MA"] = df_single["Close"].rolling(60).mean()

            fig = go.Figure()

            # K線
            fig.add_trace(go.Candlestick(
                x=df_single.index,
                open=df_single["Open"], high=df_single["High"],
                low=df_single["Low"], close=df_single["Close"],
                name="K線"
            ))

            # 均線
            fig.add_trace(go.Scatter(x=df_single.index, y=df_single["20MA"], name="20MA (月線)", line=dict(color="orange", width=1.5)))
            fig.add_trace(go.Scatter(x=df_single.index, y=df_single["60MA"], name="60MA (季線)", line=dict(color="green", width=1.5)))

            fig.update_layout(
                title=f"{focus_label} 股價走勢與均線分析",
                xaxis_title="日期",
                yaxis_title="價格 (TWD)",
                xaxis_rangeslider_visible=False,
                height=550,
                template="plotly_white"
            )
            st.plotly_chart(fig, use_container_width=True)

    # -----------------------------------------------------
    # Tab 3: 多標的走勢對比 (Normalized Comparison)
    # -----------------------------------------------------
    with tab3:
        st.subheader("⚖️ 自選標的累計報酬率比較 (以期初為 100%)")

        if len(symbols) == 1:
            st.info("請在左側選單選擇 2 檔以上的標的進行比較。")
        else:
            # 各標的交易日可能不同，先對齊日期再正規化，避免欄位錯位
            close_dict = {}
            for label in selected_labels:
                df_sym = frames[label]
                if not df_sym.empty:
                    close_dict[STOCK_DICT[label]] = df_sym["Close"]

            df_close = pd.DataFrame(close_dict).dropna(how="all")

            if df_close.empty or len(df_close) < 2:
                st.error("⚠️ 無法取得足夠的收盤價進行比較。")
            else:
                # 以各標的「共同的起始日」為基準正規化
                df_close = df_close.dropna()
                if df_close.empty or len(df_close) < 2:
                    st.error("⚠️ 所選標的在期間內沒有重疊的交易日，無法比較。")
                else:
                    df_norm = (df_close / df_close.iloc[0]) * 100

                    sym_to_label = {STOCK_DICT[l]: l for l in selected_labels}
                    fig_comp = go.Figure()
                    for sym in df_norm.columns:
                        fig_comp.add_trace(go.Scatter(
                            x=df_norm.index,
                            y=df_norm[sym],
                            name=sym_to_label.get(sym, sym),
                            mode="lines"
                        ))

                    fig_comp.update_layout(
                        title="自選標的累計走勢對比",
                        xaxis_title="日期",
                        yaxis_title="相對漲跌幅 (%)",
                        height=500,
                        template="plotly_white"
                    )
                    st.plotly_chart(fig_comp, use_container_width=True)

    # -----------------------------------------------------
    # Tab 4: 歷史數據明細 (Data Table + CSV 下載)
    # -----------------------------------------------------
    with tab4:
        st.subheader("📋 交易數據明細與下載")
        focus_label_dt = st.selectbox("選擇下載明細標的：", selected_labels, key="dt_select")
        df_dt = frames[focus_label_dt]

        if df_dt.empty:
            st.error(f"⚠️ {focus_label_dt} 無法取得歷史數據。")
        else:
            df_dt = df_dt.sort_index(ascending=False)
            st.dataframe(df_dt.head(30), use_container_width=True)

            csv_data = df_dt.to_csv(encoding="utf-8-sig")  # BOM 讓 Excel 開啟中文不亂碼
            st.download_button(
                label=f"⬇️ 下載 {focus_label_dt} 全部歷史數據 (CSV)",
                data=csv_data,
                file_name=f"{focus_label_dt.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
            )
