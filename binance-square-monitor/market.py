"""
币安公开行情 API 封装（只读，不需要 API Key）

涉及接口：
- /api/v3/ticker/24hr           现货 24h 数据
- /fapi/v1/exchangeInfo         合约上市列表（判断某币有没有永续合约）
- /fapi/v1/premiumIndex         合约标记价 + 资金费率
- /fapi/v1/openInterest         未平仓合约量
- /futures/data/openInterestHist OI 历史（用于算变化率）
- /futures/data/globalLongShortAccountRatio  全网多空账户比
- /futures/data/topLongShortPositionRatio    大户持仓多空比
- /fapi/v1/klines               合约 K 线（算短期动量/波动）
"""
from __future__ import annotations
import time
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json
import config
import ssl

# 创建一个不验证证书的 SSL 上下文
ssl_context = ssl._create_unverified_context()
SPOT_BASE = "https://api.binance.com"
FAPI_BASE = "https://fapi.binance.com"

# 简单内存缓存：合约上市列表几小时才变一次，没必要每轮都请求
_FUTURES_SYMBOLS_CACHE: dict = {"ts": 0.0, "symbols": set()}
_CACHE_TTL = 3600  # 1 小时


def _http_get(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    if params:
        url = f"{url}?{urlencode(params)}"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 market-monitor"})
        # 在 urlopen 中加入 context 参数
        with urlopen(req, timeout=timeout, context=ssl_context) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"DEBUG: 请求失败! URL: {url} | 错误原因: {e}")
        return None


def get_futures_symbols() -> set[str]:
    """返回币安 USDT 永续合约上市的所有 base 币种 set，比如 {'BTC', 'ETH', 'PEPE', ...}"""
    global _FUTURES_SYMBOLS_CACHE
    now = time.time()
    if now - _FUTURES_SYMBOLS_CACHE["ts"] < _CACHE_TTL and _FUTURES_SYMBOLS_CACHE["symbols"]:
        return _FUTURES_SYMBOLS_CACHE["symbols"]

    data = _http_get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
    if not data:
        return _FUTURES_SYMBOLS_CACHE["symbols"]  # 返回上次的缓存

    symbols = set()
    for s in data.get("symbols", []):
        # 只收 USDT 永续、状态为 TRADING
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
            symbols.add(s.get("baseAsset", "").upper())

    _FUTURES_SYMBOLS_CACHE = {"ts": now, "symbols": symbols}
    return symbols


def has_perpetual(token: str) -> bool:
    return token.upper() in get_futures_symbols()


def _perp_symbol(token: str) -> str:
    return f"{token.upper()}USDT"


def get_mark_price(token: str) -> Optional[float]:
    """轻量拉取 USDT 永续标记价，用于持仓价格兜底刷新。"""
    data = _http_get(
        f"{FAPI_BASE}/fapi/v1/premiumIndex",
        {"symbol": _perp_symbol(token)},
        timeout=5,
    )
    if not data:
        return None
    try:
        return float(data.get("markPrice"))
    except (TypeError, ValueError):
        return None


