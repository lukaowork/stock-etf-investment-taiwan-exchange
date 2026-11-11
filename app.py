import json
import re
from urllib.parse import quote

import streamlit as st
import yfinance as yf
import plotly.graph_objects as go
import pandas as pd
import requests
from datetime import datetime, timedelta

# Google Gemini SDK（未安裝時不中斷程式，於分析頁提示安裝）
try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except Exception:
    genai = None
    genai_types = None
    GENAI_AVAILABLE = False

# feedparser 為可選套件，未安裝時改用 requests 解析 Google News RSS
try:
    import feedparser
    FEEDPARSER_AVAILABLE = True
except Exception:
    feedparser = None
    FEEDPARSER_AVAILABLE = False

# 共用模組（供 app.py 與 pages/ 多頁共用，遵循 DRY）
from auth_gate import check_password
from gsheets_store import (
    DEFAULT_WATCHLIST,
    fetch_transactions_frame,
    get_gsheets_connection,
    gsheets_can_write,
    load_watchlist_stocks,
)
from market_data import (
    GAP_FILLED,
    GAP_TYPE_BREAKAWAY,
    GAP_TYPE_EXHAUSTION,
    GAP_TYPE_OPTIONS,
    GAP_UNFILLED,
    GAP_UP,
    build_indicator_summary,
    detect_gaps,
    download_frames,
    fetch_benchmark_close,
    fetch_treasury_yield,
    get_cached_ai_analysis,
)
from health_check import (
    build_health_report,
    build_radar_figure,
    build_summary_text,
    compute_valuation_metrics,
)
from twse_data import (
    fetch_margin_usage,
    fetch_market_valuation,
    lookup_margin_usage,
    market_median_pe,
    market_percentile,
)
from valuation import (
    ALPHA_DEFAULT,
    ALPHA_MAX,
    ALPHA_MIN,
    BASELINE_OPTIONS,
    BASELINE_REVENUE,
    MIN_PE_POINTS,
    build_scatter_figure,
    build_ttm_eps_series,
    build_valuation_gap_table,
    build_valuation_river_figure,
    compute_baseline_eps,
    compute_correlation,
    compute_fair_value,
    compute_pe_history,
    compute_win_rate,
    find_near_support_gaps,
    format_stat,
)

# 1. 頁面配置
st.set_page_config(
    page_title="台股與 ETF 專屬投資研報系統",
    page_icon="📈",
    layout="wide"
)

# 2. 登入驗證機制（實作於 auth_gate.py，密碼存放於 .streamlit/secrets.toml）
if not check_password():
    st.stop()  # 未驗證成功則中斷後續程式碼執行

# ---------------------------------------------------------
# 2.5 自選股寫回 Google Sheets（讀取相關函式已抽至 gsheets_store.py）
# ---------------------------------------------------------
def sync_watchlist_to_sheets() -> bool:
    """把 session 中的自選股清單寫回 Watchlist 工作表，成功回傳 True。

    - 需服務帳戶（type = "service_account"）才有寫入權限
    - 寫入成功後執行 st.cache_data.clear()，確保立即重新讀取最新數據
    - 失敗時丟出 Exception（含詳細原因），由呼叫端以 st.error 顯示
    """
    if not gsheets_can_write():
        raise Exception(
            '目前為唯讀模式（secrets 未設定 type = "service_account"），'
            "沒有 Google Sheets 寫入權限；請於 .streamlit/secrets.toml 的 "
            "[connections.gsheets] 設定服務帳戶金鑰並分享試算表給 client_email。"
        )

    conn = get_gsheets_connection()
    if conn is None:
        raise Exception(
            '無法建立 st.connection("gsheets")，請確認已安裝 st-gsheets-connection，'
            "並於 .streamlit/secrets.toml 設定 [connections.gsheets]。"
        )

    # 讀回原表以保留 category / added_date 等既有欄位
    original = None
    try:
        original = conn.read(worksheet="Watchlist", ttl=1)
    except Exception:
        original = None

    def _cell(value, default: str) -> str:
        """把 NaN / None / 空字串統一成預設值。"""
        if value is None:
            return default
        text = str(value).strip()
        if not text or text.lower() == "nan" or text.lower() == "nat":
            return default
        return text

    orig_map = {}
    if original is not None and not original.empty:
        ocols = {str(c).strip().lower(): c for c in original.columns}
        sym_col = ocols.get("symbol")
        if sym_col is not None:
            for _, r in original.iterrows():
                code = str(r[sym_col]).split(".")[0].strip()
                orig_map[code] = {
                    key: r[col] for key, col in ocols.items() if key != "symbol"
                }

    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for label, symbol in st.session_state["custom_stocks"].items():
        code = symbol.split(".")[0]
        name = label.rsplit(" (", 1)[0] if label.endswith(")") else label
        prev = orig_map.get(code, {})
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "category": _cell(prev.get("category"), "自訂"),
                "added_date": _cell(prev.get("added_date"), today),
            }
        )

    updated_df = pd.DataFrame(rows, columns=["symbol", "name", "category", "added_date"])

    try:
        conn.update(worksheet="Watchlist", data=updated_df)
    except Exception as e:
        # 帶出明確的 Exception 內容，避免只顯示模糊警告
        raise Exception(f"{type(e).__name__}: {e}") from e

    # 寫入成功後清除快取，確保 Streamlit 立即重新讀取最新數據
    st.cache_data.clear()
    return True


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

# 1. 自選股清單：優先從 Google Sheets 的 Watchlist 工作表載入
if "custom_stocks" not in st.session_state:
    sheet_stocks = load_watchlist_stocks()
    if sheet_stocks:
        st.session_state["custom_stocks"] = sheet_stocks
        st.session_state["watchlist_from_sheets"] = True
    else:
        # Sheets 讀取失敗或為空時，退回內建預設清單（DEFAULT_WATCHLIST 於 gsheets_store.py）
        st.session_state["custom_stocks"] = dict(DEFAULT_WATCHLIST)
        st.session_state["watchlist_from_sheets"] = False

# 側邊欄標示清單來源
if st.session_state.get("watchlist_from_sheets"):
    st.sidebar.caption("☁️ 自選股清單已連結 Google Sheets（Watchlist）")
else:
    st.sidebar.caption(
        "⚠️ Google Sheets 未連結（讀取失敗或為空），目前使用本機預設清單；"
        "新增/移除將嘗試寫回。"
    )

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

                    # 寫回 Google Sheets：conn.update(worksheet="Watchlist", data=updated_df)
                    # 成功後由 sync_watchlist_to_sheets() 內部執行 st.cache_data.clear()
                    try:
                        sync_watchlist_to_sheets()
                    except Exception as e:
                        st.error(f"寫入 Google Sheets 失敗，詳細原因：{e}")
                    else:
                        st.caption("☁️ 已同步寫回 Google Sheets（Watchlist），快取已清除")

# 2.5 手動移除股票的功能區（同步寫回 Google Sheets）
with st.sidebar.expander("➖ 移除自選股票/ETF"):
    if st.session_state["custom_stocks"]:
        remove_options = list(st.session_state["custom_stocks"].keys())
        # 防護：上次選取的標的已被移除時，先重設避免 selectbox 報錯
        if st.session_state.get("remove_select") not in remove_options:
            st.session_state["remove_select"] = remove_options[0]
        remove_label = st.selectbox(
            "選擇要移除的標的：",
            options=remove_options,
            key="remove_select",
        )
        if st.button("移除選定標的", use_container_width=True):
            removed_symbol = st.session_state["custom_stocks"].pop(remove_label, None)
            if removed_symbol is not None:
                # 同步從勾選清單移除（multiselect 尚未建立，可安全修改 session_state）
                if remove_label in st.session_state.get("watchlist", []):
                    st.session_state["watchlist"].remove(remove_label)
                st.success(f"🗑️ 已移除 {remove_label}")
                try:
                    sync_watchlist_to_sheets()
                except Exception as e:
                    st.error(f"寫入 Google Sheets 失敗，詳細原因：{e}")
                else:
                    st.caption("☁️ 已同步寫回 Google Sheets（Watchlist），快取已清除")
    else:
        st.info("目前沒有可移除的標的。")

# 下方對照的標的字典改為調用 session_state
STOCK_DICT = st.session_state["custom_stocks"]

# 3. 供使用者選擇要追蹤的股票
# 預設值寫入 session_state（用 key 綁定），新增的股票可自動勾選
if "watchlist" not in st.session_state:
    defaults = ["台積電 (2330)", "元大台灣50 (0050)"]
    st.session_state["watchlist"] = [d for d in defaults if d in STOCK_DICT]

# 清除已不存在於選單的勾選項目（避免 Sheets 內容變動後 multiselect 報錯）
st.session_state["watchlist"] = [
    label for label in st.session_state.get("watchlist", []) if label in STOCK_DICT
]

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

# ---------------------------------------------------------
# EPS 動態基準切換器：供「EPS 價值與缺口對比」分頁切換基本面估算基準
# ---------------------------------------------------------
eps_baseline = st.sidebar.selectbox(
    "選擇 EPS 估算基準：",
    options=list(BASELINE_OPTIONS),
    help=(
        "TTM＝近 4 季累積已發布 EPS；單季季化＝最新單季 × 4；"
        "月營收動態加權＝單季年化 × (1 + 近3個月營收YoY × Alpha)。"
    ),
)
if eps_baseline == BASELINE_REVENUE:
    eps_alpha = st.sidebar.slider(
        "經營槓桿 Alpha",
        min_value=ALPHA_MIN,
        max_value=ALPHA_MAX,
        value=ALPHA_DEFAULT,
        step=0.1,
        help="Alpha 越大，營收成長對 EPS 的放大效果越強。",
    )
else:
    eps_alpha = ALPHA_DEFAULT


# ---------------------------------------------------------
# AI 分析模組（Gemini）：金鑰、新聞抓取、三健檢分析
# ---------------------------------------------------------
GEMINI_MODEL = "gemini-3.8-flash"


def get_gemini_api_key() -> str:
    """從 Streamlit secrets 讀取 GEMINI_API_KEY，未設定回傳空字串。"""
    try:
        return str(st.secrets["GEMINI_API_KEY"]).strip()
    except Exception:
        return ""


def fetch_recent_news(stock_name: str, stock_symbol: str, max_items: int = 6) -> str:
    """以 feedparser（或 requests 備援）抓取該標的近期新聞標題，失敗回傳空字串。"""
    code = stock_symbol.split(".")[0]
    query = quote(f"{stock_name} {code} 台股")
    url = (
        f"https://news.google.com/rss/search?q={query}"
        "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    titles = []
    try:
        if FEEDPARSER_AVAILABLE:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_items]:
                published = entry.get("published", "")
                titles.append(f"- {entry.get('title', '')}（{published}）")
        else:
            resp = requests.get(
                url, timeout=8, headers={"User-Agent": "Mozilla/5.0"}
            )
            resp.raise_for_status()
            found = re.findall(r"<title>(.*?)</title>", resp.text, flags=re.S)
            for raw in found[1:max_items + 1]:
                titles.append(f"- {raw.strip()}")
    except Exception:
        return ""
    return "\n".join(titles)


def _fmt_revenue(value: float) -> str:
    """把營收數字格式化成台股習慣的『億』單位字串。"""
    return f"{value / 1e8:,.1f} 億"


