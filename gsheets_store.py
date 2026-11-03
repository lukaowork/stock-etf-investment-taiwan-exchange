"""Google Sheets 讀取共用模組（app.py 與 pages/ 多頁共用）。

- 服務帳戶模式（type = "service_account"）：走 st.connection，可讀寫
- 唯讀模式 / 未設定 secrets：走 Google Sheets 公開 gviz CSV 備援
- 憑證一律存放於 .streamlit/secrets.toml，程式碼中不出現明文
"""

import pandas as pd
import streamlit as st

# st-gsheets-connection 為可選套件，未安裝時自選股改存本機 session_state
try:
    from streamlit_gsheets import GSheetsConnection
    GSHEETS_AVAILABLE = True
except Exception:
    GSheetsConnection = None
    GSHEETS_AVAILABLE = False

# 試算表（Stock_Watchlist_Master）
GSHEETS_SPREADSHEET_ID = "11LXIBpOyjvqIrvzjkyoyc0pZSulI3D0in0va2cfi0aI"
GSHEETS_SPREADSHEET = (
    f"https://docs.google.com/spreadsheets/d/{GSHEETS_SPREADSHEET_ID}/edit?usp=sharing"
)

# 內建預設自選股（Sheets 讀取失敗或為空時的降級清單）
DEFAULT_WATCHLIST = {
    "台積電 (2330)": "2330.TW",
    "鴻海 (2317)": "2317.TW",
    "聯發科 (2454)": "2454.TW",
    "元大台灣50 (0050)": "0050.TW",
    "國泰永續高股息 (00878)": "00878.TW",
    "群益台灣精選高息 (00919)": "00919.TW",
    "復華台灣科技優息 (00929)": "00929.TW",
}


def get_gsheets_connection():
    """建立 st.connection("gsheets")；未安裝套件或未設定 secrets 時回傳 None。"""
    if not GSHEETS_AVAILABLE:
        return None
    try:
        return st.connection("gsheets", type=GSheetsConnection)
    except Exception:
        return None


def gsheets_can_write() -> bool:
    """是否為服務帳戶 CRUD 模式（可讀寫）；唯讀公開連結只能讀取。"""
    try:
        cfg = st.secrets["connections"]["gsheets"]
        try:
            raw = cfg.get("type", "")
        except Exception:
            raw = cfg["type"] if "type" in cfg else ""
        return str(raw).strip().lower() == "service_account"
    except Exception:
        return False


def _sheet_frame(worksheet: str):
    """讀取指定工作表為 DataFrame；失敗回傳 None。"""
    if gsheets_can_write():
        conn = get_gsheets_connection()
        if conn is not None:
            try:
                df = conn.read(worksheet=worksheet, ttl=60)
                if df is not None:
                    return pd.DataFrame(df)
            except Exception:
                pass
    try:
        url = (
            f"https://docs.google.com/spreadsheets/d/{GSHEETS_SPREADSHEET_ID}"
            f"/gviz/tq?tqx=out:csv&sheet={worksheet}"
        )
        return pd.read_csv(url)
    except Exception:
        return None


def load_watchlist_stocks() -> dict:
    """讀取 Watchlist 工作表，轉成 {顯示名稱: yfinance代號}；失敗回傳空 dict。"""
    df = _sheet_frame("Watchlist")
    if df is None or df.empty:
        return {}
    df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
    if "symbol" not in df.columns or "name" not in df.columns:
        return {}

    stocks = {}
    for _, row in df.iterrows():
        raw_symbol = str(row["symbol"]).strip().upper()
        name = str(row["name"]).strip()
        code = raw_symbol.split(".")[0]
        if not code.isdigit() or not name or name.lower() == "nan":
            continue
        symbol = raw_symbol if raw_symbol.endswith((".TW", ".TWO")) else f"{code}.TW"
        label = name if f"({code})" in name else f"{name} ({code})"
        stocks[label] = symbol
    return stocks


def fetch_transactions_frame():
    """讀取 Transactions 工作表（庫存帳本來源），失敗回傳 None。"""
    return _sheet_frame("Transactions")
