"""株ポートフォリオ管理アプリ (Flask)."""
import json
from datetime import datetime, timedelta

from flask import Flask, abort, jsonify, render_template, request

from db import connect, get_settings, init_db, now_jst, set_setting
from fundamentals import fetch_fundamentals
from prices import default_currency, fetch_fx_rate, fetch_name, fetch_quote

FUNDAMENTALS_TTL = timedelta(hours=24)

app = Flask(__name__)
app.json.ensure_ascii = False

EDITABLE = {
    "symbol": str,
    "name": str,
    "currency": str,
    "avg_cost": float,
    "cost_fx_rate": float,
    "shares": float,
    "current_price": float,
    "dividend_per_share": float,
}
NULLABLE = {"current_price", "cost_fx_rate"}


def stamp() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S")


def load_rates(conn) -> dict:
    """{通貨: {rate, updated_at}}。JPY は常に 1."""
    rates = {r["currency"]: {"rate": r["rate"], "updated_at": r["updated_at"]}
             for r in conn.execute("SELECT * FROM fx_rates")}
    rates["JPY"] = {"rate": 1.0, "updated_at": None}
    return rates


def enrich(row, rates: dict) -> dict:
    """DB の行に円換算の評価額・取得額・損得・配当額を付与する.

    株価・取得単価・1株配当はその銘柄の通貨のまま、計算結果は円。
    取得額は取得時レート（未入力なら現在レート）で円換算するので、
    損得額には為替差損益も含まれる。
    """
    h = dict(row)
    fx = (rates.get(h["currency"]) or {}).get("rate")
    cost_fx = h["cost_fx_rate"] or fx
    price = h["current_price"]
    h["fx_rate"] = fx
    h["cost"] = h["avg_cost"] * h["shares"] * cost_fx if cost_fx else None
    h["value"] = price * h["shares"] * fx if price is not None and fx else None
    ok = h["value"] is not None and h["cost"] is not None
    h["pl"] = h["value"] - h["cost"] if ok else None
    h["pl_rate"] = h["pl"] / h["cost"] * 100 if ok and h["cost"] else None
    h["dividend_total"] = h["dividend_per_share"] * h["shares"] * fx if fx else None
    # 配当利回り（銘柄の通貨どうしで計算するので為替は関係しない）
    dps = h["dividend_per_share"]
    h["yield_cost"] = dps / h["avg_cost"] * 100 if h["avg_cost"] else None
    h["yield_value"] = dps / price * 100 if price else None
    # 外貨建て銘柄は、その通貨での損得も出す（為替の影響を含まない）。合計には使わない
    h["pl_native"] = h["pl_native_rate"] = None
    if h["currency"] != "JPY" and price is not None:
        cost_native = h["avg_cost"] * h["shares"]
        h["pl_native"] = (price - h["avg_cost"]) * h["shares"]
        h["pl_native_rate"] = h["pl_native"] / cost_native * 100 if cost_native else None
    return h


def _parse(data: dict) -> dict:
    out = {}
    for key, cast in EDITABLE.items():
        if key not in data:
            continue
        val = data[key]
        if cast is float:
            if val in (None, ""):
                if key in NULLABLE:
                    out[key] = None
                    continue
                val = 0
            try:
                val = float(val)
            except (TypeError, ValueError):
                abort(400, f"{key} は数値で入力してください")
        else:
            val = str(val).strip()
            if key == "currency":
                val = val.upper()
                if not (len(val) == 3 and val.isalpha()):
                    abort(400, "通貨は JPY / USD のような3文字で入力してください")
        out[key] = val
    return out


def get_cash(conn) -> float:
    """買付余力（円）。settings に保存する。"""
    row = conn.execute("SELECT value FROM settings WHERE key = 'buying_power'").fetchone()
    return float(row["value"]) if row else 0.0


def _holdings_payload(conn) -> dict:
    rates = load_rates(conn)
    rows = conn.execute("SELECT * FROM holdings ORDER BY id").fetchall()
    return {"holdings": [enrich(r, rates) for r in rows], "rates": rates, "cash": get_cash(conn)}


def _fill_name(data: dict) -> None:
    """銘柄コードがあって銘柄名が指定されていなければ、銘柄名を自動取得して入れる."""
    if data.get("symbol") and not data.get("name"):
        name = fetch_name(data["symbol"])
        if name:
            data["name"] = name


