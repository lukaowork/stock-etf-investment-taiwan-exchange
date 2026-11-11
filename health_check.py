"""四層全方位健檢模組 (4-Layer Corporate Health Check)。

純計算層：不含任何 Streamlit UI 呼叫（與 valuation.py 一致，SRP）。
外部資料由呼叫端（app.py）備妥後傳入；單一指標缺漏時該子項不計分，
以「可用子項平均」降級，避免因單一指標缺漏造成整頁崩潰（防禦性程式設計）。

四層權重（合計 100%）：
    總經脈搏 15% ／ 產業風向 25% ／ 籌碼體質 25% ／ 財務與獲利 35%

評分 0 ~ 100 分，80 分以上為健康區間（與 AGENTS.md 一致）。
任一層觸發「一票否決」(veto) 時，該層分數封頂 VETO_CAP，並由 UI 顯示 🚨 警示。

資料來源與代理說明（重要）：
    - 大盤 20MA        ：yfinance ^TWII
    - 無風險利率        ：美債 10Y (^TNX) 代理（台灣公債無公開 API）
    - 市場中位本益比    ：TWSE BWIBBU_d 全市場橫斷面
    - 融資使用率        ：TWSE MI_MARGN（僅上市；上櫃查無資料）
    - 主力／法人連續買賣超天數：證交所無免費 API，
      改以「A/D 累積線連續天數」作量價代理（已在 UI 標註）
    - 產業庫存天數 DIO  ：以公司自身存貨循環代表所處產業庫存週期，
      避免逐檔抓取同業財報造成 N 次外部請求
"""

import numpy as np
import pandas as pd
import plotly.graph_objects as go

# ---- 權重常數（合計 1.0）----
WEIGHT_MACRO = 0.15
WEIGHT_INDUSTRY = 0.25
WEIGHT_FLOW = 0.25
WEIGHT_FINANCIAL = 0.35

# ---- 評分門檻 ----
GRADE_GREEN = 80.0       # 綠燈（健康）下限
GRADE_YELLOW = 60.0      # 黃燈（觀察）下限
VETO_CAP = 40.0          # 一票否決時該層分數上限

# 籌碼層門檻
MARGIN_COMFORT_MAX = 45.0   # 融資使用率舒適區上限（%），超過視為籌碼浮濫
MARGIN_COMFORT_MIN = 15.0   # 舒適區下限（%），過低視為市場冷清
VR_SURGE = 1.5              # 爆量倍數門檻（今日量 / 20 日均量）

# 財務層門檻
CFO_VETO_RATIO = 0.30       # CFO / 淨利低於此值 → 獲利真實度一票否決
VGR_VETO = 40.0             # 價值偏差率過高 → 估值泡沫一票否決
REV_COLLAPSE = -20.0        # 營收年增率崩落門檻（%）
DIO_SWELL = 15.0            # 庫存天數惡化門檻（天）

# 一票否決文案（供 UI 顯示，集中管理避免字串散落）
VETO_CFO = "🚨 警告！CFO/淨利過低，財務存在灌水疑慮（一票否決）"
VETO_NEG_CFO = "🚨 警告！有淨利卻無營業現金流，獲利品質存疑（一票否決）"
VETO_MARGIN = "🚨 警告！融資使用率逼近上限，籌碼隨時可能鬆動（一票否決）"
VETO_MARGIN_DUMP = "🚨 警告！高檔爆量下跌且融資偏高，出貨疑慮（一票否決）"
VETO_MACRO = "🚨 警告！無風險利率快速上修且大盤跌破 20MA，總經逆風（一票否決）"
VETO_INDUSTRY = "🚨 警告！營收大幅衰退且庫存天數攀升，需求端疑慮（一票否決）"
VETO_VGR = "🚨 警告！現價明顯高於合理價，估值泡沫風險（一票否決）"


# ---------------------------------------------------------------------------
# 私有工具函式
# ---------------------------------------------------------------------------
def _num(value):
    """轉 float；None / 非有限值回傳 None。"""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _clip(value, low=0.0, high=100.0):
    """裁切到 [low, high]；None / 非有限值回傳 None。"""
    v = _num(value)
    if v is None:
        return None
    return max(low, min(high, v))


def _mean(values):
    """可用數值的算術平均；全空回傳 None。"""
    usable = [v for v in (_num(item) for item in values) if v is not None]
    if not usable:
        return None
    return float(sum(usable) / len(usable))


def _yi(value) -> str:
    """金額格式化為『億』單位（yfinance 財報金額為新台幣元）。"""
    v = _num(value)
    if v is None:
        return "N/A"
    return f"{v / 1e8:,.1f} 億"


def _ascending(mapping) -> pd.Series | None:
    """把 {日期字串: 數值} 轉成日期升冪的 Series；無效資料回傳 None。"""
    if not isinstance(mapping, dict) or not mapping:
        return None
    rows = {}
    for key, value in mapping.items():
        v = _num(value)
        if v is None:
            continue
        try:
            rows[pd.Timestamp(key)] = v
        except Exception:
            continue
    if not rows:
        return None
    return pd.Series(rows).sort_index()


def _layer(key, short, name, icon, weight, score, verdict,
           metrics, narrative, advice, vetoes=None, notes=None) -> dict:
    """組裝單一層的標準化結果結構（UI 依此渲染，避免各層格式不一致）。"""
    return {
        "key": key,
        "short": short,
        "name": name,
        "icon": icon,
        "weight": weight,
        "score": score,
        "verdict": verdict,
        "metrics": metrics,
        "narrative": narrative,
        "advice": advice,
        "vetoes": vetoes or [],
        "notes": notes or [],
    }


