"""市場資料共用模組：OHLCV 欄位正規化與每日異動指標計算。

依 agents.md 規範：
- 業務邏輯（Z-Score / 相對強度 / 評分）集中於此，不含任何 UI（SRP）
- 外部資料判空與 try-except 優雅降級
- 評分維持 0 ~ 100 分量化機制，80 分以上為健康區間
"""

import pandas as pd

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