@app.get("/")
def index():
    return render_template("index.html")


# ---- 銘柄 (holdings) ----

@app.get("/api/holdings")
def list_holdings():
    with connect() as conn:
        return jsonify(_holdings_payload(conn))


@app.post("/api/holdings")
def add_holding():
    data = _parse(request.get_json(force=True) or {})
    data.setdefault("symbol", "")
    data.setdefault("currency", default_currency(data["symbol"]))
    _fill_name(data)
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    with connect() as conn:
        conn.execute(f"INSERT INTO holdings ({cols}) VALUES ({marks})", list(data.values()))
        return jsonify(_holdings_payload(conn)), 201


@app.put("/api/holdings/<int:hid>")
def update_holding(hid: int):
    data = _parse(request.get_json(force=True) or {})
    if not data:
        abort(400, "更新項目がありません")
    # 銘柄コードを変えたら通貨も推定し直す（次の株価取得で正しい通貨に更新される）
    if "symbol" in data and "currency" not in data:
        data["currency"] = default_currency(data["symbol"])
    # 銘柄コードを変えたら銘柄名も取り直す
    if "symbol" in data:
        _fill_name(data)
    sets = ", ".join(f"{k} = ?" for k in data)
    with connect() as conn:
        cur = conn.execute(f"UPDATE holdings SET {sets} WHERE id = ?", [*data.values(), hid])
        if cur.rowcount == 0:
            abort(404)
        return jsonify(_holdings_payload(conn))


@app.delete("/api/holdings/<int:hid>")
def delete_holding(hid: int):
    with connect() as conn:
        conn.execute("DELETE FROM holdings WHERE id = ?", (hid,))
    return "", 204


# ---- 為替レート ----

@app.put("/api/fx/<currency>")
def set_fx_rate(currency: str):
    currency = currency.upper()
    if currency == "JPY":
        abort(400, "JPY のレートは変更できません")
    try:
        rate = float((request.get_json(force=True) or {}).get("rate"))
    except (TypeError, ValueError):
        abort(400, "レートは数値で入力してください")
    if rate <= 0:
        abort(400, "レートは正の数で入力してください")
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO fx_rates (currency, rate, updated_at) VALUES (?, ?, ?)",
                     (currency, rate, stamp()))
        return jsonify(_holdings_payload(conn))


# ---- 買付余力 ----

@app.put("/api/cash")
def set_cash():
    try:
        cash = float((request.get_json(force=True) or {}).get("cash") or 0)
    except (TypeError, ValueError):
        abort(400, "買付余力は数値で入力してください")
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('buying_power', ?)", (str(cash),))
        return jsonify(_holdings_payload(conn))


# ---- 現在値・為替レート取得 ----

@app.post("/api/prices/refresh")
def refresh_prices():
    errors = []
    ts = stamp()
    with connect() as conn:
        rows = conn.execute("SELECT id, symbol, name, currency FROM holdings WHERE symbol != ''").fetchall()
        currencies = set()
        for r in rows:
            try:
                price, currency = fetch_quote(r["symbol"])
            except Exception as e:  # noqa: BLE001
                errors.append(str(e) if isinstance(e, ValueError) else f"{r['symbol']}: {e}")
                currencies.add(r["currency"])
                continue
            currencies.add(currency)
            if not r["name"].strip():  # 銘柄名が空なら自動で入れる
                name = fetch_name(r["symbol"])
                if name:
                    conn.execute("UPDATE holdings SET name = ? WHERE id = ?", (name, r["id"]))
            conn.execute(
                "UPDATE holdings SET current_price = ?, currency = ?, price_updated_at = ? WHERE id = ?",
                (price, currency, ts, r["id"]),
            )
        for currency in sorted(currencies - {"JPY"}):
            try:
                rate = fetch_fx_rate(currency)
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
                continue
            conn.execute("INSERT OR REPLACE INTO fx_rates (currency, rate, updated_at) VALUES (?, ?, ?)",
                         (currency, rate, ts))
        payload = _holdings_payload(conn)
    payload["errors"] = errors
    return jsonify(payload)


# ---- 設定 ----