def get_klines_1h(token: str, limit: int = 30) -> list[dict] | None:
    """
    拉 1h K 线，用于 ATR 止损计算。返回按时间升序的 list，每项：
        {"open_time", "open", "high", "low", "close", "volume"}
    失败返回 None。
    """
    raw = _http_get(
        f"{FAPI_BASE}/fapi/v1/klines",
        {"symbol": _perp_symbol(token), "interval": "1h", "limit": limit},
        timeout=8,
    )
    if not raw or not isinstance(raw, list):
        return None
    try:
        return [
            {
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in raw
        ]
    except (TypeError, ValueError, IndexError):
        return None


def _pct_change(latest: float, past: float) -> Optional[float]:
    if past <= 0:
        return None
    return (latest - past) / past * 100


def _oi_value(row: dict) -> Optional[float]:
    try:
        return float(row.get("sumOpenInterestValue"))
    except (TypeError, ValueError, AttributeError):
        return None


def _depth_metrics(depth: dict, mark_price: float | None) -> dict:
    metrics = {
        "bid_ask_spread_pct": None,
        "depth_bid_1pct_usd": None,
        "depth_ask_1pct_usd": None,
        "depth_imbalance_pct": None,
    }
    if not depth:
        return metrics
    try:
        bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
    except (TypeError, ValueError):
        return metrics
    if not bids or not asks:
        return metrics
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = mark_price or ((best_bid + best_ask) / 2)
    if mid <= 0:
        return metrics
    metrics["bid_ask_spread_pct"] = (best_ask - best_bid) / mid * 100
    band = config.DEPTH_RANGE_PCT / 100
    bid_floor = mid * (1 - band)
    ask_ceiling = mid * (1 + band)
    bid_usd = sum(price * qty for price, qty in bids if price >= bid_floor)
    ask_usd = sum(price * qty for price, qty in asks if price <= ask_ceiling)
    metrics["depth_bid_1pct_usd"] = bid_usd
    metrics["depth_ask_1pct_usd"] = ask_usd
    total = bid_usd + ask_usd
    if total > 0:
        metrics["depth_imbalance_pct"] = (bid_usd - ask_usd) / total * 100
    return metrics


def _taker_metrics(rows: list) -> dict:
    """
    从 taker long/short ratio 数据计算指标。

    rows 通常是 Binance /futures/data/takerlongshortRatio 返回的 list，
    按时间升序（最早在前，最新在后），每项含 buyVol / sellVol。

    返回：
      taker_buy_sell_ratio  —— 所有区间总合的 buy/sell（现有行为）
      taker_buy_pct         —— buy / (buy+sell) * 100
      taker_ratio_recent    —— 最新一根的 buy/sell
      taker_ratio_older     —— 较早 2 根的平均 buy/sell
      taker_trend_pct       —— (recent - older) / older * 100，正=买盘增强，负=衰退
    """
    metrics = {
        "taker_buy_sell_ratio": None,
        "taker_buy_vol": None,
        "taker_sell_vol": None,
        "taker_buy_pct": None,
        "taker_ratio_recent": None,
        "taker_ratio_older": None,
        "taker_trend_pct": None,
    }
    clean = []
    for row in rows or []:
        try:
            b = float(row.get("buyVol") or 0)
            s = float(row.get("sellVol") or 0)
            if b >= 0 and s >= 0:
                clean.append((b, s))
        except (TypeError, ValueError, AttributeError):
            continue

    if not clean:
        return metrics

    # 总合比例（保持旧行为）
    buy_total = sum(b for b, _ in clean)
    sell_total = sum(s for _, s in clean)
    if buy_total <= 0 and sell_total <= 0:
        return metrics
    metrics["taker_buy_vol"] = buy_total
    metrics["taker_sell_vol"] = sell_total
    if sell_total > 0:
        metrics["taker_buy_sell_ratio"] = buy_total / sell_total
    tot = buy_total + sell_total
    if tot > 0:
        metrics["taker_buy_pct"] = buy_total / tot * 100

    # 趋势指标：需要至少 2 根数据才有意义
    if len(clean) >= 2:
        b_recent, s_recent = clean[-1]
        # 除最新外的所有作为"较早"基线（取平均，抗噪）
        older = clean[:-1]
        b_older = sum(b for b, _ in older) / len(older)
        s_older = sum(s for _, s in older) / len(older)

        r_recent = (b_recent / s_recent) if s_recent > 0 else None
        r_older = (b_older / s_older) if s_older > 0 else None
        metrics["taker_ratio_recent"] = r_recent
        metrics["taker_ratio_older"] = r_older

        if r_recent is not None and r_older is not None and r_older > 0:
            metrics["taker_trend_pct"] = (r_recent - r_older) / r_older * 100

    return metrics


def get_market_snapshot(token: str) -> Optional[dict]:
    """
    拉取某代币的完整市场快照。有永续合约才调用（has_perpetual 外部先判断）
    返回字段：
      symbol              USDT 永续交易对名
      mark_price          合约标记价
      funding_rate        当前资金费率（每 8h）
      funding_rate_pct    百分比形式（0.0001 -> 0.01%）
      oi_usd              当前未平仓合约金额（美元）
      oi_change_1h_pct    OI 1 小时变化率（%）
      change_15m_pct      15 分钟价格变化（%）
      change_1h_pct       1 小时价格变化（%）
      change_4h_pct       4 小时价格变化（%）
      change_24h_pct      24 小时价格变化（%）
      volume_24h_usd      24 小时成交额
      long_short_ratio    全网多空账户比（>1 = 多头多）
      top_trader_ls_ratio 大户持仓多空比
    任何字段取不到就是 None
    """
    symbol = _perp_symbol(token)
    snap = {
        "token": token.upper(),
        "symbol": symbol,
        "mark_price": None,
        "funding_rate": None,
        "funding_rate_pct": None,
        "oi_usd": None,
        "oi_change_15m_pct": None,
        "oi_change_1h_pct": None,
        "oi_change_4h_pct": None,
        "oi_change_48h_pct": None,       # 新增：48h OI 变化
        "change_15m_pct": None,
        "change_1h_pct": None,
        "change_4h_pct": None,
        "change_24h_pct": None,
        "change_48h_pct": None,          # 新增：48h 价格变化
        "volume_24h_usd": None,
        "long_short_ratio": None,
        "top_trader_ls_ratio": None,
        "taker_buy_sell_ratio": None,
        "taker_buy_vol": None,
        "taker_sell_vol": None,
        "taker_buy_pct": None,
        "bid_ask_spread_pct": None,
        "depth_bid_1pct_usd": None,
        "depth_ask_1pct_usd": None,
        "depth_imbalance_pct": None,
    }

    # 1) 标记价 + 资金费率
    prem = _http_get(f"{FAPI_BASE}/fapi/v1/premiumIndex", {"symbol": symbol})
    if prem:
        try:
            snap["mark_price"] = float(prem.get("markPrice"))
            fr = float(prem.get("lastFundingRate"))
            snap["funding_rate"] = fr
            snap["funding_rate_pct"] = fr * 100
        except (TypeError, ValueError):
            pass

    # 2) 未平仓合约
    oi_now = _http_get(f"{FAPI_BASE}/fapi/v1/openInterest", {"symbol": symbol})
    if oi_now and snap["mark_price"]:
        try:
            oi_coins = float(oi_now.get("openInterest"))
            snap["oi_usd"] = oi_coins * snap["mark_price"]
        except (TypeError, ValueError):
            pass

    # 3) OI 历史（近 1 小时变化）—— 用 5m 粒度，取最近 13 个点
    oi_hist = _http_get(
        f"{FAPI_BASE}/futures/data/openInterestHist",
        {"symbol": symbol, "period": "5m", "limit": 13},
    )
    if oi_hist and len(oi_hist) >= 2:
        oi_latest = _oi_value(oi_hist[-1])
        if oi_latest is not None:
            if len(oi_hist) >= 4:
                oi_15m = _oi_value(oi_hist[-4])
                if oi_15m is not None:
                    snap["oi_change_15m_pct"] = _pct_change(oi_latest, oi_15m)
            oi_1h = _oi_value(oi_hist[0])
            if oi_1h is not None:
                snap["oi_change_1h_pct"] = _pct_change(oi_latest, oi_1h)

    oi_hist_4h = _http_get(
        f"{FAPI_BASE}/futures/data/openInterestHist",
        {"symbol": symbol, "period": "15m", "limit": 17},
    )
    if oi_hist_4h and len(oi_hist_4h) >= 2:
        oi_latest = _oi_value(oi_hist_4h[-1])
        oi_past = _oi_value(oi_hist_4h[0])
        if oi_latest is not None and oi_past is not None:
            snap["oi_change_4h_pct"] = _pct_change(oi_latest, oi_past)

    # 3b) 48h OI 变化 —— 用 4h 粒度，取 13 个点（覆盖 48h）
    oi_hist_48h = _http_get(
        f"{FAPI_BASE}/futures/data/openInterestHist",
        {"symbol": symbol, "period": "4h", "limit": 13},
    )
    if oi_hist_48h and len(oi_hist_48h) >= 2:
        try:
            oi_latest = float(oi_hist_48h[-1].get("sumOpenInterestValue"))
            oi_past = float(oi_hist_48h[0].get("sumOpenInterestValue"))
            if oi_past > 0:
                snap["oi_change_48h_pct"] = (oi_latest - oi_past) / oi_past * 100
        except (TypeError, ValueError):
            pass

    # 4) 价格动量 —— 用合约 K 线，15m 粒度
    klines = _http_get(
        f"{FAPI_BASE}/fapi/v1/klines",
        {"symbol": symbol, "interval": "15m", "limit": 100},
    )
    if klines and len(klines) >= 17:
        try:
            # K 线第 4 位是收盘价
            closes = [float(k[4]) for k in klines]
            now_price = closes[-1]
            # 最近一根的起始价（15m 前）
            snap["change_15m_pct"] = (now_price - closes[-2]) / closes[-2] * 100
            # 1h 前（4 根）
            if len(closes) >= 5:
                snap["change_1h_pct"] = (now_price - closes[-5]) / closes[-5] * 100
            # 4h 前（16 根）
            if len(closes) >= 17:
                snap["change_4h_pct"] = (now_price - closes[-17]) / closes[-17] * 100
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # 4b) 48h 价格变化 —— 用 1h 粒度，取 49 个点
    klines_48h = _http_get(
        f"{FAPI_BASE}/fapi/v1/klines",
        {"symbol": symbol, "interval": "1h", "limit": 49},
    )
    if klines_48h and len(klines_48h) >= 49:
        try:
            closes = [float(k[4]) for k in klines_48h]
            if closes[0] > 0:
                snap["change_48h_pct"] = (closes[-1] - closes[0]) / closes[0] * 100
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # 5) 现货 24h 数据
    ticker = _http_get(f"{SPOT_BASE}/api/v3/ticker/24hr", {"symbol": symbol})
    # 如果现货接口请求失败（比如 RAVE 只有合约没现货），则尝试从合约接口拿
    if ticker is None or "priceChangePercent" not in ticker:
        ticker = _http_get(f"{FAPI_BASE}/fapi/v1/ticker/24hr", {"symbol": symbol})
    if ticker:
        try:
            snap["change_24h_pct"] = float(ticker.get("priceChangePercent"))
            snap["volume_24h_usd"] = float(ticker.get("quoteVolume"))
        except (TypeError, ValueError):
            pass

    # 6) 多空比
    lsr = _http_get(
        f"{FAPI_BASE}/futures/data/globalLongShortAccountRatio",
        {"symbol": symbol, "period": "15m", "limit": 1},
    )
    if lsr and len(lsr) >= 1:
        try:
            snap["long_short_ratio"] = float(lsr[0].get("longShortRatio"))
        except (TypeError, ValueError):
            pass

    # 7) 大户持仓多空比
    tlsr = _http_get(
        f"{FAPI_BASE}/futures/data/topLongShortPositionRatio",
        {"symbol": symbol, "period": "15m", "limit": 1},
    )
    if tlsr and len(tlsr) >= 1:
        try:
            snap["top_trader_ls_ratio"] = float(tlsr[0].get("longShortRatio"))
        except (TypeError, ValueError):
            pass

    # 8) 主动买卖量：近 20m taker buy/sell（4 根 5m，用于计算趋势）
    taker = _http_get(
        f"{FAPI_BASE}/futures/data/takerlongshortRatio",
        {"symbol": symbol, "period": "5m", "limit": 4},
    )
    snap.update(_taker_metrics(taker if isinstance(taker, list) else []))

    # 9) 盘口深度 / 流动性
    depth = _http_get(
        f"{FAPI_BASE}/fapi/v1/depth",
        {"symbol": symbol, "limit": config.DEPTH_LIMIT},
    )
    snap.update(_depth_metrics(depth, snap.get("mark_price")))

    return snap


if __name__ == "__main__":
    print("=== 开始测试 market.py ===")

    # 1. 测试合约列表获取
    symbols = get_futures_symbols()
    print(f"成功获取合约列表！总数: {len(symbols)}")
    print(f"前 10 个币种样例: {list(symbols)[:10]}")

    # 2. 测试特定币种是否存在
    test_token = "BTC"
    exists = has_perpetual(test_token)
    print(f"币种 {test_token} 是否有合约: {exists}")

    # 3. 测试快照获取 (这一步最关键)
    if exists:
        print(f"正在拉取 {test_token} 的市场快照...")
        snap = get_market_snapshot(test_token)
        if snap:
            print("--- 快照数据成功返回 ---")
            # 打印几个核心指标看看
            print(f"价格: {snap['mark_price']}")
            print(f"资金费率: {snap['funding_rate_pct']}%")
            print(f"24h 成交额: {snap['volume_24h_usd']} USD")
            print(f"1h OI 变化: {snap['oi_change_1h_pct']}%")
        else:
            print("快照返回为空，请检查网络或币安接口。")