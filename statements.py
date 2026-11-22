"""過去5年分の年次決算の取得.

- 米国株: SEC EDGAR の XBRL company facts API（無料・キー不要、連絡先の申告が必要）
- 日本株: EDINET API v2 の有価証券報告書「主要な経営指標等の推移」（無料・API キーが必要）

どちらも行の形式は fundamentals._annual() と同じ dict（古い順）で返す。
"""
import csv
import gzip
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

from db import connect, now_jst
from prices import _ssl_context

YEARS = 5


class StatementError(Exception):
    """取得できなかった理由（画面に表示する）."""


def _get(url: str, headers: dict, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", **headers})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as res:
        body = res.read()
        if res.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body


def _ratio(a, b):
    return a / b * 100 if a is not None and b else None


def _finish(row: dict) -> dict:
    """比率と FCF を計算して行を仕上げる."""
    row.setdefault("operating_margin", _ratio(row.get("operating_income"), row.get("revenue")))
    if row.get("equity_ratio") is None:
        row["equity_ratio"] = _ratio(row.get("equity"), row.get("total_assets"))
    if row.get("roe") is None:
        row["roe"] = _ratio(row.get("net_income"), row.get("equity"))
    return row


# ======================================================================
# SEC EDGAR（米国株）
# ======================================================================

SEC_CONCEPTS = {
    # 上にある概念を優先し、同じ決算期の値が無ければ次の概念で補う
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "total_assets": ["Assets"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "operating_cf": ["NetCashProvidedByUsedInOperatingActivities",
                     "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "dividend_per_share": ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"],
}
SEC_INSTANT = {"total_assets", "equity"}
_SEC_TICKERS_TTL = timedelta(days=30)


def _sec_headers(contact: str) -> dict:
    if not contact or "@" not in contact:
        raise StatementError("SEC の連絡先（名前とメールアドレス）が未設定です。設定画面で入力してください")
    return {"User-Agent": contact}


def _sec_cik(ticker: str, contact: str) -> int:
    """ティッカー → CIK。SEC の一覧を 30 日キャッシュする."""
    with connect() as conn:
        row = conn.execute("SELECT data, fetched_at FROM fundamentals WHERE symbol = '__SEC_TICKERS__'").fetchone()
    table = None
    if row and now_jst() - datetime.fromisoformat(row["fetched_at"]) < _SEC_TICKERS_TTL:
        table = json.loads(row["data"])
    if table is None:
        try:
            raw = json.loads(_get("https://www.sec.gov/files/company_tickers.json", _sec_headers(contact)))
        except urllib.error.HTTPError as e:
            raise StatementError(f"SEC の銘柄一覧を取得できません（HTTP {e.code}）") from e
        table = {v["ticker"].upper(): v["cik_str"] for v in raw.values()}
        with connect() as conn:
            conn.execute("INSERT OR REPLACE INTO fundamentals (symbol, data, fetched_at) VALUES (?, ?, ?)",
                         ("__SEC_TICKERS__", json.dumps(table), now_jst().isoformat()))
    t = ticker.upper()
    cik = table.get(t) or table.get(t.replace(".", "-")) or table.get(t.replace("-", "."))
    if not cik:
        raise StatementError(f"{ticker} は SEC の登録銘柄に見つかりません（米国上場の企業のみ対応）")
    return int(cik)


def _sec_annual_values(facts: dict, concept: str, instant: bool) -> dict:
    """{決算期末: 値}。10-K の通期（約1年）の値、同じ期は最後に提出されたもの."""
    node = facts.get(concept)
    if not node:
        return {}
    out = {}
    for unit, items in node.get("units", {}).items():
        if unit not in ("USD", "USD/shares"):
            continue
        for x in sorted(items, key=lambda x: x.get("filed", "")):
            if x.get("form") not in ("10-K", "10-K/A"):
                continue
            if not instant:
                if "start" not in x:
                    continue
                days = (date.fromisoformat(x["end"]) - date.fromisoformat(x["start"])).days
                if not 350 <= days <= 380:
                    continue
            out[x["end"]] = float(x["val"])
    return out


def sec_annual(ticker: str, contact: str) -> list:
    cik = _sec_cik(ticker, contact)
    try:
        data = json.loads(_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                               _sec_headers(contact), timeout=60))
    except urllib.error.HTTPError as e:
        raise StatementError(f"SEC の財務データを取得できません（HTTP {e.code}）") from e
    facts = data.get("facts", {}).get("us-gaap", {})
    if not facts:
        raise StatementError("SEC に US-GAAP の財務データがありません（外国企業は対象外の場合があります）")

    series = {}
    for key, concepts in SEC_CONCEPTS.items():
        merged = {}
        for c in concepts:
            for end, v in _sec_annual_values(facts, c, key in SEC_INSTANT).items():
                merged.setdefault(end, v)
        series[key] = merged

    ends = sorted(set(series["revenue"]) | set(series["net_income"]))
    # 決算期末が数日ずれる年 (52/53 週決算) に備え、売上か純利益のある期末だけを使う
    rows = []
    for end in ends[-YEARS:]:
        row = {"period": end[:7]}
        for key in SEC_CONCEPTS:
            row[key] = series[key].get(end)
        ocf, capex = row.get("operating_cf"), row.pop("capex", None)
        row["free_cf"] = ocf - capex if ocf is not None and capex is not None else None
        rows.append(_finish(row))
    if not rows:
        raise StatementError("SEC のデータに年次決算が見つかりません")
    return rows


# ======================================================================
# EDINET（日本株）
# ======================================================================

EDINET_BASE = "https://api.edinet-fsa.go.jp/api/v2"
ANNUAL_REPORT = ("120", "130")  # 有価証券報告書, 訂正有価証券報告書
SCAN_DAYS = 110                 # 決算期末から何日後まで提出を探すか（提出期限は原則3か月）

# 「主要な経営指標等の推移」の要素名（SummaryOfBusinessResults と IFRS/USGAAP を除いた部分）
EDINET_ELEMENTS = {
    "revenue": ["NetSales", "Revenue", "Revenues", "OperatingRevenue1", "OperatingRevenue2",
                "NetSalesOfCompletedConstructionContracts", "OrdinaryIncome", "GrossOperatingRevenue",
                "OperatingRevenue", "SalesAndFinancialServicesRevenue", "TotalRevenue", "NetRevenue"],
    "operating_income": ["OperatingIncomeLoss", "OperatingProfitLoss"],
    "ordinary_income": ["OrdinaryIncomeLoss", "ProfitLossBeforeTax", "IncomeLossBeforeIncomeTaxes"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "NetIncomeLossAttributableToOwnersOfParent",
                   "NetIncomeLoss", "ProfitLoss"],
    "total_assets": ["TotalAssets"],
    "equity": ["EquityAttributableToOwnersOfParent", "ShareholdersEquity", "NetAssets", "TotalEquity"],
    "eps": ["DilutedEarningsPerShare", "BasicEarningsLossPerShare", "BasicEarningsPerShare",
            "DilutedEarningsLossPerShare"],
    "equity_ratio": ["RatioOfOwnersEquityToGrossAssets", "EquityToAssetRatio",
                     "RatioOfEquityAttributableToOwnersOfParentToTotalAssets"],
    "roe": ["RateOfReturnOnEquity"],
    "operating_cf": ["NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"],
    "investing_cf": ["NetCashProvidedByUsedInInvestingActivities", "CashFlowsFromUsedInInvestingActivities"],
    "dividend_per_share": ["DividendPaidPerShare"],
    "payout_ratio": ["PayoutRatio"],
}
# 要素名で見つからない場合の日本語ラベルによる判定（項目名の先頭一致）
EDINET_LABELS = {
    "revenue": ["売上高", "売上収益", "営業収益", "収益", "経常収益", "営業総収入"],
    "operating_income": ["営業利益"],
    "ordinary_income": ["経常利益", "税引前利益", "税引前当期利益"],
    "net_income": ["親会社株主に帰属する当期純利益", "親会社の所有者に帰属する当期利益", "当社株主に帰属する当期純利益",
                   "当期純利益"],
    "total_assets": ["総資産額", "資産合計"],
    "equity": ["親会社の所有者に帰属する持分", "株主資本", "純資産額"],
    "eps": ["基本的１株当たり当期利益", "１株当たり当期純利益"],
    "equity_ratio": ["自己資本比率", "親会社所有者帰属持分比率"],
    "roe": ["自己資本利益率", "親会社所有者帰属持分当期利益率"],
    "operating_cf": ["営業活動によるキャッシュ・フロー"],
    "investing_cf": ["投資活動によるキャッシュ・フロー"],
    "dividend_per_share": ["１株当たり配当額"],
    "payout_ratio": ["配当性向"],
}
PERCENT_KEYS = {"equity_ratio", "roe", "payout_ratio"}
_CTX = re.compile(r"^(CurrentYear|Prior(\d)Year)(Duration|Instant)(_NonConsolidatedMember)?$")
_SUFFIX = re.compile(r"(IFRS|USGAAP|JMIS)?SummaryOfBusinessResults$")


def _edinet_key(api_key: str) -> str:
    if not api_key:
        raise StatementError("EDINET の API キーが未設定です。設定画面で入力してください")
    return api_key


def _edinet_json(path: str, params: dict, api_key: str) -> dict:
    q = urllib.parse.urlencode({**params, "Subscription-Key": api_key})
    try:
        data = json.loads(_get(f"{EDINET_BASE}{path}?{q}", {"User-Agent": "PortfolioApp"}))
    except urllib.error.HTTPError as e:
        raise StatementError(f"EDINET に接続できません（HTTP {e.code}）") from e
    status = str(data.get("StatusCode") or (data.get("metadata") or {}).get("status") or "200")
    if status != "200":
        msg = data.get("message") or (data.get("metadata") or {}).get("message") or ""
        if status == "401":
            raise StatementError("EDINET の API キーが無効です。設定画面で確認してください")
        raise StatementError(f"EDINET エラー（{status} {msg}）")
    return data


def _edinet_scan(day: date, api_key: str) -> None:
    """その日に提出された有価証券報告書を索引に登録する（1日1回だけ問い合わせる）."""
    d = day.isoformat()
    with connect() as conn:
        if conn.execute("SELECT 1 FROM edinet_scanned WHERE date = ?", (d,)).fetchone():
            return
    data = _edinet_json("/documents.json", {"date": d, "type": 2}, api_key)
    rows = [(r["docID"], r["secCode"], r.get("periodEnd"), r.get("submitDateTime") or d, r["docTypeCode"])
            for r in data.get("results") or []
            if r.get("docTypeCode") in ANNUAL_REPORT and r.get("secCode") and r.get("csvFlag") == "1"
            and r.get("withdrawalStatus", "0") == "0"]
    with connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO edinet_docs VALUES (?, ?, ?, ?, ?)", rows)
        # 今日の分はまだ提出が続くので記録しない
        if day < now_jst().date():
            conn.execute("INSERT OR IGNORE INTO edinet_scanned (date) VALUES (?)", (d,))


def _edinet_find_doc(code: str, fiscal_year_end: date, api_key: str):
    """最新の有価証券報告書の (doc_id, period_end) を探す."""
    sec_code = code.upper() + "0"
    today = now_jst().date()
    # 直近の決算期 → 1年前の決算期 の順に、期末から SCAN_DAYS 日の間の提出書類を調べる
    def lookup(fy_end: date):
        with connect() as conn:
            return conn.execute(
                "SELECT doc_id, period_end FROM edinet_docs WHERE sec_code = ? AND period_end >= ?"
                " ORDER BY period_end DESC, submitted DESC LIMIT 1",
                (sec_code, (fy_end - timedelta(days=10)).isoformat())).fetchone()

    for fy_end in (fiscal_year_end, fiscal_year_end.replace(year=fiscal_year_end.year - 1)):
        hit = lookup(fy_end)
        day = fy_end + timedelta(days=1)
        last = min(today, fy_end + timedelta(days=SCAN_DAYS))
        while not hit and day <= last:
            if day.weekday() < 5:  # 土日は提出なし
                _edinet_scan(day, api_key)
                hit = lookup(fy_end)
                time.sleep(0.1)
            day += timedelta(days=1)
        if hit:
            return hit["doc_id"], hit["period_end"]
    raise StatementError("EDINET で有価証券報告書が見つかりません")


def _edinet_csv_rows(doc_id: str, api_key: str):
    q = urllib.parse.urlencode({"type": 5, "Subscription-Key": api_key})
    try:
        body = _get(f"{EDINET_BASE}/documents/{doc_id}?{q}", {"User-Agent": "PortfolioApp"}, timeout=60)
    except urllib.error.HTTPError as e:
        raise StatementError(f"EDINET から書類を取得できません（HTTP {e.code}）") from e
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as e:
        raise StatementError("EDINET の書類データ（CSV）を読み込めません") from e
    names = [n for n in zf.namelist() if n.endswith(".csv") and "/jpcrp" in n]
    if not names:
        raise StatementError("EDINET の書類に財務データ（CSV）が含まれていません")
    for name in names:
        text = zf.read(name).decode("utf-16")
        reader = csv.reader(io.StringIO(text), delimiter="\t")
        next(reader, None)  # ヘッダー
        yield from reader


def _to_float(s: str):
    s = (s or "").replace(",", "").strip()
    if s in ("", "-", "－", "―"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def edinet_annual(code: str, fiscal_year_end: date, api_key: str) -> list:
    api_key = _edinet_key(api_key)
    doc_id, period_end = _edinet_find_doc(code, fiscal_year_end, api_key)
    end = date.fromisoformat(period_end)

    # values[key][prior_n][consolidated?] = value
    by_name = {}
    for key, names in EDINET_ELEMENTS.items():
        for i, n in enumerate(names):
            by_name.setdefault(n, (key, i))
    values: dict = {}
    for row in _edinet_csv_rows(doc_id, api_key):
        if len(row) < 9:
            continue
        element, label, ctx, _rel, _cons, _per, unit_id, _unit, raw = row[:9]
        if "SummaryOfBusinessResults" not in element:
            continue
        m = _CTX.match(ctx)
        if not m:
            continue
        prior = int(m.group(2) or 0)
        consolidated = m.group(4) is None
        local = _SUFFIX.sub("", element.split(":")[-1])
        found = by_name.get(local)
        if found is None:  # 要素名で判定できなければラベルで判定
            for key, labels in EDINET_LABELS.items():
                if any(label.startswith(lb) for lb in labels):
                    found = (key, 50 + labels.index(next(lb for lb in labels if label.startswith(lb))))
                    break
        if found is None:
            continue
        key, rank = found
        v = _to_float(raw)
        if v is None:
            continue
        if key in PERCENT_KEYS and unit_id.lower() == "pure" and abs(v) < 10:
            v *= 100  # XBRL の比率は 0.378 のような小数
        slot = values.setdefault(key, {}).setdefault(prior, {})
        cur = slot.get(consolidated)
        if cur is None or rank < cur[0]:
            slot[consolidated] = (rank, v)

    rows = []
    for prior in range(YEARS - 1, -1, -1):
        try:
            pe = end.replace(year=end.year - prior)
        except ValueError:  # 2/29
            pe = end.replace(year=end.year - prior, day=28)
        row = {"period": pe.strftime("%Y-%m")}
        for key in EDINET_ELEMENTS:
            slot = values.get(key, {}).get(prior, {})
            # 連結を優先、無ければ個別（1株配当や配当性向は個別のみの会社が多い）
            hit = slot.get(True) or slot.get(False)
            row[key] = hit[1] if hit else None
        ocf, icf = row.get("operating_cf"), row.pop("investing_cf", None)
        row["free_cf"] = ocf + icf if ocf is not None and icf is not None else None
        if any(row[k] is not None for k in ("revenue", "net_income", "total_assets")):
            rows.append(_finish(row))
    if not rows:
        raise StatementError("有価証券報告書から経営指標を読み取れませんでした")
    return rows