def _mask(key: str) -> str:
    return ("•" * 8 + key[-4:]) if key else ""


@app.get("/api/settings")
def read_settings():
    s = get_settings()
    return jsonify({"sec_contact": s.get("sec_contact", ""),
                    "edinet_api_key_set": bool(s.get("edinet_api_key")),
                    "edinet_api_key_masked": _mask(s.get("edinet_api_key", ""))})


@app.put("/api/settings")
def write_settings():
    data = request.get_json(force=True) or {}
    if "sec_contact" in data:
        contact = str(data["sec_contact"] or "").strip()
        if contact and "@" not in contact:
            abort(400, "SEC の連絡先には名前とメールアドレスを入力してください（例: Taro Yamada taro@example.com）")
        set_setting("sec_contact", contact)
    if "edinet_api_key" in data:  # 空文字で削除、キーが無い場合は変更しない
        set_setting("edinet_api_key", str(data["edinet_api_key"] or "").strip())
    # 設定が変わったら財務情報のキャッシュを捨てて、次回から新しい設定で取得する
    with connect() as conn:
        conn.execute("DELETE FROM fundamentals WHERE symbol NOT LIKE '\\_\\_%' ESCAPE '\\'")
    return read_settings()


# ---- 財務情報 ----

@app.get("/api/fundamentals/<symbol>")
def get_fundamentals(symbol: str):
    """財務情報を返す。24時間以内に取得済みならキャッシュを使う（?refresh=1 で再取得）."""
    symbol = symbol.strip().upper()
    if not symbol:
        abort(400, "銘柄コードがありません")
    refresh = request.args.get("refresh") == "1"
    with connect() as conn:
        cached = conn.execute("SELECT * FROM fundamentals WHERE symbol = ?", (symbol,)).fetchone()
    if cached and not refresh:
        fetched = datetime.fromisoformat(cached["fetched_at"])
        if now_jst() - fetched < FUNDAMENTALS_TTL:
            return jsonify({**json.loads(cached["data"]), "fetched_at": fetched.strftime("%Y-%m-%d %H:%M"),
                            "cached": True})
    try:
        data = fetch_fundamentals(symbol, get_settings())
    except ValueError as e:
        if cached:  # 取得に失敗したら古いキャッシュでも返す
            fetched = datetime.fromisoformat(cached["fetched_at"])
            return jsonify({**json.loads(cached["data"]), "fetched_at": fetched.strftime("%Y-%m-%d %H:%M"),
                            "cached": True, "warning": f"最新の取得に失敗したため前回のデータです（{e}）"})
        abort(400, str(e))
    now = now_jst()
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO fundamentals (symbol, data, fetched_at) VALUES (?, ?, ?)",
                     (symbol, json.dumps(data, ensure_ascii=False), now.isoformat()))
    return jsonify({**data, "fetched_at": now.strftime("%Y-%m-%d %H:%M"), "cached": False})


# ---- 日次スナップショット ----

