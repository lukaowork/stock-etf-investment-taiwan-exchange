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
        "expected_eps": str(data.get("expected_eps", "") or "").strip(),
        "actual_eps": str(data.get("actual_eps", "") or "").strip(),
        "expected_revenue": str(data.get("expected_revenue", "") or "").strip(),
        "actual_revenue": str(data.get("actual_revenue", "") or "").strip(),
        "surprise_outlook": str(data.get("surprise_outlook", "") or "").strip(),
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

    prompt = f"""你是台股資深研究員，擅長基本面、技術面與題材面的綜合判斷。
請針對下列標的執行「AI 股市三健檢」，並**嚴格只回傳一個 JSON 物件**（不要 markdown、不要任何額外文字）。

【標的】{stock_name}（{stock_symbol}）
【技術面數據】{tech_summary if tech_summary else "（未提供）"}
【近期網路新聞與法說摘要】
{news_block}

回傳 JSON 格式（四個區塊都不可省略）：
{{
  "health_score": 0到100的整數,
  "company_health": "【1. 公司健康度】評分理由、基本面護城河與主要風險，條列白話，200字內",
  "buy_timing": "【2. 買進時機點】結合當前股價與 20MA/60MA 位階，給明確的進場/觀望策略與停損思考，白話，200字內",
  "future_dynamics": "【3. 未來動態性】最新接單情況、訂單能見度、新產品與研發進度，條列，200字內",
  "surprise_status": "四選一：強烈超預期 🚀 / 符合預期 ⚖️ / 低於預期 ⚠️ / 資料不足 ❓",
  "surprise_rate": 驚喜度百分比數字（公式：（實際-預期）/預期×100，以最新一季 EPS 計算，保留兩位小數；無法計算時填 null）,
  "expected_eps": "最新一季的市場法人預估 EPS（含季度與幣別）",
  "actual_eps": "最新公佈的實際 EPS（含季度與幣別）",
  "expected_revenue": "最新一季的市場法人預估營收（含單位）",
  "actual_revenue": "最新公佈的實際營收（含單位）",
  "surprise_outlook": "驚喜度對股價的前瞻影響：是否可能引發法人上修評等/目標價或追價買盤，120字內"
}}

要求：
1. 三大核心指標與財報驚喜度區塊必須涵蓋，缺一不可。
2. 【財報驚喜度】請檢索並對比市場**法人預估的 EPS / 營收**與公司**最新公佈的實際 EPS / 營收**，自行計算驚喜度百分比。
3. 狀態判斷基準：驚喜度 > 5% →「強烈超預期 🚀」；-5% ~ +5% →「符合預期 ⚖️」；< -5% →「低於預期 ⚠️」。
4. 若查不到可靠的預估或實際數字，surprise_status 填「資料不足 ❓」、surprise_rate 填 null、其餘財報欄位填「N/A」，**嚴禁虛構數字**。
5. 新聞若不足，請明確寫「網路資料有限」，不得把不確定資訊寫成確定事實。
6. 語氣務實，不得有保證獲利的字眼。"""

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
        return parsed
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
    "expected_eps": "1.15 元（2026 Q3）",
    "actual_eps": "1.28 元（2026 Q3）",
    "expected_revenue": "655.0 億（2026 Q3）",
    "actual_revenue": "671.4 億（2026 Q3）",
    "surprise_outlook": (
        "（靜態範例）單季 EPS 與營收雙雙超越法人預期，高股息標的易吸引法人上修目標價、"
        "並於除息前引發追價買盤；惟電信股股性平穩，驚喜度對股價的推升多屬溫和補漲而非飆漲。"
    ),
}


def render_analysis_cards(result: dict) -> None:
    """將三健檢結果渲染為 info / warning / success 三張卡片。"""
    score = result.get("health_score", 0)
    st.info(f"**🏥 【1. 公司健康度】**　評分 **{score}/100**  \n{result['company_health']}")
    st.warning(f"**⏱️ 【2. 買進時機點】**  \n{result['buy_timing']}")
    st.success(f"**🚀 【3. 未來動態性】**  \n{result['future_dynamics']}")


def render_surprise_block(result: dict) -> None:
    """呈現「🎯 財報與營收驚喜度檢測」區塊（指標 + 狀態卡片）。"""
    with st.expander("🎯 財報與營收驚喜度檢測 (Earnings Surprise Model)", expanded=True):
        status = str(result.get("surprise_status", "")).strip() or "（模型未回傳狀態）"
        rate = result.get("surprise_rate")

        expected_eps = str(result.get("expected_eps", "") or "").strip() or "N/A"
        actual_eps = str(result.get("actual_eps", "") or "").strip() or "N/A"
        expected_rev = str(result.get("expected_revenue", "") or "").strip() or "N/A"
        actual_rev = str(result.get("actual_revenue", "") or "").strip() or "N/A"

        st.markdown(f"**驚喜度狀態：{status}**")

        # 預期 EPS / 實際 EPS / 驚喜度差距
        col_eps1, col_eps2, col_eps3 = st.columns(3)
        col_eps1.metric("法人預期 EPS", expected_eps)
        col_eps2.metric("實際公佈 EPS", actual_eps)
        col_eps3.metric(
            "驚喜度差距 %",
            f"{rate:+.2f}%" if rate is not None else "N/A",
            delta=f"{rate:+.2f}%" if rate is not None else None,
        )

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
        else:
            st.info(
                "👆 尚未對此標的執行分析，以下先顯示 **靜態範例（中華電 2412）**；"
                "點擊上方「🚀 開始 AI 健檢與情報分析」即可取得即時結果。"
            )
            render_analysis_cards(SAMPLE_ANALYSIS)
            render_surprise_block(SAMPLE_ANALYSIS)

        # 底部警示與來源說明
        st.caption(
            "⚠️ 警示：AI 產出內容（含靜態範例）由語言模型生成，可能有誤、過時或遺漏，"
            "僅供研究參考，不構成任何投資建議；投資決策請自行查證並自負風險。\n\n"
            "📎 資料來源：Google News RSS 近期新聞標題、本機行情計算之 20MA/60MA 均線位階，"
            f"並由 {GEMINI_MODEL} 綜合研判。"
        )