# ---------------------------------------------------------------------------
# 財報衍生指標
# ---------------------------------------------------------------------------
def derive_growth(fund: dict) -> dict:
    """由逐季財報字典推導 YoY、毛利率與 Price vs. Volume 拆解。

    Price vs. Volume 拆解假設「單位銷貨成本穩定」：
        數量面 (volume)     ≈ 銷貨成本 YoY
        價格/組合面 (price)  ≈ 營收 YoY − 銷貨成本 YoY
        經營槓桿 (DOL)       = 營業利益 YoY / 營收 YoY

    回傳 dict；無法計算的項目為 None。
    """
    result = {
        "rev_yoy": None,
        "cogs_yoy": None,
        "oi_yoy": None,
        "gm_now": None,
        "gm_prev": None,
        "gm_delta": None,
        "price_effect": None,
        "volume_effect": None,
        "dol": None,
    }
    if not isinstance(fund, dict):
        return result

    def yoy_of(field):
        series = _ascending(fund.get(field))
        if series is None or len(series) < 5:
            return None
        prev = _num(series.iloc[-5])   # 去年同季（相隔 4 個季度）
        last = _num(series.iloc[-1])
        if prev is None or last is None or prev == 0:
            return None
        return (last / prev - 1.0) * 100.0

    rev_yoy = yoy_of("revenue_q")
    cogs_yoy = yoy_of("cogs_q")
    oi_yoy = yoy_of("operating_income_q")
    result["rev_yoy"] = rev_yoy
    result["cogs_yoy"] = cogs_yoy
    result["oi_yoy"] = oi_yoy

    # 毛利率：最新一季 vs 上年同季（同一張損益表，日期鍵必然對齊）
    rev_map = fund.get("revenue_q") if isinstance(fund.get("revenue_q"), dict) else {}
    cogs_map = fund.get("cogs_q") if isinstance(fund.get("cogs_q"), dict) else {}
    common_keys = sorted(set(rev_map) & set(cogs_map), reverse=True)

    def margin_at(back: int):
        if len(common_keys) <= back:
            return None
        key = common_keys[back]
        r = _num(rev_map.get(key))
        c = _num(cogs_map.get(key))
        if r is None or c is None or r == 0:
            return None
        return (r - c) / r * 100.0

    gm_now = margin_at(0)
    gm_prev = margin_at(4)
    result["gm_now"] = gm_now
    result["gm_prev"] = gm_prev
    if gm_now is not None and gm_prev is not None:
        result["gm_delta"] = gm_now - gm_prev

    if rev_yoy is not None:
        result["volume_effect"] = cogs_yoy
        if cogs_yoy is not None:
            result["price_effect"] = rev_yoy - cogs_yoy
    if rev_yoy is not None and oi_yoy is not None and abs(rev_yoy) >= 1.0:
        result["dol"] = oi_yoy / rev_yoy
    return result


def build_dio_series(inventory: dict, cogs: dict, days: int = 90) -> pd.Series:
    """產業庫存天數 DIO = 季末存貨 / 銷貨成本 × 天數（季度以 90 天計）。"""
    if not isinstance(inventory, dict) or not isinstance(cogs, dict):
        return pd.Series(dtype=float)
    if not inventory or not cogs:
        return pd.Series(dtype=float)

    rows = {}
    for key in set(inventory) & set(cogs):
        inv = _num(inventory.get(key))
        cost = _num(cogs.get(key))
        if inv is None or cost is None or cost <= 0:
            continue
        try:
            rows[pd.Timestamp(key)] = inv / cost * days
        except Exception:
            continue
    if not rows:
        return pd.Series(dtype=float)
    return pd.Series(rows).sort_index()


def build_revenue_yoy_series(revenue_q: dict) -> pd.Series:
    """逐季營收 YoY (%)：同一季 vs 去年同季（相隔 4 個季度）。"""
    series = _ascending(revenue_q)
    if series is None or len(series) < 5:
        return pd.Series(dtype=float)
    yoy = (series / series.shift(4) - 1.0) * 100.0
    return yoy.replace([np.inf, -np.inf], np.nan).dropna()


def compute_ad_streak(df: pd.DataFrame) -> int | None:
    """A/D 累積線連續買方主導天數（正＝連續買超，負＝連續賣超）。

    計算方式：
        mfm = ((收盤 − 最低) − (最高 − 收盤)) / (最高 − 最低)   # 收盤在當日區間的位置
        mfv = mfm × 成交量                                      # 資金流
        mfv > 0 → 當日由買方主導；連續同號即為連續天數

    用途：作為「主力／外資／投信連續買賣超天數」的量價代理
    （證交所未提供個股法人買賣超的免費 API）。
    回傳 None 代表資料不足。
    """
    if df is None or df.empty:
        return None
    required = ["High", "Low", "Close", "Volume"]
    if any(col not in df.columns for col in required):
        return None

    data = df.dropna(subset=required)
    if len(data) < 5:
        return None

    high = data["High"].astype(float)
    low = data["Low"].astype(float)
    close = data["Close"].astype(float)
    volume = data["Volume"].astype(float)

    rng = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    mfm = mfm.fillna(0.0)          # 漲跌停無區間日視為中立
    mfv = (mfm * volume).tail(60)  # 只看近 60 根，避免舊慣性影響當前判讀

    direction = 0
    streak = 0
    for raw in reversed(mfv.values.tolist()):
        sign = int(np.sign(raw)) if np.isfinite(raw) else 0
        if sign == 0:
            break
        if direction == 0:
            direction = sign
            streak = 1
        elif sign == direction:
            streak += 1
        else:
            break
    return direction * streak