def _status_from_rate(rate: float) -> str:
    """依驚喜度百分比回傳狀態標籤。"""
    if rate > 5:
        return "強烈超預期 🚀"
    if rate < -5:
        return "低於預期 ⚠️"
    return "符合預期 ⚖️"


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fundamentals(symbol: str) -> dict:
    """yfinance 財務數據備援：在呼叫 Gemini 前先抓最新一季 EPS/營收與法人預估。

    依序嘗試 ticker.earnings_history、ticker.earnings_dates、
    ticker.quarterly_income_stmt，回傳：
      - text          ：可直接放進 Prompt 的數據摘要（空字串代表沒抓到）
      - eps_estimate  ：法人預估 EPS（有才覆蓋模型結果）
      - eps_actual    ：實際 EPS（yfinance 實數）
      - revenue_actual：實際營收（yfinance 實數）
      - avg_eps_4q    ：近 4 季平均 EPS（供模型推估基準）
      - eps_quarterly ：逐季 EPS 序列 {公告日: 該季 EPS}（供估值分頁算 TTM 與本益比）
      - revenue_yoy   ：最近一季營收年增率 YoY %
      - revenue_q / cogs_q / operating_income_q：逐季財報 {財期末: 金額}
        （供四層健檢計算 DIO 與 Price vs. Volume 拆解）
      - inventory_q   ：逐季存貨 {財期末: 金額}
      - cfo_ttm / net_income_ttm：近 4 季營業現金流與淨利（獲利真實度）
      - sector / industry：yfinance 產業分類
    """
    data = {
        "text": "",
        "eps_estimate": None,
        "eps_actual": None,
        "revenue_estimate": None,
        "revenue_actual": None,
        "avg_eps_4q": None,
        "revenue_yoy": None,
        "eps_quarterly": {},
        "revenue_q": {},
        "cogs_q": {},
        "operating_income_q": {},
        "inventory_q": {},
        "cfo_ttm": None,
        "net_income_ttm": None,
        "sector": None,
        "industry": None,
    }

    try:
        ticker = yf.Ticker(symbol)
    except Exception:
        return data

    # --- A. earnings_history：dict 結構的法人預估 vs 實際（防禦式解析） ---
    try:
        history = ticker.earnings_history
        if isinstance(history, dict):
            for outer_key, group in history.items():
                key = str(outer_key).lower()
                if not isinstance(group, dict):
                    continue
                for info in group.values():
                    if not isinstance(info, dict):
                        continue
                    # EPS：epsEstimate / epsActual
                    if data["eps_actual"] is None and (
                        "eps" in key or "earning" in key or "epsActual" in info
                    ):
                        actual = _to_number(info.get("epsActual"))
                        if actual is not None:
                            data["eps_actual"] = actual
                            data["eps_estimate"] = _to_number(info.get("epsEstimate"))
                    # 營收：revenueEstimate / revenueActual
                    if data["revenue_actual"] is None and (
                        "revenue" in key or "revenueActual" in info
                    ):
                        actual = _to_number(info.get("revenueActual"))
                        if actual is not None:
                            data["revenue_actual"] = actual
                            data["revenue_estimate"] = _to_number(
                                info.get("revenueEstimate")
                            )
    except Exception:
        pass

    # --- B. earnings_dates：表格形式的預估 vs 實際 ---
    try:
        dates = ticker.earnings_dates
        if dates is not None and hasattr(dates, "empty") and not dates.empty:
            colmap = {str(c).lower(): c for c in dates.columns}
            actual_col = colmap.get("reported eps")
            estimate_col = colmap.get("eps estimate")
            if actual_col is not None:
                done = dates.dropna(subset=[actual_col]).sort_index()
                if not done.empty and data["eps_actual"] is None:
                    last_row = done.iloc[-1]
                    data["eps_actual"] = _to_number(last_row[actual_col])
                    if estimate_col is not None:
                        data["eps_estimate"] = _to_number(last_row[estimate_col])
                # 逐季 EPS 序列（公告日），供估值分頁計算 TTM EPS 與歷史本益比
                series_eps = {}
                for earn_date, earn_val in done[actual_col].items():
                    if pd.isna(earn_date):
                        continue
                    num = _to_number(earn_val)
                    if num is None:
                        continue
                    series_eps[str(pd.Timestamp(earn_date).date())] = float(num)
                if len(series_eps) >= 4:
                    data["eps_quarterly"] = series_eps
    except Exception:
        pass

    # --- C. quarterly_income_stmt：台股主要來源（實際營收/EPS、近4季平均、YoY） ---
    try:
        stmt = ticker.quarterly_income_stmt
        if stmt is not None and hasattr(stmt, "empty") and not stmt.empty:
            idx = {str(i).lower(): i for i in stmt.index}

            rev_row = idx.get("total revenue")
            if rev_row is not None:
                rev_series = stmt.loc[rev_row].dropna()
                if data["revenue_actual"] is None and not rev_series.empty:
                    data["revenue_actual"] = _to_number(rev_series.iloc[0])
                # 同一季 vs 去年同季（第 5 欄）→ 營收年增率 YoY
                if len(rev_series) >= 5:
                    last_val = _to_number(rev_series.iloc[0])
                    year_ago = _to_number(rev_series.iloc[4])
                    if last_val is not None and year_ago not in (None, 0):
                        data["revenue_yoy"] = (last_val - year_ago) / abs(year_ago) * 100

            eps_row = idx.get("diluted eps") or idx.get("basic eps")
            if eps_row is not None:
                eps_series = stmt.loc[eps_row].dropna()
                if data["eps_actual"] is None and not eps_series.empty:
                    data["eps_actual"] = _to_number(eps_series.iloc[0])
                if len(eps_series) >= 4:
                    data["avg_eps_4q"] = float(eps_series.iloc[:4].mean())
                # 逐季 EPS 序列備援（公告日來源不足時才用）：以財期末代替公告日，
                # 可能略有前視偏差，但比完全沒有估值分析來得好
                if len(eps_series) >= 4 and len(eps_series) > len(
                    data["eps_quarterly"]
                ):
                    fallback_eps = {}
                    for period_end, period_val in eps_series.items():
                        if pd.isna(period_end):
                            continue
                        num = _to_number(period_val)
                        if num is None:
                            continue
                        fallback_eps[str(pd.Timestamp(period_end).date())] = float(num)
                    if len(fallback_eps) >= 4:
                        data["eps_quarterly"] = fallback_eps

            # 逐季營收／銷貨成本／營業利益（四層健檢：DIO 與 Price vs. Volume 拆解）
            for field, names in (
                ("revenue_q", ("total revenue",)),
                ("cogs_q", ("cost of revenue", "reconciled cost of revenue")),
                ("operating_income_q", ("operating income",)),
            ):
                row = next((idx[name] for name in names if name in idx), None)
                if row is None:
                    continue
                series = stmt.loc[row].dropna()
                for period, val in series.items():
                    num = _to_number(val)
                    if num is None or pd.isna(period):
                        continue
                    data[field][str(pd.Timestamp(period).date())] = float(num)

            # 近 4 季淨利（獲利真實度 CFO / 淨利 的分母）
            ni_row = idx.get("net income") or idx.get("net income common stockholders")
            if ni_row is not None:
                ni_series = stmt.loc[ni_row].dropna()
                ni_values = [
                    v
                    for v in (_to_number(v) for v in ni_series.iloc[:4])
                    if v is not None
                ]
                if len(ni_values) >= 4:
                    data["net_income_ttm"] = float(sum(ni_values))
    except Exception:
        pass

    # --- D. quarterly_cashflow：營業活動現金流量（獲利真實度 CFO） ---
    try:
        cashflow = ticker.quarterly_cashflow
        if cashflow is not None and hasattr(cashflow, "empty") and not cashflow.empty:
            idx_c = {str(i).lower(): i for i in cashflow.index}
            cfo_row = next(
                (
                    idx_c[name]
                    for name in (
                        "operating cash flow",
                        "total cash from operating activities",
                        "cash flow from operating activities",
                    )
                    if name in idx_c
                ),
                None,
            )
            if cfo_row is not None:
                series = cashflow.loc[cfo_row].dropna()
                values = [
                    v for v in (_to_number(v) for v in series.iloc[:4]) if v is not None
                ]
                if len(values) >= 4:
                    data["cfo_ttm"] = float(sum(values))
    except Exception:
        pass

    # --- E. quarterly_balance_sheet：存貨（產業庫存天數 DIO） ---
    try:
        balance = ticker.quarterly_balance_sheet
        if balance is not None and hasattr(balance, "empty") and not balance.empty:
            idx_b = {str(i).lower(): i for i in balance.index}
            inv_row = idx_b.get("inventory")
            if inv_row is not None:
                series = balance.loc[inv_row].dropna()
                for period, val in series.items():
                    num = _to_number(val)
                    if num is None or pd.isna(period):
                        continue
                    data["inventory_q"][str(pd.Timestamp(period).date())] = float(num)
    except Exception:
        pass

    # --- F. info：產業分類（四層健檢的產業風向層顯示用） ---
    try:
        info = ticker.info
        if isinstance(info, dict):
            data["sector"] = info.get("sector") or None
            data["industry"] = info.get("industry") or None
    except Exception:
        pass

    # --- 組成放入 Prompt 的文字摘要 ---
    lines = []
    if data["eps_actual"] is not None:
        line = f"- 最新一季實際 EPS：{data['eps_actual']:.2f} 元"
        if data["eps_estimate"]:
            rate = (data["eps_actual"] - data["eps_estimate"]) / data["eps_estimate"] * 100
            line += f"；同季法人預估 EPS：{data['eps_estimate']:.2f} 元；官方驚喜度：{rate:+.2f}%"
        lines.append(line + "（來源：yfinance 實數）")
    if data["revenue_actual"] is not None:
        lines.append(
            f"- 最新一季實際營收：{_fmt_revenue(data['revenue_actual'])}（來源：yfinance 實數）"
        )
    if data["revenue_yoy"] is not None:
        lines.append(
            f"- 最近一季營收年增率（YoY）：{data['revenue_yoy']:+.2f}%（可用於推估基準）"
        )
    if data["avg_eps_4q"] is not None:
        lines.append(
            f"- 近 4 季平均 EPS：{data['avg_eps_4q']:.2f} 元（可作為法人預估基準）"
        )
    data["text"] = "\n".join(lines)
    return data


def _apply_fundamentals(parsed: dict, fund: dict) -> dict:
    """用 yfinance 真實數據覆蓋模型回傳值，並在有預估+實際時重算驚喜度。"""
    if not fund:
        return parsed

    if fund.get("revenue_actual") is not None:
        parsed["actual_revenue"] = (
            f"{_fmt_revenue(fund['revenue_actual'])}（yfinance 實數）"
        )

    eps_actual = fund.get("eps_actual")
    eps_estimate = fund.get("eps_estimate")
    if eps_actual is not None:
        parsed["actual_eps"] = f"{eps_actual:.2f} 元（yfinance 實數）"

    # 同時具備法人預估與實際 → 用真實數據直接計算驚喜度（非推估）
    if eps_actual is not None and eps_estimate:
        rate = (eps_actual - eps_estimate) / eps_estimate * 100
        parsed["expected_eps"] = f"{eps_estimate:.2f} 元（法人預估，yfinance 實數）"
        parsed["surprise_rate"] = round(rate, 2)
        parsed["surprise_status"] = _status_from_rate(rate)
        parsed["surprise_is_estimate"] = False
    return parsed


def _match_row(df: pd.DataFrame, candidates):
    """在 DataFrame 中找出名稱符合候選字串的列（不區分大小寫）。"""
    if df is None:
        return None
    lowered = {str(i).strip().lower(): i for i in df.index}
    for cand in candidates:
        if cand in lowered:
            return lowered[cand]
    for low, orig in lowered.items():
        if any(cand in low for cand in candidates):
            return orig
    return None


