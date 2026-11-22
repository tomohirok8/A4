"""銘柄の財務情報取得 (yfinance / Yahoo Finance).

投資判断の材料になる指標を、画面表示しやすい形 (dict) にまとめて返す。
金額はその企業の決算通貨、比率は % 表記の数値。
"""
import math
from typing import Optional
import warnings
from datetime import datetime

from prices import to_ticker
from statements import StatementError, edinet_annual, sec_annual

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except Exception:  # noqa: BLE001
    yf = None

SECTOR_JA = {
    "Technology": "情報技術",
    "Consumer Cyclical": "一般消費財",
    "Consumer Defensive": "生活必需品",
    "Communication Services": "通信サービス",
    "Healthcare": "ヘルスケア",
    "Financial Services": "金融",
    "Industrials": "資本財・産業",
    "Basic Materials": "素材",
    "Energy": "エネルギー",
    "Utilities": "公益",
    "Real Estate": "不動産",
}

RECOMMEND_JA = {
    "strong_buy": "強い買い",
    "buy": "買い",
    "hold": "中立",
    "underperform": "弱気",
    "sell": "売り",
    "strong_sell": "強い売り",
}

# 年次推移で使う決算書の行（上にあるものを優先して使う）
STATEMENT_ROWS = {
    "revenue": ("income_stmt", ["Total Revenue", "Operating Revenue"]),
    "operating_income": ("income_stmt", ["Operating Income", "EBIT"]),
    "net_income": ("income_stmt", ["Net Income", "Net Income Common Stockholders"]),
    "eps": ("income_stmt", ["Diluted EPS", "Basic EPS"]),
    "total_assets": ("balance_sheet", ["Total Assets"]),
    "equity": ("balance_sheet", ["Stockholders Equity", "Common Stock Equity"]),
    "total_debt": ("balance_sheet", ["Total Debt"]),
    "operating_cf": ("cashflow", ["Operating Cash Flow"]),
    "free_cf": ("cashflow", ["Free Cash Flow"]),
}


