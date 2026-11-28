"""株価取得.

1. yfinance ライブラリ（主）
2. Yahoo Finance のチャート API を直接呼び出す（予備）
   yfinance が Yahoo の仕様変更に追従できず壊れた場合でも取得できるようにする。
"""
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import warnings

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except Exception:  # noqa: BLE001  yfinance が壊れていても予備で動かす
    yf = None

# 東証の銘柄コード (4桁数字、または 130A のような英数字4桁)
_JP_CODE = re.compile(r"^\d{3}[0-9A-Z]$")

_CHART_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept": "application/json"}
_TIMEOUT = 10


def to_ticker(symbol: str) -> str:
    """'7203' -> '7203.T' のように Yahoo Finance 用ティッカーに変換する."""
    s = symbol.strip().upper()
    if _JP_CODE.match(s):
        return s + ".T"
    return s


def _valid(price) -> bool:
    return isinstance(price, (int, float)) and price == price and price > 0


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def _from_yfinance(ticker: str):
    if yf is None:
        raise RuntimeError("yfinance を読み込めません")
    t = yf.Ticker(ticker)
    price = currency = None
    try:
        price = t.fast_info["last_price"]
        currency = t.fast_info["currency"]
    except Exception:  # noqa: BLE001
        pass
    if not _valid(price):
        hist = t.history(period="5d")
        if not hist.empty:
            price = float(hist["Close"].dropna().iloc[-1])
    if not _valid(price):
        raise RuntimeError("yfinance で株価が取得できません")
    return float(price), currency


def _from_yahoo_chart(ticker: str):
    query = urllib.parse.urlencode({"range": "5d", "interval": "1d"})
    path = f"/v8/finance/chart/{urllib.parse.quote(ticker)}?{query}"
    ctx = _ssl_context()
    last_err = None
    for host in _CHART_HOSTS:
        req = urllib.request.Request(f"https://{host}{path}", headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as res:
                data = json.load(res)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError("銘柄が見つかりません") from e
            last_err = e
            continue
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue

        result = (data.get("chart") or {}).get("result") or []
        if not result:
            raise RuntimeError("銘柄が見つかりません")
        meta = result[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if not _valid(price):
            closes = (((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
            closes = [c for c in closes if _valid(c)]
            price = closes[-1] if closes else None
        if _valid(price):
            return float(price), meta.get("currency")
        raise RuntimeError("価格データがありません")
    raise RuntimeError(f"Yahoo に接続できません ({last_err})")


SOURCES = (("yfinance", _from_yfinance), ("Yahoo直接", _from_yahoo_chart))


def default_currency(symbol: str) -> str:
    """取得前の通貨の推定: 東証コードなら JPY、それ以外は USD."""
    return "JPY" if _JP_CODE.match(symbol.strip().upper()) else "USD"


def _fetch(ticker: str, label: str):
    reasons = []
    for name, func in SOURCES:
        try:
            return func(ticker)
        except Exception as e:  # noqa: BLE001
            reasons.append(f"{name}: {e}")
    raise ValueError(f"{label}: 取得できませんでした（" + " / ".join(reasons) + "）")


def fetch_quote(symbol: str):
    """最新株価と通貨 (price, currency) を返す。全ての取得先で失敗したら ValueError."""
    price, currency = _fetch(to_ticker(symbol), symbol)
    return round(price, 4), (currency or default_currency(symbol)).upper()


def fetch_fx_rate(currency: str) -> float:
    """1 単位の外貨が何円かを返す（例: USD -> 159.8）."""
    currency = currency.upper()
    if currency == "JPY":
        return 1.0
    price, _ = _fetch(f"{currency}JPY=X", f"{currency}/JPY 為替レート")
    return round(price, 4)


def fetch_name(symbol: str) -> str:
    """銘柄名を返す。日本株は日本語名（Yahoo の検索 API、全角英数は半角に直す）。見つからなければ空文字."""
    import unicodedata

    ticker = to_ticker(symbol)
    query = urllib.parse.urlencode({"q": ticker, "lang": "ja-JP", "region": "JP",
                                    "quotesCount": 5, "newsCount": 0})
    ctx = _ssl_context()
    for host in _CHART_HOSTS:
        req = urllib.request.Request(f"https://{host}/v1/finance/search?{query}", headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as res:
                data = json.load(res)
        except Exception:  # noqa: BLE001
            continue
        for q in data.get("quotes") or []:
            if str(q.get("symbol", "")).upper() == ticker:
                name = q.get("longname") or q.get("shortname") or ""
                return unicodedata.normalize("NFKC", name).strip()
        break
    # 予備: チャート API のメタ情報（英語名）
    try:
        path = f"/v8/finance/chart/{urllib.parse.quote(ticker)}?range=1d&interval=1d"
        req = urllib.request.Request(f"https://{_CHART_HOSTS[0]}{path}", headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as res:
            meta = (json.load(res)["chart"]["result"] or [{}])[0].get("meta") or {}
        return (meta.get("longName") or meta.get("shortName") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
