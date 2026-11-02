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

    # ---------------------------------------------------------
    # 產業與接單動態：模擬情報資料（展示用，非真實資料）
    # ---------------------------------------------------------
    INDUSTRY_MOCK = {
        "2330.TW": {
            "order": (
                "- **客戶投片量**：AI 加速器客戶 4Q26 投片量月增約 6~8%，N3/N2 產能利用率維持 95% 以上；"
                "智慧型手機客戶投片較前季持平。\n"
                "- **訂單排程**：CoWoS-L 先進封裝 12 月排程已滿，月產能再增約 8%；12 吋先進製程投片排至 2027/01。\n"
                "- **能見度摘要**：先進製程訂單能見度看到 **2027 上半年**；成熟製程能見度約 2~3 季，"
                "車用客戶仍積極去化庫存。"
            ),
            "rd": (
                "- **新產品開發**：N2 GAA 良率爬坡順利、按計畫 2H26 量產；N1.6 進入 risk production。\n"
                "- **認證進度**：N2 客戶產品已完成可靠性認證 3/4 家；CoWoS-L 通過大客戶系統級驗證。\n"
                "- **建廠量產時程**：寶山與高雄新產線 1Q27 設備驗證；熊本二廠 2027 量產；美國二廠預計 2028 量產。"
            ),
            "news": [
                {"date": "2026/10/30", "source": "法人研報", "title": "外資上調目標價，重申「加碼」評等",
                 "summary": "看好 2nm 2027 年貢獻營收雙位數成長，AI 相關營收占比將逾 20%。"},
                {"date": "2026/10/27", "source": "新聞", "title": "法說會釋出 4Q26 展望：美元營收季增個位數",
                 "summary": "毛利率維持 57~59%，先進製程與先進封裝為成長雙引擎。"},
                {"date": "2026/10/23", "source": "法人研報", "title": "CoWoS 產能調查：2027 年供給缺口收斂",
                 "summary": "券商預估 2027 年產能年增 6~8 成，惟需求仍集中於少數 AI 客戶。"},
            ],
        },
        "2317.TW": {
            "order": (
                "- **客戶投片量**：AI 伺服器機櫃 4Q26 出貨量月增約 10%，GB 系列客戶加單明確；"
                "消費電子零組件投片維持正常水準。\n"
                "- **訂單排程**：越南新產線 12 月試產、墨西哥廠擴建工程 1H27 完成；電動車新車 1Q27 開始拉貨。\n"
                "- **能見度摘要**：AI 伺服器訂單能見度看到 **2027 上半年**；傳統消費電子能見度約 1 季。"
            ),
            "rd": (
                "- **新產品開發**：新一代 AI 機櫃設計定案、液冷方案導入客戶驗證中；EV 平台新車款 1Q27 試量產。\n"
                "- **認證進度**：車用 ISO 26262 功能安全認證通過；部分醫療產品進入法規送審。\n"
                "- **建廠量產時程**：越南、墨西哥、泰國三地新廠依期推進，2027 年上半年陸續量產。"
            ),
            "news": [
                {"date": "2026/10/29", "source": "法人研報", "title": "AI 機櫃出貨放量，4Q 營運季增高個位數",
                 "summary": "券商上調 2027 年 EPS 預估，看好 AI 伺服器營收占比突破 25%。"},
                {"date": "2026/10/25", "source": "新聞", "title": "電動車品牌公布新車時程，年底啟動預售",
                 "summary": "法人關注量產初期毛利率與認列時點，短期對獲利貢獻有限。"},
                {"date": "2026/10/20", "source": "新聞", "title": "墨西哥廠擴建，因應客戶北美供應鏈需求",
                 "summary": "規劃新增組裝產線與倉儲，預計 2027 上半年完工。"},
            ],
        },
        "2454.TW": {
            "order": (
                "- **客戶投片量**：旗艦手機 SoC 客戶 4Q26 投片量季增約 5%，中高階晶片需求回溫；"
                "邊緣 AI 與車用晶片投片穩步成長。\n"
                "- **訂單排程**：3nm 旗艦平台 12 月投片排滿；NRE 專案排程已排至 2027 年 1 季。\n"
                "- **能見度摘要**：旗艦與邊緣 AI 產品能見度 **2~3 季**；IoT 與消費性晶片能見度約 1~2 季。"
            ),
            "rd": (
                "- **新產品開發**：新一代旗艦 SoC 進入 Design In、邊緣運算 AI 平台 2H27 推出。\n"
                "- **認證進度**：車用晶片通過部分車廠設計驗證；Wi-Fi 7 產品量產認證完成。\n"
                "- **建廠量產時程**：與代工夥伴 3nm 產能合作延續至 2027 年，無自建廠計畫（以 Fabless 為主）。"
            ),
            "news": [
                {"date": "2026/10/31", "source": "法人研報", "title": "旗艦平台導入率優於預期，維持「增持」",
                 "summary": "券商看好 2027 年 AI 手機滲透率提升帶來的 ASP 上揚。"},
                {"date": "2026/10/26", "source": "新聞", "title": "發表新一代邊緣 AI 平台，攜手雲端夥伴",
                 "summary": "強調端側推論效能與功耗優化，鎖定智慧裝置與機器人應用。"},
                {"date": "2026/10/21", "source": "法人研報", "title": "IoT 客戶庫存調整近尾聲",
                 "summary": "調查顯示部分客戶 4Q26 回補庫存，出貨可望於 1Q27 回溫。"},
            ],
        },
        "0050.TW": {
            "order": (
                "- **成分股獲利能見度**：前十大成分股 4Q26 獲利預估年增約 12%，以半導體與 AI 供應鏈為主幹。\n"
                "- **資金流與申購排程**：近 4 週淨申購維持正成長；成分股除息與權重調整於 9/12、12/12 生效。\n"
                "- **能見度摘要**：整體成分股營收能見度約 **1~2 季**，指數走勢與全球科技股情緒高度連動。"
            ),
            "rd": (
                "- **追蹤優化**：年中成分股審核完成，市值涵蓋率維持 70% 以上，追蹤誤差低於 0.5%。\n"
                "- **產品進度**：定期調整前十大權重，降低單一個股集中度風險。\n"
                "- **費用與規模**：基金規模穩健成長，經理費率維持低廉水準。"
            ),
            "news": [
                {"date": "2026/10/30", "source": "法人研報", "title": "台股權值行情延續，建議定期定額布局",
                 "summary": "法人認為 AI 供應鏈獲利成長仍實質支撐指數表現。"},
                {"date": "2026/10/24", "source": "新聞", "title": "ETF 權重調整名單公布，成分股無重大變動",
                 "summary": "本次審核維持原有成分結構，資金流向以內資定期定額為主。"},
            ],
        },
        "00878.TW": {
            "order": (
                "- **成分股配息能見度**：成分股 3Q26 配息陸續公布，年化配息率維持約 5~6% 區間。\n"
                "- **資金流與除息排程**：月配息機制於每月初除息，近期申購量回升。\n"
                "- **能見度摘要**：高股息成分股獲利能見度約 **1~2 季**，受惠 AI 與傳產雙輪動。"
            ),
            "rd": (
                "- **追蹤調整**：ESG 過濾條件維持嚴格，成分股汰換率低於 15%。\n"
                "- **產品進度**：受益人數持續增加，規模穩居高股息 ETF 前段班。\n"
                "- **風險控管**：單一產業上限設定，降低電子權重過度集中。"
            ),
            "news": [
                {"date": "2026/10/28", "source": "新聞", "title": "月配息金額公布，填息表現穩健",
                 "summary": "近期除息後 5 個交易日內完成填息，維持穩定現金流特性。"},
                {"date": "2026/10/19", "source": "法人研報", "title": "高股息資金動能回溫，散戶定期定額加碼",
                 "summary": "法人觀察內資對防禦型標的配置比重上升。"},
            ],
        },
        "00919.TW": {
            "order": (
                "- **成分股獲利能見度**：成分股以航運、金融與電子為主，4Q26 獲利能見度約 1~2 季。\n"
                "- **資金流與除息排程**：季配息機制，12 月除息前預期有資金卡位。\n"
                "- **能見度摘要**：高息成分股殖利率支撐明顯，但需留意產業循環波動。"
            ),
            "rd": (
                "- **追蹤調整**：精選高息篩選邏輯維持，前十大權重集中度偏高需留意。\n"
                "- **產品進度**：配息紀錄延續，受益人數維持高檔。\n"
                "- **風險控管**：航運與鋼鐵族群占比高，景氣反轉時波動較大。"
            ),
            "news": [
                {"date": "2026/10/27", "source": "新聞", "title": "宣布配息金額，年化殖利率約 5%",
                 "summary": "除息日訂於 12 月初，投信提醒留意短期殖利率題材反應。"},
                {"date": "2026/10/18", "source": "法人研報", "title": "航運族群旺季後獲利趨緩，留意權重影響",
                 "summary": "建議投資人關注成分股產業集中度帶來的波動。"},
            ],
        },
        "00929.TW": {
            "order": (
                "- **成分股獲利能見度**：科技高息成分股 4Q26 獲利年增約 8%，AI 供應鏈為主要貢獻。\n"
                "- **資金流與除息排程**：月配息機制，近期申購與贖回趨於平衡。\n"
                "- **能見度摘要**：成分股獲利能見度約 **1~2 季**，配息來源以資本利得與股利並行。"
            ),
            "rd": (
                "- **追蹤調整**：科技主題篩選維持，電子股占比逾七成。\n"
                "- **產品進度**：月配息穩定，受益人數居月配息 ETF 前列。\n"
                "- **風險控管**：科技股集中度高，市場修正時淨值波動較大。"
            ),
            "news": [
                {"date": "2026/10/29", "source": "新聞", "title": "月配息金額持平，年化殖利率約 6%",
                 "summary": "投信表示配息主要來自股利與已實現資本利得，財務體質正常。"},
                {"date": "2026/10/17", "source": "法人研報", "title": "科技股波動加大，建議以分批布局取代單筆",
                 "summary": "法人提醒月配息不等於保證報酬，仍須關注成分股估值。"},
            ],
        },
    }

    def _fallback_brief(label: str, symbol: str) -> dict:
        """使用者自行新增、無預設模擬資料的標的，產生通用版模擬摘要。"""
        code = symbol.split(".")[0]
        if code.startswith("00"):  # ETF
            return {
                "order": (
                    f"- **成分股獲利能見度**：（模擬）{label} 成分股整體營收能見度約 1~2 季。\n"
                    "- **資金流與排程**：（模擬）近期申購/贖回量平穩，除息排程依基金公告為準。\n"
                    "- **能見度摘要**：（模擬）指數型商品走勢與大盤連動，殖利率題材支撐評價。"
                ),
                "rd": (
                    "- **追蹤調整**：（模擬）成分股審核依指數規則定期進行。\n"
                    "- **產品進度**：（模擬）基金規模與受益人數穩定成長。\n"
                    "- **風險控管**：（模擬）留意產業集中度與市場系統性風險。"
                ),
                "news": [
                    {"date": "2026/10/26", "source": "新聞", "title": f"{label} 近期資金動能觀察（模擬）",
                     "summary": "（模擬數據）內資定期定額買盤為主要支撐，短線籌碼穩定。"},
                ],
            }
        return {
            "order": (
                f"- **客戶投片量**：（模擬）{label} 近期主要客戶投片量月增約 3~5%。\n"
                "- **訂單排程**：（模擬）產線排程已排至下季初，急單比例小幅上升。\n"
                "- **能見度摘要**：（模擬）訂單能見度約 **2 季**，需持續觀察終端需求。"
            ),
            "rd": (
                f"- **新產品開發**：（模擬）{label} 新產品進入客戶驗證階段。\n"
                "- **認證進度**：（模擬）主要產品認證進度符合預期。\n"
                "- **建廠量產時程**：（模擬）產能擴建依計畫推進，預計明年上半年量產。"
            ),
            "news": [
                {"date": "2026/10/26", "source": "法人研報", "title": f"{label} 近況追蹤（模擬）",
                 "summary": "（模擬數據）券商評等與目標價僅供展示，非真實研究報告。"},
            ],
        }

    # 主畫面 5 大分頁
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 核心觀察",
        "📈 K線與技術分析",
        "⚖️ 多標的走勢對比",
        "📋 歷史數據明細",
        "📰 產業與接單動態",
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
    # Tab 5: 產業與接單動態 (Industry & Order Dynamics)
    # -----------------------------------------------------
    with tab5:
        st.subheader("📰 產業與接單動態")
        focus_label_news = st.selectbox(
            "選擇要調閱情報的自選股標的：",
            selected_labels,
            key="news_select",
        )
        focus_sym_news = STOCK_DICT[focus_label_news]
        brief = INDUSTRY_MOCK.get(focus_sym_news) or _fallback_brief(focus_label_news, focus_sym_news)

        # 左右雙欄：訂單能見度 / 研發進度
        col_order, col_rd = st.columns(2)

        with col_order:
            st.info(f"**【📦 訂單能見度與接單狀況】**  \n{brief['order']}")

        with col_rd:
            st.success(f"**【🔬 研發與新技術進度】**  \n{brief['rd']}")

        # 近期法人研報與新聞提煉明細
        with st.expander("📄 近期的法人研報與新聞提煉明細"):
            for item in brief["news"]:
                st.markdown(
                    f"**🗓️ {item['date']}｜{item['source']}｜{item['title']}**"
                )
                st.write(item["summary"])
                st.divider()

        # 底部警示與來源說明
        st.caption(
            "⚠️ 警示：本分頁所有內容皆為**模擬展示資料**，僅用於介面功能示範，"
            "不代表真實訂單、研發進度或法人意見，亦不構成任何投資建議；"
            "實際投資決策請自行查證並自負風險。\n\n"
            "📎 資料來源（模擬假設）：公開資訊觀測站、公司法說會/重大訊息、券商研究報告、"
            "產業新聞等公開管道；正式版本應接上上述來源之即時資料。"
        )
