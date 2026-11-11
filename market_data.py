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

# 缺口分析狀態常數（供 UI 與資料表共用，避免字串散落各處）
GAP_UP = "向上跳空"
GAP_DOWN = "向下跳空"
GAP_FILLED = "已回補 (Filled)"
GAP_UNFILLED = "未回補 (Unfilled)"

# 缺口類型自動分類標籤（資料表顯示值與 UI 篩選器共用同一份字串）
GAP_TYPE_BREAKAWAY = "突破缺口 (Breakaway)"
GAP_TYPE_EXHAUSTION = "竭盡缺口 (Exhaustion)"
GAP_TYPE_RUNAWAY = "逃逸/持續缺口 (Runaway)"
GAP_TYPE_COMMON = "普通缺口 (Common)"
GAP_TYPE_OPTIONS = (
    GAP_TYPE_BREAKAWAY,
    GAP_TYPE_EXHAUSTION,
    GAP_TYPE_RUNAWAY,
    GAP_TYPE_COMMON,
)

# 缺口表固定輸出型別：空表（無任何缺口）也維持相同 dtype，
# 避免呼叫端對 object 型別執行數值比較時發生 TypeError
GAP_DTYPES = {
    "日期": "string",
    "類型": "string",
    "缺口分類": "string",
    "缺口底部": "float64",
    "缺口頂部": "float64",
    "缺口幅度(%)": "float64",
    "爆量倍數": "float64",
    "是否回補": "string",
    "回補日期": "string",
}


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


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_treasury_yield(start) -> pd.Series:
    """美國 10 年期公債殖利率 (%)，作為無風險利率代理（快取 1 小時）。

    台灣公債殖利率沒有公開 API，故以美債 10Y (^TNX) 代理 —— 台股折現率
    高度連動 Fed 政策利率，適合作為市場風險溢酬 (ERP) 的無風險利率 leg。

    ^TNX 若以「殖利率 ×10」報價（值 > 20）會自動除以 10 正規化。
    抓取失敗回傳空序列，由呼叫端降級為「ERP 無法計算」。
    """
    frames = download_frames(["^TNX"], start=start)
    frame = frames.get("^TNX")
    if frame is None or frame.empty or "Close" not in frame.columns:
        return pd.Series(dtype=float)

    series = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if series.empty:
        return pd.Series(dtype=float)
    if float(series.iloc[-1]) > 20:
        series = series / 10.0
    return series


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


def _classify_gap(
    vol_ratio: float,
    ret_20: float,
    box_high_20: float,
    cur_low: float,
) -> str:
    """依量能與趨勢情境自動分類缺口，規則依序判斷、先符合者勝出。

    1. 突破缺口：量能 > 1.8 倍 且 當日 Low > 過去 20 日最高（強勢突破箱型）
    2. 竭盡缺口：過去 20 日累積漲幅 > 25% 且 量能 > 2.5 倍（高檔極致爆量）
    3. 逃逸/持續缺口：多頭趨勢（20 日漲幅 > 8%）且 量能 > 1.2 倍
    4. 普通缺口：其餘情形（規則 4 之「成交量未明顯放大」為描述性條件，
       因分類僅有四種，故以最後的 else 收斂）

    說明：NaN 參與比較一律回傳 False，因此前 20 個交易日或量能資料不足時
    會自然落入「普通缺口」，不會拋出例外。
    """
    # 1) 突破缺口
    if vol_ratio > 1.8 and cur_low > box_high_20:
        return GAP_TYPE_BREAKAWAY
    # 2) 竭盡缺口
    if ret_20 > 25 and vol_ratio > 2.5:
        return GAP_TYPE_EXHAUSTION
    # 3) 逃逸/持續缺口
    if ret_20 > 8 and vol_ratio > 1.2:
        return GAP_TYPE_RUNAWAY
    # 4) 普通缺口
    return GAP_TYPE_COMMON


