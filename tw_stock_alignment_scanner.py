#!/usr/bin/env python3
"""
Taiwan stock daily/weekly alignment scanner.

Data model:
- Symbols: TWSE / TPEx public OpenAPI current quote lists.
- History: Yahoo Finance chart endpoint, using .TW for listed and .TWO for OTC.

The scanner is intended for end-of-day filtering, not intraday trading.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import http.client
import html
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Iterable
import alignment_plan_state


TWSE_QUOTES_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_QUOTES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TWSE_MIS_QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
OUTPUT_FIELDS = ["code", "name", "market", "data_date", "current_price", "entry_gap", "alignment", "list_state", "entry_eligibility", "day_state", "week_state", "suggested_buy", "warning_line", "stop_loss", "tp1", "tp2", "tp3", "tp_status"]
ACTIVE_ENTRY_STATUSES = {"ENTRY TOUCHED", "TP1 GET", "TP2 GET", "TP3 GET", "ORDER UNKNOWN", "STOP HIT / WAIT RESET"}
OPEN_TRACKING_STATUSES = {"ENTRY TOUCHED", "TP1 GET", "TP2 GET", "ORDER UNKNOWN"}
YAHOO_REFRESH_AVAILABLE = True


def is_active_entry_status(status: object) -> bool:
    return str(status).split(" / RISK WIDE", 1)[0] in ACTIVE_ENTRY_STATUSES


def is_open_tracking_status(status: object) -> bool:
    return str(status).split(" / RISK WIDE", 1)[0] in OPEN_TRACKING_STATUSES


def list_state_value(row: dict[str, object]) -> str:
    """Separate current setups, active tracking, and previously qualified watches."""
    alignment = str(row.get("alignment", ""))
    status = str(row.get("tp_status", ""))

    if status == "CANCELLED / ALIGN LOST":
        lost: list[str] = []
        if row.get("day_state") != "BULL":
            lost.append("DAY")
        if row.get("week_state") != "BULL":
            lost.append("WEEK")
        if len(lost) >= 2:
            return "ARCHIVED"
        return "WATCH - " + ("+".join(lost) + " LOST" if lost else "WAIT RESET")

    if alignment == "BULL ALIGN":
        return "QUALIFIED" if status == "WAIT BUY" else "TRACKING"
    if is_open_tracking_status(status):
        return "TRACKING"
    if status.split(" / RISK WIDE", 1)[0] in {"TP3 GET", "STOP HIT / WAIT RESET"}:
        return "ARCHIVED"
    if alignment == "BEAR ALIGN":
        return "BEAR ALIGN"
    return ""


def entry_eligibility_value(row: dict[str, object]) -> str:
    list_state = str(row.get("list_state", ""))
    status = str(row.get("tp_status", ""))
    base_status = status.split(" / RISK WIDE", 1)[0]

    if list_state == "QUALIFIED" and status == "WAIT BUY":
        return "NEW ENTRY"
    if status == "TOO FAR / NO CHASE":
        return "NO CHASE"
    if status == "RISK TOO WIDE / SKIP":
        return "SKIP"
    if base_status == "ORDER UNKNOWN":
        return "VERIFY FIRST"
    if base_status in {"TP1 GET", "TP2 GET", "TP3 GET", "STOP HIT / WAIT RESET"}:
        return "MANAGE ONLY"
    if base_status == "ENTRY TOUCHED":
        if "RISK WIDE" in status:
            return "MANAGE ONLY"
        try:
            current = float(row["current_price"])
            stop = float(row["stop_loss"])
            tp1 = float(row["tp1"])
        except (TypeError, ValueError):
            return "MANAGE ONLY"
        trend_ok = row.get("alignment") == "BULL ALIGN" and row.get("day_state") == "BULL" and row.get("week_state") == "BULL"
        price_ok = stop < current < tp1
        return "RETEST ENTRY" if trend_ok and price_ok else "MANAGE ONLY"
    if list_state.startswith("WATCH"):
        return "WAIT"
    if list_state == "BEAR ALIGN":
        return "INVALID"
    return ""


def include_in_main_list(row: dict[str, object], only_align: bool, only_bull: bool) -> bool:
    """The public list contains only stocks that currently keep day/week bull alignment."""
    if only_align or only_bull:
        return row.get("alignment") == "BULL ALIGN"
    return True


@dataclass(frozen=True)
class QuoteBar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Bar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    market: str
    yahoo_symbol: str
    current_quote: QuoteBar | None = None


@dataclass(frozen=True)
class TrendState:
    state: str
    close: float
    ema: float
    plus_di: float
    minus_di: float
    adx: float
    distance_to_ema_pct: float


@dataclass(frozen=True)
class MarketContext:
    date: str
    contract: str
    contract_month: str
    last: float
    change: float
    pct: float
    bias: str


def fetch_json(url: str, timeout: int = 30, retries: int = 5) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AboTaiwanStockScanner/1.0",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8-sig"))
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise last_error if last_error is not None else RuntimeError("fetch_json failed")


def clean_number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "－", "N/A", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def clean_percent(value: object) -> float | None:
    text = clean_text(value).replace("%", "")
    return clean_number(text)


def parse_roc_date(value: object) -> dt.date | None:
    text = clean_text(value).replace("/", "").replace("-", "")
    if len(text) != 7 or not text.isdigit():
        return None
    year = int(text[:3]) + 1911
    month = int(text[3:5])
    day = int(text[5:7])
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_yyyymmdd_date(value: object) -> dt.date | None:
    text = clean_text(value).replace("/", "").replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return dt.date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def clean_text(value: object) -> str:
    return "" if value is None else str(value).strip()


def is_common_stock_code(code: str) -> bool:
    return len(code) == 4 and code.isdigit() and int(code) >= 1000


def first_existing(row: dict, keys: Iterable[str]) -> str:
    for key in keys:
        if key in row and clean_text(row[key]):
            return clean_text(row[key])
    return ""


def parse_quote_bar(row: dict, market: str) -> QuoteBar | None:
    date = parse_roc_date(first_existing(row, ["Date", "日期"]))
    if date is None:
        return None

    if market == "TWSE":
        open_price = clean_number(row.get("OpeningPrice"))
        high = clean_number(row.get("HighestPrice"))
        low = clean_number(row.get("LowestPrice"))
        close = clean_number(row.get("ClosingPrice"))
        volume = clean_number(row.get("TradeVolume")) or 0
    else:
        open_price = clean_number(row.get("Open"))
        high = clean_number(row.get("High"))
        low = clean_number(row.get("Low"))
        close = clean_number(row.get("Close"))
        volume = clean_number(row.get("TradingShares")) or 0

    if open_price is None or high is None or low is None or close is None:
        return None
    if high < low or close <= 0:
        return None

    return QuoteBar(date=date, open=open_price, high=high, low=low, close=close, volume=volume)


def parse_mis_quote_bar(row: dict) -> QuoteBar | None:
    date = parse_yyyymmdd_date(row.get("d") or row.get("^"))
    open_price = clean_number(row.get("o"))
    high = clean_number(row.get("h"))
    low = clean_number(row.get("l"))
    close = clean_number(row.get("z")) or clean_number(row.get("y"))
    volume_lots = clean_number(row.get("v")) or 0
    if date is None or open_price is None or high is None or low is None or close is None:
        return None
    if high < low or close <= 0:
        return None
    return QuoteBar(date=date, open=open_price, high=high, low=low, close=close, volume=volume_lots * 1000)


def better_quote(base: QuoteBar | None, candidate: QuoteBar | None) -> QuoteBar | None:
    if candidate is None:
        return base
    if base is None or candidate.date >= base.date:
        return candidate
    return base


def mis_channel(stock: Stock) -> str:
    exchange = "tse" if stock.market == "TWSE" else "otc"
    return f"{exchange}_{stock.code}.tw"


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def fetch_realtime_quotes(stocks: list[Stock], chunk_size: int = 80) -> dict[str, QuoteBar]:
    if not stocks:
        return {}

    out: dict[str, QuoteBar] = {}
    channels = [mis_channel(stock) for stock in stocks]
    stock_by_code = {stock.code: stock for stock in stocks}

    for group in chunks(channels, chunk_size):
        params = urllib.parse.urlencode(
            {
                "ex_ch": "|".join(group),
                "json": "1",
                "delay": "0",
                "_": str(int(time.time() * 1000)),
            }
        )
        url = f"{TWSE_MIS_QUOTE_URL}?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 AboTaiwanStockScanner/1.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://mis.twse.com.tw/stock/index.jsp",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8-sig"))
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            continue

        for row in data.get("msgArray", []) if isinstance(data, dict) else []:
            if not isinstance(row, dict):
                continue
            code = clean_text(row.get("c"))
            if code not in stock_by_code:
                continue
            quote = parse_mis_quote_bar(row)
            if quote is not None:
                out[code] = quote
    return out


def apply_realtime_quotes(stocks: list[Stock]) -> list[Stock]:
    realtime = fetch_realtime_quotes(stocks)
    if not realtime:
        return stocks
    return [replace(stock, current_quote=better_quote(stock.current_quote, realtime.get(stock.code))) for stock in stocks]


def parse_twse_stocks(rows: object) -> list[Stock]:
    stocks: list[Stock] = []
    if not isinstance(rows, list):
        return stocks
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = first_existing(row, ["Code", "SecuritiesCompanyCode", "公司代號", "代號"])
        name = first_existing(row, ["Name", "CompanyName", "公司名稱", "證券名稱", "名稱"])
        if is_common_stock_code(code):
            stocks.append(Stock(code=code, name=name, market="TWSE", yahoo_symbol=f"{code}.TW", current_quote=parse_quote_bar(row, "TWSE")))
    return stocks


def parse_tpex_stocks(rows: object) -> list[Stock]:
    stocks: list[Stock] = []
    if not isinstance(rows, list):
        return stocks
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = first_existing(row, ["SecuritiesCompanyCode", "Code", "代號", "證券代號"])
        name = first_existing(row, ["CompanyName", "Name", "公司名稱", "證券名稱", "名稱"])
        if not code:
            for value in row.values():
                text = clean_text(value)
                if is_common_stock_code(text):
                    code = text
                    break
        if is_common_stock_code(code):
            stocks.append(Stock(code=code, name=name, market="TPEX", yahoo_symbol=f"{code}.TWO", current_quote=parse_quote_bar(row, "TPEX")))
    return stocks


def load_stock_universe(markets: set[str]) -> list[Stock]:
    stocks: list[Stock] = []
    if "TWSE" in markets:
        stocks.extend(parse_twse_stocks(fetch_json(TWSE_QUOTES_URL)))
    if "TPEX" in markets:
        try:
            stocks.extend(parse_tpex_stocks(fetch_json(TPEX_QUOTES_URL)))
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            print("Warning: TPEX stock universe unavailable; continuing with available markets.", file=sys.stderr)

    seen: set[str] = set()
    unique: list[Stock] = []
    for stock in stocks:
        key = f"{stock.market}:{stock.code}"
        if key not in seen:
            seen.add(key)
            unique.append(stock)
    return sorted(unique, key=lambda s: (s.market, s.code))


def fetch_market_context(args: argparse.Namespace) -> MarketContext | None:
    if not args.use_taifex_night:
        return None

    try:
        rows = fetch_json("https://openapi.taifex.com.tw/v1/DailyMarketReportFut")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None

    candidates: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if clean_text(row.get("Contract")) != args.market_contract:
            continue
        if clean_text(row.get("TradingSession")) != "盤後":
            continue
        last = clean_number(row.get("Last"))
        pct = clean_percent(row.get("%"))
        volume = clean_number(row.get("Volume"))
        if last is None or pct is None or volume is None or volume <= 0:
            continue
        candidates.append(row)

    if not candidates:
        return None

    # 近月合約通常是盤後觀察隔日台股情緒最直覺的參考。
    front = sorted(candidates, key=lambda r: clean_text(r.get("ContractMonth(Week)")))[0]
    pct = clean_percent(front.get("%"))
    change = clean_number(front.get("Change"))
    last = clean_number(front.get("Last"))
    if pct is None or change is None or last is None:
        return None

    bias = "NEUTRAL"
    if pct >= args.night_bull_pct:
        bias = "BULL"
    elif pct <= -args.night_bear_pct:
        bias = "BEAR"

    return MarketContext(
        date=clean_text(front.get("Date")),
        contract=clean_text(front.get("Contract")),
        contract_month=clean_text(front.get("ContractMonth(Week)")),
        last=last,
        change=change,
        pct=pct,
        bias=bias,
    )


def yahoo_history_url(yahoo_symbol: str, years: int) -> str:
    end = int(time.time())
    start = int((dt.datetime.now() - dt.timedelta(days=365 * years + 30)).timestamp())
    params = urllib.parse.urlencode(
        {
            "period1": start,
            "period2": end,
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
    )
    return f"{YAHOO_CHART_URL.format(symbol=urllib.parse.quote(yahoo_symbol))}?{params}"


def load_cached_history(cache_dir: Path, yahoo_symbol: str, max_age_hours: float | None) -> object | None:
    path = cache_dir / f"{yahoo_symbol}.json"
    if not path.exists():
        return None
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    if max_age_hours is not None and age_hours > max_age_hours:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_cached_history(cache_dir: Path, yahoo_symbol: str, data: object) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{yahoo_symbol}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def fetch_history(stock: Stock, years: int, cache_dir: Path, cache_hours: float, sleep_seconds: float) -> list[Bar]:
    global YAHOO_REFRESH_AVAILABLE
    cached = load_cached_history(cache_dir, stock.yahoo_symbol, cache_hours)
    if cached is None:
        stale = load_cached_history(cache_dir, stock.yahoo_symbol, None)
        if not YAHOO_REFRESH_AVAILABLE and stale is not None:
            return parse_yahoo_bars(stale)
        try:
            data = fetch_json(yahoo_history_url(stock.yahoo_symbol, years), timeout=30)
            save_cached_history(cache_dir, stock.yahoo_symbol, data)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            # Yahoo may temporarily rate-limit a full-market run. A stale history
            # cache is still usable because the latest TWSE/TPEx quote is overlaid later.
            YAHOO_REFRESH_AVAILABLE = False
            data = stale
            if data is None:
                raise
    else:
        data = cached
    return parse_yahoo_bars(data)


def apply_current_quote(bars: list[Bar], quote: QuoteBar | None) -> list[Bar]:
    if quote is None:
        return bars

    quote_bar = Bar(
        date=quote.date,
        open=quote.open,
        high=quote.high,
        low=quote.low,
        close=quote.close,
        volume=quote.volume,
    )
    if not bars:
        return [quote_bar]

    if quote.date > bars[-1].date:
        return [*bars, quote_bar]
    if quote.date == bars[-1].date:
        return [*bars[:-1], quote_bar]
    return bars


def parse_yahoo_bars(data: object) -> list[Bar]:
    result = (((data or {}).get("chart") or {}).get("result") or [None])[0]
    if not result:
        return []
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    bars: list[Bar] = []
    for i, ts in enumerate(timestamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            v = volumes[i] if i < len(volumes) else 0
        except IndexError:
            continue
        if o is None or h is None or l is None or c is None:
            continue
        date = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
        bars.append(Bar(date=date, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v or 0)))
    return bars


def ema(values: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    alpha = 2 / (length + 1)
    prev = seed
    for i in range(length, len(values)):
        prev = values[i] * alpha + prev * (1 - alpha)
        out[i] = prev
    return out


def rma(values: list[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < length:
        return out
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    prev = seed
    for i in range(length, len(values)):
        prev = (prev * (length - 1) + values[i]) / length
        out[i] = prev
    return out


def atr(bars: list[Bar], length: int) -> list[float | None]:
    true_ranges = [0.0] * len(bars)
    for i, bar in enumerate(bars):
        if i == 0:
            true_ranges[i] = bar.high - bar.low
            continue
        prev_close = bars[i - 1].close
        true_ranges[i] = max(
            bar.high - bar.low,
            abs(bar.high - prev_close),
            abs(bar.low - prev_close),
        )
    return rma(true_ranges, length)


def resistance_levels(bars: list[Bar], entry_price: float, lookback: int, min_gap_pct: float) -> list[float]:
    if not bars or lookback <= 0:
        return []

    recent = bars[-lookback:]
    min_price = entry_price * (1 + min_gap_pct * 0.01)
    raw_levels: list[float] = []
    for i, bar in enumerate(recent):
        left_high = recent[i - 1].high if i > 0 else -math.inf
        right_high = recent[i + 1].high if i < len(recent) - 1 else -math.inf
        if bar.high >= min_price and bar.high >= left_high and bar.high >= right_high:
            raw_levels.append(bar.high)

    raw_levels.append(max(b.high for b in recent))
    levels = sorted(level for level in raw_levels if level >= min_price)

    merged: list[float] = []
    for level in levels:
        if not merged or abs(level - merged[-1]) / entry_price * 100 > min_gap_pct:
            merged.append(level)
    return merged


def stock_tick_size(price: float) -> Decimal:
    if price < 10:
        return Decimal("0.01")
    if price < 50:
        return Decimal("0.05")
    if price < 100:
        return Decimal("0.1")
    if price < 500:
        return Decimal("0.5")
    if price < 1000:
        return Decimal("1")
    return Decimal("5")


def floor_stock_tick(price: float) -> float:
    value = Decimal(str(price))
    tick = stock_tick_size(float(value))
    return float((value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)


def next_stock_tick(price: float) -> float:
    tick = stock_tick_size(price)
    return float(Decimal(str(price)) + tick)


def dmi_adx(bars: list[Bar], length: int, smoothing: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    n = len(bars)
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    for i in range(1, n):
        high_diff = bars[i].high - bars[i - 1].high
        low_diff = bars[i - 1].low - bars[i].low
        plus_dm[i] = high_diff if high_diff > low_diff and high_diff > 0 else 0.0
        minus_dm[i] = low_diff if low_diff > high_diff and low_diff > 0 else 0.0
        tr[i] = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )

    tr_rma = rma(tr, length)
    plus_rma = rma(plus_dm, length)
    minus_rma = rma(minus_dm, length)

    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    dx = [0.0] * n

    for i in range(n):
        if tr_rma[i] is None or tr_rma[i] == 0 or plus_rma[i] is None or minus_rma[i] is None:
            continue
        plus_di[i] = 100 * plus_rma[i] / tr_rma[i]
        minus_di[i] = 100 * minus_rma[i] / tr_rma[i]
        di_sum = plus_di[i] + minus_di[i]
        dx[i] = 0.0 if di_sum == 0 else 100 * abs(plus_di[i] - minus_di[i]) / di_sum

    adx = rma(dx, smoothing)
    return plus_di, minus_di, adx


def weekly_bars(daily: list[Bar]) -> list[Bar]:
    groups: dict[tuple[int, int], list[Bar]] = {}
    for bar in daily:
        iso = bar.date.isocalendar()
        groups.setdefault((iso.year, iso.week), []).append(bar)

    weekly: list[Bar] = []
    for key in sorted(groups):
        rows = sorted(groups[key], key=lambda b: b.date)
        weekly.append(
            Bar(
                date=rows[-1].date,
                open=rows[0].open,
                high=max(b.high for b in rows),
                low=min(b.low for b in rows),
                close=rows[-1].close,
                volume=sum(b.volume for b in rows),
            )
        )
    return weekly


def calc_trend(bars: list[Bar], ema_length: int, dmi_length: int, adx_smoothing: int, adx_threshold: float, slope_filter: bool) -> TrendState | None:
    min_bars = max(ema_length + 5, dmi_length + adx_smoothing + 5)
    if len(bars) < min_bars:
        return None

    closes = [b.close for b in bars]
    ema_values = ema(closes, ema_length)
    plus_di, minus_di, adx_values = dmi_adx(bars, dmi_length, adx_smoothing)

    i = len(bars) - 1
    ema_value = ema_values[i]
    plus = plus_di[i]
    minus = minus_di[i]
    adx_value = adx_values[i]
    if ema_value is None or plus is None or minus is None or adx_value is None:
        return None

    ema_prev = ema_values[i - 1]
    ema_slope_up = ema_prev is not None and ema_value > ema_prev
    ema_slope_down = ema_prev is not None and ema_value < ema_prev

    bull = closes[i] > ema_value and plus > minus and adx_value > adx_threshold and (not slope_filter or ema_slope_up)
    bear = closes[i] < ema_value and minus > plus and adx_value > adx_threshold and (not slope_filter or ema_slope_down)
    state = "BULL" if bull else "BEAR" if bear else "WAIT"
    distance_pct = (closes[i] - ema_value) / ema_value * 100 if ema_value else 0.0

    return TrendState(
        state=state,
        close=closes[i],
        ema=ema_value,
        plus_di=plus,
        minus_di=minus,
        adx=adx_value,
        distance_to_ema_pct=distance_pct,
    )


def score_alignment(day: TrendState | None, week: TrendState | None) -> str:
    if day is None or week is None:
        return "NO DATA"
    if day.state == "BULL" and week.state == "BULL":
        return "BULL ALIGN"
    if day.state == "BEAR" and week.state == "BEAR":
        return "BEAR ALIGN"
    return "MIXED"


def scan(args: argparse.Namespace) -> list[dict[str, object]]:
    plans = alignment_plan_state.load(args.plan_state)
    cache_dir = Path(args.cache_dir)
    markets = {m.strip().upper() for m in args.markets.split(",") if m.strip()}
    args.market_context = fetch_market_context(args)
    universe = load_stock_universe(markets)
    if args.symbols:
        wanted = {s.strip() for s in args.symbols.split(",") if s.strip()}
        universe = [s for s in universe if s.code in wanted or s.yahoo_symbol in wanted]
    if args.limit:
        universe = universe[: args.limit]
    if args.use_realtime_quote:
        universe = apply_realtime_quotes(universe)

    rows: list[dict[str, object]] = []
    total = len(universe)
    for idx, stock in enumerate(universe, start=1):
        try:
            daily = apply_current_quote(fetch_history(stock, args.years, cache_dir, args.cache_hours, args.sleep), stock.current_quote)
            day = calc_trend(daily, args.ema_length, args.dmi_length, args.adx_smoothing, args.adx_threshold, args.ema_slope)
            week = calc_trend(weekly_bars(daily), args.ema_length, args.dmi_length, args.adx_smoothing, args.adx_threshold, args.ema_slope)
            align = score_alignment(day, week)
            row = row_for(stock, day, week, align, daily, args)
            if daily:
                row = alignment_plan_state.update(plans, stock.yahoo_symbol, row, daily, min_stop_risk_pct=args.min_stop_risk_pct)
            row["tp_status"] = presentation_status(row, args.far_entry_pct, args.max_stop_risk_pct)
            row["entry_gap"] = entry_gap_value(row)
            row["list_state"] = list_state_value(row)
            row["entry_eligibility"] = entry_eligibility_value(row)
            if not include_in_main_list(row, args.only_align, args.only_bull):
                continue
            rows.append(row)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as exc:
            if not (args.only_align or args.only_bull):
                rows.append(error_row(stock, str(exc)))
        if args.verbose:
            print(f"[{idx}/{total}] {stock.market}:{stock.code} {stock.name}", file=sys.stderr)
    rows.sort(key=sort_key)
    alignment_plan_state.save(args.plan_state, plans)
    return rows


def entry_plan(
    daily: list[Bar],
    align: str,
    buy_ema_length: int,
    stop_lookback: int,
    stop_ema_length: int,
    stop_atr_length: int,
    stop_atr_mult: float,
    stop_buffer_pct: float,
    market_context: MarketContext | None,
    night_neutral_entry_atr: float,
    night_bear_entry_atr: float,
    tp_ratio_1: float,
    tp_ratio_2: float,
    tp_ratio_3: float,
    tp_resistance_lookback: int,
    tp_min_gap_pct: float,
    tp_min_resistance_r: float,
    tp_min_step_r: float,
    min_stop_risk_pct: float,
    max_stop_risk_pct: float,
) -> tuple[object, object, object, object, object, object, object]:
    min_bars = max(buy_ema_length, stop_ema_length, stop_atr_length) + 1
    if align != "BULL ALIGN" or len(daily) < min_bars:
        return "", "", "", "", "", "", ""
    closes = [b.close for b in daily]
    buy_ema_values = ema(closes, buy_ema_length)
    stop_ema_values = ema(closes, stop_ema_length)
    buy_ema = buy_ema_values[-1]
    stop_ema = stop_ema_values[-1]
    stop_atr = atr(daily, stop_atr_length)[-1]
    if buy_ema is None or stop_ema is None or stop_atr is None:
        return "", "", "", "", "", "", ""

    warning_line = floor_stock_tick(buy_ema)
    suggested_buy = buy_ema
    if market_context is not None:
        if market_context.bias == "BEAR":
            suggested_buy = buy_ema - stop_atr * night_bear_entry_atr
        elif market_context.bias == "NEUTRAL":
            suggested_buy = buy_ema - stop_atr * night_neutral_entry_atr

    suggested_buy = floor_stock_tick(suggested_buy)
    stop_loss = (stop_ema - stop_atr * stop_atr_mult) * (1 - stop_buffer_pct * 0.01)
    if suggested_buy <= stop_loss:
        suggested_buy = floor_stock_tick(buy_ema)

    stop_loss = floor_stock_tick(stop_loss)
    minimum_risk_stop = floor_stock_tick(suggested_buy * (1 - min_stop_risk_pct * 0.01))
    if (suggested_buy - stop_loss) / suggested_buy * 100 < min_stop_risk_pct:
        stop_loss = minimum_risk_stop

    risk = suggested_buy - stop_loss
    if risk <= 0:
        return suggested_buy, warning_line, stop_loss, "", "", "", ""

    risk_pct = risk / suggested_buy * 100
    tp_status = 'RISK TOO WIDE / SKIP' if risk_pct > max_stop_risk_pct else 'WAIT BUY'

    risk_targets = [
        suggested_buy + risk * tp_ratio_1,
        suggested_buy + risk * tp_ratio_2,
        suggested_buy + risk * tp_ratio_3,
    ]
    resistances = resistance_levels(daily, suggested_buy, tp_resistance_lookback, tp_min_gap_pct)

    tp_values: list[float] = []
    last_target = suggested_buy
    for risk_target in risk_targets:
        min_structural_target = max(last_target + risk * tp_min_step_r, suggested_buy + risk * tp_min_resistance_r)
        structural_target = next((level for level in resistances if level > min_structural_target), None)
        tp = min(risk_target, structural_target) if structural_target is not None else risk_target
        if tp <= last_target:
            tp = risk_target
        tp = floor_stock_tick(tp)
        if tp <= last_target:
            tp = next_stock_tick(last_target)
        tp_values.append(tp)
        last_target = tp

    tp1, tp2, tp3 = tp_values
    return suggested_buy, warning_line, stop_loss, tp1, tp2, tp3, tp_status


def row_for(stock: Stock, day: TrendState | None, week: TrendState | None, align: str, daily: list[Bar] | None = None, args: argparse.Namespace | None = None) -> dict[str, object]:
    suggested_buy, warning_line, stop_loss, tp1, tp2, tp3, tp_status = ("", "", "", "", "", "", "")
    data_date = daily[-1].date.isoformat() if daily else ""
    current_price = round_value(stock.current_quote.close) if stock.current_quote is not None else round_value(state_value(day, "close"))
    if daily is not None and args is not None:
        suggested_buy, warning_line, stop_loss, tp1, tp2, tp3, tp_status = entry_plan(
            daily,
            align,
            args.buy_ema_length,
            args.stop_lookback,
            args.stop_ema_length,
            args.stop_atr_length,
            args.stop_atr_mult,
            args.stop_buffer_pct,
            args.market_context,
            args.night_neutral_entry_atr,
            args.night_bear_entry_atr,
            args.tp_ratio_1,
            args.tp_ratio_2,
            args.tp_ratio_3,
            args.tp_resistance_lookback,
            args.tp_min_gap_pct,
            args.tp_min_resistance_r,
            args.tp_min_step_r,
            args.min_stop_risk_pct,
            args.max_stop_risk_pct,
        )
    return {
        "code": stock.code,
        "name": stock.name,
        "market": stock.market,
        "yahoo_symbol": stock.yahoo_symbol,
        "data_date": data_date,
        "current_price": current_price,
        "entry_gap": "",
        "alignment": align,
        "list_state": "",
        "entry_eligibility": "",
        "day_state": state_value(day, "state"),
        "week_state": state_value(week, "state"),
        "day_close": round_value(state_value(day, "close")),
        "week_close": round_value(state_value(week, "close")),
        "day_adx": round_value(state_value(day, "adx")),
        "week_adx": round_value(state_value(week, "adx")),
        "day_plus_di": round_value(state_value(day, "plus_di")),
        "day_minus_di": round_value(state_value(day, "minus_di")),
        "week_plus_di": round_value(state_value(week, "plus_di")),
        "week_minus_di": round_value(state_value(week, "minus_di")),
        "day_dist_ema_pct": round_value(state_value(day, "distance_to_ema_pct")),
        "week_dist_ema_pct": round_value(state_value(week, "distance_to_ema_pct")),
        "suggested_buy": suggested_buy,
        "warning_line": warning_line,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "tp_status": tp_status,
        "error": "",
    }


def error_row(stock: Stock, error: str) -> dict[str, object]:
    return {
        "code": stock.code,
        "name": stock.name,
        "market": stock.market,
        "yahoo_symbol": stock.yahoo_symbol,
        "data_date": "",
        "current_price": "",
        "entry_gap": "",
        "alignment": "ERROR",
        "list_state": "",
        "entry_eligibility": "",
        "day_state": "",
        "week_state": "",
        "day_close": "",
        "week_close": "",
        "day_adx": "",
        "week_adx": "",
        "day_plus_di": "",
        "day_minus_di": "",
        "week_plus_di": "",
        "week_minus_di": "",
        "day_dist_ema_pct": "",
        "week_dist_ema_pct": "",
        "suggested_buy": "",
        "warning_line": "",
        "stop_loss": "",
        "tp1": "",
        "tp2": "",
        "tp3": "",
        "tp_status": "",
        "error": error[:180],
    }


def state_value(state: TrendState | None, field: str) -> object:
    if state is None:
        return ""
    return getattr(state, field)


def round_value(value: object) -> object:
    if isinstance(value, float) and math.isfinite(value):
        return round(value, 2)
    return value


def entry_gap_value(row: dict[str, object]) -> object:
    if not is_active_entry_status(row.get("tp_status")):
        return ""
    try:
        gap = float(row["current_price"]) - float(row["suggested_buy"])
    except (TypeError, ValueError):
        return ""
    return round_value(gap)


def presentation_status(row: dict[str, object], far_entry_pct: float, max_stop_risk_pct: float) -> str:
    status = str(row.get("tp_status", ""))
    try:
        current = float(row["current_price"])
        entry = float(row["suggested_buy"])
        stop = float(row["stop_loss"])
    except (TypeError, ValueError):
        return status
    if entry <= 0:
        return status
    risk_wide = (entry - stop) / entry * 100 > max_stop_risk_pct
    if status == "WAIT BUY" and risk_wide:
        return "RISK TOO WIDE / SKIP"
    if status == "WAIT BUY" and (current - entry) / entry * 100 > far_entry_pct:
        return "TOO FAR / NO CHASE"
    if is_active_entry_status(status) and risk_wide and "RISK WIDE" not in status:
        return f"{status} / RISK WIDE"
    return status


def sort_key(row: dict[str, object]) -> tuple[int, str, str]:
    list_rank = {
        "QUALIFIED": 0,
        "TRACKING": 1,
        "WATCH - DAY LOST": 2,
        "WATCH - WEEK LOST": 2,
        "WATCH - WAIT RESET": 2,
        "BEAR ALIGN": 3,
        "ARCHIVED": 9,
    }
    rank = {"BULL ALIGN": 0, "BEAR ALIGN": 1, "MIXED": 2, "NO DATA": 3, "ERROR": 4}
    market_rank = {"TWSE": 0, "TPEX": 1}
    primary = list_rank.get(str(row.get("list_state", "")), 8) * 10 + rank.get(str(row["alignment"]), 9)
    return (primary, str(market_rank.get(str(row["market"]), 9)), str(row["code"]))


def qualified_row_issues(rows: list[dict[str, object]]) -> list[str]:
    issues: list[str] = []
    for row in rows:
        if row.get("list_state") != "QUALIFIED":
            continue
        valid = (
            row.get("alignment") == "BULL ALIGN"
            and row.get("day_state") == "BULL"
            and row.get("week_state") == "BULL"
            and row.get("tp_status") == "WAIT BUY"
            and bool(row.get("data_date"))
        )
        if not valid:
            issues.append(f"{row.get('code', '?')}:{row.get('name', '')}")
    return issues


def entry_eligibility_issues(rows: list[dict[str, object]]) -> list[str]:
    issues: list[str] = []
    for row in rows:
        eligibility = row.get("entry_eligibility")
        if eligibility == "NEW ENTRY" and row.get("list_state") != "QUALIFIED":
            issues.append(f"{row.get('code', '?')}:invalid-new-entry")
        if eligibility == "RETEST ENTRY":
            try:
                price_ok = float(row["stop_loss"]) < float(row["current_price"]) < float(row["tp1"])
            except (TypeError, ValueError):
                price_ok = False
            valid = (
                row.get("list_state") == "TRACKING"
                and row.get("alignment") == "BULL ALIGN"
                and row.get("day_state") == "BULL"
                and row.get("week_state") == "BULL"
                and str(row.get("tp_status", "")).split(" / RISK WIDE", 1)[0] == "ENTRY TOUCHED"
                and "RISK WIDE" not in str(row.get("tp_status", ""))
                and price_ok
            )
            if not valid:
                issues.append(f"{row.get('code', '?')}:invalid-retest-entry")
    return issues


def write_csv(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict[str, object]], path: Path, args: argparse.Namespace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    css = """
body{font-family:Segoe UI,Arial,sans-serif;background:#111827;color:#e5e7eb;margin:24px}
h1{font-size:22px;margin:0 0 8px}.meta{color:#9ca3af;margin-bottom:18px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:7px 9px;border-bottom:1px solid #243244;text-align:right}
th{position:sticky;top:0;background:#1f2937;color:#f9fafb}td:nth-child(1),td:nth-child(2),td:nth-child(3),td:nth-child(4),th:nth-child(1),th:nth-child(2),th:nth-child(3),th:nth-child(4){text-align:left}
.bull{color:#22c55e;font-weight:700}.bear{color:#fb7185;font-weight:700}.mixed{color:#9ca3af}.error{color:#f59e0b}
.watch{color:#fbbf24}.tracking{color:#38bdf8}.watch td{background:rgba(251,191,36,.045)}
.entry-ready{color:#22c55e;font-weight:700}.entry-ready td{background:rgba(34,197,94,.065)}
.live-up{color:#22c55e}.live-down{color:#fb7185}.live-flat{color:#e5e7eb}.live-stale{color:#f59e0b}
.entry-gap-positive{color:#22c55e}.entry-gap-negative{color:#fb7185}.entry-gap-flat{color:#e5e7eb}
"""
    live_quotes = getattr(args, "live_quotes", True)
    script = """
<script>
const refreshMs = 30000;
const priceCells = new Map();

function chunk(items, size) {
  const out = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

function marketPrefix(market) {
  return market === "TWSE" ? "tse" : "otc";
}

function formatPrice(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  return n >= 1000 ? n.toFixed(0) : n.toFixed(2).replace(/\\.00$/, "");
}

function formatEntryGap(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  const digits = Math.abs(n) >= 1000 ? 0 : 2;
  return `${n > 0 ? "+" : ""}${n.toFixed(digits)}`;
}

function updateEntryGap(row, price) {
  if (!row || row.dataset.entryActive !== "1") return;
  const gapCell = row.querySelector("[data-field='entry_gap']");
  const entryCell = row.querySelector("[data-field='suggested_buy']");
  const entry = Number(entryCell?.textContent);
  if (!gapCell || !Number.isFinite(entry)) return;
  const gap = price - entry;
  gapCell.textContent = formatEntryGap(gap);
  gapCell.classList.remove("entry-gap-positive", "entry-gap-negative", "entry-gap-flat");
  gapCell.classList.add(gap > 0 ? "entry-gap-positive" : gap < 0 ? "entry-gap-negative" : "entry-gap-flat");
}

function updatePendingStatus(row, price) {
  if (!row || row.dataset.baseStatus !== "WAIT BUY") return;
  const statusCell = row.querySelector("[data-field='tp_status']");
  const entryCell = row.querySelector("[data-field='suggested_buy']");
  const entry = Number(entryCell?.textContent);
  const threshold = Number(row.dataset.farEntryPct);
  if (!statusCell || !Number.isFinite(entry) || entry <= 0 || !Number.isFinite(threshold)) return;
  const tooFar = (price - entry) / entry * 100 > threshold;
  statusCell.textContent = tooFar ? "TOO FAR / NO CHASE" : "WAIT BUY";
  statusCell.title = tooFar ? `現價高於參考買進價超過 ${threshold}%，不追價` : "計畫建立後尚未確認觸價";
}

function quoteEndpoint(params) {
  if (window.location.hostname.endsWith("chatgpt.site")) {
    return `/api/quotes?${params}`;
  }
  return `https://mis.twse.com.tw/stock/api/getStockInfo.jsp?${params}`;
}

function setLiveMeta(text, stale = false) {
  const el = document.getElementById("live-meta");
  if (!el) return;
  el.textContent = text;
  el.className = stale ? "meta live-stale" : "meta";
}

async function refreshCurrentPrices() {
  if (refreshCurrentPrices.busy) return;
  refreshCurrentPrices.busy = true;
  const rows = [...document.querySelectorAll("tbody tr[data-code][data-market]")];
  const channels = rows.map(row => `${marketPrefix(row.dataset.market)}_${row.dataset.code}.tw`);
  rows.forEach(row => priceCells.set(row.dataset.code, row.querySelector("[data-field='current_price']")));

  try {
    let updated = 0;
    rows.forEach(row => {
      const cell = row.querySelector("[data-field='current_price']");
      if (cell) { cell.classList.add('live-stale'); cell.title = 'Quote not refreshed'; }
    });
    for (const group of chunk(channels, 80)) {
      const params = new URLSearchParams({
        ex_ch: group.join("|"),
        json: "1",
        delay: "0",
        _: String(Date.now())
      });
      const response = await fetch(quoteEndpoint(params), {
        headers: { "Accept": "application/json,text/plain,*/*" },
        cache: "no-store"
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      for (const quote of data.msgArray || []) {
        const code = quote.c;
        const cell = priceCells.get(code);
        const price = Number(quote.z || quote.y);
        const prevClose = Number(quote.y);
        if (!cell || !Number.isFinite(price)) continue;
        cell.textContent = formatPrice(price);
        updateEntryGap(cell.closest("tr"), price);
        updatePendingStatus(cell.closest("tr"), price);
        const stamp = Number(quote.quote_time);
        const quoteDate = stamp > 0 ? new Date(stamp * 1000) : null;
        cell.title = quoteDate ? `行情時間: ${quoteDate.toLocaleString('zh-TW', {timeZone: 'Asia/Taipei', hour12: false})} | Yahoo` : '行情時間未知';
        cell.classList.toggle('live-stale', !quoteDate || Date.now() - stamp * 1000 > 900000);
        cell.classList.remove("live-up", "live-down", "live-flat");
        cell.classList.add(price > prevClose ? "live-up" : price < prevClose ? "live-down" : "live-flat");
        updated += 1;
      }
    }
    setLiveMeta(`報價查詢時間: ${new Date().toLocaleTimeString("zh-TW", { hour12: false })} | ${updated}/${rows.length} 檔；滑鼠移至價格查看行情時間，橘色表示逾15分鐘或時間未知。`, updated < rows.length);
  } catch (error) {
    setLiveMeta(`Current price unavailable: ${new Date().toLocaleTimeString("zh-TW", { hour12: false })}`, true);
  } finally {
    refreshCurrentPrices.busy = false;
  }
}

window.addEventListener("DOMContentLoaded", () => {
  refreshCurrentPrices();
  setInterval(refreshCurrentPrices, refreshMs);
});
</script>
""" if live_quotes else ""
    headers = OUTPUT_FIELDS
    market = getattr(args, "market_context", None)
    if market is None:
        market_text = "TAIFEX night: unavailable"
    else:
        market_text = (
            f"TAIFEX night: {market.contract} {market.contract_month} "
            f"{market.bias} | Last {round_value(market.last)} | "
            f"Change {round_value(market.change)} ({round_value(market.pct)}%)"
        )
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>TW Day Week Alignment Scanner</title>",
        f"<style>{css}</style></head><body>",
        "<h1>TW Day Week Alignment Scanner</h1>",
        f"<div class='meta'>Generated: {html.escape(generated)} | Rows: {len(rows)}</div>",
        f"<div class='meta'>{html.escape(market_text)}</div>",
        "<div id='live-meta' class='meta'>Current price loading...</div>" if live_quotes else "<div class='meta'>Current price: scan snapshot</div>",
        "<table><thead><tr>",
    ]
    parts.extend(f"<th>{html.escape(h)}</th>" for h in headers)
    parts.append("</tr></thead><tbody>")
    for row in rows:
        align = str(row["alignment"])
        list_state = str(row.get("list_state", ""))
        entry_eligibility = str(row.get("entry_eligibility", ""))
        cls = "entry-ready" if align == "BULL ALIGN" else "watch" if list_state.startswith("WATCH") else "tracking" if list_state == "TRACKING" else "bear" if align == "BEAR ALIGN" else "error" if align == "ERROR" else "mixed"
        code = html.escape(str(row.get("code", "")))
        market_code = html.escape(str(row.get("market", "")))
        entry_active = "1" if is_active_entry_status(row.get("tp_status")) else "0"
        base_status = "WAIT BUY" if row.get("tp_status") in {"WAIT BUY", "TOO FAR / NO CHASE"} else str(row.get("tp_status", ""))
        parts.append(f"<tr class='{cls}' data-code='{code}' data-market='{market_code}' data-entry-active='{entry_active}' data-base-status='{html.escape(base_status)}' data-far-entry-pct='{args.far_entry_pct}'>")
        for h in headers:
            descriptions = {'ORDER UNKNOWN': '日線無法確定觸價先後，需核對分時資料',
                            'WAIT BUY': '計畫建立後尚未確認觸價',
                            'TOO FAR / NO CHASE': '現價高於參考買進價超過設定門檻，不追價',
                            'RISK TOO WIDE / SKIP': '停損距離超過風險上限，本次略過',
                            'ENTRY TOUCHED / RISK WIDE': '已觸及進場，但原計畫停損風險超過15%，應降低部位並主動管理',
                            'ENTRY TOUCHED': '計畫價位已觸及，非券商成交紀錄',
                            'STOP HIT / WAIT RESET': '原計畫已觸及停損，等待條件重設'}
            raw_value = row.get(h, '')
            value = f"{float(raw_value):+.2f}" if h == 'entry_gap' and raw_value != '' else str(raw_value)
            title = html.escape(descriptions.get(value, ''), quote=True) if h == 'tp_status' else ''
            parts.append(f"<td data-field='{html.escape(h)}' title='{title}'>{html.escape(value)}</td>")
        parts.append("</tr>")
    parts.append(f"</tbody></table>{script}</body></html>")
    path.write_text("\n".join(parts), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan Taiwan stocks for daily/weekly DMI ADX EMA alignment.")
    parser.add_argument("--markets", default="TWSE,TPEX", help="Comma-separated markets: TWSE,TPEX")
    parser.add_argument("--symbols", default="", help="Optional comma-separated stock codes, e.g. 2330,2881")
    parser.add_argument("--limit", type=int, default=0, help="Limit symbols for quick tests.")
    parser.add_argument("--years", type=int, default=6, help="Years of daily history to request.")
    parser.add_argument("--cache-dir", default="data/tw_stock_cache", help="Yahoo history cache directory.")
    parser.add_argument("--plan-state", default=str(Path(__file__).resolve().parent / 'data' / 'alignment_plans.json'))
    parser.add_argument("--cache-hours", type=float, default=18, help="Reuse cached history younger than this many hours.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Delay between uncached Yahoo requests.")
    parser.add_argument("--no-realtime-quote", dest="use_realtime_quote", action="store_false", help="Disable TWSE MIS realtime quote overlay.")
    parser.set_defaults(use_realtime_quote=True)
    parser.add_argument("--live-quotes", action=argparse.BooleanOptionalAction, default=True, help="Refresh current prices in the browser when the hosting platform supports it.")
    parser.add_argument("--ema-length", type=int, default=200)
    parser.add_argument("--dmi-length", type=int, default=14)
    parser.add_argument("--adx-smoothing", type=int, default=14)
    parser.add_argument("--adx-threshold", type=float, default=30.0)
    parser.add_argument("--ema-slope", action="store_true", help="Require EMA slope to agree with trend direction.")
    parser.add_argument("--buy-ema-length", type=int, default=20, help="Bull alignment suggested buy reference EMA length.")
    parser.add_argument("--stop-lookback", type=int, default=10, help="Bull alignment stop-loss recent low lookback.")
    parser.add_argument("--stop-ema-length", type=int, default=50, help="EMA length for stop-loss invalidation line.")
    parser.add_argument("--stop-atr-length", type=int, default=14, help="ATR length for tighter stop-loss reference.")
    parser.add_argument("--stop-atr-mult", type=float, default=0.5, help="ATR multiplier below stop EMA reference.")
    parser.add_argument("--stop-buffer-pct", type=float, default=0.0, help="Percent buffer below the selected stop-loss reference.")
    parser.add_argument("--min-stop-risk-pct", type=float, default=3.0, help="Minimum entry-to-stop risk percent.")
    parser.add_argument("--max-stop-risk-pct", type=float, default=15.0, help="Maximum acceptable entry-to-stop risk percent.")
    parser.add_argument("--far-entry-pct", type=float, default=15.0, help="Mark an untouched order as no-chase above this distance from entry.")
    parser.add_argument("--use-taifex-night", action=argparse.BooleanOptionalAction, default=True, help="Use latest TAIFEX after-hours index futures as market context.")
    parser.add_argument("--market-contract", default="TX", help="TAIFEX futures contract for market context, e.g. TX, MTX, TMF.")
    parser.add_argument("--night-bull-pct", type=float, default=0.3, help="Night-session percent change at or above this is bullish.")
    parser.add_argument("--night-bear-pct", type=float, default=0.3, help="Night-session percent change at or below negative this is bearish.")
    parser.add_argument("--night-neutral-entry-atr", type=float, default=0.2, help="Lower bull-aligned suggested buy by this ATR amount when night bias is neutral.")
    parser.add_argument("--night-bear-entry-atr", type=float, default=0.5, help="Lower bull-aligned suggested buy by this ATR amount when night bias is bearish.")
    parser.add_argument("--tp-ratio-1", type=float, default=1.0, help="First take-profit R multiple.")
    parser.add_argument("--tp-ratio-2", type=float, default=2.0, help="Second take-profit R multiple.")
    parser.add_argument("--tp-ratio-3", type=float, default=3.0, help="Third take-profit R multiple.")
    parser.add_argument("--tp-resistance-lookback", type=int, default=120, help="Daily bars to scan for structural resistance targets.")
    parser.add_argument("--tp-min-gap-pct", type=float, default=1.0, help="Minimum percent above suggested buy to count as resistance.")
    parser.add_argument("--tp-min-resistance-r", type=float, default=0.8, help="Ignore structural resistance closer than this R multiple.")
    parser.add_argument("--tp-min-step-r", type=float, default=0.5, help="Minimum R distance between take-profit levels.")
    parser.add_argument("--only-align", action="store_true", help="Only output bull/bear aligned stocks.")
    parser.add_argument("--only-bull", action="store_true", help="Only output bull aligned stocks.")
    parser.add_argument("--csv", default="output/tw_day_week_alignment.csv")
    parser.add_argument("--html", default="output/tw_day_week_alignment.html")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    rows = scan(args)
    qualified_issues = qualified_row_issues(rows)
    if qualified_issues:
        raise RuntimeError("Invalid QUALIFIED rows: " + ", ".join(qualified_issues[:20]))
    eligibility_issues = entry_eligibility_issues(rows)
    if eligibility_issues:
        raise RuntimeError("Invalid entry eligibility rows: " + ", ".join(eligibility_issues[:20]))
    write_csv(rows, Path(args.csv))
    write_html(rows, Path(args.html), args)

    bull = sum(1 for r in rows if r["alignment"] == "BULL ALIGN")
    bear = sum(1 for r in rows if r["alignment"] == "BEAR ALIGN")
    mixed = sum(1 for r in rows if r["alignment"] == "MIXED")
    errors = sum(1 for r in rows if r["alignment"] == "ERROR")
    print(f"Rows: {len(rows)} | Bull align: {bull} | Bear align: {bear} | Mixed: {mixed} | Errors: {errors}")
    print(f"CSV: {args.csv}")
    print(f"HTML: {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
