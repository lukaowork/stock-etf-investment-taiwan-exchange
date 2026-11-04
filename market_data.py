"""市場資料共用模組：OHLCV 欄位正規化與每日異動指標計算。

依 agents.md 規範：
- 業務邏輯（Z-Score / 相對強度 / 評分）集中於此，不含任何 UI（SRP）
- 外部資料判空與 try-except 優雅降級
- 評分維持 0 ~ 100 分量化機制，80 分以上為健康區間
"""

import pandas as pd
import streamlit as st
import yfinance as yf

# yf.download 所需的標準欄位
REQUIRED_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# 相對強度基準指數（agents.md 命名範例 ^TWII）
DEFAULT_BENCHMARK = "^TWII"


def get_symbol_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """從 yf.download 的回傳值取出單一標的的 OHLCV 資料；失敗回傳空 DataFrame。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame()

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

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return pd.DataFrame()
    return df[REQUIRED_COLUMNS].dropna(how="all")


def ticker_candidates(symbol: str) -> list:
    """產生 yfinance 代號候選清單，自動相容上市（.TW）與上櫃（.TWO）後綴。

    第 1 個為主代號（優先請求，避免多餘流量）；若主代號回傳為空，
    依序改試備用代號：先 .TWO（上櫃），再 .TW（上市）。
    非台股純數字代號（如 ^TWII、AAPL）不加後綴，只回傳原代號。
    """
    raw = str(symbol or "").strip().upper()
    if not raw:
        return []
    code = raw.split(".")[0]
    if not code.isdigit():
        return [raw]

    primary = raw if raw.endswith((".TW", ".TWO")) else f"{code}.TW"
    candidates = [primary]
    for suffix in (".TWO", ".TW"):
        alternate = f"{code}{suffix}"
        if alternate not in candidates:
            candidates.append(alternate)
    return candidates


def download_frames(symbols, start=None, period=None, threads=True) -> dict:
    """批次下載多檔標的的 OHLCV，回傳 {原始代號: DataFrame}。

    - 主代號回傳為 Empty（例如上櫃股票被預設成 .TW）時，自動改試備用代號：
      先 .TWO（上櫃），再 .TW（上市）；且只針對失敗標的補抓，避免重複請求
    - 無法取得資料的標的回傳空 DataFrame，由呼叫端以 st.warning 降級提示
    - 本函式不含快取，請由呼叫端以 @st.cache_data(ttl=3600) 包覆（AGENTS.md 快取規範）
    """
    requested = []
    for item in symbols or []:
        code = str(item).strip().upper()
        if code and code not in requested:
            requested.append(code)
    if not requested:
        return {}

    # 主代號：未帶上市/上櫃後綴的純數字代號補上預設 .TW
    primary = {code: ticker_candidates(code)[0] for code in requested}

    def _download(codes):
        """執行一次 yf.download；失敗或空結果回傳 None。"""
        if not codes:
            return None
        kwargs = {"progress": False, "group_by": "ticker", "threads": threads}
        if start is not None:
            kwargs["start"] = start
        else:
            kwargs["period"] = period or "1mo"
        try:
            raw = yf.download(list(codes), **kwargs)
        except Exception:
            return None
        if raw is None or len(raw) == 0:
            return None
        return raw

    frames = {}
    pending = []

    raw = _download(list(dict.fromkeys(primary.values())))
    for code in requested:
        frame = get_symbol_frame(raw, primary[code]) if raw is not None else pd.DataFrame()
        if frame is not None and not frame.empty:
            frames[code] = frame
        else:
            pending.append(code)

    # 主代號回傳 Empty：批次改試備用代號（先 .TWO 上櫃，再 .TW 上市）
    backup = {}
    for code in pending:
        for candidate in ticker_candidates(code)[1:]:
            backup.setdefault(candidate, []).append(code)
    if backup:
        raw_backup = _download(list(backup))
        for candidate, codes in backup.items():
            frame = (
                get_symbol_frame(raw_backup, candidate)
                if raw_backup is not None
                else pd.DataFrame()
            )
            if frame is None or frame.empty:
                continue
            for code in codes:
                if code in pending:
                    frames[code] = frame
                    pending.remove(code)

    for code in requested:
        frames.setdefault(code, pd.DataFrame())
    return frames


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_benchmark_close(start) -> pd.Series:
    """抓取基準指數（^TWII）收盤價序列，供相對強度 RS% 計算（快取 1 小時）。

    抓取失敗回傳空序列，由呼叫端降級提示。
    """
    frames = download_frames([DEFAULT_BENCHMARK], start=start)
    frame = frames.get(DEFAULT_BENCHMARK)
    if frame is None or frame.empty or "Close" not in frame.columns:
        return pd.Series(dtype=float)
    return frame["Close"].dropna()


def compute_volume_zscore(volume: pd.Series, window: int = 20) -> pd.Series:
    """量能 Z-Score = (Volume - MA_window) / Std_window。

    母體標準差為 0 或資料不足時回傳 NaN（避免除零產生 inf）。
    """
    if volume is None or len(volume) <= window:
        return pd.Series(dtype=float)

    mean = volume.rolling(window).mean()
    std = volume.rolling(window).std()
    z = (volume - mean) / std
    return z.where(std > 0)


def score_alert(
    z_score: float,
    pct_change: float,
    rs: float,
    above_ma20,
    z_threshold: float = 2.0,
):
    """量化每日異動警戒評分（0 ~ 100，80 分以上為健康區間）。

    回傳 (分數, 警訊清單)。
    """
    score = 100.0
    signals = []

    # 1. 量能異動：Z-Score 越高代表當日成交量相對 20 日均量越異常
    if pd.notna(z_score):
        if z_score >= z_threshold * 1.5:
            score -= 30
            signals.append(f"🚨 嚴重爆量 Z={z_score:.2f}")
        elif z_score >= z_threshold:
            score -= 20
            signals.append(f"🔴 爆量 Z={z_score:.2f}")
        elif z_score >= z_threshold * 0.75:
            score -= 10
            signals.append(f"🟡 量能偏高 Z={z_score:.2f}")

    # 2. 單日漲跌幅：極端波動需關注
    if pd.notna(pct_change):
        if pct_change <= -5:
            score -= 25
            signals.append(f"🔴 重挫 {pct_change:+.2f}%")
        elif pct_change <= -3:
            score -= 15
            signals.append(f"🟡 下跌 {pct_change:+.2f}%")
        elif pct_change <= -2:
            score -= 8
            signals.append(f"🟡 偏弱 {pct_change:+.2f}%")
        if pct_change >= 9:
            score -= 10
            signals.append(f"🟡 急漲 {pct_change:+.2f}%")

    # 3. 相對強度：個股報酬 - 基準(^TWII)報酬
    if pd.notna(rs):
        if rs <= -5:
            score -= 15
            signals.append(f"🔴 明顯弱於大盤 RS={rs:+.2f}%")
        elif rs <= -2:
            score -= 8
            signals.append(f"🟡 落後大盤 RS={rs:+.2f}%")

    # 4. 趨勢位置：跌破 20 日均線
    if above_ma20 is False:
        score -= 10
        signals.append("跌破 20MA")

    return max(0.0, min(100.0, score)), signals


def build_alert_table(
    frames: dict,
    labels: dict,
    benchmark_close: pd.Series,
    z_window: int = 20,
    rs_window: int = 20,
    z_threshold: float = 2.0,
):
    """計算每日異動警戒表。

    參數：
    - frames：{yfinance 代號: OHLCV DataFrame}
    - labels：{yfinance 代號: 顯示名稱}
    - benchmark_close：基準指數收盤價序列（可為空，空則不算 RS）

    回傳：(明細 DataFrame, 資料不足的標的清單)
    """
    rows = []
    skipped = []
    bench = (
        benchmark_close.dropna()
        if isinstance(benchmark_close, pd.Series)
        else pd.Series(dtype=float)
    )

    for symbol, df in frames.items():
        label = labels.get(symbol, symbol)
        if df is None or df.empty:
            skipped.append(label)
            continue

        close = df["Close"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < 2 or len(volume) <= z_window:
            skipped.append(label)
            continue

        # 量能 Z-Score（當日量相對 20 日均量的標準差倍數）
        z_series = compute_volume_zscore(volume, z_window)
        z_score = float(z_series.iloc[-1]) if len(z_series) else float("nan")

        # 量能倍數（今日量 / 20 日均量）
        vol_ratio = float("nan")
        ma = volume.rolling(z_window).mean().iloc[-1]
        if pd.notna(ma) and ma > 0:
            vol_ratio = float(volume.iloc[-1]) / float(ma)

        # 單日漲跌幅
        price = float(close.iloc[-1])
        prev_price = float(close.iloc[-2])
        pct = (price / prev_price - 1) * 100 if prev_price else float("nan")

        # 相對強度：個股報酬 - 基準報酬
        rs = float("nan")
        if len(close) > rs_window and len(bench) > rs_window:
            base_price = float(close.iloc[-1 - rs_window])
            bench_base = float(bench.iloc[-1 - rs_window])
            if base_price and bench_base:
                stock_ret = (price / base_price - 1) * 100
                bench_ret = (float(bench.iloc[-1]) / bench_base - 1) * 100
                rs = stock_ret - bench_ret

        # 20 日均線位置
        ma20 = close.rolling(20).mean().iloc[-1] if len(close) >= 20 else float("nan")
        above_ma20 = bool(price > ma20) if pd.notna(ma20) else None

        score, signals = score_alert(z_score, pct, rs, above_ma20, z_threshold)
        light = "🟢" if score >= 80 else ("🟡" if score >= 60 else "🔴")

        rows.append(
            {
                "標的": label,
                "代號": symbol,
                "收盤價": round(price, 2),
                "漲跌幅 %": round(pct, 2) if pd.notna(pct) else float("nan"),
                "今日量": float(volume.iloc[-1]),
                "量能Z-Score": round(z_score, 2) if pd.notna(z_score) else float("nan"),
                "量能倍數": round(vol_ratio, 2) if pd.notna(vol_ratio) else float("nan"),
                "RS %": round(rs, 2) if pd.notna(rs) else float("nan"),
                "評分": round(score, 1),
                "燈號": light,
                "警訊": "；".join(signals) if signals else "無明顯異動",
            }
        )

    columns = [
        "標的",
        "代號",
        "收盤價",
        "漲跌幅 %",
        "今日量",
        "量能Z-Score",
        "量能倍數",
        "RS %",
        "評分",
        "燈號",
        "警訊",
    ]
    return pd.DataFrame(rows, columns=columns), skipped


def build_indicator_summary(
    df: pd.DataFrame,
    benchmark_close: pd.Series = None,
    z_window: int = 20,
    rs_window: int = 20,
    z_threshold: float = 2.0,
) -> str:
    """以 Python 預先算好指標摘要，供 AI 健檢 Prompt 使用。

    涵蓋收盤價、單日漲跌幅、20MA/60MA 位階、量能 Z-Score、相對強度 RS%
    與健康度評分；回傳精簡單行文字，不附任何歷史資料陣列，以節省 Input Token。
    """
    if df is None or df.empty:
        return "（無行情資料）"

    close = df["Close"].dropna()
    if len(close) < 2:
        return "（資料不足，無法計算指標）"

    price = float(close.iloc[-1])
    prev_price = float(close.iloc[-2])
    if prev_price:
        pct = (price / prev_price - 1) * 100
        pct_text = f"單日 {pct:+.2f}%"
    else:
        pct = float("nan")
        pct_text = "單日漲跌幅無法計算"

    # 20MA / 60MA 位階
    ma20 = float(close.tail(20).mean()) if len(close) >= 20 else float("nan")
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else float("nan")

    def _ma_desc(ma: float, name: str) -> str:
        """描述股價相對某條均線的位置與差距百分比。"""
        if pd.isna(ma):
            return f"{name}：資料期間不足以計算"
        diff = (price - ma) / ma * 100
        where = "上方" if price > ma else "下方"
        return f"{name}={ma:.2f} 元（股價在其{where} {abs(diff):.2f}%）"

    parts = [f"最新收盤價 {price:.2f} 元（{pct_text}）"]
    parts.append(_ma_desc(ma20, "20MA（月線）"))
    parts.append(_ma_desc(ma60, "60MA（季線）"))
    if not pd.isna(ma20) and not pd.isna(ma60):
        parts.append(
            "均線型態：短均在長均之上（多頭排列）"
            if ma20 > ma60
            else "均線型態：短均在長均之下（空頭排列）"
        )

    # 量能 Z-Score（當日量相對 20 日均量的標準差倍數）
    volume = df["Volume"].dropna() if "Volume" in df.columns else pd.Series(dtype=float)
    z_score = float("nan")
    vol_ratio = float("nan")
    if len(volume) > z_window:
        z_series = compute_volume_zscore(volume, z_window)
        z_score = float(z_series.iloc[-1]) if len(z_series) else float("nan")
        mean_vol = volume.rolling(z_window).mean().iloc[-1]
        if pd.notna(mean_vol) and mean_vol > 0:
            vol_ratio = float(volume.iloc[-1]) / float(mean_vol)
    if pd.notna(z_score):
        ratio_text = f"{vol_ratio:.2f}x" if pd.notna(vol_ratio) else "無法計算"
        parts.append(
            f"量能 Z-Score={z_score:.2f}（門檻 {z_threshold}，量能倍數 {ratio_text}）"
        )
    else:
        parts.append("量能 Z-Score：資料期間不足以計算")

    # 相對強度 RS%：個股報酬 - 基準指數報酬
    rs = float("nan")
    bench = (
        benchmark_close.dropna()
        if isinstance(benchmark_close, pd.Series)
        else pd.Series(dtype=float)
    )
    if len(close) > rs_window and len(bench) > rs_window:
        base_price = float(close.iloc[-1 - rs_window])
        bench_base = float(bench.iloc[-1 - rs_window])
        if base_price and bench_base:
            stock_ret = (price / base_price - 1) * 100
            bench_ret = (float(bench.iloc[-1]) / bench_base - 1) * 100
            rs = stock_ret - bench_ret
    if pd.notna(rs):
        parts.append(
            f"相對強度 RS={rs:+.2f}%（近 {rs_window} 個交易日，基準 {DEFAULT_BENCHMARK}）"
        )
    else:
        parts.append("相對強度 RS：基準指數或歷史資料不足，暫無法計算")

    # 健康度評分（0 ~ 100，80 分以上為健康區間）
    above_ma20 = bool(price > ma20) if not pd.isna(ma20) else None
    score, signals = score_alert(z_score, pct, rs, above_ma20, z_threshold)
    light = "🟢" if score >= 80 else ("🟡" if score >= 60 else "🔴")
    parts.append(f"健康度評分 {score:.0f}/100（{light}，80 分以上為健康區間）")
    if signals:
        parts.append("扣分警訊：" + "、".join(signals))

    return "；".join(parts) + "。"


@st.cache_data(ttl=86400, show_spinner=False)
def get_cached_ai_analysis(
    stock_name: str,
    stock_symbol: str,
    _indicator_summary: str,
    _analyze,
) -> dict:
    """取得 AI 健檢結果，同一檔股票 24 小時內只消耗一次 Gemini API Token。

    - 快取鍵僅取 stock_name / stock_symbol（純字串），符合「一天一檔一次」的預期
    - 以底線前綴的 `_indicator_summary` 與 `_analyze` 會被 Streamlit 排除在快取鍵之外：
      股價微動不會造成重複呼叫，且由 app.py 注入分析函式（無循環依賴）
    - 分析函式丟出的例外不會被快取，下次按鈕會重新嘗試
    """
    return _analyze(stock_name, stock_symbol, _indicator_summary)
