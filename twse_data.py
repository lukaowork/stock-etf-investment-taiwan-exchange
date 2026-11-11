"""台灣證交所 (TWSE) OpenAPI 資料存取層。

依 agents.md 規範：
- 只負責外部資料抓取與正規化，不含任何評分或 UI 邏輯（單一職責原則）
- 每個函式皆以 try-except 包覆，抓取失敗回傳空結構，由呼叫端優雅降級
- 以 @st.cache_data 快取，避免頻繁請求證交所造成延遲與限流

已實測可用端點（2026-11 驗證）：
- GET /v1/exchangeReport/MI_MARGN   集中市場融資融券餘額（個股，含融資限額）
- GET /v1/exchangeReport/BWIBBU_d   上市個股日本益比、殖利率及股價淨值比

已實測「不可用」的項目（避免日後重複踩雷）：
- 個股三大法人買賣超日報、個股主力分點買賣超 → 無免費 API
- 個股融資「歷史」序列 → 僅提供當日快照，無法計算連續天數
- 上櫃（TPEx）融資／法人端點 → 路徑已改版，回傳 404
"""

import pandas as pd
import requests
import streamlit as st

TWSE_API_BASE = "https://openapi.twse.com.tw/v1"
_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
_HTTP_TIMEOUT = 12

# 融資表固定輸出欄位（空表也維持相同欄位，避免呼叫端 KeyError）
MARGIN_COLUMNS = ["股票代號", "融資餘額", "融資限額", "融資使用率(%)"]
VALUATION_COLUMNS = ["Code", "Name", "PEratio", "PBratio", "DividendYield", "ClosePrice"]


def _get_json(url: str, params: dict | None = None):
    """發送 GET 並解析 JSON；任何例外回傳 None（防禦性設計）。"""
    try:
        resp = requests.get(
            url, params=params, headers=_HTTP_HEADERS, timeout=_HTTP_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def _to_number(value):
    """把證交所字串（含千分位逗號、'-'、空字串）轉為 float；失敗回傳 None。"""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--", "N/A", "null", "None"):
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_margin_usage() -> pd.DataFrame:
    """個股融資使用率 = 融資今日餘額 / 融資限額 × 100（%）。

    回傳 MARGIN_COLUMNS 的 DataFrame；抓取失敗回傳空表。
    僅涵蓋集中市場（上市），上櫃股票查無資料，由呼叫端降級提示。
    """
    payload = _get_json(f"{TWSE_API_BASE}/exchangeReport/MI_MARGN")
    if not isinstance(payload, list):
        return pd.DataFrame(columns=MARGIN_COLUMNS)

    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        code = str(item.get("股票代號", "")).strip()
        balance = _to_number(item.get("融資今日餘額"))
        cap = _to_number(item.get("融資限額"))
        if not code or balance is None or cap is None or cap <= 0:
            continue
        rows.append(
            {
                "股票代號": code,
                "融資餘額": balance,
                "融資限額": cap,
                "融資使用率(%)": round(balance / cap * 100.0, 2),
            }
        )
    return pd.DataFrame(rows, columns=MARGIN_COLUMNS)


def lookup_margin_usage(margin_frame: pd.DataFrame, code: str) -> float | None:
    """由融資表取出指定代號的融資使用率 (%)；查無或空表回傳 None。"""
    if margin_frame is None or margin_frame.empty:
        return None
    code = str(code or "").strip()
    if not code:
        return None
    hit = margin_frame.loc[margin_frame["股票代號"] == code, "融資使用率(%)"]
    if hit.empty:
        return None
    return float(hit.iloc[0])


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_market_valuation() -> pd.DataFrame:
    """上市個股日本益比、殖利率、股價淨值比（全市場橫斷面，每日收盤後更新）。

    用途：
      1. 市場中位本益比 → 總經層的市場風險溢酬 (ERP) 之 E/P
      2. 個股 P/B 的全市場百分位 → 財務層的估值安全度

    回傳 VALUATION_COLUMNS 的 DataFrame；抓取失敗回傳空表。
    """
    payload = _get_json(f"{TWSE_API_BASE}/exchangeReport/BWIBBU_d")
    if not isinstance(payload, list):
        return pd.DataFrame(columns=VALUATION_COLUMNS)

    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "Code": str(item.get("Code", "")).strip(),
                "Name": str(item.get("Name", "")).strip(),
                "PEratio": _to_number(item.get("PEratio")),
                "PBratio": _to_number(item.get("PBratio")),
                "DividendYield": _to_number(item.get("DividendYield")),
                "ClosePrice": _to_number(item.get("ClosePrice")),
            }
        )
    return pd.DataFrame(rows, columns=VALUATION_COLUMNS)


def market_median_pe(market_frame: pd.DataFrame) -> float | None:
    """全市場本益比中位數（剔除虧損與無效值）；失敗回傳 None。

    以中位數而非加權平均，可避免個別極端值與權重資料缺失造成失真。
    """
    if market_frame is None or market_frame.empty or "PEratio" not in market_frame:
        return None
    pe = pd.to_numeric(market_frame["PEratio"], errors="coerce").dropna()
    pe = pe[pe > 0]
    if pe.empty:
        return None
    return float(pe.median())


def market_percentile(market_frame: pd.DataFrame, code: str, column: str) -> float | None:
    """指定代號的數值在全市場同欄位中的百分位（0~100，數值越低越便宜）。

    查無該股、欄位不存在，或全市場有效樣本不足 20 筆時回傳 None，
    避免樣本過少造成百分位失真。
    """
    if market_frame is None or market_frame.empty:
        return None
    if column not in market_frame.columns:
        return None

    values = pd.to_numeric(market_frame[column], errors="coerce").dropna()
    values = values[values > 0]
    if len(values) < 20:
        return None

    code = str(code or "").strip()
    if not code:
        return None
    hit = market_frame.loc[market_frame["Code"] == code, column]
    if hit.empty:
        return None

    own = pd.to_numeric(hit, errors="coerce").iloc[0]
    own = _to_number(own)
    if own is None or own <= 0:
        return None
    return float((values < own).mean() * 100.0)