# ---------------------------------------------------------------------------
# 總經：市場風險溢酬 (ERP)
# ---------------------------------------------------------------------------
def compute_erp(
    market_median_pe,
    treasury_yield: pd.Series,
    benchmark_close: pd.Series,
) -> dict:
    """市場風險溢酬 ERP = 市場 E/P − 無風險利率，與其近 20 日變化。

    - E/P = 100 / 市場中位本益比（TWSE 全市場橫斷面，剔除虧損股）
    - 無風險利率：美債 10Y (^TNX) 代理（台灣公債無公開 API）
    - ERP 變化：假設近 20 日 EPS 穩定，E/P 的變化可由指數報酬反推
          ΔE/P ≈ −(E/P) × r / (1 + r)        （r 為 20 日報酬，小數）
          ΔERP = ΔE/P − Δr

    回傳 {"level", "change", "ep", "yield_now", "yield_change", "note"}；
    任一元件缺漏時對應值為 None。
    """
    out = {
        "level": None,
        "change": None,
        "ep": None,
        "yield_now": None,
        "yield_change": None,
        "note": "",
    }

    # 無風險利率
    r = (
        pd.to_numeric(treasury_yield, errors="coerce").dropna()
        if isinstance(treasury_yield, pd.Series)
        else pd.Series(dtype=float)
    )
    r_now = _num(r.iloc[-1]) if len(r) >= 1 else None
    r_prev = _num(r.iloc[-21]) if len(r) >= 21 else None
    out["yield_now"] = r_now
    if r_now is not None and r_prev is not None:
        out["yield_change"] = r_now - r_prev

    # 市場 E/P
    pe = _num(market_median_pe)
    if pe is not None and pe > 0:
        out["ep"] = 100.0 / pe
        if r_now is not None:
            out["level"] = out["ep"] - r_now

    # 大盤 20 日報酬（供 E/P 變化反推）
    close = (
        pd.to_numeric(benchmark_close, errors="coerce").dropna()
        if isinstance(benchmark_close, pd.Series)
        else pd.Series(dtype=float)
    )
    ret20 = None
    if len(close) >= 21:
        base = _num(close.iloc[-21])
        last = _num(close.iloc[-1])
        if base is not None and last is not None and base > 0:
            ret20 = last / base - 1.0

    delta_ep = None
    if out["ep"] is not None and ret20 is not None and ret20 > -1.0:
        delta_ep = -out["ep"] * ret20 / (1.0 + ret20)

    if delta_ep is not None and out["yield_change"] is not None:
        out["change"] = delta_ep - out["yield_change"]

    missing = []
    if out["ep"] is None:
        missing.append("市場中位本益比")
    if r_now is None:
        missing.append("無風險利率")
    if missing:
        out["note"] = "缺 " + "、".join(missing) + "，ERP 無法完整計算。"
    return out


# ---------------------------------------------------------------------------
# 估值快照（供財務層使用）
# ---------------------------------------------------------------------------
def compute_valuation_metrics(
    price,
    ttm_eps,
    pe_history: pd.Series,
    fair: dict,
    pb_percentile=None,
) -> dict:
    """彙整估值安全度所需的四個數值。

    - pe_now / pe_percentile：目前股價對應的本益比及其在「歷史分布」的百分位
    - vgr：現價相對合理價的價值偏差率 (%)
    - pb_percentile：個股 P/B 在「全市場橫斷面」的百分位（TWSE 資料）

    回傳 dict；任一元件缺漏時對應值為 None。
    """
    out = {
        "price": _num(price),
        "ttm_eps": _num(ttm_eps),
        "pe_now": None,
        "pe_percentile": None,
        "fair_value": None,
        "vgr": None,
        "pb_percentile": _num(pb_percentile),
    }

    if out["price"] is not None and out["ttm_eps"] and out["ttm_eps"] > 0:
        out["pe_now"] = out["price"] / out["ttm_eps"]

    pe = (
        pd.to_numeric(pe_history, errors="coerce").dropna()
        if isinstance(pe_history, pd.Series)
        else pd.Series(dtype=float)
    )
    if out["pe_now"] is not None and len(pe) > 0:
        out["pe_percentile"] = float((pe <= out["pe_now"]).mean() * 100.0)

    if isinstance(fair, dict):
        fv = _num(fair.get("fair_value"))
        out["fair_value"] = fv
        if fv is not None and fv > 0 and out["price"] is not None:
            out["vgr"] = (out["price"] - fv) / fv * 100.0
    return out


