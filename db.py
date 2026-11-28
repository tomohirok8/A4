"""SQLite によるデータ永続化."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 環境変数 PORTFOLIO_DB で別の DB ファイルを使える（動作確認用）
DB_PATH = Path(os.environ.get("PORTFOLIO_DB") or Path(__file__).with_name("portfolio.db"))
JST = timezone(timedelta(hours=9), "JST")

SCHEMA = """
CREATE TABLE IF NOT EXISTS holdings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol             TEXT    NOT NULL,          -- 銘柄コード (例: 7203, AAPL)
    name               TEXT    NOT NULL DEFAULT '',
    avg_cost           REAL    NOT NULL DEFAULT 0, -- 取得単価
    shares             REAL    NOT NULL DEFAULT 0, -- 所持数
    current_price      REAL,                       -- 現在株価
    dividend_per_share REAL    NOT NULL DEFAULT 0, -- 1株あたり年間配当
    price_updated_at   TEXT                        -- 株価取得日時 (JST)
);

-- 日次スナップショット (1日1レコード、同日は上書き)
CREATE TABLE IF NOT EXISTS snapshots (
    date        TEXT PRIMARY KEY,   -- YYYY-MM-DD (JST)
    total_value REAL NOT NULL,      -- 総評価額
    total_cost  REAL NOT NULL,      -- 総取得額
    total_pl    REAL NOT NULL,      -- 総損得
    saved_at    TEXT NOT NULL       -- 保存日時 (JST)
);

-- 日次スナップショットの銘柄別明細
CREATE TABLE IF NOT EXISTS snapshot_items (
    date     TEXT NOT NULL REFERENCES snapshots(date) ON DELETE CASCADE,
    symbol   TEXT NOT NULL,
    name     TEXT NOT NULL,
    price    REAL NOT NULL,   -- その日の株価
    shares   REAL NOT NULL,
    value    REAL NOT NULL,   -- 所持数 × 株価
    avg_cost REAL NOT NULL,   -- 取得単価
    cost     REAL NOT NULL,   -- 所持数 × 取得単価
    pl       REAL NOT NULL,   -- 損得額
    PRIMARY KEY (date, symbol)
);

-- 為替レート（1 外貨 = rate 円）
CREATE TABLE IF NOT EXISTS fx_rates (
    currency   TEXT PRIMARY KEY,  -- USD など
    rate       REAL NOT NULL,
    updated_at TEXT NOT NULL      -- 取得/入力日時 (JST)
);

-- アプリ設定（SEC の連絡先、EDINET の API キーなど）
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- EDINET の有価証券報告書の索引（日付ごとの書類一覧を取得した結果を保存）
CREATE TABLE IF NOT EXISTS edinet_docs (
    doc_id      TEXT PRIMARY KEY,
    sec_code    TEXT NOT NULL,     -- 証券コード5桁 (例: 72030)
    period_end  TEXT,              -- 決算期末 YYYY-MM-DD
    submitted   TEXT NOT NULL,     -- 提出日時
    doc_type    TEXT NOT NULL      -- 120: 有価証券報告書, 130: 訂正
);
CREATE INDEX IF NOT EXISTS edinet_docs_sec ON edinet_docs (sec_code, period_end);
CREATE TABLE IF NOT EXISTS edinet_scanned (
    date TEXT PRIMARY KEY          -- 書類一覧を取得済みの日付
);

-- 実際に受け取った配当（西暦年ごと）
CREATE TABLE IF NOT EXISTS dividend_records (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    year     INTEGER NOT NULL,              -- 西暦年
    symbol   TEXT    NOT NULL DEFAULT '',
    name     TEXT    NOT NULL DEFAULT '',
    currency TEXT    NOT NULL DEFAULT 'JPY',
    dps      REAL    NOT NULL DEFAULT 0,    -- 配当単価（1株あたり、銘柄の通貨）
    shares   REAL    NOT NULL DEFAULT 0,    -- 配当を受け取った株数
    fx_rate  REAL,                          -- 受取時の為替レート（外貨のみ。空なら現在レート）
    tax_rate REAL                           -- 税率 %（空なら通貨ごとの標準税率）
);
CREATE INDEX IF NOT EXISTS dividend_records_year ON dividend_records (year);

-- 財務情報のキャッシュ（取得に時間がかかるため一定時間再利用する）
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol     TEXT PRIMARY KEY,
    data       TEXT NOT NULL,     -- JSON
    fetched_at TEXT NOT NULL      -- 取得日時 (JST, ISO 8601)
);
"""

# 既存 DB に後から追加した列（init_db で不足分を ALTER TABLE する）
MIGRATIONS = {
    "holdings": {
        "currency": "TEXT NOT NULL DEFAULT 'JPY'",  # 株価・取得単価・配当の通貨
        "cost_fx_rate": "REAL",                       # 取得時の為替レート（空なら現在レート）
    },
    "snapshot_items": {
        "currency": "TEXT NOT NULL DEFAULT 'JPY'",
        "fx_rate": "REAL NOT NULL DEFAULT 1",         # その日の為替レート
    },
    "snapshots": {
        "fx_rates": "TEXT",                           # その日のレート一覧 (JSON)
        "cash": "REAL",                               # その日の買付余力（以前の保存分は空）
    },
}


def now_jst() -> datetime:
    return datetime.now(JST)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_settings() -> dict:
    with connect() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings")}


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        if value:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        else:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
