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


def _build_tech_summary(df: pd.DataFrame) -> str:
    """組出最新股價與 20MA/60MA 位階描述，供 Prompt 使用。"""
    if df is None or df.empty:
        return "（無技術面資料）"
    close = df["Close"].dropna()
    if len(close) < 2:
        return "（資料不足，無法計算均線）"

    price = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean()) if len(close) >= 20 else None
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else None

    def _pos(ma, name):
        if ma is None:
            return f"{name}：資料期間不足以計算"
        diff = (price - ma) / ma * 100
        where = "上方" if price > ma else "下方"
        return f"{name}={ma:.2f} 元（股價在其{where} {abs(diff):.2f}%）"

    parts = [
        f"最新收盤價 {price:.2f} 元",
        _pos(ma20, "20MA（月線）"),
        _pos(ma60, "60MA（季線）"),
    ]
    if ma20 is not None and ma60 is not None:
        parts.append(
            "均線型態：短均在長均之上（多頭排列）"
            if ma20 > ma60
            else "均線型態：短均在長均之下（空頭排列）"
        )
    return "；".join(parts) + "。"


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
      - revenue_yoy   ：最近一季營收年增率 YoY %
    """
    data = {
        "text": "",
        "eps_estimate": None,
        "eps_actual": None,
        "revenue_estimate": None,
        "revenue_actual": None,
        "avg_eps_4q": None,
        "revenue_yoy": None,
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


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def fetch_us_lead_indicators() -> dict:
    """yfinance 抓取 NVDA / AAPL 最新財報，計算 CapEx 年增率與庫存天數(DOI)趨勢。

    回傳 {"NVDA": {capex_yoy, doi, doi_change}, "AAPL": {...}, "text": 摘要文字}
    - capex_yoy  ：資本支出年增率 %（最新一季 vs 去年同季）
    - doi        ：庫存天數 = 季末庫存 / 季 COGS * 91.25
    - doi_change ：庫存天數較去年同期變化（天）
    """
    result = {
        "NVDA": {"capex_yoy": None, "doi": None, "doi_change": None},
        "AAPL": {"capex_yoy": None, "doi": None, "doi_change": None},
    }

    for ticker_id in ("NVDA", "AAPL"):
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
    lines = []
    for ticker_id in ("NVDA", "AAPL"):
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


def analyze_stock_with_gemini(stock_name, stock_symbol, tech_summary: str = ""):
    """抓新聞 + 組 Prompt → 送 Gemini (gemini-3.8-flash)，回傳三健檢與財報驚喜度 dict。

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

    # 領先指標：上游巨頭 CapEx/DOI 與該股月營收二階加速度
    us_lead = fetch_us_lead_indicators()
    rev_accel = calculate_revenue_acceleration(stock_symbol)
    us_lead_text = us_lead.get("text") or "（NVDA/AAPL 領先指標資料暫時無法取得）"
    rev_accel_text = rev_accel.get("text") or "（月營收領先指標資料暫時無法取得）"

    prompt = f"""你是台股資深研究員，擅長基本面、技術面與題材面的綜合判斷。
請針對下列標的執行「AI 股市三健檢」，並**嚴格只回傳一個 JSON 物件**（不要 markdown、不要任何額外文字）。

【標的】{stock_name}（{stock_symbol}）
【技術面數據】{tech_summary if tech_summary else "（未提供）"}
【yfinance 即時財務數據（真實數據優先採用）】
{fund["text"] if fund["text"] else "（yfinance 未提供財務數據，請依規則 3 推估基準）"}
【近期網路新聞與法說摘要】
{news_block}

【上游領先指標（NVDA/AAPL 客觀財報數據，請直接引用）】
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
  "leading_outlook": "前瞻供應鏈領先訊號摘要：結合 NVDA/AAPL 的 CapEx 與庫存天數、以及台廠月營收二階加速度，說明美股客戶拉貨狀況對該台股未來 3-6 個月訂單的實質影響，150字內",
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
10. **【領先指標】**必須結合上方「NVDA/AAPL CapEx 與庫存天數」與「台廠月營收二階加速度」兩項客觀數據，在 `leading_outlook` 產出 **150 字內**的前瞻供應鏈領先訊號摘要，具體說明美股客戶拉貨狀況對該台股未來 3-6 個月訂單的實質影響；數據若缺失請如實註明，不得虛構。"""

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

    - 上方 st.metric：NVDA/AAPL CapEx 年增率與庫存天數變化（即時運算、快取 6h）
    - 中段：台股月營收二階加速度狀態與動能燈號
    - 下方 st.info：Gemini 提煉的未來 3-6 個月訂單前瞻展望
    """
    with st.expander("🔮 供應鏈與營收領先指標 (Leading Signal Model)", expanded=True):
        us = fetch_us_lead_indicators()
        accel = calculate_revenue_acceleration(symbol)

        # --- 上游巨頭領先指標 ---
        st.markdown("**🏭 上游巨頭領先指標（NVDA / AAPL 最新財報）**")
        cols = st.columns(4)
        for i, name in enumerate(("NVDA", "AAPL")):
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


    # 主畫面 5 大分頁
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 核心觀察",
        "📈 K線與技術分析",
        "⚖️ 多標的走勢對比",
        "📋 歷史數據明細",
        "🤖 AI 股市三健檢與動態分析",
    ])

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

    # -----------------------------------------------------
    # Tab 5: AI 股市三健檢與動態分析 (Gemini)
    # -----------------------------------------------------
    with tab5:
        st.subheader("🤖 AI 股市三健檢與動態分析")

        if "ai_results" not in st.session_state:
            st.session_state["ai_results"] = {}

        api_key = get_gemini_api_key()
        ai_ready = GENAI_AVAILABLE and bool(api_key)

        # 頂部：股票選擇框
        focus_label_ai = st.selectbox(
            "選擇要分析的自選股標的：",
            selected_labels,
            key="ai_select",
        )
        focus_sym_ai = STOCK_DICT[focus_label_ai]

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
            tech_summary = _build_tech_summary(frames.get(focus_label_ai))
            try:
                with st.spinner("AI 正在檢索最新網路資料與分析中..."):
                    ai_result = analyze_stock_with_gemini(
                        focus_label_ai, focus_sym_ai, tech_summary
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
            "📎 資料來源：Google News RSS 近期新聞標題、本機行情計算之 20MA/60MA 均線位階，"
            f"並由 {GEMINI_MODEL} 綜合研判。"
        )