# 產業類別 → 對應追蹤的上游美股龍頭（Industry Mapping）
INDUSTRY_LEADS: dict = {
    "AI/伺服器": {
        "leads": ["NVDA", "MSFT"],
        "symbols": ["2317", "2382", "3231", "6669", "2368", "3324"],
        "keywords": ["鴻海", "廣達", "緯創", "緯穎", "金像電", "雙鴻", "AI", "伺服器"],
    },
    "半導體/設備": {
        "leads": ["ASML", "AMAT"],
        "symbols": ["2330", "2303", "3680", "3131", "6187", "1540"],
        "keywords": ["台積電", "聯電", "家登", "弘塑", "萬潤", "半導體", "設備"],
    },
    "網通/光通訊": {
        "leads": ["ANET", "CSCO"],
        "symbols": ["2345", "5388", "3596"],
        "keywords": ["智邦", "中磊", "智易", "網通", "光通訊"],
    },
    "車用/電力": {
        "leads": ["TSLA", "ON"],
        "symbols": ["2308", "3665", "2481"],
        "keywords": ["台達", "貿聯", "強茂", "車用", "電力", "電源"],
    },
    "預設": {
        "leads": ["NVDA", "AAPL"],
        "symbols": [],
        "keywords": [],
    },
}


def get_industry_lead(symbol: str, name: str = "") -> tuple[str, list]:
    """根據台股標的代號或名稱，回傳所屬產業類別與對應的上游美股龍頭。

    代號比對優先，其次用名稱關鍵字，皆未命中時回傳「預設」(NVDA, AAPL)。
    """
    code = str(symbol or "").split(".")[0].strip().upper()
    label = str(name or "")

    # 1) 代號比對
    for category, cfg in INDUSTRY_LEADS.items():
        if code and code in cfg["symbols"]:
            return category, list(cfg["leads"])

    # 2) 名稱關鍵字比對
    for category, cfg in INDUSTRY_LEADS.items():
        if label and any(kw in label for kw in cfg["keywords"]):
            return category, list(cfg["leads"])

    # 3) 預設
    default = INDUSTRY_LEADS["預設"]
    return "預設", list(default["leads"])


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_us_lead_indicators(ticker_symbol: str = "", stock_name: str = "") -> dict:
    """依產業別抓取對應 2 支上游美股最新財報，計算 CapEx 年增率與庫存天數(DOI)趨勢。

    產業對應見 INDUSTRY_LEADS（AI/伺服器→NVDA,MSFT；半導體→ASML,AMAT；
    網通→ANET,CSCO；車用/電力→TSLA,ON；預設→NVDA,AAPL）。

    回傳 {"NVDA": {capex_yoy, doi, doi_change}, ..., "category": 產業類別,
          "leads": [美股代號...], "text": 摘要文字}
    - capex_yoy  ：資本支出年增率 %（最新一季 vs 去年同季）
    - doi        ：庫存天數 = 季末庫存 / 季 COGS * 91.25
    - doi_change ：庫存天數較去年同期變化（天）
    """
    category, leads = get_industry_lead(ticker_symbol, stock_name)
    result = {
        ticker_id: {"capex_yoy": None, "doi": None, "doi_change": None}
        for ticker_id in leads
    }
    result["category"] = category
    result["leads"] = list(leads)

    for ticker_id in leads:
        info = result[ticker_id]
        try:
            tk = yf.Ticker(ticker_id)
        except Exception:
            continue

        # --- CapEx 年增率 ---
        try:
            cf = tk.quarterly_cash_flow
            if cf is not None and not cf.empty:
                row = _match_row(cf, ["capital expenditure", "capital exp"])
                if row is not None:
                    vals = [abs(float(v)) for v in cf.loc[row].dropna().tolist() if v]
                    if len(vals) >= 5 and vals[4]:
                        info["capex_yoy"] = (vals[0] - vals[4]) / abs(vals[4]) * 100
        except Exception:
            pass

        # --- 庫存天數 DOI 與變化趨勢 ---
        try:
            bs = tk.quarterly_balance_sheet
            inc = tk.quarterly_income_stmt
            inv_row = _match_row(bs, ["inventory", "inventories"])
            cogs_row = _match_row(inc, ["cost of revenue", "cost of sales"])
            if inv_row is not None and cogs_row is not None:
                inv = [float(v) for v in bs.loc[inv_row].dropna().tolist()]
                cogs = [abs(float(v)) for v in inc.loc[cogs_row].dropna().tolist()]
                dois = [
                    inv[i] / cogs[i] * 91.25
                    for i in range(min(len(inv), len(cogs)))
                    if cogs[i]
                ]
                if dois:
                    info["doi"] = dois[0]
                    if len(dois) >= 5:  # 與去年同期（4 季前）比較
                        info["doi_change"] = dois[0] - dois[4]
                    elif len(dois) >= 2:  # 備援：與前一季比較
                        info["doi_change"] = dois[0] - dois[1]
        except Exception:
            pass

    # --- 組成給 Prompt / 畫面用的摘要文字 ---
    lines = [f"- 產業類別：{category}（上游美股：{' / '.join(leads)}）"]
    for ticker_id in leads:
        info = result[ticker_id]
        capex, doi, change = info["capex_yoy"], info["doi"], info["doi_change"]
        parts = [
            f"{ticker_id}："
            + (f"CapEx 年增率 {capex:+.1f}%" if capex is not None else "CapEx 年增率（資料不足）")
        ]
        if doi is not None:
            trend = f"，較去年同期 {change:+.1f} 天" if change is not None else ""
            parts.append(f"庫存天數 {doi:.1f} 天{trend}")
        else:
            parts.append("庫存天數（資料不足）")
        lines.append("- " + "，".join(parts))
    result["text"] = "\n".join(lines)
    return result


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def calculate_revenue_acceleration(symbol: str) -> dict:
    """取得台股近 3 個月月營收 YoY，計算營收二階加速度並判定動能狀態。

    Acceleration = YoY_t - 2 × YoY_(t-1) + YoY_(t-2)
    狀態：【🚀 訂單剛爆發 (加速度正向)】/【⚖️ 穩健成長】/【⚠️ 成長減速中】
    主資料源：FinMind 月營收（免 token、有速率限制）
    備援：TWSE OpenAPI 單月營收（僅最新一個月，無法算加速度）
    """
    code = symbol.split(".")[0]
    out = {
        "code": code,
        "yoy_list": [],          # 近 3 個月 YoY（舊 → 新）
        "month_labels": [],      # 對應的年/月標籤
        "latest_yoy": None,
        "acceleration": None,
        "status": "⚪ 資料不足",
        "source": "",
        "text": "",
    }

    if code.startswith("00"):  # ETF（00 開頭）無個別公司月營收
        out["status"] = "⚪ ETF 無月營收資料"
        out["text"] = "- ETF 無個別公司月營收，營收二階加速度指標不適用。"
        return out

    # --- 1) FinMind 月營收歷史 ---
    monthly = {}
    try:
        start = (datetime.now() - timedelta(days=430)).strftime("%Y-%m-%d")
        resp = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={
                "dataset": "TaiwanStockMonthRevenue",
                "data_id": code,
                "start_date": start,
            },
            timeout=12,
        )
        for row in resp.json().get("data", []):
            year = int(row.get("revenue_year") or 0)
            month = int(row.get("revenue_month") or 0)
            revenue = _to_number(row.get("revenue"))
            if year and month and revenue is not None:
                monthly[(year, month)] = revenue
        if monthly:
            out["source"] = "FinMind 月營收"
    except Exception:
        monthly = {}

    # --- 2) 計算近 3 個月 YoY 與二階加速度 ---
    if monthly:
        recent = sorted(monthly)[-3:]
        yoy_values, labels = [], []
        for year, month in recent:
            prev = monthly.get((year - 1, month))
            cur = monthly[(year, month)]
            if prev:
                yoy_values.append((cur - prev) / abs(prev) * 100)
                labels.append(f"{year}/{month:02d}")
        if yoy_values:
            out["yoy_list"] = yoy_values
            out["month_labels"] = labels
            out["latest_yoy"] = yoy_values[-1]
            if len(yoy_values) >= 3:
                t, t1, t2 = yoy_values[-1], yoy_values[-2], yoy_values[-3]
                out["acceleration"] = t - 2 * t1 + t2

    # --- 3) 備援：TWSE OpenAPI（僅最新單月） ---
    if out["latest_yoy"] is None:
        try:
            resp = requests.get(
                "https://openapi.twse.com.tw/v1/opendata/t187ap05_L", timeout=15
            )
            for row in resp.json():
                if str(row.get("公司代號", "")) == code:
                    yoy = _to_number(row.get("營業收入-去年同月增減(%)"))
                    if yoy is not None:
                        out["latest_yoy"] = yoy
                        out["source"] = "TWSE OpenAPI（單月）"
                    break
        except Exception:
            pass

    # --- 4) 判定動能狀態 ---
    accel, latest = out["acceleration"], out["latest_yoy"]
    if accel is not None:
        if latest is not None and latest < 0:
            out["status"] = "⚠️ 成長減速中"
        elif accel > 0.5:
            out["status"] = "🚀 訂單剛爆發 (加速度正向)"
        elif accel < -0.5:
            out["status"] = "⚠️ 成長減速中"
        else:
            out["status"] = "⚖️ 穩健成長"
    elif latest is not None:
        out["status"] = "⚖️ 穩健成長" if latest >= 0 else "⚠️ 成長減速中"

    # --- 5) 組成給 Prompt / 畫面用的摘要文字 ---
    lines = []
    if out["month_labels"]:
        yoy_txt = "、".join(
            f"{lab} {val:+.1f}%"
            for lab, val in zip(out["month_labels"], out["yoy_list"])
        )
        lines.append(f"- 近 3 個月營收 YoY：{yoy_txt}（{out['source']}）")
    elif latest is not None:
        lines.append(
            f"- 最新單月營收 YoY：{latest:+.1f}%（{out['source']}，僅單月資料）"
        )
    else:
        lines.append("- 月營收資料暫時無法取得（資料源異常或標的無月營收）")
    if accel is not None:
        lines.append(
            f"- 營收二階加速度（YoY_t − 2×YoY_(t-1) + YoY_(t-2)）：{accel:+.2f}"
        )
    lines.append(f"- 動能狀態：{out['status']}")
    out["text"] = "\n".join(lines)
    return out