def _num(v):
    """NaN / 非数値を None にする."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _pct(v):
    """0.12 -> 12.0（%）."""
    v = _num(v)
    return v * 100 if v is not None else None


def _div(a, b):
    a, b = _num(a), _num(b)
    return a / b if a is not None and b else None


def _annual(ticker) -> list:
    """直近の年次決算（古い順）."""
    frames = {}
    for attr in {a for a, _ in STATEMENT_ROWS.values()}:
        try:
            df = getattr(ticker, attr)
            frames[attr] = df if df is not None and not df.empty else None
        except Exception:  # noqa: BLE001
            frames[attr] = None

    periods = set()
    for df in frames.values():
        if df is not None:
            periods.update(df.columns)
    years = []
    for col in sorted(periods):
        row = {"period": col.strftime("%Y-%m")}
        for key, (attr, names) in STATEMENT_ROWS.items():
            df = frames.get(attr)
            val = None
            if df is not None and col in df.columns:
                for n in names:
                    if n in df.index:
                        val = _num(df.at[n, col])
                        if val is not None:
                            break
            row[key] = val
        row["operating_margin"] = _pct(_div(row["operating_income"], row["revenue"]))
        row["equity_ratio"] = _pct(_div(row["equity"], row["total_assets"]))
        row["roe"] = _pct(_div(row["net_income"], row["equity"]))
        # 決算書のデータが一つもない期（取得範囲外の古い期など）は除く
        if any(row[k] is not None for k in ("revenue", "net_income", "operating_cf")):
            years.append(row)
    return years[-5:]


def _dividends_by_year(ticker) -> list:
    """暦年ごとの1株配当合計（直近6年、古い順）。今年分は途中までの合計."""
    try:
        s = ticker.dividends
    except Exception:  # noqa: BLE001
        return []
    if s is None or s.empty:
        return []
    by_year = s.groupby(s.index.year).sum()
    this_year = datetime.now().year
    out = [{"year": int(y), "amount": _num(v), "partial": int(y) == this_year}
           for y, v in by_year.items()]
    return out[-6:]


def _long_term_annual(symbol: str, info: dict, yahoo_rows: list, settings: dict):
    """SEC / EDINET から5年分を取得し、欠けている項目を Yahoo の同じ期の値で補う.

    戻り値: (rows, source, note)。取得できなければ Yahoo の行と理由を返す。
    """
    ticker = to_ticker(symbol)
    try:
        if ticker.endswith(".T"):
            fy = _num(info.get("lastFiscalYearEnd"))
            if not fy:
                raise StatementError("決算期末が分からないため EDINET を検索できません")
            rows = edinet_annual(symbol, datetime.fromtimestamp(fy).date(), settings.get("edinet_api_key", ""))
            source = "EDINET（有価証券報告書）"
        else:
            rows = sec_annual(ticker, settings.get("sec_contact", ""))
            source = "SEC EDGAR（10-K）"
    except StatementError as e:
        return yahoo_rows, "Yahoo Finance", f"5年分を取得できなかったため Yahoo Finance の{len(yahoo_rows)}年分を表示（{e}）"
    except Exception as e:  # noqa: BLE001  想定外の形式などでも画面は出す
        return yahoo_rows, "Yahoo Finance", f"5年分を取得できなかったため Yahoo Finance の{len(yahoo_rows)}年分を表示（{type(e).__name__}: {e}）"

    yahoo_by_period = {r["period"]: r for r in yahoo_rows}
    filled = False
    for r in rows:
        y = yahoo_by_period.get(r["period"])
        if not y:
            continue
        for k, v in y.items():
            if r.get(k) is None and v is not None:
                r[k] = v
                filled = True
    note = "一部の項目は Yahoo Finance の値で補完" if filled else None
    return rows, source, note


def fetch_fundamentals(symbol: str, settings: Optional[dict] = None) -> dict:
    if yf is None:
        raise ValueError("yfinance を読み込めないため財務情報を取得できません")
    t = yf.Ticker(to_ticker(symbol))
    try:
        info = t.info or {}
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{symbol}: 財務情報を取得できませんでした（{e}）") from e
    if not info or (info.get("quoteType") is None and info.get("longName") is None
                    and info.get("shortName") is None):
        raise ValueError(f"{symbol}: 財務情報が見つかりません")

    price = _num(info.get("currentPrice") or info.get("regularMarketPrice"))
    dividend_rate = _num(info.get("dividendRate"))
    target = _num(info.get("targetMeanPrice"))
    sector = info.get("sector")

    result = {
        "symbol": symbol.upper(),
        "ticker": to_ticker(symbol),
        "name": info.get("longName") or info.get("shortName") or symbol,
        "sector": SECTOR_JA.get(sector, sector),
        "industry": info.get("industry"),
        "currency": info.get("currency"),
        "financial_currency": info.get("financialCurrency") or info.get("currency"),
        "employees": _num(info.get("fullTimeEmployees")),
        "summary": info.get("longBusinessSummary"),
        "price": price,
        "market_cap": _num(info.get("marketCap")),
        "high_52w": _num(info.get("fiftyTwoWeekHigh")),
        "low_52w": _num(info.get("fiftyTwoWeekLow")),
        "beta": _num(info.get("beta")),
        "valuation": {
            "per": _num(info.get("trailingPE")),
            "forward_per": _num(info.get("forwardPE")),
            "pbr": _num(info.get("priceToBook")),
            "ev_ebitda": _num(info.get("enterpriseToEbitda")),
            "eps": _num(info.get("trailingEps")),
            "forward_eps": _num(info.get("forwardEps")),
            "bps": _num(info.get("bookValue")),
        },
        "dividend": {
            "rate": dividend_rate,
            "yield": _pct(_div(dividend_rate, price)),
            "payout_ratio": _pct(info.get("payoutRatio")),
            "ex_date": (datetime.fromtimestamp(info["exDividendDate"]).strftime("%Y-%m-%d")
                        if _num(info.get("exDividendDate")) else None),
            "history": _dividends_by_year(t),
        },
        "profitability": {
            "roe": _pct(info.get("returnOnEquity")),
            "roa": _pct(info.get("returnOnAssets")),
            "operating_margin": _pct(info.get("operatingMargins")),
            "profit_margin": _pct(info.get("profitMargins")),
            "revenue_growth": _pct(info.get("revenueGrowth")),
            "earnings_growth": _pct(info.get("earningsGrowth")),
        },
        "safety": {
            "debt_to_equity": _num(info.get("debtToEquity")),  # %
            "current_ratio": _num(info.get("currentRatio")),
            "total_cash": _num(info.get("totalCash")),
            "total_debt": _num(info.get("totalDebt")),
            "free_cashflow": _num(info.get("freeCashflow")),
        },
        "analyst": {
            "target_mean": target,
            "target_high": _num(info.get("targetHighPrice")),
            "target_low": _num(info.get("targetLowPrice")),
            "upside": _pct(_div(target - price, price)) if target and price else None,
            "recommendation": RECOMMEND_JA.get(info.get("recommendationKey"), info.get("recommendationKey")),
            "analysts": _num(info.get("numberOfAnalystOpinions")),
        },
    }
    annual, source, note = _long_term_annual(symbol, info, _annual(t), settings or {})
    result.update({"annual": annual, "annual_source": source, "annual_note": note})
    return result