def detect_gaps(df: pd.DataFrame, volume_window: int = 20) -> pd.DataFrame:
    """偵測相鄰兩日 K 線的跳空缺口，追蹤後續是否回補，並自動分類缺口類型。

    向上跳空：當日 Low > 前一日 High（缺口底部=前一日 High，缺口頂部=當日 Low）
    向下跳空：當日 High < 前一日 Low（缺口頂部=前一日 Low，缺口底部=當日 High）
    回補判定：向上缺口後續任一日 Low <= 缺口底部；向下缺口後續任一日 High >= 缺口頂部
    類型判定：見 _classify_gap（突破 / 竭盡 / 逃逸 / 普通）

    回傳欄位（依日期升冪，供呼叫端排序）：
    日期 / 類型 / 缺口分類 / 缺口底部 / 缺口頂部 / 缺口幅度(%) /
    爆量倍數 / 是否回補 / 回補日期
    """
    columns = [
        "日期",
        "類型",
        "缺口分類",
        "缺口底部",
        "缺口頂部",
        "缺口幅度(%)",
        "爆量倍數",
        "是否回補",
        "回補日期",
    ]
    if df is None or df.empty or len(df) < 2:
        return pd.DataFrame(columns=columns).astype(GAP_DTYPES)

    data = df.dropna(subset=["High", "Low"])
    if len(data) < 2:
        return pd.DataFrame(columns=columns).astype(GAP_DTYPES)

    highs = data["High"].astype(float)
    lows = data["Low"].astype(float)
    if "Volume" in data.columns:
        volumes = data["Volume"].astype(float)
        volume_ma = volumes.rolling(volume_window).mean()
    else:
        volumes = pd.Series(dtype=float)
        volume_ma = pd.Series(dtype=float)

    # 預先計算分類所需的逐日指標（20 日累積漲幅、20 日箱型高點）
    # 注意：不使用 pct_change() 以避免 pandas 2.x 的 fill_method 棄用警告
    if "Close" in data.columns:
        closes = data["Close"].astype(float)
        ret_20 = (closes / closes.shift(20) - 1.0) * 100
    else:
        ret_20 = pd.Series(float("nan"), index=data.index)
    box_high_20 = highs.shift(1).rolling(20).max()

    dates = data.index
    records = []

    for i in range(1, len(data)):
        prev_high = float(highs.iloc[i - 1])
        prev_low = float(lows.iloc[i - 1])
        cur_high = float(highs.iloc[i])
        cur_low = float(lows.iloc[i])

        # 1. 缺口判定
        if cur_low > prev_high:
            gap_type = GAP_UP
            bottom, top = prev_high, cur_low
            pct = (cur_low - prev_high) / prev_high * 100 if prev_high else float("nan")
        elif cur_high < prev_low:
            gap_type = GAP_DOWN
            bottom, top = cur_high, prev_low
            pct = (prev_low - cur_high) / prev_low * 100 if prev_low else float("nan")
        else:
            continue

        # 2. 追蹤後續交易日是否回補（只看 i 之後的交易日）
        fill_date = "N/A"
        filled = False
        for j in range(i + 1, len(data)):
            if gap_type == GAP_UP and float(lows.iloc[j]) <= bottom:
                filled = True
            elif gap_type == GAP_DOWN and float(highs.iloc[j]) >= top:
                filled = True
            if filled:
                fill_date = pd.Timestamp(dates[j]).strftime("%Y-%m-%d")
                break

        # 3. 當日量能倍數（當日量 / 20 日均量）
        vol_ratio = float("nan")
        if len(volume_ma) > i:
            ma = volume_ma.iloc[i]
            if pd.notna(ma) and ma > 0:
                vol_ratio = float(volumes.iloc[i]) / float(ma)

        # 4. 缺口類型自動分類（量能倍數 × 趨勢情境）
        gap_class = _classify_gap(
            vol_ratio=vol_ratio,
            ret_20=float(ret_20.iloc[i]),
            box_high_20=float(box_high_20.iloc[i]),
            cur_low=cur_low,
        )

        records.append(
            {
                "日期": pd.Timestamp(dates[i]).strftime("%Y-%m-%d"),
                "類型": gap_type,
                "缺口分類": gap_class,
                "缺口底部": round(bottom, 2),
                "缺口頂部": round(top, 2),
                "缺口幅度(%)": round(pct, 2),
                "爆量倍數": round(vol_ratio, 2),
                "是否回補": GAP_FILLED if filled else GAP_UNFILLED,
                "回補日期": fill_date,
            }
        )

    return pd.DataFrame(records, columns=columns).astype(GAP_DTYPES)


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