def _to_number(value):
    """把模型回傳的數值（數字或 '12.5%' 類字串）轉成 float，失敗回傳 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))
    return float(match.group()) if match else None


def _parse_analysis(text: str):
    """解析 Gemini 回傳的 JSON（容忍 markdown code fence 與雜訊）。"""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S).strip()

    data = None
    try:
        data = json.loads(cleaned)
    except Exception:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(cleaned[start:end + 1])
            except Exception:
                data = None

    if not isinstance(data, dict):
        return None

    health = str(data.get("company_health", "")).strip()
    timing = str(data.get("buy_timing", "")).strip()
    future = str(data.get("future_dynamics", "")).strip()
    if not (health or timing or future):
        return None

    try:
        score = int(round(float(data.get("health_score", 0))))
    except Exception:
        score = 0

    return {
        "health_score": max(0, min(100, score)),
        "company_health": health or "（模型未回傳）",
        "buy_timing": timing or "（模型未回傳）",
        "future_dynamics": future or "（模型未回傳）",
        # 財報與營收驚喜度（Earnings Surprise）欄位
        "surprise_status": str(data.get("surprise_status", "")).strip(),
        "surprise_rate": _to_number(data.get("surprise_rate")),
        "surprise_is_estimate": str(
            data.get("surprise_is_estimate", "true")
        ).strip().lower() in ("true", "1", "yes"),
        "expected_eps": str(data.get("expected_eps", "") or "").strip(),
        "actual_eps": str(data.get("actual_eps", "") or "").strip(),
        "expected_revenue": str(data.get("expected_revenue", "") or "").strip(),
        "actual_revenue": str(data.get("actual_revenue", "") or "").strip(),
        "surprise_outlook": str(data.get("surprise_outlook", "") or "").strip(),
        # 籌碼面（Market Capital Flow）欄位
        "chip_insight": str(data.get("chip_insight", "") or "").strip(),
        "chip_alert": str(data.get("chip_alert", "") or "").strip(),
        # 供應鏈與營收領先指標（Leading Signal）欄位
        "leading_outlook": str(data.get("leading_outlook", "") or "").strip(),
    }


def analyze_stock_with_gemini(stock_name, stock_symbol, indicator_summary: str = ""):
    """抓新聞 + 組 Prompt → 送 Gemini (gemini-3.8-flash)，回傳三健檢與財報驚喜度 dict。

    indicator_summary 由 market_data.build_indicator_summary 預先算好
    （收盤價 / 量能 Z-Score / 相對強度 RS% / 健康度評分），不傳送歷史資料陣列。
    失敗時丟出例外，由呼叫端捕捉並顯示詳細錯誤訊息；
    僅在未安裝套件或未設定金鑰時回傳 None。
    """
    api_key = get_gemini_api_key()
    if not GENAI_AVAILABLE or not api_key:
        return None

    news_block = fetch_recent_news(stock_name, stock_symbol)
    if not news_block:
        news_block = "（暫無近期網路新聞，請依技術面資料判斷，並註明資料有限）"

    # 資料備援：呼叫 Gemini 前，先從 yfinance 抓真實財報數據
    fund = fetch_fundamentals(stock_symbol)

    # 領先指標：依產業別抓對應上游美股 CapEx/DOI 與該股月營收二階加速度
    us_lead = fetch_us_lead_indicators(stock_symbol, stock_name)
    rev_accel = calculate_revenue_acceleration(stock_symbol)
    lead_category = us_lead.get("category", "預設")
    lead_tickers = us_lead.get("leads") or ["NVDA", "AAPL"]
    lead_names = " / ".join(lead_tickers)
    us_lead_text = us_lead.get("text") or f"（{lead_names} 領先指標資料暫時無法取得）"
    rev_accel_text = rev_accel.get("text") or "（月營收領先指標資料暫時無法取得）"

    prompt = f"""你是台股資深研究員，擅長基本面、技術面與題材面的綜合判斷。
請針對下列標的執行「AI 股市三健檢」，並**嚴格只回傳一個 JSON 物件**（不要 markdown、不要任何額外文字）。

【標的】{stock_name}（{stock_symbol}）
【Python 預先計算之指標摘要（收盤價、量能 Z-Score、相對強度 RS%、健康度評分）】{indicator_summary if indicator_summary else "（未提供）"}
【yfinance 即時財務數據（真實數據優先採用）】
{fund["text"] if fund["text"] else "（yfinance 未提供財務數據，請依規則 3 推估基準）"}
【近期網路新聞與法說摘要】
{news_block}

【上游領先指標（該股產業類別：{lead_category}，上游美股 {lead_names} 客觀財報數據，請直接引用）】
{us_lead_text}

【台廠月營收領先指標（近3月 YoY 與二階加速度）】
{rev_accel_text}

回傳 JSON 格式（四個區塊都不可省略）：
{{
  "health_score": 0到100的整數,
  "company_health": "【1. 公司健康度】評分理由、基本面護城河與主要風險，條列白話，200字內",
  "buy_timing": "【2. 買進時機點】結合當前股價與 20MA/60MA 位階，給明確的進場/觀望策略與停損思考，白話，200字內",
  "future_dynamics": "【3. 未來動態性】最新接單情況、訂單能見度、新產品與研發進度，條列，200字內",
  "chip_insight": "【籌碼面洞察】近5~10個交易日三大法人（外資/投信/自營）買賣超動向、千張大戶持股比率與總股東人數變化、籌碼集中或散亂判讀，條列120字內",
  "chip_alert": "若發現籌碼與股價背離（主力暗中建倉：籌碼集中但股價未漲／主力拉高出貨：散戶接盤），填『前瞻籌碼警訊：...』60字內；沒有發現則填空字串 \"\"",
  "leading_outlook": "前瞻供應鏈領先訊號摘要：結合上游美股 {lead_names} 的 CapEx 與庫存天數、以及台廠月營收二階加速度，說明該產業美股客戶/上游設備投資動向對該台股未來 3-6 個月訂單的實質影響，150字內",
  "surprise_status": "四選一：強烈超預期 🚀 / 符合預期 ⚖️ / 低於預期 ⚠️ / 資料不足 ❓",
  "surprise_rate": 驚喜度百分比數字（公式：（實際-預期）/預期×100，以最新一季 EPS 計算，保留兩位小數；無法計算時填 null）,
  "surprise_is_estimate": true或false（true=預估基準為推估、false=市場法人共識數字）,
  "expected_eps": "最新一季的市場法人預估 EPS（含季度與幣別）",
  "actual_eps": "最新公佈的實際 EPS（含季度與幣別）",
  "expected_revenue": "最新一季的市場法人預估營收（含單位）",
  "actual_revenue": "最新公佈的實際營收（含單位）",
  "surprise_outlook": "驚喜度對股價的前瞻影響：是否可能引發法人上修評等/目標價或追價買盤，120字內"
}}