@app.post("/api/snapshots")
def save_snapshot():
    now = now_jst()
    date = now.strftime("%Y-%m-%d")
    with connect() as conn:
        rates = load_rates(conn)
        rows = [enrich(r, rates) for r in conn.execute(
            "SELECT * FROM holdings WHERE symbol != '' ORDER BY id").fetchall()]
        if not rows:
            abort(400, "保存する銘柄がありません")
        no_fx = sorted({h["currency"] for h in rows if h["fx_rate"] is None})
        if no_fx:
            abort(400, "為替レートが未設定です: " + ", ".join(no_fx) + "（現在値を取得するか、レートを入力してください）")
        missing = [h["symbol"] for h in rows if h["current_price"] is None]
        if missing:
            abort(400, "現在株価が未設定の銘柄があります: " + ", ".join(missing))

        total_value = sum(h["value"] for h in rows)
        total_cost = sum(h["cost"] for h in rows)
        total_pl = total_value - total_cost
        cash = get_cash(conn)
        used = {h["currency"]: rates[h["currency"]]["rate"] for h in rows if h["currency"] != "JPY"}

        # 同日のデータは上書き
        conn.execute("DELETE FROM snapshots WHERE date = ?", (date,))
        conn.execute(
            "INSERT INTO snapshots (date, total_value, total_cost, total_pl, saved_at, fx_rates, cash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, total_value, total_cost, total_pl, now.strftime("%Y-%m-%d %H:%M:%S"),
             json.dumps(used) if used else None, cash),
        )
        # 同じ銘柄コードが複数行ある場合は合算して保存
        # price / avg_cost は銘柄の通貨、value / cost / pl は円
        merged: dict = {}
        for h in rows:
            key = h["symbol"].upper()
            m = merged.setdefault(key, {"name": h["name"], "price": h["current_price"],
                                        "currency": h["currency"], "fx_rate": h["fx_rate"],
                                        "shares": 0.0, "value": 0.0, "cost": 0.0, "cost_native": 0.0})
            m["shares"] += h["shares"]
            m["value"] += h["value"]
            m["cost"] += h["cost"]
            m["cost_native"] += h["avg_cost"] * h["shares"]
        for sym, m in merged.items():
            avg_cost = m["cost_native"] / m["shares"] if m["shares"] else 0
            conn.execute(
                "INSERT INTO snapshot_items (date, symbol, name, price, shares, value, avg_cost, cost, pl,"
                " currency, fx_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date, sym, m["name"], m["price"], m["shares"], m["value"], avg_cost, m["cost"],
                 m["value"] - m["cost"], m["currency"], m["fx_rate"]),
            )
    return jsonify({"date": date, "total_value": total_value, "total_cost": total_cost, "total_pl": total_pl,
                    "cash": cash})


@app.get("/api/snapshots")
def list_snapshots():
    with connect() as conn:
        snaps = [dict(r) for r in conn.execute("SELECT * FROM snapshots ORDER BY date").fetchall()]
        items = conn.execute("SELECT * FROM snapshot_items ORDER BY date, symbol").fetchall()
    by_date: dict = {}
    for it in items:
        by_date.setdefault(it["date"], []).append(dict(it))
    for s in snaps:
        s["items"] = by_date.get(s["date"], [])
        s["fx_rates"] = json.loads(s["fx_rates"]) if s.get("fx_rates") else {}
    return jsonify(snaps)


@app.delete("/api/snapshots/<date>")
def delete_snapshot(date: str):
    with connect() as conn:
        conn.execute("DELETE FROM snapshots WHERE date = ?", (date,))
    return "", 204


# ---- 配当実績（西暦年ごと） ----

# 配当にかかる税率 %（未入力時の標準）
# 日本株: 所得税15.315% + 住民税5%。米国株: 米国で10%源泉徴収後に日本で20.315% → 実質 28.28%
DEFAULT_TAX = {"JPY": 20.315}
DEFAULT_TAX_FOREIGN = round((1 - 0.9 * (1 - 0.20315)) * 100, 3)
DIV_EDITABLE = {"symbol": str, "name": str, "currency": str, "dps": float, "shares": float,
                "fx_rate": float, "tax_rate": float}
DIV_NULLABLE = {"fx_rate", "tax_rate"}


def default_tax(currency: str) -> float:
    return DEFAULT_TAX.get(currency, DEFAULT_TAX_FOREIGN)


def enrich_dividend(row, rates: dict) -> dict:
    d = dict(row)
    fx = d["fx_rate"] or (rates.get(d["currency"]) or {}).get("rate")
    tax = d["tax_rate"] if d["tax_rate"] is not None else default_tax(d["currency"])
    d["fx_used"] = fx
    d["tax_used"] = tax
    d["gross_native"] = d["dps"] * d["shares"]
    d["gross"] = d["gross_native"] * fx if fx else None          # 額面（円）
    d["net"] = d["gross"] * (1 - tax / 100) if d["gross"] is not None else None  # 手取り（円）
    return d


def _parse_dividend(data: dict) -> dict:
    out = {}
    for key, cast in DIV_EDITABLE.items():
        if key not in data:
            continue
        val = data[key]
        if cast is float:
            if val in (None, ""):
                if key in DIV_NULLABLE:
                    out[key] = None
                    continue
                val = 0
            try:
                val = float(val)
            except (TypeError, ValueError):
                abort(400, f"{key} は数値で入力してください")
        else:
            val = str(val).strip()
            if key == "currency":
                val = val.upper()
                if not (len(val) == 3 and val.isalpha()):
                    abort(400, "通貨は JPY / USD のような3文字で入力してください")
        out[key] = val
    return out