# ---------------------------------------------------------------------------
# 第 1 層：總經脈搏健檢（15%）
# ---------------------------------------------------------------------------
def score_macro(benchmark_close: pd.Series, erp: dict) -> dict:
    """第 1 層：總經脈搏健檢（權重 15%）。

    診斷指標：大盤 20MA 多空位置、市場風險溢酬 (ERP) 變化。
    健檢評語：大環境順風 (Risk-on) / 總經折現率上修逆風 (Risk-off)。
    """
    metrics = []
    notes = []
    subs = []

    close = (
        pd.to_numeric(benchmark_close, errors="coerce").dropna()
        if isinstance(benchmark_close, pd.Series)
        else pd.Series(dtype=float)
    )

    pos_pct = None
    ma20 = None
    ret20 = None
    if len(close) >= 21:
        ma20 = float(close.tail(20).mean())
        last = _num(close.iloc[-1])
        if ma20 > 0 and last is not None:
            pos_pct = (last - ma20) / ma20 * 100.0
        base = _num(close.iloc[-21])
        if base is not None and base > 0 and last is not None:
            ret20 = (last / base - 1.0) * 100.0
    else:
        notes.append("基準指數 (^TWII) 歷史不足 21 個交易日，多空位置無法計算。")

    # 指標 1：大盤 20MA 多空位置
    if pos_pct is not None:
        score_ma = _clip(50.0 + 4.0 * pos_pct, 5.0, 100.0)
        if score_ma is not None:
            subs.append(score_ma)
        metrics.append({
            "label": "大盤 vs 20MA",
            "value": f"{pos_pct:+.2f}%",
            "delta": f"20MA = {ma20:,.0f}",
            "help": "大盤收盤價偏離 20 日均線的百分比；正值代表站上月線。",
        })
    else:
        metrics.append({
            "label": "大盤 vs 20MA",
            "value": "N/A",
            "delta": None,
            "help": "基準指數資料不足，本子項不計分。",
        })

    # 指標 2：市場風險溢酬 (ERP) 變化
    erp = erp if isinstance(erp, dict) else {}
    level = _num(erp.get("level"))
    change = _num(erp.get("change"))
    if change is not None:
        score_erp = _clip(50.0 + 60.0 * change, 5.0, 100.0)
        if level is not None and level < 0:
            score_erp = min(score_erp, 40.0)
            notes.append("ERP 為負：市場 E/P 低於無風險利率，估值相對債券偏貴。")
        if score_erp is not None:
            subs.append(score_erp)
        metrics.append({
            "label": "市場風險溢酬 (ERP)",
            "value": f"{level:+.2f}%" if level is not None else "N/A",
            "delta": f"20日 {change:+.2f}pp",
            "help": "ERP = 市場 E/P − 無風險利率；變化為正代表風險補償擴大（順風）。",
        })
    else:
        metrics.append({
            "label": "市場風險溢酬 (ERP)",
            "value": "N/A",
            "delta": None,
            "help": "市場中位本益比或無風險利率資料不足，本子項不計分。",
        })
        if erp.get("note"):
            notes.append(str(erp["note"]))

    # 補充資訊（不計分）
    if level is not None:
        metrics.append({
            "label": "市場 E/P（倒數本益比）",
            "value": f"{erp.get('ep'):.2f}%" if _num(erp.get("ep")) is not None else "N/A",
            "delta": None,
            "help": "100 / 全市場中位本益比（TWSE 橫斷面，已剔除虧損股）。",
        })
    if _num(erp.get("yield_now")) is not None:
        yc = _num(erp.get("yield_change"))
        metrics.append({
            "label": "無風險利率（美債 10Y）",
            "value": f"{_num(erp.get('yield_now')):.2f}%",
            "delta": f"20日 {yc:+.2f}pp" if yc is not None else None,
            "help": "台灣公債無公開 API，以美債 10Y 作為折現率代理。",
        })
    if ret20 is not None:
        metrics.append({
            "label": "大盤 20 日報酬",
            "value": f"{ret20:+.2f}%",
            "delta": None,
            "help": "近 20 個交易日累積報酬，用於反推 E/P 的變化。",
        })

    score = _mean(subs)
    vetoes = []
    if change is not None and change <= -0.5 and (pos_pct is None or pos_pct < 0):
        vetoes.append(VETO_MACRO)

    if score is None:
        verdict = "資料不足，本層不計分"
        narrative = "大盤與總經資料不足，本層權重將由其他三層重新分配。"
        advice = "請確認網路可連線至 yfinance 取得 ^TWII 與 ^TNX 歷史資料後重試。"
    else:
        if score >= 65:
            verdict = "大環境順風 (Risk-on)"
        elif score <= 40:
            verdict = "總經折現率上修逆風 (Risk-off)"
        else:
            verdict = "多空拉鋸 (Neutral)"

        parts = []
        if pos_pct is not None:
            parts.append(
                f"大盤{'站上' if pos_pct >= 0 else '跌破'} 20MA（{pos_pct:+.2f}%）"
            )
        if change is not None:
            parts.append(
                f"ERP 近 20 日{'擴大' if change > 0 else '壓縮'} {abs(change):.2f}pp"
            )
        narrative = "；".join(parts) + f"，整體判定為「{verdict}」。"

        if verdict.startswith("大環境"):
            advice = "總經環境偏多，可維持正常持股水位，優先布局產業動能強的標的。"
        elif verdict.startswith("總經"):
            advice = "折現率上修逆風，建議降低整體曝險、提高現金比重，等待 ERP 壓縮結束。"
        else:
            advice = "總經多空拉鋸，建議以個股紀律為主，並留意大盤 20MA 的得失。"

    if vetoes and score is not None:
        score = min(score, VETO_CAP)

    return _layer(
        key="macro",
        short="總經",
        name="總經脈搏健檢",
        icon="🌐",
        weight=WEIGHT_MACRO,
        score=score,
        verdict=verdict,
        metrics=metrics,
        narrative=narrative,
        advice=advice,
        vetoes=vetoes,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 第 2 層：產業風向健檢（25%）
# ---------------------------------------------------------------------------
def score_industry(fund: dict) -> dict:
    """第 2 層：產業風向健檢（權重 25%）。

    診斷指標：近 3 個月營收 YoY 動能、產業庫存天數 (DIO) 趨勢。
    健檢評語：產業成長順風車 / 庫存修正・價格下行區。
    """
    fund = fund if isinstance(fund, dict) else {}
    metrics = []
    notes = []
    subs = []

    # 指標 1：近 3 個月（＝最近一季）營收 YoY 動能
    rev_yoy = _num(fund.get("revenue_yoy"))
    yoy_series = build_revenue_yoy_series(fund.get("revenue_q"))
    if rev_yoy is None and len(yoy_series) > 0:
        rev_yoy = _num(yoy_series.iloc[-1])
    avg3 = float(yoy_series.tail(3).mean()) if len(yoy_series) > 0 else None

    if rev_yoy is not None:
        score_rev = _clip(50.0 + 2.5 * rev_yoy, 5.0, 100.0)
        if score_rev is not None:
            subs.append(score_rev)
        metrics.append({
            "label": "近 3 個月營收 YoY",
            "value": f"{rev_yoy:+.2f}%",
            "delta": f"近 3 季均值 {avg3:+.2f}%" if avg3 is not None else None,
            "help": "yfinance 季營收年增率（一季＝3 個月），即近 3 個月營收 YoY。",
        })
    else:
        metrics.append({
            "label": "近 3 個月營收 YoY",
            "value": "N/A",
            "delta": None,
            "help": "未取得季營收資料，本子項不計分。",
        })
        notes.append("缺少季營收序列，產業動能無法計算。")

    # 動能趨勢（不計分，供判讀）
    if rev_yoy is not None and avg3 is not None:
        diff = rev_yoy - avg3
        trend = "加速" if diff > 1 else ("減速" if diff < -1 else "持平")
        metrics.append({
            "label": "動能趨勢",
            "value": trend,
            "delta": f"{diff:+.2f}pp",
            "help": "最新一季 YoY 與近 3 季均值的差異；正值代表動能加速。",
        })

    # 指標 2：產業庫存天數 (DIO) 趨勢
    dio = build_dio_series(fund.get("inventory_q"), fund.get("cogs_q"))
    dio_now = _num(dio.iloc[-1]) if len(dio) > 0 else None
    dio_delta = _num(dio.iloc[-1] - dio.iloc[-5]) if len(dio) >= 5 else None

    if dio_delta is not None:
        score_dio = _clip(50.0 - 3.0 * dio_delta, 5.0, 100.0)
        if score_dio is not None:
            subs.append(score_dio)
    if dio_now is not None:
        metrics.append({
            "label": "產業庫存天數 (DIO)",
            "value": f"{dio_now:,.0f} 天",
            "delta": (
                f"較 4 季前 {dio_delta:+,.0f} 天" if dio_delta is not None else None
            ),
            "help": "DIO = 季末存貨 / 銷貨成本 × 90；天數下降代表庫存去化順利。",
        })
    else:
        metrics.append({
            "label": "產業庫存天數 (DIO)",
            "value": "N/A",
            "delta": None,
            "help": "未取得存貨與銷貨成本資料，本子項不計分。",
        })
    if dio_now is not None and dio_delta is None:
        notes.append("DIO 序列不足 5 季，無法判斷庫存趨勢（僅顯示當前水位）。")

    # 產業分類（不計分，供判讀）
    sector = fund.get("sector")
    industry = fund.get("industry")
    if sector or industry:
        metrics.append({
            "label": "產業分類",
            "value": str(industry or sector),
            "delta": str(sector) if sector and industry else None,
            "help": "yfinance 產業分類，用於判斷公司所處的產業循環位置。",
        })

    score = _mean(subs)
    vetoes = []
    if (
        rev_yoy is not None
        and rev_yoy <= REV_COLLAPSE
        and dio_delta is not None
        and dio_delta >= DIO_SWELL
    ):
        vetoes.append(VETO_INDUSTRY)

    if score is None:
        verdict = "資料不足，本層不計分"
        narrative = "缺少季營收或存貨資料，本層無法判讀產業風向。"
        advice = "請確認 yfinance 可取得該股的損益表與資產負債表。"
    else:
        healthy = score >= 65
        deteriorating = (
            rev_yoy is not None
            and rev_yoy < 0
            and dio_delta is not None
            and dio_delta > 0
        )
        if healthy and not deteriorating:
            verdict = "產業成長順風車"
        elif score <= 40 or deteriorating:
            verdict = "庫存修正／價格下行區"
        else:
            verdict = "產業盤整"

        bits = []
        if rev_yoy is not None:
            bits.append(f"近 3 個月營收 YoY {rev_yoy:+.2f}%")
        if dio_now is not None:
            if dio_delta is not None:
                bits.append(
                    f"庫存天數 {dio_now:,.0f} 天（4 季{'增加' if dio_delta > 0 else '減少'}"
                    f" {abs(dio_delta):,.0f} 天）"
                )
            else:
                bits.append(f"庫存天數 {dio_now:,.0f} 天")
        narrative = "；".join(bits) + f"，判定為「{verdict}」。"

        if verdict == "產業成長順風車":
            advice = "產業動能向上且庫存去化，可提高關注度，並追蹤下一季營收能否延續。"
        elif verdict == "庫存修正／價格下行區":
            advice = "營收衰退與庫存堆疊並存，建議減碼或嚴設停損，等待庫存天數拐點。"
        else:
            advice = "產業處於盤整，建議觀察 DIO 是否由升轉降作為進場的先行訊號。"

    if vetoes and score is not None:
        score = min(score, VETO_CAP)

    return _layer(
        key="industry",
        short="產業",
        name="產業風向健檢",
        icon="🏭",
        weight=WEIGHT_INDUSTRY,
        score=score,
        verdict=verdict,
        metrics=metrics,
        narrative=narrative,
        advice=advice,
        vetoes=vetoes,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 第 3 層：籌碼體質健檢（25%）
# ---------------------------------------------------------------------------
def score_flow(df: pd.DataFrame, margin_usage) -> dict:
    """第 3 層：籌碼體質健檢（權重 25%）。

    診斷指標：融資使用率、成交量倍數 (VR)、A/D 累積線連續天數
    （代理主力／法人連續買賣超，證交所無免費 API）。
    健檢評語：機構資金集中卡位 / 融資籌碼鬆動・籌碼凌亂。
    """
    metrics = []
    notes = [
        "個股主力、外資、投信連續買賣超天數：證交所未提供免費 API，"
        "已以 A/D 累積線連續天數作量價代理。"
    ]
    subs = []

    use = _num(margin_usage)
    vr = None
    pct = None

    required = ["High", "Low", "Close", "Volume"]
    if df is None or df.empty or any(col not in df.columns for col in required):
        notes.append("缺少行情資料，量能與 A/D 線無法計算。")
    else:
        # 指標 1：融資使用率（TWSE 真資料）
        if use is not None:
            over = max(0.0, use - MARGIN_COMFORT_MAX) * 2.2
            under = max(0.0, MARGIN_COMFORT_MIN - use) * 1.5
            score_margin = _clip(100.0 - over - under, 5.0, 100.0)
            if score_margin is not None:
                subs.append(score_margin)
            metrics.append({
                "label": "融資使用率",
                "value": f"{use:.1f}%",
                "delta": f"舒適區 {MARGIN_COMFORT_MIN:.0f}–{MARGIN_COMFORT_MAX:.0f}%",
                "help": "融資今日餘額 / 融資限額（TWSE MI_MARGN）；過高代表散戶槓桿偏重。",
            })
        else:
            metrics.append({
                "label": "融資使用率",
                "value": "N/A",
                "delta": None,
                "help": "上櫃股票或抓取失敗時無資料，本子項不計分。",
            })
            notes.append("融資使用率：證交所僅提供集中市場（上市）資料。")

        # 指標 2：成交量倍數 (VR) ＋當日方向
        volume = pd.to_numeric(df["Volume"], errors="coerce").dropna()
        close = pd.to_numeric(df["Close"], errors="coerce").dropna()
        if len(volume) > 20:
            mean_vol = _num(volume.tail(20).mean())
            last_vol = _num(volume.iloc[-1])
            if mean_vol is not None and mean_vol > 0 and last_vol is not None:
                vr = last_vol / mean_vol
        if len(close) >= 2:
            prev = _num(close.iloc[-2])
            last = _num(close.iloc[-1])
            if prev is not None and last is not None and prev != 0:
                pct = (last / prev - 1.0) * 100.0

        if vr is not None:
            if vr >= VR_SURGE:
                score_vr = 82.0 if (pct is not None and pct > 0) else 32.0
            elif vr >= 1.0:
                score_vr = 75.0
            elif vr >= 0.6:
                score_vr = 62.0
            elif vr >= 0.3:
                score_vr = 50.0
            else:
                score_vr = 42.0  # 嚴重量縮，缺乏關注
            subs.append(score_vr)
            metrics.append({
                "label": "成交量倍數 (VR)",
                "value": f"{vr:.2f}x",
                "delta": f"單日 {pct:+.2f}%" if pct is not None else None,
                "help": "今日成交量 / 20 日均量；爆量下跌視為出貨疑慮，爆量上漲視為換手承接。",
            })
        else:
            metrics.append({
                "label": "成交量倍數 (VR)",
                "value": "N/A",
                "delta": None,
                "help": "成交量歷史不足 21 根，本子項不計分。",
            })

        # 指標 3：A/D 累積線連續天數（代理連續買賣超）
        streak = compute_ad_streak(df)
        if streak is not None:
            if streak >= 5:
                score_streak = 88.0
            elif streak >= 3:
                score_streak = 78.0
            elif streak >= 1:
                score_streak = 62.0
            elif streak == 0:
                score_streak = 50.0
            elif streak >= -2:
                score_streak = 38.0
            else:
                score_streak = 22.0
            subs.append(score_streak)
            label = "連續買超（代理）" if streak > 0 else ("連續賣超（代理）" if streak < 0 else "多空平衡")
            metrics.append({
                "label": "A/D 連續天數（代理）",
                "value": f"{streak:+d} 日",
                "delta": label,
                "help": "A/D 累積線連續買方主導天數，代理主力／法人連續買賣超。",
            })
        else:
            metrics.append({
                "label": "A/D 連續天數（代理）",
                "value": "N/A",
                "delta": None,
                "help": "行情資料不足，本子項不計分。",
            })

    score = _mean(subs)
    vetoes = []
    if use is not None and use > 80:
        vetoes.append(VETO_MARGIN)
    if (
        use is not None
        and use > 60
        and vr is not None
        and vr >= 2.0
        and pct is not None
        and pct < 0
    ):
        vetoes.append(VETO_MARGIN_DUMP)

    if score is None:
        verdict = "資料不足，本層不計分"
        narrative = "缺少行情或融資資料，本層無法判讀籌碼結構。"
        advice = "請確認網路可連線取得行情資料。"
    else:
        if score >= 65:
            verdict = "機構／大戶資金集中卡位"
        elif score <= 40:
            verdict = "融資籌碼鬆動／籌碼凌亂"
        else:
            verdict = "籌碼中性"

        bits = []
        if use is not None:
            bits.append(f"融資使用率 {use:.1f}%")
        if vr is not None:
            bits.append(f"量能 {vr:.2f}x")
        streak_text = next(
            (m["value"] for m in metrics if m["label"].startswith("A/D")), None
        )
        if streak_text:
            bits.append(f"A/D 連續 {streak_text}")
        narrative = "；".join(bits) + f"，判定為「{verdict}」。"

        if verdict.startswith("機構"):
            advice = "籌碼集中且量價配合，可視為多方結構；留意爆量不漲的轉弱訊號。"
        elif verdict.startswith("融資"):
            advice = "籌碼浮濫或連續賣超，建議降低持股並設定停損，避免融資斷頭壓力。"
        else:
            advice = "籌碼無明確方向，建議等待 A/D 連續買超天數明顯放大再加碼。"

    if vetoes and score is not None:
        score = min(score, VETO_CAP)

    return _layer(
        key="flow",
        short="籌碼",
        name="籌碼體質健檢",
        icon="💰",
        weight=WEIGHT_FLOW,
        score=score,
        verdict=verdict,
        metrics=metrics,
        narrative=narrative,
        advice=advice,
        vetoes=vetoes,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 第 4 層：財務與獲利健檢（35%）
# ---------------------------------------------------------------------------
def score_financial(fund: dict, valuation: dict) -> dict:
    """第 4 層：財務與獲利健檢（權重 35%）。

    診斷指標（三項等權）：
      1. 獲利真實度：CFO / 淨利比率（預防 EPS 灌水）
      2. 估值安全度：價值偏差率 VGR 與 歷史 P/E / P/B 分位數
      3. 經營槓桿敏感度：Price vs. Volume 拆解
    """
    fund = fund if isinstance(fund, dict) else {}
    valuation = valuation if isinstance(valuation, dict) else {}
    metrics = []
    notes = []
    sub_layers = []

    # ---- 1. 獲利真實度：CFO / 淨利 ----
    cfo = _num(fund.get("cfo_ttm"))
    ni = _num(fund.get("net_income_ttm"))
    cfo_ratio = None
    if cfo is not None and ni is not None and ni > 0:
        cfo_ratio = cfo / ni

    if cfo_ratio is not None:
        if cfo_ratio >= 0.9:
            score_truth = 92.0
        elif cfo_ratio >= 0.7:
            score_truth = 80.0
        elif cfo_ratio >= 0.5:
            score_truth = 65.0
        elif cfo_ratio >= CFO_VETO_RATIO:
            score_truth = 45.0
        else:
            score_truth = 20.0
        sub_layers.append(("獲利真實度", score_truth))
        metrics.append({
            "label": "CFO / 淨利比率",
            "value": f"{cfo_ratio:.2f}x",
            "delta": f"CFO {_yi(cfo)}／淨利 {_yi(ni)}",
            "help": "近 4 季營業活動現金流量 / 淨利；低於 0.30 代表獲利缺乏現金支撐。",
        })
    elif ni is not None and ni <= 0:
        score_truth = 40.0
        sub_layers.append(("獲利真實度", score_truth))
        metrics.append({
            "label": "CFO / 淨利比率",
            "value": "虧損",
            "delta": f"CFO {_yi(cfo)}／淨利 {_yi(ni)}",
            "help": "公司處於虧損，比率無意義；本子項給 40 分中性偏低。",
        })
        notes.append("近 4 季為虧損，獲利真實度無法以 CFO/淨利驗證。")
    else:
        metrics.append({
            "label": "CFO / 淨利比率",
            "value": "N/A",
            "delta": None,
            "help": "未取得現金流量表或損益表，本子項不計分。",
        })
        notes.append("缺少 CFO 或淨利資料，獲利真實度無法計算。")

    # ---- 2. 估值安全度：VGR + P/E 分位 + P/B 分位 ----
    vgr = _num(valuation.get("vgr"))
    pe_pct = _num(valuation.get("pe_percentile"))
    pb_pct = _num(valuation.get("pb_percentile"))
    val_subs = []

    if vgr is not None:
        # VGR 越低（相對合理價越便宜）分數越高
        val_subs.append(_clip(50.0 - 1.6 * vgr, 5.0, 100.0))
        metrics.append({
            "label": "價值偏差率 VGR",
            "value": f"{vgr:+.1f}%",
            "delta": f"合理價 {_num(valuation.get('fair_value')):,.2f} 元"
            if _num(valuation.get("fair_value")) is not None else None,
            "help": "現價相對合理價的偏差；正值代表高於合理價（相對偏貴）。",
        })
    else:
        metrics.append({
            "label": "價值偏差率 VGR",
            "value": "N/A",
            "delta": None,
            "help": "合理價無法建立（EPS 或歷史本益比樣本不足），本子項不計分。",
        })

    if pe_pct is not None:
        # 歷史本益比分位：低分位代表相對歷史便宜
        val_subs.append(_clip(50.0 + 0.8 * (50.0 - pe_pct), 5.0, 100.0))
        pe_now = _num(valuation.get("pe_now"))
        metrics.append({
            "label": "歷史 P/E 分位",
            "value": f"{pe_pct:.0f}%",
            "delta": f"目前 P/E {pe_now:.1f}x" if pe_now is not None else None,
            "help": "目前本益比在自身歷史分布的百分位；低分位代表相對歷史便宜。",
        })
    else:
        metrics.append({
            "label": "歷史 P/E 分位",
            "value": "N/A",
            "delta": None,
            "help": "歷史本益比樣本不足，本子項不計分。",
        })

    if pb_pct is not None:
        val_subs.append(_clip(50.0 + 0.8 * (50.0 - pb_pct), 5.0, 100.0))
        metrics.append({
            "label": "P/B 分位（全市場）",
            "value": f"{pb_pct:.0f}%",
            "delta": "TWSE 橫斷面",
            "help": "個股股價淨值比在全市場的百分位；低分位代表相對便宜。",
        })
    else:
        metrics.append({
            "label": "P/B 分位（全市場）",
            "value": "N/A",
            "delta": None,
            "help": "上櫃股票或 TWSE 資料不足，本子項不計分。",
        })
        notes.append("P/B 分位採 TWSE 全市場橫斷面；上櫃股票或證交所資料缺漏時無法計算。")

    score_valuation = _mean(val_subs)
    if score_valuation is not None:
        sub_layers.append(("估值安全度", score_valuation))

    # ---- 3. 經營槓桿敏感度：Price vs. Volume 拆解 ----
    growth = derive_growth(fund)
    rev_yoy = growth["rev_yoy"]
    cogs_yoy = growth["cogs_yoy"]
    oi_yoy = growth["oi_yoy"]
    gm_delta = growth["gm_delta"]
    dol = growth["dol"]
    price_effect = growth["price_effect"]
    volume_effect = growth["volume_effect"]

    lev_subs = []
    if oi_yoy is not None:
        lev_subs.append(_clip(50.0 + 1.5 * oi_yoy, 5.0, 100.0))
        metrics.append({
            "label": "營業利益 YoY",
            "value": f"{oi_yoy:+.2f}%",
            "delta": None,
            "help": "最近一季營業利益相對去年同季的年增率。",
        })
    if gm_delta is not None:
        lev_subs.append(_clip(50.0 + 6.0 * gm_delta, 5.0, 100.0))
    if dol is not None:
        if rev_yoy is not None and rev_yoy <= 0 and dol > 1:
            score_dol = 30.0       # 營收衰退時槓桿反噬
        elif dol < 0:
            score_dol = 35.0       # 負槓桿：營業利益與營收背離
        elif dol <= 2.5:
            score_dol = 85.0       # 槓桿適中
        elif dol <= 4.0:
            score_dol = 65.0       # 偏高
        else:
            score_dol = 45.0       # 過度敏感
        lev_subs.append(score_dol)

    # Price vs. Volume 拆解明細（不計分，供 expander 顯示）
    metrics.append({
        "label": "價格/組合面貢獻 (Price)",
        "value": f"{price_effect:+.2f}%" if price_effect is not None else "N/A",
        "delta": f"毛利率 {growth['gm_now']:.1f}%"
        if growth["gm_now"] is not None else None,
        "help": "≈ 營收 YoY − 銷貨成本 YoY（假設單位銷貨成本穩定）。",
    })
    metrics.append({
        "label": "數量面貢獻 (Volume)",
        "value": f"{volume_effect:+.2f}%" if volume_effect is not None else "N/A",
        "delta": None,
        "help": "≈ 銷貨成本 YoY，作為銷量成長的代理。",
    })
    metrics.append({
        "label": "經營槓桿 (DOL)",
        "value": f"{dol:.2f}x" if dol is not None else "N/A",
        "delta": f"毛利率變動 {gm_delta:+.1f}pp" if gm_delta is not None else None,
        "help": "營業利益 YoY / 營收 YoY；大於 1 代表營收成長會放大到營業利益。",
    })

    score_leverage = _mean(lev_subs)
    if score_leverage is not None:
        sub_layers.append(("經營槓桿敏感度", score_leverage))

    # ---- 三項等權合成 ----
    score = _mean([s for _, s in sub_layers])

    vetoes = []
    if cfo_ratio is not None and cfo_ratio < CFO_VETO_RATIO:
        # 現金流為負時採用更精確的警示文案
        vetoes.append(VETO_NEG_CFO if cfo is not None and cfo <= 0 else VETO_CFO)
    if vgr is not None and vgr > VGR_VETO:
        vetoes.append(VETO_VGR)

    if score is None:
        verdict = "資料不足，本層不計分"
        narrative = "缺少財報或估值資料，本層無法判讀財務體質。"
        advice = "請確認 yfinance 可取得該股的三大報表與歷史交易資料。"
    else:
        if score >= 65:
            verdict = "財務體質健全、估值合理"
        elif score <= 40:
            verdict = "獲利真實度或估值有疑慮"
        else:
            verdict = "財務中等、需持續追蹤"

        bits = []
        if cfo_ratio is not None:
            bits.append(f"CFO/淨利 {cfo_ratio:.2f}x")
        if vgr is not None:
            bits.append(f"價值偏差率 {vgr:+.1f}%")
        if pe_pct is not None:
            bits.append(f"歷史 P/E 分位 {pe_pct:.0f}%")
        if dol is not None:
            bits.append(f"經營槓桿 {dol:.2f}x")
        narrative = (
            "；".join(bits)
            + f"，判定為「{verdict}」"
            + "（"
            + "、".join(f"{name} {val:.0f} 分" for name, val in sub_layers)
            + "）。"
        )

        if verdict.startswith("財務體質"):
            advice = "獲利有現金支撐且估值未過熱，可依計畫分批布局並定期追蹤 CFO。"
        elif verdict.startswith("獲利"):
            advice = "優先檢查現金流量表與應收帳款變化，確認是否存在獲利灌水。"
        else:
            advice = "建議同時監控估值分位與 CFO/淨利比率，任一惡化即降低持股。"

    if vetoes and score is not None:
        score = min(score, VETO_CAP)

    return _layer(
        key="financial",
        short="財務",
        name="財務與獲利健檢",
        icon="🏦",
        weight=WEIGHT_FINANCIAL,
        score=score,
        verdict=verdict,
        metrics=metrics,
        narrative=narrative,
        advice=advice,
        vetoes=vetoes,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# 綜合評分與視覺化
# ---------------------------------------------------------------------------
def aggregate_layers(layers: list) -> dict:
    """四層加權合成綜合健康評分、健康等級與一票否決清單。

    任一層 score 為 None（資料不足）時該層不計入，
    其權重按比例重新分配到其餘層，避免整體分數被拉低。
    """
    layers = layers or []
    available = [item for item in layers if item.get("score") is not None]
    total_weight = float(sum(item["weight"] for item in available))

    total = None
    if total_weight > 0:
        total = float(
            sum(item["score"] * item["weight"] for item in available) / total_weight
        )

    if total is None:
        grade, icon = "資料不足", "⚪"
    elif total >= GRADE_GREEN:
        grade, icon = "綠燈（健康）", "🟢"
    elif total >= GRADE_YELLOW:
        grade, icon = "黃燈（觀察）", "🟡"
    else:
        grade, icon = "紅燈（警戒）", "🔴"

    vetoes = [v for item in layers for v in item.get("vetoes", [])]
    return {
        "total": total,
        "grade": grade,
        "icon": icon,
        "vetoes": vetoes,
        "available_weight": total_weight,
        "scored_layers": len(available),
    }


def build_radar_figure(layers: list) -> go.Figure:
    """四層蜘蛛雷達圖（Plotly Scatterpolar），附 80 分健康門檻參考線。"""
    labels = [item.get("short", item.get("name", "")) for item in layers]
    values = [
        float(item["score"]) if item.get("score") is not None else 0.0
        for item in layers
    ]
    closed_labels = labels + labels[:1]
    closed_values = values + values[:1]

    fig = go.Figure()
    fig.add_trace(
        go.Scatterpolar(
            r=closed_values,
            theta=closed_labels,
            fill="toself",
            name="四層得分",
            line=dict(width=2, color="#1f77b4"),
            fillcolor="rgba(31, 119, 180, 0.25)",
            hovertemplate="%{theta}：%{r:.1f} 分<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatterpolar(
            r=[GRADE_GREEN] * len(closed_labels),
            theta=closed_labels,
            name=f"健康門檻 {GRADE_GREEN:.0f} 分",
            line=dict(width=1, dash="dash", color="green"),
            hoverinfo="skip",
        )
    )
    fig.update_layout(
        polar=dict(
            radialaxis=dict(visible=True, range=[0, 100], tickfont=dict(size=10)),
            angularaxis=dict(tickfont=dict(size=13)),
        ),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=-0.25),
        height=430,
        margin=dict(t=30, b=60, l=40, r=40),
        title=dict(text="四層健檢雷達圖", font=dict(size=15)),
    )
    return fig


def build_summary_text(layers: list, agg: dict) -> str:
    """組出「四層健檢分析總結」Markdown 文字。"""
    total = agg.get("total")
    total_text = "N/A" if total is None else f"{total:.1f}"
    lines = [
        f"**四層健檢分析總結**　綜合健康評分 **{total_text} / 100**"
        f"　→　{agg.get('icon', '')} {agg.get('grade', '')}",
        "",
    ]
    for item in layers:
        score = item.get("score")
        score_text = "N/A" if score is None else f"{score:.1f}"
        lines.append(
            f"- {item.get('icon', '')} **{item.get('name', '')}**"
            f"（權重 {item['weight'] * 100:.0f}%）：**{score_text} 分**"
            f" — {item.get('verdict', '')}"
        )
    return "\n".join(lines)


def build_health_report(
    benchmark_close: pd.Series,
    treasury_yield: pd.Series,
    market_median_pe,
    fund: dict,
    df: pd.DataFrame,
    margin_usage,
    valuation: dict,
) -> dict:
    """一口氣計算四層得分與綜合結果（供 UI 單點呼叫，保持呼叫端簡潔）。"""
    erp = compute_erp(market_median_pe, treasury_yield, benchmark_close)
    layers = [
        score_macro(benchmark_close, erp),
        score_industry(fund),
        score_flow(df, margin_usage),
        score_financial(fund, valuation),
    ]
    agg = aggregate_layers(layers)
    return {"layers": layers, "agg": agg, "erp": erp}