要求：
1. 三大核心指標與財報驚喜度區塊必須涵蓋，缺一不可。
2. 【財報驚喜度】請檢索並對比市場**法人預估的 EPS / 營收**與公司**最新公佈的實際 EPS / 營收**，自行計算驚喜度百分比；若上方已提供 yfinance 真實數據，實際值一律以其為準。
3. **預估基準規則**：若新聞中未直接標示「法人預估數字」，請根據該股近期的**月營收年增率（YoY）**與**過去四季平均 EPS** 進行合理的基準預估再對比，並將 surprise_is_estimate 設為 true、在文字中標註「(推估)」；若來自市場法人共識則設為 false。
4. **狀態判斷基準**：驚喜度 > 5% →「強烈超預期 🚀」；-5% ~ +5% →「符合預期 ⚖️」；< -5% →「低於預期 ⚠️」。
5. **除非完全沒有任何財務數據，否則不要輕易輸出 N/A**；只有查無任何實際與可推估數據時，才填「資料不足 ❓」與 null，嚴禁虛構確定性數字。
6. 新聞若不足，請明確寫「網路資料有限」，不得把不確定資訊寫成確定事實。
7. 語氣務實，不得有保證獲利的字眼。
8. **【籌碼面 (Market Capital Flow)】請主動檢索**：近 5~10 個交易日**三大法人（外資、投信、自營商）的買賣超動向**（例如：外資連續賣超、投信連續買超/逢低布局），以及「**千張大戶持股比率**」與「**總股東人數**」的近期變化，判讀籌碼是趨於集中還是沉澱散亂。
9. **【籌碼融合與背離警訊】**將上述籌碼動向融入 `buy_timing`（買進時機點）與 `future_dynamics`（未來動態性）的評估中；若發現「**主力暗中建倉（籌碼集中但股價未漲）**」或「**主力逢高拉高出貨（散戶接盤）**」等籌碼與股價背離狀況，必須在 `chip_alert` 填入「前瞻籌碼警訊」加以提示，否則填空字串。
10. **【領先指標】**必須結合上方「上游美股 {lead_names} 的 CapEx 與庫存天數」與「台廠月營收二階加速度」兩項客觀數據，在 `leading_outlook` 產出 **150 字內**的前瞻供應鏈領先訊號摘要，具體說明該產業上游/客戶端動向對該台股未來 3-6 個月訂單的實質影響；數據若缺失請如實註明，不得虛構。"""

    try:
        client = genai.Client(api_key=api_key)
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json"
                ),
            )
        except Exception:
            # 舊版 SDK 不支援 response_mime_type 時，改用一般呼叫重試
            resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        parsed = _parse_analysis(resp.text)
        if parsed is None:
            raise ValueError(
                "模型回傳內容無法解析為 JSON，原始回應前 200 字："
                f"{str(resp.text)[:200]}"
            )
        # yfinance 有抓到實數時，直接覆蓋並帶入驚喜度計算
        return _apply_fundamentals(parsed, fund)
    except Exception:
        # 不吞掉錯誤：交由呼叫端捕捉，並在畫面上顯示完整錯誤訊息
        raise


# 尚未執行即時分析時顯示的靜態範例（中華電 2412）
SAMPLE_ANALYSIS = {
    "health_score": 86,
    "company_health": (
        "（靜態範例，非即時資料）\n"
        "• **評分理由**：國內電信龍頭，行動與寬頻市佔率領先，現金流與股利發放長期穩定。\n"
        "• **護城河**：網路基建規模、品牌信賴與高轉換成本，屬典型防禦型標的。\n"
        "• **主要風險**：5G 資本支出壓力、ARPU 成長有限，高股息特性也限制上檔空間。"
    ),
    "buy_timing": (
        "（靜態範例）\n"
        "• 股價守穩 20MA 之上且 60MA 走平，視為低檔築底完成的觀察區。\n"
        "• 回測 20MA 不破可分批布局、跌至年線附近再加碼；有效跌破 60MA 且量放大則退場觀望。\n"
        "• 適合長期持有領息，不宜期待短線價差。"
    ),
    "future_dynamics": (
        "（靜態範例）\n"
        "• **接單/能見度**：電信服務採合約制，能見度穩定但成長平緩。\n"
        "• **新產品**：企業雲、資安與 IoT 服務為新動能。\n"
        "• **研發進度**：持續投入 5G 與光纖覆蓋擴充，資本支出維持穩定水準。"
    ),
    # 財報與營收驚喜度（靜態範例）
    "surprise_status": "強烈超預期 🚀",
    "surprise_rate": 11.30,
    "surprise_is_estimate": True,  # 靜態範例：預估基準為推估
    "expected_eps": "1.15 元（2026 Q3）",
    "actual_eps": "1.28 元（2026 Q3）",
    "expected_revenue": "655.0 億（2026 Q3）",
    "actual_revenue": "671.4 億（2026 Q3）",
    "surprise_outlook": (
        "（靜態範例）單季 EPS 與營收雙雙超越法人預期，高股息標的易吸引法人上修目標價、"
        "並於除息前引發追價買盤；惟電信股股性平穩，驚喜度對股價的推升多屬溫和補漲而非飆漲。"
    ),
    # 籌碼面（靜態範例）
    "chip_insight": (
        "（靜態範例）\n"
        "• **三大法人近 5~10 日**：外資小幅賣超累計約 2,300 張，投信連 3 日買超、自營小幅調節。\n"
        "• **千張大戶持股比率**：由 61.2% 微升至 61.6%，籌碼略趨集中。\n"
        "• **總股東人數**：單週微增 0.4%，散戶增加有限，整體屬『集中微升、未見過度散亂』。"
    ),
    "chip_alert": "",  # 未見籌碼與股價背離，故不發出警訊
    # 供應鏈與營收領先指標（靜態範例）
    "leading_outlook": (
        "（靜態範例）NVDA 資本支出年增率維持高檔、AAPL 庫存天數回落，顯示美系客戶拉貨轉強；"
        "該股近 3 月營收 YoY 穩步走高且加速度為正，研判未來 3-6 個月訂單能見度維持樂觀，"
        "惟仍須留意客戶出貨節奏與庫存去化速度。"
    ),
}


def render_analysis_cards(result: dict) -> None:
    """將三健檢結果渲染為 info / warning / success 卡片，並附籌碼面洞察卡。"""
    score = result.get("health_score", 0)
    st.info(f"**🏥 【1. 公司健康度】**　評分 **{score}/100**  \n{result['company_health']}")
    st.warning(f"**⏱️ 【2. 買進時機點】**  \n{result['buy_timing']}")
    st.success(f"**🚀 【3. 未來動態性】**  \n{result['future_dynamics']}")

    # 📊 籌碼面洞察（三大法人動向 + 大戶集中度，含前瞻籌碼警訊）
    chip_insight = str(result.get("chip_insight", "") or "").strip()
    chip_alert = str(result.get("chip_alert", "") or "").strip()
    if chip_insight or chip_alert:
        with st.container(border=True):
            st.markdown(
                "**📊 籌碼面洞察：法人連買/連賣與大戶集中度 (Market Capital Flow)**"
            )
            if chip_insight:
                st.markdown(chip_insight)
            if chip_alert:
                st.warning(chip_alert)


def render_surprise_block(result: dict) -> None:
    """呈現「🎯 財報與營收驚喜度檢測」區塊（指標 + 狀態卡片）。"""
    with st.expander("🎯 財報與營收驚喜度檢測 (Earnings Surprise Model)", expanded=True):
        status = str(result.get("surprise_status", "")).strip() or "（模型未回傳狀態）"
        rate = result.get("surprise_rate")
        is_estimate = bool(result.get("surprise_is_estimate"))

        expected_eps = str(result.get("expected_eps", "") or "").strip() or "N/A"
        actual_eps = str(result.get("actual_eps", "") or "").strip() or "N/A"
        expected_rev = str(result.get("expected_revenue", "") or "").strip() or "N/A"
        actual_rev = str(result.get("actual_revenue", "") or "").strip() or "N/A"

        st.markdown(f"**驚喜度狀態：{status}**")

        # 預期 EPS / 實際 EPS / 驚喜度差距
        col_eps1, col_eps2, col_eps3 = st.columns(3)
        col_eps1.metric("法人預期 EPS", expected_eps)
        col_eps2.metric("實際公佈 EPS", actual_eps)
        if rate is not None:
            gap_value = f"{rate:+.2f}%" + (" (推估)" if is_estimate else "")
            gap_delta = f"{rate:+.2f}%" + (" 推估" if is_estimate else "")
        else:
            gap_value = "N/A"
            gap_delta = None
        col_eps3.metric("驚喜度差距 %", gap_value, delta=gap_delta)

        # 預期營收 / 實際營收
        col_rev1, col_rev2 = st.columns(2)
        col_rev1.metric("法人預期營收", expected_rev)
        col_rev2.metric("實際公佈營收", actual_rev)

        # 驚喜度狀態卡片（> 5% 綠色利多、< -5% 紅色警戒）
        if rate is None:
            st.info(
                "❓ **無法計算驚喜度**：模型未取得可靠的法人預估或實際財報數字"
                "（尚未進入財報季、或網路資料不足），請於財報公布後重試。"
            )
        elif rate > 5:
            st.success(
                f"🟢 **利多驚喜，具前瞻拉抬動能**（驚喜度 {rate:+.2f}%）："
                "實績明顯優於法人預期，後續可留意目標價與評等是否上修。"
            )
        elif rate < -5:
            st.error(
                f"🔴 **低於預期，防範賣壓**（驚喜度 {rate:+.2f}%）："
                "實績不如法人預期，短線可能面臨調節賣壓，建議保守觀察。"
            )
        else:
            st.info(
                f"⚖️ **符合預期**（驚喜度 {rate:+.2f}%）："
                "實績落在法人預估區間內，股價反應應以既有趨勢為主。"
            )

        outlook = str(result.get("surprise_outlook", "") or "").strip()
        if outlook:
            st.markdown(f"**🔬 驚喜度對股價的前瞻影響：**  \n{outlook}")


def render_leading_block(result: dict, symbol: str, label: str) -> None:
    """呈現「🔮 供應鏈與營收領先指標」卡片區塊。

    - 上方 st.metric：依產業別動態抓取的上游美股 CapEx 年增率與庫存天數變化
      （INDUSTRY_LEADS 對應，即時運算、快取 6h）
    - 中段：台股月營收二階加速度狀態與動能燈號
    - 下方 st.info：Gemini 提煉的未來 3-6 個月訂單前瞻展望
    """
    with st.expander("🔮 供應鏈與營收領先指標 (Leading Signal Model)", expanded=True):
        us = fetch_us_lead_indicators(symbol, label)
        accel = calculate_revenue_acceleration(symbol)
        lead_category = us.get("category", "預設")
        lead_tickers = us.get("leads") or ["NVDA", "AAPL"]

        # --- 上游巨頭領先指標（標題依產業動態切換） ---
        st.markdown(
            f"**🏭 {lead_category}上游領先指標 ({' / '.join(lead_tickers)} 最新財報)**"
        )
        cols = st.columns(2 * len(lead_tickers))
        for i, name in enumerate(lead_tickers):
            info = us.get(name) or {}
            capex = info.get("capex_yoy")
            doi = info.get("doi")
            doi_change = info.get("doi_change")
            cols[2 * i].metric(
                f"{name} 資本支出年增率",
                f"{capex:+.1f}%" if capex is not None else "N/A",
            )
            cols[2 * i + 1].metric(
                f"{name} 庫存天數 (DOI)",
                f"{doi:.1f} 天" if doi is not None else "N/A",
                delta=f"{doi_change:+.1f} 天 vs 去年同期" if doi_change is not None else None,
                delta_color="inverse",  # 庫存天數上升為警訊（紅色）
            )

        # --- 台股營收二階加速度與動能燈號 ---
        st.markdown(f"**📈 {label} 營收二階加速度（月營收動能）**")
        yoy_list = accel.get("yoy_list") or []
        acceleration = accel.get("acceleration")
        status = accel.get("status", "⚪ 資料不足")

        col1, col2, col3 = st.columns(3)
        col1.metric(
            "近 3 月營收 YoY",
            " → ".join(f"{v:+.1f}%" for v in yoy_list[-3:]) if yoy_list else "N/A",
        )
        col2.metric(
            "營收二階加速度",
            f"{acceleration:+.2f}" if acceleration is not None else "N/A",
        )
        if status.startswith("🚀"):
            light = "🟢"
        elif status.startswith("⚖️"):
            light = "🟡"
        elif status.startswith("⚠️"):
            light = "🔴"
        else:
            light = "⚪"
        col3.metric("動能燈號", f"{light} {status}")

        if accel.get("source"):
            st.caption(f"📡 營收資料來源：{accel['source']}")

        # --- Gemini 提煉的前瞻展望 ---
        outlook = str(result.get("leading_outlook", "") or "").strip()
        st.info(
            "**🔮 未來 3-6 個月訂單前瞻展望：**  \n"
            + (
                outlook
                if outlook
                else "（尚未執行 AI 分析，點擊上方按鈕即可取得 Gemini 提煉的領先訊號摘要）"
            )
        )


@st.cache_data(ttl=300, show_spinner=False)
def fetch_latest_prices(symbol_list) -> dict:
    """抓取多標的最新收盤價，回傳 {yfinance代號: 價格}（快取 5 分鐘）。"""
    prices = {}
    symbols = [s for s in dict.fromkeys(symbol_list) if s]
    if not symbols:
        return prices

    # 主路徑：一次批次下載；主代號為空時由 download_frames 自動改試備用代號
    try:
        price_frames = download_frames(symbols, period="5d")
        for sym in symbols:
            try:
                closes = price_frames.get(sym, pd.DataFrame())["Close"].dropna()
                if len(closes):
                    prices[sym] = float(closes.iloc[-1])
            except Exception:
                continue
    except Exception:
        pass

    # 備援：批次失敗的標的改用 fast_info 單獨抓
    for sym in symbols:
        if sym in prices:
            continue
        try:
            price = yf.Ticker(sym).fast_info["last_price"]
            if price:
                prices[sym] = float(price)
        except Exception:
            continue
    return prices


def _tx_is_closed(status) -> bool:
    """判斷交易狀態是否為已賣出/已平倉（不計入目前持股）。"""
    text = str(status or "").strip().lower()
    if not text or text == "nan":
        return False
    # 明確標示持有/open 的狀態優先判為未賣出
    if any(key in text for key in ("持有", "未賣", "hold", "holding", "open", "持股")):
        return False
    return any(key in text for key in ("賣", "sold", "closed", "平倉", "已實現"))


def render_transactions_tab() -> None:
    """📦 庫存帳本：讀取 Google Sheets Transactions，結合 yfinance 計算即時市值與未實現損益。"""
    st.subheader("📦 庫存帳本（Google Sheets Transactions）")

    tx = fetch_transactions_frame()
    if tx is None or tx.empty:
        st.info(
            "📝 目前 Google Sheets 的 `Transactions` 工作表沒有交易資料，或無法讀取。\n\n"
            "請在試算表中建立 `Transactions` 工作表，表頭為："
            "`symbol, name, buy_date, buy_price, shares, current_status`"
        )
        return

    # 欄位正規化（相容英文與中文表頭）
    tx = tx.rename(columns={c: str(c).strip().lower() for c in tx.columns})
    alias = {
        "代號": "symbol", "股票代號": "symbol",
        "名稱": "name", "股票名稱": "name",
        "買進日期": "buy_date", "成交日期": "buy_date",
        "買進價格": "buy_price", "成本價": "buy_price", "均價": "buy_price",
        "股數": "shares", "數量": "shares", "持股": "shares",
        "狀態": "current_status", "目前狀態": "current_status",
    }
    tx = tx.rename(columns={k: v for k, v in alias.items() if k in tx.columns})

    required = ["symbol", "buy_price", "shares"]
    missing = [c for c in required if c not in tx.columns]
    if missing:
        st.error(
            f"❌ Transactions 工作表缺少必要欄位：{', '.join(missing)}\n\n"
            "必要表頭：`symbol, name, buy_date, buy_price, shares, current_status`"
        )
        st.dataframe(tx, use_container_width=True)
        return

    # 整理資料
    tx["symbol"] = tx["symbol"].astype(str).str.strip().str.upper()
    tx["symbol"] = tx["symbol"].apply(
        lambda s: s if s.endswith((".TW", ".TWO")) else (f"{s}.TW" if s.split(".")[0].isdigit() else s)
    )
    tx["buy_price"] = pd.to_numeric(tx["buy_price"], errors="coerce")
    tx["shares"] = pd.to_numeric(tx["shares"], errors="coerce")
    if "name" not in tx.columns:
        tx["name"] = tx["symbol"]
    if "current_status" not in tx.columns:
        tx["current_status"] = ""
    if "buy_date" not in tx.columns:
        tx["buy_date"] = ""

    bad = tx["buy_price"].isna() | tx["shares"].isna() | (tx["shares"] <= 0)
    if bad.any():
        st.warning(f"⚠️ 有 {int(bad.sum())} 筆交易的買進價格或股數無法解析，已略過。")
        tx = tx[~bad].copy()

    if tx.empty:
        st.info("📝 沒有可計算的有效交易紀錄。")
        return

    # 抓即時股價並計算市值與損益
    with st.spinner("📡 正在抓取即時股價..."):
        prices = fetch_latest_prices(tx["symbol"].tolist())

    tx["現價"] = tx["symbol"].map(prices)
    tx["持股成本"] = tx["buy_price"] * tx["shares"]
    tx["目前市值"] = tx["現價"] * tx["shares"]
    tx["未實現損益"] = tx["目前市值"] - tx["持股成本"]
    tx["ROI %"] = (tx["未實現損益"] / tx["持股成本"] * 100).round(2)
    tx["已賣出"] = tx["current_status"].apply(_tx_is_closed)

    holdings = tx[~tx["已賣出"]].copy()
    missing_price = holdings["現價"].isna()
    if missing_price.any():
        st.warning(
            f"⚠️ {int(missing_price.sum())} 檔標的抓不到即時股價，相關欄位顯示為 N/A，"
            "且未計入總計。"
        )
        holdings = holdings[~missing_price]

    # ---- 上方總覽指標 ----
    total_cost = holdings["持股成本"].sum()
    total_value = holdings["目前市值"].sum()
    total_pl = holdings["未實現損益"].sum()
    total_roi = (total_pl / total_cost * 100) if total_cost else 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🏦 持股總成本", f"{total_cost:,.0f} 元")
    c2.metric("💰 目前總市值", f"{total_value:,.0f} 元")
    c3.metric("📈 未實現損益", f"{total_pl:+,.0f} 元", delta=f"{total_roi:+.2f}%")
    c4.metric("🎯 整體 ROI", f"{total_roi:+.2f}%",
              delta=f"{len(holdings)} 檔持股" if len(holdings) else "無持股")

    st.divider()

    # ---- 持股明細表 ----
    st.markdown("**📋 目前持股明細**")
    detail = tx.copy()
    detail["狀態"] = detail["current_status"].apply(lambda s: "✅ 持有" if not _tx_is_closed(s) else "✔️ 已賣出")
    show_cols = ["symbol", "name", "buy_date", "buy_price", "shares", "現價",
                 "持股成本", "目前市值", "未實現損益", "ROI %", "狀態"]
    show_cols = [c for c in show_cols if c in detail.columns]
    display_df = detail[show_cols].sort_values("symbol")

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            key: cfg
            for key, cfg in {
                "buy_price": st.column_config.NumberColumn("買進價", format="%.2f"),
                "shares": st.column_config.NumberColumn("股數", format="%.0f"),
                "現價": st.column_config.NumberColumn("即時股價", format="%.2f"),
                "持股成本": st.column_config.NumberColumn("持股成本", format="%.0f"),
                "目前市值": st.column_config.NumberColumn("目前市值", format="%.0f"),
                "未實現損益": st.column_config.NumberColumn("未實現損益", format="%.0f"),
                "ROI %": st.column_config.NumberColumn("ROI %", format="%.2f%%"),
            }.items()
            if key in display_df.columns
        },
    )

    csv_data = display_df.to_csv(encoding="utf-8-sig")
    st.download_button(
        label="⬇️ 下載庫存帳本明細 (CSV)",
        data=csv_data,
        file_name=f"portfolio_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )

    st.caption(
        "📎 資料來源：Google Sheets `Transactions` 工作表 + yfinance 即時股價"
        "（股價快取 5 分鐘）；未實現損益僅計入尚未賣出的持股。"
    )


# ---------------------------------------------------------
# 四層全方位健檢：儀表板 + 雷達圖 + 逐層展開詳情（UI 層）
# ---------------------------------------------------------
def render_health_check(
    df: pd.DataFrame,
    label: str,
    symbol: str,
    benchmark_close: pd.Series,
    start_date,
    eps_baseline: str,
    eps_alpha: float,
) -> None:
    """渲染四層全方位健檢 (4-Layer Corporate Health Check)。

    資料流：yfinance 財報／行情 + TWSE OpenAPI（融資、本益比、股價淨值比）
          + 美債 10Y（無風險利率代理）
          → health_check.build_health_report 純計算
          → 本函式只負責 UI 呈現（SRP 解耦）。

    任一資料源失敗時，health_check 會把該子項排除計分，
    本函式則以 N/A 欄位呈現，不會中斷整個分頁。
    """
    # ---- 1. 原始資料（皆有 @st.cache_data 快取與防禦式降級）----
    with st.spinner("載入四層健檢資料..."):
        fund = fetch_fundamentals(symbol) or {}
        margin_frame = fetch_margin_usage()
        market_frame = fetch_market_valuation()
        treasury = fetch_treasury_yield(start_date.strftime("%Y-%m-%d"))

    code = str(symbol).split(".")[0]
    margin_usage = lookup_margin_usage(margin_frame, code)
    pb_percentile = market_percentile(market_frame, code, "PBratio")

    # ---- 2. 估值快照（與「EPS 價值與缺口相關性」分頁共用同一套基準）----
    valuation = {
        "price": None,
        "ttm_eps": None,
        "pe_now": None,
        "pe_percentile": None,
        "fair_value": None,
        "vgr": None,
        "pb_percentile": pb_percentile,
    }
    if df is not None and not df.empty and "Close" in df.columns:
        close = pd.to_numeric(df["Close"], errors="coerce").dropna()
        if len(close) > 0:
            price = float(close.iloc[-1])
            eps_quarterly = fund.get("eps_quarterly") or {}
            baseline = compute_baseline_eps(
                quarterly_eps=eps_quarterly,
                baseline=eps_baseline,
                revenue_yoy_pct=fund.get("revenue_yoy"),
                alpha=eps_alpha,
            )
            # 歷史本益比固定以實際 TTM EPS 換算；合理價則套用側邊欄選定的基準
            ttm_series = build_ttm_eps_series(close.index, eps_quarterly)
            pe_history = compute_pe_history(close, ttm_series)
            fair = compute_fair_value(pe_history, baseline["eps"])
            valuation = compute_valuation_metrics(
                price=price,
                ttm_eps=float(ttm_series.iloc[-1]) if len(ttm_series) else None,
                pe_history=pe_history,
                fair=fair,
                pb_percentile=pb_percentile,
            )

    # ---- 3. 四層純計算 ----
    report = build_health_report(
        benchmark_close=benchmark_close,
        treasury_yield=treasury,
        market_median_pe=market_median_pe(market_frame),
        fund=fund,
        df=df,
        margin_usage=margin_usage,
        valuation=valuation,
    )
    layers = report["layers"]
    agg = report["agg"]
    total = agg.get("total")
    vetoes = agg.get("vetoes", [])

    # ---- A. 健檢總結儀表板 ----
    st.markdown("#### 🩺 健檢總結儀表板")
    st.caption(
        "四層權重：🌐 總經 **15%** ｜ 🏭 產業 **25%** ｜ 💰 籌碼 **25%** ｜ "
        "🏦 財務 **35%**　·　評分 0–100，**80 分以上為健康區間**"
    )

    col_score, col_grade, col_veto = st.columns(3)
    col_score.metric(
        "綜合健康評分",
        "N/A" if total is None else f"{total:.1f} 分",
        help="四層加權平均；資料不足的層不計入，其權重重新分配到其餘層。",
    )
    col_grade.metric(
        "健康等級",
        f"{agg.get('icon', '')} {agg.get('grade', '')}",
        help="🟢 ≥80 綠燈（健康）｜ 🟡 60–79 黃燈（觀察）｜ 🔴 <60 紅燈（警戒）",
    )
    col_veto.metric(
        "一票否決",
        f"{len(vetoes)} 項" if vetoes else "無",
        delta="🚨 有否決" if vetoes else "✅ 無",
        delta_color="normal" if vetoes else "off",
        help="任一層觸發一票否決時，該層分數封頂 40 分並顯示 🚨 警示。",
    )

    for text in vetoes:
        st.error(text)

    # ---- 四層健檢分析總結 ----
    st.info(build_summary_text(layers, agg))

    # ---- B. 四層蜘蛛雷達圖 ＋ 各層分數 ----
    left, right = st.columns([1.15, 1])
    with left:
        st.plotly_chart(
            build_radar_figure(layers),
            use_container_width=True,
            key="health_radar",
        )
    with right:
        st.markdown("**各層得分**")
        for item in layers:
            score = item.get("score")
            if score is None:
                st.warning(
                    f"{item['icon']} {item['name']}：資料不足，本層不計分"
                )
                continue
            st.progress(
                max(0.0, min(1.0, score / 100.0)),
                text=(
                    f"{item['icon']} {item['name']}（{item['weight'] * 100:.0f}%）"
                    f"　{score:.1f} 分 — {item['verdict']}"
                ),
            )

    # ---- C. 逐層展開診斷詳情 ----
    st.markdown("#### 🔬 逐層展開診斷詳情")
    for item in layers:
        score = item.get("score")
        score_text = "N/A" if score is None else f"{score:.1f}"
        with st.expander(
            f"{item['icon']} {item['name']}（權重 {item['weight'] * 100:.0f}%）"
            f" — {score_text} 分｜{item['verdict']}",
            expanded=False,
        ):
            metrics = item.get("metrics") or []
            for row_start in range(0, len(metrics), 4):
                row = metrics[row_start:row_start + 4]
                cols = st.columns(len(row))
                for col, metric in zip(cols, row):
                    with col:
                        st.metric(
                            metric.get("label", ""),
                            metric.get("value", ""),
                            delta=metric.get("delta"),
                            help=metric.get("help"),
                        )
            st.markdown(f"**🧠 健檢評語**：{item.get('narrative', '')}")
            st.markdown(f"**🧭 建議**：{item.get('advice', '')}")
            for veto_text in item.get("vetoes", []):
                st.error(veto_text)
            for note in item.get("notes", []):
                st.caption(f"ℹ️ {note}")

    st.caption(
        "📎 資料來源：yfinance（^TWII 大盤、^TNX 美債 10Y、個股三大財報）、"
        "台灣證交所 OpenAPI（MI_MARGN 融資餘額、BWIBBU_d 本益比與股價淨值比）。"
        "個股主力／外資／投信連續買賣超天數無免費 API，已以 A/D 累積線連續天數代理；"
        "台灣公債殖利率無公開 API，無風險利率採美債 10Y 代理。"
        "本模組為規則計算結果，僅供研究參考，非投資建議。"
    )
    if label:
        st.caption(f"📌 本輪健檢標的：{label}（{symbol}）｜ EPS 基準：{eps_baseline}")


if not selected_labels:
    st.warning("👈 請於左側邊欄選擇至少一檔股票或 ETF 開始載入數據。")
    # 未選擇標的時，仍可查看庫存帳本
    st.divider()
    render_transactions_tab()
else:
    symbols = [STOCK_DICT[label] for label in selected_labels]

    @st.cache_data(ttl=3600)  # 快取資料 1 小時以提升載入速度
    def load_data(symbol_list, start: str):
        # 回傳 {代號: DataFrame}；主代號為空時自動改試備用代號（先 .TWO 再 .TW）
        # start 必須是日期字串（非含微秒的 datetime），快取鍵才會穩定命中
        return download_frames(symbol_list, start=start)

    # 統一轉為 "YYYY-MM-DD" 字串：datetime.now() 含微秒，直接當快取鍵會每次 rerun 都失效
    raw_by_symbol = load_data(symbols, start_date.strftime("%Y-%m-%d"))

    # 資料載入全數失敗（斷線等）的統一檢查；個別標的缺資料則交由各分頁降級提示
    if not raw_by_symbol or all(df.empty for df in raw_by_symbol.values()):
        st.error("⚠️ 無法取得任何行情資料，請確認網路連線後再試一次。")
        st.stop()

    # 預先取出各標的的乾淨 OHLCV（以顯示名稱為 key），供後續分頁共用
    frames = {
        label: raw_by_symbol.get(STOCK_DICT[label], pd.DataFrame())
        for label in selected_labels
    }


    # 主畫面 8 大分頁
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
        "📊 核心觀察",
        "📈 K線與技術分析",
        "⚖️ 多標的走勢對比",
        "📋 歷史數據明細",
        "🏥 四層健檢與 AI 三健檢",
        "📦 庫存帳本",
        "🔍 K線缺口分析",
        "🧮 EPS價值與缺口相關性",
    ])

    # -----------------------------------------------------
    # Tab 1: 核心觀察 (Metrics)
    # -----------------------------------------------------
    with tab1:
        st.subheader("📌 自選標的即時概況")

        # 網格排版：每行最多 4 欄，超出的標的自動換行，避免一次性展開過多欄位造成畫面擠壓
        grid_columns = 4
        for row_start in range(0, len(selected_labels), grid_columns):
            row_labels = selected_labels[row_start:row_start + grid_columns]
            row_cols = st.columns(grid_columns)
            for col, label in zip(row_cols, row_labels):
                with col:
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
                        # 個別標的資料不足時降級顯示，不影響其他標的（AGENTS.md 防禦性程式設計）
                        st.warning(f"{label} 資料不足或載入異常")

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

    # -----------------------------------------------------
    # Tab 5: 四層全方位健檢（上）＋ AI 股市三健檢 (Gemini)（下）
    # -----------------------------------------------------
    with tab5:
        st.subheader("🏥 四層全方位健檢 (4-Layer Corporate Health Check)")

        if "ai_results" not in st.session_state:
            st.session_state["ai_results"] = {}

        # 頂部：股票選擇框（四層健檢與下方 AI 三健檢共用同一標的）
        focus_label_ai = st.selectbox(
            "選擇要分析的自選股標的：",
            selected_labels,
            key="ai_select",
        )
        focus_sym_ai = STOCK_DICT[focus_label_ai]

        # ---------- 四層健檢儀表板（規則計算，切換標的即時更新）----------
        render_health_check(
            df=frames.get(focus_label_ai),
            label=focus_label_ai,
            symbol=focus_sym_ai,
            benchmark_close=fetch_benchmark_close(start_date.strftime("%Y-%m-%d")),
            start_date=start_date,
            eps_baseline=eps_baseline,
            eps_alpha=eps_alpha,
        )

        st.divider()
        st.subheader("🤖 AI 股市三健檢與動態分析")

        api_key = get_gemini_api_key()
        ai_ready = GENAI_AVAILABLE and bool(api_key)

        # 環境檢查提示
        if not GENAI_AVAILABLE:
            st.warning("⚠️ 未安裝 google-genai 套件，請先執行：`pip install google-genai`")
        elif not api_key:
            st.warning(
                "⚠️ 未設定 GEMINI_API_KEY，請在 `.streamlit/secrets.toml` 加入："
                "`GEMINI_API_KEY = \"你的金鑰\"`"
            )

        # 分析按鈕
        run_clicked = st.button(
            "🚀 開始 AI 健檢與情報分析",
            type="primary",
            use_container_width=True,
            disabled=not ai_ready,
        )

        if run_clicked:
            # Python 預先算好指標摘要（收盤價 / 量能 Z-Score / RS% / 健康度評分），
            # 只把精簡摘要送進 Prompt，不傳送長篇歷史資料陣列
            indicator_summary = build_indicator_summary(
                frames.get(focus_label_ai),
                # 傳日期字串（而非含微秒的 datetime），確保快取鍵穩定可命中
                fetch_benchmark_close(start_date.strftime("%Y-%m-%d")),
            )
            try:
                with st.spinner("AI 正在檢索最新網路資料與分析中..."):
                    ai_result = get_cached_ai_analysis(
                        focus_label_ai,
                        focus_sym_ai,
                        indicator_summary,
                        analyze_stock_with_gemini,
                    )
            except Exception as e:
                # 除錯用：印出真正的錯誤資訊
                st.error(f"❌ AI 分析失敗，詳細錯誤訊息：{str(e)}")
            else:
                if ai_result is None:
                    st.error(
                        "❌ AI 分析失敗：未設定 GEMINI_API_KEY，"
                        "或模型未回傳可解析的結果。"
                    )
                else:
                    # 結果存入 session_state，切換分頁或元件後不會遺失
                    st.session_state["ai_results"][focus_label_ai] = ai_result

        st.divider()

        saved_result = st.session_state["ai_results"].get(focus_label_ai)
        if saved_result:
            st.caption(f"📊 以下為 **{focus_label_ai}** 最近一次的 AI 即時分析結果：")
            render_analysis_cards(saved_result)
            render_surprise_block(saved_result)
            render_leading_block(saved_result, focus_sym_ai, focus_label_ai)
        else:
            st.info(
                "👆 尚未對此標的執行分析，以下先顯示 **靜態範例（中華電 2412）**；"
                "點擊上方「🚀 開始 AI 健檢與情報分析」即可取得即時結果。"
            )
            render_analysis_cards(SAMPLE_ANALYSIS)
            render_surprise_block(SAMPLE_ANALYSIS)
            render_leading_block(SAMPLE_ANALYSIS, focus_sym_ai, focus_label_ai)

        # 底部警示與來源說明
        st.caption(
            "⚠️ 警示：AI 產出內容（含靜態範例）由語言模型生成，可能有誤、過時或遺漏，"
            "僅供研究參考，不構成任何投資建議；投資決策請自行查證並自負風險。\n\n"
            "📎 資料來源：Google News RSS 近期新聞標題、Python 預先計算之"
            "收盤價／量能 Z-Score／相對強度 RS%／健康度評分，"
            f"並由 {GEMINI_MODEL} 綜合研判；AI 結果按檔快取 24 小時，"
            "同檔當日重複點擊不再消耗 API Token。"
        )

    # -----------------------------------------------------
    # Tab 6: 庫存帳本 (Google Sheets Transactions + yfinance)
    # -----------------------------------------------------
    with tab6:
        render_transactions_tab()

    # -----------------------------------------------------
    # Tab 7: K 線缺口分析 (Gap Analysis)
    # -----------------------------------------------------
    with tab7:
        st.subheader("🔍 K 線缺口分析（Gap Analysis）")
        st.caption(
            "跳空缺口判定：向上缺口＝當日 Low > 前一日 High；"
            "向下缺口＝當日 High < 前一日 Low。"
            "缺口於後續交易日被穿越即判定為已回補。"
        )

        focus_label_gap = st.selectbox(
            "選擇要分析缺口的標的：",
            selected_labels,
            key="gap_select",
        )
        df_gap = frames.get(focus_label_gap, pd.DataFrame())

        # 頂部篩選器：最小幅度（過濾噪訊）＋ 回補狀態
        filter_col1, filter_col2 = st.columns([1, 1])
        with filter_col1:
            min_gap_pct = st.slider(
                "缺口最小幅度（%）",
                min_value=0.0,
                max_value=5.0,
                value=0.5,
                step=0.1,
                help="過濾小幅噪訊，預設只顯示幅度大於 0.5% 的缺口。",
            )
        with filter_col2:
            status_filter = st.selectbox(
                "回補狀態篩選",
                ["全部缺口", f"僅看{GAP_UNFILLED}", f"僅看{GAP_FILLED}"],
            )

        # 缺口類型多選篩選：預設全選＝全部；留空亦視為全部
        gap_class_filter = st.multiselect(
            "缺口類型篩選（Gap Type）",
            options=list(GAP_TYPE_OPTIONS),
            default=list(GAP_TYPE_OPTIONS),
            help=(
                "預設全選＝顯示全部分類；可只勾選「突破缺口」或「竭盡缺口」做單獨檢視。"
                "留空＝顯示全部。"
            ),
        )

        if df_gap.empty or len(df_gap) < 2:
            st.warning("⚠️ 該標的 K 線資料不足，無法進行缺口分析。")
        else:
            all_gaps = detect_gaps(df_gap)

            # 依最小幅度、回補狀態與缺口類型篩選
            filtered = all_gaps[all_gaps["缺口幅度(%)"] > min_gap_pct]
            if status_filter == f"僅看{GAP_UNFILLED}":
                filtered = filtered[filtered["是否回補"] == GAP_UNFILLED]
            elif status_filter == f"僅看{GAP_FILLED}":
                filtered = filtered[filtered["是否回補"] == GAP_FILLED]
            # 未選取任何分類時視為「全部」，避免誤清空後畫面全空
            if gap_class_filter:
                filtered = filtered[filtered["缺口分類"].isin(gap_class_filter)]

            # 依日期降冪排序（最新在最上面）
            filtered = filtered.sort_values(
                "日期", ascending=False
            ).reset_index(drop=True)

            # 數據指標卡片
            total_count = len(filtered)
            unfilled_count = int((filtered["是否回補"] == GAP_UNFILLED).sum())
            avg_gap = float(filtered["缺口幅度(%)"].mean()) if total_count else 0.0
            breakaway_count = int(
                (filtered["缺口分類"] == GAP_TYPE_BREAKAWAY).sum()
            )
            exhaustion_count = int(
                (filtered["缺口分類"] == GAP_TYPE_EXHAUSTION).sum()
            )

            row1_col1, row1_col2, row1_col3 = st.columns(3)
            row1_col1.metric("🕳️ 總缺口數", f"{total_count} 個")
            row1_col2.metric("⚠️ 未回補缺口數", f"{unfilled_count} 個")
            row1_col3.metric("🎯 已回補缺口數", f"{total_count - unfilled_count} 個")

            row2_col1, row2_col2, row2_col3 = st.columns(3)
            row2_col1.metric("🚀 突破缺口數量", f"{breakaway_count} 個")
            row2_col2.metric("🚨 竭盡缺口警告數量", f"{exhaustion_count} 個")
            row2_col3.metric("📊 平均缺口幅度", f"{avg_gap:.2f}%")

            if filtered.empty:
                st.info("✅ 目前篩選條件下沒有符合的缺口。")
            else:
                st.markdown("#### 📋 歷史缺口明細")
                st.dataframe(
                    filtered,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "日期": st.column_config.TextColumn("日期", width="small"),
                        "類型": st.column_config.TextColumn("類型", width="small"),
                        "缺口分類": st.column_config.TextColumn(
                            "缺口分類", width="medium"
                        ),
                        "缺口底部": st.column_config.NumberColumn(
                            "缺口底部", format="%.2f"
                        ),
                        "缺口頂部": st.column_config.NumberColumn(
                            "缺口頂部", format="%.2f"
                        ),
                        "缺口幅度(%)": st.column_config.NumberColumn(
                            "缺口幅度(%)", format="%.2f"
                        ),
                        "爆量倍數": st.column_config.NumberColumn(
                            "爆量倍數",
                            format="%.2f",
                            help="當日成交量 ÷ 20 日均量",
                        ),
                        "是否回補": st.column_config.TextColumn(
                            "是否回補", width="medium"
                        ),
                        "回補日期": st.column_config.TextColumn(
                            "回補日期", width="small"
                        ),
                    },
                )
                st.caption(
                    "📎 資料依日期降冪排序；「回補日期」為 N/A 代表尚未回補。"
                )

                # -------------------------------------------------
                # K 線圖：未回補缺口以色塊標示支撐／壓力區
                # -------------------------------------------------
                st.markdown("#### 📈 未回補缺口支撐／壓力區")
                fig_gap = go.Figure()
                fig_gap.add_trace(go.Candlestick(
                    x=df_gap.index,
                    open=df_gap["Open"],
                    high=df_gap["High"],
                    low=df_gap["Low"],
                    close=df_gap["Close"],
                    name=focus_label_gap,
                ))

                # 半透明色塊：向上缺口＝綠（支撐）、向下缺口＝紅（壓力）
                last_ts = pd.Timestamp(df_gap.index[-1])
                unfilled_rows = filtered[filtered["是否回補"] == GAP_UNFILLED]
                for _, row in unfilled_rows.iterrows():
                    gap_start = pd.Timestamp(row["日期"])
                    gap_end = (
                        last_ts
                        if last_ts > gap_start
                        else gap_start + pd.Timedelta(days=3)
                    )
                    is_up = row["類型"] == GAP_UP
                    fig_gap.add_vrect(
                        x0=gap_start,
                        x1=gap_end,
                        fillcolor=(
                            "rgba(46, 204, 113, 0.18)"
                            if is_up
                            else "rgba(229, 72, 77, 0.18)"
                        ),
                        line_width=0,
                        layer="below",
                    )

                fig_gap.update_layout(
                    title=f"{focus_label_gap} K 線與未回補缺口區間",
                    xaxis_title="日期",
                    yaxis_title="價格 (TWD)",
                    xaxis_rangeslider_visible=False,
                    height=520,
                    template="plotly_white",
                )
                st.plotly_chart(fig_gap, use_container_width=True)

                st.caption(
                    "🟢 半透明綠色區塊＝向上跳空缺口（動態支撐區，缺口底部為前一日 High）；"
                    "🔴 半透明紅色區塊＝向下跳空缺口（動態壓力區，缺口頂部為前一日 Low）。"
                    "色塊僅標示「未回補」缺口，已回補缺口請見上方明細表。"
                )

    # -----------------------------------------------------
    # Tab 8: EPS 價值與缺口相關性分析 (Valuation vs. Gap)
    # -----------------------------------------------------
    with tab8:
        st.subheader("🧮 EPS 價值與缺口相關性分析 (Valuation vs. Gap Correlation)")
        st.caption(
            "合理價＝TTM EPS × 歷史本益比（中位數）；估值下緣＝× 低點；估值上緣＝× 高點。"
            "VGR＝(缺口價格 − 合理價) ÷ 合理價 × 100%。"
            "缺口價格取缺口區間中點，報酬率以缺口當日收盤價為基準往後計算。"
        )

        focus_label_val = st.selectbox(
            "選擇要分析估值的標的：",
            selected_labels,
            key="valuation_select",
        )
        df_val = frames.get(focus_label_val, pd.DataFrame())
        symbol_val = STOCK_DICT[focus_label_val]

        if df_val.empty or len(df_val) < 60:
            st.warning(
                "⚠️ 該標的 K 線資料不足（需至少 60 個交易日），無法進行估值分析。"
            )
        else:
            # 取得逐季 EPS（與 AI 健檢分頁共用同一份快取，不會多打一次 API）
            fund_val = fetch_fundamentals(symbol_val) or {}
            eps_quarterly = fund_val.get("eps_quarterly") or {}
            gaps_val = detect_gaps(df_val)

            # 歷史本益比固定以「實際 TTM EPS」換算，反映市場真實給值
            ttm_eps = build_ttm_eps_series(df_val.index, eps_quarterly)
            pe_history = compute_pe_history(df_val["Close"], ttm_eps)

            # 依側邊欄選定的基準計算「動態 EPS (Calculated EPS)」
            baseline = compute_baseline_eps(
                quarterly_eps=eps_quarterly,
                baseline=eps_baseline,
                revenue_yoy_pct=fund_val.get("revenue_yoy"),
                alpha=eps_alpha,
            )
            st.caption(
                f"🧪 當前估算基準：{baseline['display']}｜{baseline['detail']}"
            )

            # 動態合理價 = Calculated EPS × 歷史本益比（中位數／低點／高點）
            fair = compute_fair_value(pe_history, baseline["eps"])

            if fair is None:
                st.warning(
                    "⚠️ 目前基準無法建立合理價，請切換 EPS 基準或檢查財報資料。"
                )
                st.caption(
                    f"套用基準：{baseline['label']}｜{baseline['detail']}。"
                    f"另需至少 {MIN_PE_POINTS} 個有效本益比樣本；"
                    "常見原因：ETF／指數型商品沒有 EPS、單季 EPS 為負值、"
                    "yfinance 未提供該股歷史財報，或上市時間過短。"
                )
            else:
                if baseline["warning"]:
                    st.warning(baseline["warning"])

                gap_table = build_valuation_gap_table(
                    gaps_val, df_val["Close"], fair["fair_value"]
                )
                star_gaps = find_near_support_gaps(gap_table, fair["support"])

                corr_20 = compute_correlation(
                    gap_table["VGR(%)"], gap_table["後續20日報酬率(%)"]
                )
                corr_60 = compute_correlation(
                    gap_table["VGR(%)"], gap_table["後續60日報酬率(%)"]
                )
                win_rate = compute_win_rate(gap_table)

                # ---------------- 數據指標區 ----------------
                metric_col1, metric_col2, metric_col3 = st.columns(3)
                r20 = corr_20["pearson_r"]
                p20 = corr_20["pearson_p"]
                if r20 is None:
                    metric_col1.metric(
                        "VGR 與後續報酬率相關係數",
                        "N/A",
                        help=f"有效樣本 {corr_20['n']} 筆；樣本不足或無變異無法計算。",
                    )
                else:
                    metric_col1.metric(
                        "VGR 與後續報酬率相關係數 (20日)",
                        f"r = {r20:+.3f}",
                        delta=(
                            f"P-value = {p20:.3f}"
                            if p20 is not None
                            else "P-value = N/A"
                        ),
                        delta_color="off",
                    )
                metric_col2.metric(
                    "EPS 合理價 (Fair Value)",
                    f"{fair['fair_value']:.2f} 元",
                    help=(
                        f"動態 EPS {fair['eps']:.2f} 元 × 歷史本益比中位數 "
                        f"{fair['pe_median']:.2f}（樣本 {fair['pe_points']} 個交易日）"
                        f"；{baseline['detail']}"
                    ),
                )
                metric_col3.metric(
                    "缺口回踩平均勝率 (%)",
                    "N/A" if win_rate is None else f"{win_rate:.1f}%",
                    help=(
                        "情境 A（-5% ≤ VGR ≤ +5%）的缺口中，"
                        "後續 20 日報酬率為正的比例。"
                    ),
                )

                # ---------------- 統計相關性摘要 ----------------
                st.markdown("#### 📊 相關係數與顯著性檢定")
                corr_df = pd.DataFrame(
                    [
                        {
                            "方法": "Pearson",
                            "20日相關係數": format_stat(corr_20["pearson_r"]),
                            "20日 P-value": format_stat(corr_20["pearson_p"]),
                            "60日相關係數": format_stat(corr_60["pearson_r"]),
                            "60日 P-value": format_stat(corr_60["pearson_p"]),
                        },
                        {
                            "方法": "Spearman",
                            "20日相關係數": format_stat(corr_20["spearman_r"]),
                            "20日 P-value": format_stat(corr_20["spearman_p"]),
                            "60日相關係數": format_stat(corr_60["spearman_r"]),
                            "60日 P-value": format_stat(corr_60["spearman_p"]),
                        },
                    ]
                )
                st.dataframe(corr_df, use_container_width=True, hide_index=True)
                st.caption(
                    f"有效樣本：20日 {corr_20['n']} 筆／60日 {corr_60['n']} 筆；"
                    "P-value < 0.05 視為顯著相關。"
                )

                if gap_table.empty:
                    st.info("✅ 該標的目前沒有符合條件的缺口事件，無法進行相關性分析。")
                else:
                    # ---------------- 統計散佈圖 ----------------
                    st.markdown("#### 📈 價值偏差率 VGR 與缺口後續報酬率")
                    horizon = st.radio(
                        "後續報酬期間",
                        ["20 日", "60 日"],
                        horizontal=True,
                        key="vgr_horizon",
                    )
                    y_col = (
                        "後續20日報酬率(%)"
                        if horizon == "20 日"
                        else "後續60日報酬率(%)"
                    )
                    y_label = f"缺口後續 {horizon} 股價報酬率 (%)"

                    fig_scatter = build_scatter_figure(gap_table, y_col, y_label)
                    st.plotly_chart(fig_scatter, use_container_width=True)

                    # ---------------- K 線 + 本益比河流對照 ----------------
                    st.markdown("#### 📈 K 線與 EPS 合理價河流對照")
                    fig_river = build_valuation_river_figure(
                        df=df_val,
                        pe_history=pe_history,
                        fair=fair,
                        gaps=gaps_val,
                        star_gaps=star_gaps,
                        title=f"{focus_label_val} K 線、EPS 合理價與本益比走勢",
                        baseline_label=baseline["display"],
                    )
                    st.plotly_chart(fig_river, use_container_width=True)
                    if star_gaps.empty:
                        st.caption("⭐ 目前沒有缺口落在估值下緣 ±5% 內。")
                    else:
                        st.success(
                            f"⭐ 有 {len(star_gaps)} 個缺口落在估值下緣 ±5% 內，"
                            "屬「基本面＋技術面」雙重支撐候選區。"
                        )

                    # ---------------- 統計分析數據表 + CSV ----------------
                    st.markdown("#### 📋 缺口估值事件明細")
                    st.dataframe(
                        gap_table,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "日期": st.column_config.TextColumn(
                                "日期", width="small"
                            ),
                            "缺口類型": st.column_config.TextColumn(
                                "缺口類型", width="medium"
                            ),
                            "缺口價格": st.column_config.NumberColumn(
                                "缺口價格", format="%.2f"
                            ),
                            "EPS合理價": st.column_config.NumberColumn(
                                "EPS合理價", format="%.2f"
                            ),
                            "VGR(%)": st.column_config.NumberColumn(
                                "VGR(%)", format="%.2f"
                            ),
                            "情境標記": st.column_config.TextColumn(
                                "情境標記", width="large"
                            ),
                            "缺口狀態": st.column_config.TextColumn(
                                "缺口狀態", width="medium"
                            ),
                            "後續20日報酬率(%)": st.column_config.NumberColumn(
                                "後續20日報酬率(%)", format="%.2f"
                            ),
                            "後續60日報酬率(%)": st.column_config.NumberColumn(
                                "後續60日報酬率(%)", format="%.2f"
                            ),
                            "預測評級": st.column_config.TextColumn(
                                "預測評級", width="small"
                            ),
                        },
                    )
                    st.download_button(
                        "📥 下載缺口估值分析 CSV",
                        data=gap_table.to_csv(index=False).encode("utf-8-sig"),
                        file_name=f"{symbol_val}_gap_valuation.csv",
                        mime="text/csv",
                    )
                    st.caption(
                        "📎 EPS 來源：yfinance 逐季公告日財報（備援為財期末），"
                        "本益比＝收盤價 ÷ 當日 TTM EPS；無效樣本以 N/A 呈現。"
                    )