def _dividends_payload(conn) -> dict:
    rates = load_rates(conn)
    rows = [enrich_dividend(r, rates) for r in conn.execute(
        "SELECT * FROM dividend_records ORDER BY year, id").fetchall()]
    # 年ごとの合計と累積
    years: dict = {}
    for r in rows:
        y = years.setdefault(r["year"], {"year": r["year"], "gross": 0.0, "net": 0.0, "count": 0})
        y["gross"] += r["gross"] or 0
        y["net"] += r["net"] or 0
        y["count"] += 1
    summary, cum_g, cum_n = [], 0.0, 0.0
    for y in sorted(years):
        cum_g += years[y]["gross"]
        cum_n += years[y]["net"]
        summary.append({**years[y], "cum_gross": cum_g, "cum_net": cum_n})
    return {"records": rows, "summary": summary, "rates": rates,
            "default_tax": {"JPY": DEFAULT_TAX["JPY"], "foreign": DEFAULT_TAX_FOREIGN}}


def _year(value) -> int:
    try:
        year = int(value)
    except (TypeError, ValueError):
        abort(400, "年は西暦の数字で指定してください")
    if not 1900 <= year <= 2200:
        abort(400, "年は西暦で指定してください")
    return year


@app.get("/api/dividends")
def list_dividends():
    with connect() as conn:
        return jsonify(_dividends_payload(conn))


@app.post("/api/dividends")
def add_dividend():
    body = request.get_json(force=True) or {}
    data = _parse_dividend(body)
    data["year"] = _year(body.get("year"))
    data.setdefault("symbol", "")
    data.setdefault("currency", default_currency(data["symbol"]) if data["symbol"] else "JPY")
    _fill_name(data)
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    with connect() as conn:
        conn.execute(f"INSERT INTO dividend_records ({cols}) VALUES ({marks})", list(data.values()))
        return jsonify(_dividends_payload(conn)), 201


@app.post("/api/dividends/import")
def import_dividends():
    """保有銘柄（所持数 > 0）の配当単価と所持数を、指定した年にまだ無い銘柄だけ追加する."""
    year = _year((request.get_json(force=True) or {}).get("year"))
    with connect() as conn:
        have = {r["symbol"].upper() for r in conn.execute(
            "SELECT symbol FROM dividend_records WHERE year = ?", (year,))}
        added = 0
        for h in conn.execute("SELECT * FROM holdings WHERE symbol != '' AND shares > 0 ORDER BY id"):
            if h["symbol"].upper() in have:
                continue
            have.add(h["symbol"].upper())
            conn.execute("INSERT INTO dividend_records (year, symbol, name, currency, dps, shares)"
                         " VALUES (?, ?, ?, ?, ?, ?)",
                         (year, h["symbol"], h["name"], h["currency"], h["dividend_per_share"], h["shares"]))
            added += 1
        payload = _dividends_payload(conn)
    payload["added"] = added
    return jsonify(payload)


@app.put("/api/dividends/<int:rid>")
def update_dividend(rid: int):
    body = request.get_json(force=True) or {}
    data = _parse_dividend(body)
    if "year" in body:
        data["year"] = _year(body["year"])
    if not data:
        abort(400, "更新項目がありません")
    if data.get("symbol") and "currency" not in data:
        data["currency"] = default_currency(data["symbol"])
    if "symbol" in data:
        _fill_name(data)
    sets = ", ".join(f"{k} = ?" for k in data)
    with connect() as conn:
        cur = conn.execute(f"UPDATE dividend_records SET {sets} WHERE id = ?", [*data.values(), rid])
        if cur.rowcount == 0:
            abort(404)
        return jsonify(_dividends_payload(conn))


@app.delete("/api/dividends/<int:rid>")
def delete_dividend(rid: int):
    with connect() as conn:
        conn.execute("DELETE FROM dividend_records WHERE id = ?", (rid,))
        return jsonify(_dividends_payload(conn))


@app.errorhandler(400)
@app.errorhandler(404)
def handle_error(e):
    return jsonify({"error": e.description}), e.code


init_db()

if __name__ == "__main__":
    import os
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5050)), debug=True)
