# -*- coding: utf-8 -*-
"""
db.py

資料庫層 — 連線、建表、讀寫
------------------------------------
正式環境用 MySQL。但這個模組同時支援 SQLite，理由不是「為了簡單」，
而是為了**能夠在沒有MySQL server的機器上驗證SQL邏輯是否正確**
(開發機、CI、或還沒把DB架起來的時候)。

兩個後端跑的是同一套程式碼路徑，所以在SQLite上測過的邏輯，
搬到MySQL不會因為程式流程不同而出現沒測到的分支。

用法
------------------------------------
    # 正式：MySQL
    from db import OptionDB
    db = OptionDB.connect_mysql(host="localhost", user="root",
                                password="xxx", database="taifex")
    db.create_tables()

    # 本機驗證：SQLite(不需要任何server)
    db = OptionDB.connect_sqlite("test.db")     # 或 ":memory:"
    db.create_tables()

    # 兩者之後的操作完全一樣
    db.save_live_quotes(rows)
    baseline = db.get_monthly_baseline_iv(17500, "C", "day", date(2026,9,16))

連線設定也可以放環境變數，用 OptionDB.connect_from_env() 讀取：
    TAIFEX_DB_HOST / TAIFEX_DB_PORT / TAIFEX_DB_USER /
    TAIFEX_DB_PASSWORD / TAIFEX_DB_NAME
    (設 TAIFEX_DB_SQLITE=路徑 則改用SQLite)
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Literal, Optional, Sequence

Session = Literal["day", "night"]
OptionType = Literal["C", "P"]
ExpiryType = Literal["week", "month"]
Dialect = Literal["mysql", "sqlite"]


# ---------------------------------------------------------------------------
# 建表SQL
# ---------------------------------------------------------------------------
#
# 跟README最初提議的schema相比，這裡做了幾處調整，每一處都有實測依據：
#
# 1. deviation_pct: DECIMAL(6,4) → DECIMAL(8,2)
#    原本的上限是 99.9999，但實測真實資料時看到偏差超過100%的情況
#    (深度價外合約的合理價很小，一點價差就是很大的百分比)。
#    程式端已經把偏差夾在±999.99，欄位開DECIMAL(8,2)剛好容納且有餘裕。
#
# 2. option_live_quote 新增 underlying_price / implied_vol / volume /
#    quote_time / price_source / reliable 欄位。
#    原本的schema只存了判斷結果，但實測發現「資料品質」才是這個系統最大的風險：
#    報價可能是好幾小時前的、可能完全沒成交量、可能是用買賣中價推的。
#    前端如果看不到這些，使用者就會把僵滯報價算出來的訊號當真。
#
# 3. option_live_quote 新增 session 欄位並納入主鍵。
#    原本主鍵是(expiry_type, expiry_date, strike_price, right_type)，
#    日盤跟夜盤的報價會互相覆蓋。加上session才能分開存。
#
# 4. 兩張表都加了 updated_at 的索引，前端查詢時常會需要「最近更新的」。

MYSQL_DDL = [
    """
    CREATE TABLE IF NOT EXISTS option_iv_history (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        trade_date DATE NOT NULL,
        session ENUM('day','night') NOT NULL,
        expiry_type ENUM('week','month') NOT NULL,
        expiry_date DATE NOT NULL,
        strike_price DECIMAL(10,2) NOT NULL,
        right_type ENUM('C','P') NOT NULL,
        underlying_price DECIMAL(10,2) NOT NULL,
        close_price DECIMAL(10,4) NOT NULL,
        implied_vol DECIMAL(8,6) NOT NULL,
        UNIQUE KEY uniq_contract_session
            (trade_date, session, expiry_date, strike_price, right_type),
        KEY idx_baseline_lookup
            (strike_price, right_type, session, expiry_date, trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS option_live_quote (
        expiry_type ENUM('week','month') NOT NULL,
        expiry_date DATE NOT NULL,
        strike_price DECIMAL(10,2) NOT NULL,
        right_type ENUM('C','P') NOT NULL,
        session ENUM('day','night') NOT NULL,
        underlying_price DECIMAL(10,2) NOT NULL,
        market_price DECIMAL(10,4) NOT NULL,
        fair_price DECIMAL(10,4) NOT NULL,
        implied_vol DECIMAL(8,6) NOT NULL,
        baseline_iv DECIMAL(8,6) NOT NULL,
        deviation_pct DECIMAL(8,2) NOT NULL,
        status ENUM('expensive','fair','cheap') NOT NULL,
        volume INT NOT NULL DEFAULT 0,
        quote_time VARCHAR(8) NOT NULL DEFAULT '',
        price_source VARCHAR(8) NOT NULL DEFAULT '',
        reliable TINYINT(1) NOT NULL DEFAULT 1,
        updated_at DATETIME NOT NULL,
        PRIMARY KEY (expiry_type, expiry_date, strike_price, right_type, session),
        KEY idx_updated (updated_at),
        KEY idx_chain (expiry_type, expiry_date, right_type, session)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

# SQLite沒有ENUM/AUTO_INCREMENT等語法，用等價寫法。
# 欄位名稱與語意跟MySQL版完全一致，所以上層程式碼不用分岔。
SQLITE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS option_iv_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        session TEXT NOT NULL CHECK (session IN ('day','night')),
        expiry_type TEXT NOT NULL CHECK (expiry_type IN ('week','month')),
        expiry_date TEXT NOT NULL,
        strike_price REAL NOT NULL,
        right_type TEXT NOT NULL CHECK (right_type IN ('C','P')),
        underlying_price REAL NOT NULL,
        close_price REAL NOT NULL,
        implied_vol REAL NOT NULL,
        UNIQUE (trade_date, session, expiry_date, strike_price, right_type)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_baseline_lookup ON option_iv_history
        (strike_price, right_type, session, expiry_date, trade_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS option_live_quote (
        expiry_type TEXT NOT NULL CHECK (expiry_type IN ('week','month')),
        expiry_date TEXT NOT NULL,
        strike_price REAL NOT NULL,
        right_type TEXT NOT NULL CHECK (right_type IN ('C','P')),
        session TEXT NOT NULL CHECK (session IN ('day','night')),
        underlying_price REAL NOT NULL,
        market_price REAL NOT NULL,
        fair_price REAL NOT NULL,
        implied_vol REAL NOT NULL,
        baseline_iv REAL NOT NULL,
        deviation_pct REAL NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('expensive','fair','cheap')),
        volume INTEGER NOT NULL DEFAULT 0,
        quote_time TEXT NOT NULL DEFAULT '',
        price_source TEXT NOT NULL DEFAULT '',
        reliable INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (expiry_type, expiry_date, strike_price, right_type, session)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_updated ON option_live_quote (updated_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_chain ON option_live_quote
        (expiry_type, expiry_date, right_type, session)
    """,
]


@dataclass
class LiveQuoteRow:
    """要寫進 option_live_quote 的一筆資料"""
    expiry_type: ExpiryType
    expiry_date: date
    strike_price: float
    right_type: OptionType
    session: Session
    underlying_price: float
    market_price: float
    fair_price: float
    implied_vol: float
    baseline_iv: float
    deviation_pct: float
    status: str
    volume: int = 0
    quote_time: str = ""
    price_source: str = ""
    reliable: bool = True


@dataclass
class IvHistoryRow:
    """要寫進 option_iv_history 的一筆資料"""
    trade_date: date
    session: Session
    expiry_type: ExpiryType
    expiry_date: date
    strike_price: float
    right_type: OptionType
    underlying_price: float
    close_price: float
    implied_vol: float


def _adapt(value: Any) -> Any:
    """
    SQLite不吃 date/datetime 物件，轉成字串。
    MySQL(pymysql)可以直接吃，但轉成字串一樣正確，所以兩邊共用同一套轉換，
    避免「只在某個後端才會踩到」的型別問題。
    """
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return 1 if value else 0
    return value


class OptionDB:
    """
    資料庫操作的統一入口。

    所有SQL都寫MySQL方言(用 %s 佔位符)，需要跑SQLite時再由 _translate() 轉換。
    這樣做的好處是：正式環境跑的SQL就是你在程式碼裡讀到的樣子，沒有隱藏轉換。
    """

    def __init__(self, conn, dialect: Dialect):
        self.conn = conn
        self.dialect = dialect

    # -- 連線 ---------------------------------------------------------------

    @classmethod
    def connect_mysql(cls, host: str = "localhost", port: int = 3306,
                      user: str = "root", password: str = "",
                      database: str = "taifex", charset: str = "utf8mb4") -> "OptionDB":
        import pymysql
        conn = pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset=charset, autocommit=False,
        )
        return cls(conn, "mysql")

    @classmethod
    def connect_sqlite(cls, path: str = ":memory:") -> "OptionDB":
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        return cls(conn, "sqlite")

    @classmethod
    def connect_from_env(cls) -> "OptionDB":
        """
        從環境變數建立連線。設了 TAIFEX_DB_SQLITE 就用SQLite，否則用MySQL。
        排程/正式部署建議用這個，密碼才不會寫死在程式碼裡。
        """
        sqlite_path = os.environ.get("TAIFEX_DB_SQLITE")
        if sqlite_path:
            return cls.connect_sqlite(sqlite_path)
        return cls.connect_mysql(
            host=os.environ.get("TAIFEX_DB_HOST", "localhost"),
            port=int(os.environ.get("TAIFEX_DB_PORT", "3306")),
            user=os.environ.get("TAIFEX_DB_USER", "root"),
            password=os.environ.get("TAIFEX_DB_PASSWORD", ""),
            database=os.environ.get("TAIFEX_DB_NAME", "taifex"),
        )

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "OptionDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- SQL方言轉換 ---------------------------------------------------------

    def _translate(self, sql: str) -> str:
        """
        把MySQL方言的SQL轉成SQLite可執行的形式。只處理兩件事：
          1. 佔位符 %s → ?
          2. INSERT ... ON DUPLICATE KEY UPDATE x = VALUES(x)
             → INSERT ... ON CONFLICT DO UPDATE SET x = excluded.x

        故意只支援這兩種轉換、而且轉不了就直接報錯，
        不做「盡量猜」的模糊處理——寧可測試時就爆掉，也不要在正式環境靜默寫錯資料。
        """
        if self.dialect == "mysql":
            return sql

        out = sql.replace("%s", "?")

        m = re.search(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", out, re.I)
        if m:
            head, tail = out[:m.start()], out[m.end():]
            # VALUES(col) → excluded.col
            tail = re.sub(r"VALUES\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
                          r"excluded.\1", tail, flags=re.I)
            out = f"{head} ON CONFLICT DO UPDATE SET {tail}"

        return out

    @contextmanager
    def cursor(self):
        cur = self.conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.cursor() as cur:
            cur.execute(self._translate(sql), tuple(_adapt(p) for p in params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        with self.cursor() as cur:
            cur.execute(self._translate(sql), tuple(_adapt(p) for p in params))
            return list(cur.fetchall())

    def commit(self) -> None:
        self.conn.commit()

    # -- 建表 ---------------------------------------------------------------

    def create_tables(self) -> None:
        ddl = MYSQL_DDL if self.dialect == "mysql" else SQLITE_DDL
        with self.cursor() as cur:
            for stmt in ddl:
                cur.execute(stmt)
        self.conn.commit()

    def drop_tables(self) -> None:
        """測試用。正式環境請小心。"""
        with self.cursor() as cur:
            for t in ("option_live_quote", "option_iv_history"):
                cur.execute(f"DROP TABLE IF EXISTS {t}")
        self.conn.commit()

    # -- 寫入：即時報價 -------------------------------------------------------

    def save_live_quotes(self, rows: Iterable[LiveQuoteRow]) -> int:
        """
        把一批算好的判斷結果寫進 option_live_quote(有就更新、沒有就新增)。

        每分鐘排程會呼叫這個，前端再從這張表讀資料。
        """
        sql = """
            INSERT INTO option_live_quote
                (expiry_type, expiry_date, strike_price, right_type, session,
                 underlying_price, market_price, fair_price, implied_vol,
                 baseline_iv, deviation_pct, status, volume, quote_time,
                 price_source, reliable, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                underlying_price = VALUES(underlying_price),
                market_price = VALUES(market_price),
                fair_price = VALUES(fair_price),
                implied_vol = VALUES(implied_vol),
                baseline_iv = VALUES(baseline_iv),
                deviation_pct = VALUES(deviation_pct),
                status = VALUES(status),
                volume = VALUES(volume),
                quote_time = VALUES(quote_time),
                price_source = VALUES(price_source),
                reliable = VALUES(reliable),
                updated_at = VALUES(updated_at)
        """
        now = datetime.now()
        count = 0
        with self.cursor() as cur:
            stmt = self._translate(sql)
            for r in rows:
                cur.execute(stmt, tuple(_adapt(v) for v in (
                    r.expiry_type, r.expiry_date, r.strike_price, r.right_type,
                    r.session, r.underlying_price, r.market_price, r.fair_price,
                    r.implied_vol, r.baseline_iv, r.deviation_pct, r.status,
                    r.volume, r.quote_time, r.price_source, r.reliable, now,
                )))
                count += 1
        self.conn.commit()
        return count

    # -- 寫入：IV歷史 --------------------------------------------------------

    def save_iv_history(self, rows: Iterable[IvHistoryRow]) -> int:
        """
        把某個交易時段的收盤IV寫進歷史庫，餵給之後的5日移動平均。
        用upsert，同一天重跑腳本不會產生重複資料。
        """
        sql = """
            INSERT INTO option_iv_history
                (trade_date, session, expiry_type, expiry_date, strike_price,
                 right_type, underlying_price, close_price, implied_vol)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                underlying_price = VALUES(underlying_price),
                close_price = VALUES(close_price),
                implied_vol = VALUES(implied_vol)
        """
        count = 0
        with self.cursor() as cur:
            stmt = self._translate(sql)
            for r in rows:
                cur.execute(stmt, tuple(_adapt(v) for v in (
                    r.trade_date, r.session, r.expiry_type, r.expiry_date,
                    r.strike_price, r.right_type, r.underlying_price,
                    r.close_price, r.implied_vol,
                )))
                count += 1
        self.conn.commit()
        return count

    # -- 讀取 ---------------------------------------------------------------

    def get_monthly_baseline_iv(
        self, strike_price: float, right_type: OptionType, session: Session,
        expiry_date: date, lookback_days: int = 5,
    ) -> Optional[float]:
        """
        月選的baseline：同履約價、同買賣權、同交易時段，最近N天IV的平均。

        回傳None代表歷史筆數不足(冷啟動階段)，呼叫端要自己決定fallback
        (run_live_pipeline.py 的做法是退回用偏斜曲線)。
        """
        rows = self.query(
            """
            SELECT implied_vol FROM option_iv_history
            WHERE strike_price = %s AND right_type = %s AND session = %s
              AND expiry_date = %s AND expiry_type = 'month'
            ORDER BY trade_date DESC
            LIMIT %s
            """,
            (strike_price, right_type, session, expiry_date, lookback_days),
        )
        if not rows:
            return None
        ivs = [float(r[0]) for r in rows]
        return sum(ivs) / len(ivs)

    def get_baseline_iv_batch(
        self, right_type: OptionType, session: Session, expiry_date: date,
        lookback_days: int = 5,
    ) -> dict[float, float]:
        """
        一次撈回整條鏈所有履約價的5日均IV。

        為什麼要有這個：排程每分鐘要處理幾百檔合約，
        如果每一檔都各發一次 get_monthly_baseline_iv 查詢，
        一分鐘內會打出幾百次DB往返，這在正式環境是不必要的負擔。
        改成一次撈回來在記憶體裡算，DB只需要一次查詢。
        """
        rows = self.query(
            """
            SELECT strike_price, trade_date, implied_vol
            FROM option_iv_history
            WHERE right_type = %s AND session = %s AND expiry_date = %s
              AND expiry_type = 'month'
            ORDER BY strike_price ASC, trade_date DESC
            """,
            (right_type, session, expiry_date),
        )
        buckets: dict[float, list[float]] = {}
        for strike, _trade_date, iv in rows:
            k = float(strike)
            lst = buckets.setdefault(k, [])
            if len(lst) < lookback_days:      # 已按日期倒序，取前N筆就是最近N天
                lst.append(float(iv))
        return {k: sum(v) / len(v) for k, v in buckets.items() if v}

    def load_live_quotes(
        self, expiry_type: Optional[ExpiryType] = None,
        expiry_date: Optional[date] = None,
        right_type: Optional[OptionType] = None,
        session: Optional[Session] = None,
        status: Optional[str] = None,
        only_reliable: bool = False,
    ) -> list[dict]:
        """
        前端API會用到的查詢：依條件撈出目前的判斷結果。
        全部參數都是選填，不給就是不篩。
        """
        where, params = [], []
        for col, val in (("expiry_type", expiry_type), ("expiry_date", expiry_date),
                         ("right_type", right_type), ("session", session),
                         ("status", status)):
            if val is not None:
                where.append(f"{col} = %s")
                params.append(val)
        if only_reliable:
            where.append("reliable = 1")

        sql = """
            SELECT expiry_type, expiry_date, strike_price, right_type, session,
                   underlying_price, market_price, fair_price, implied_vol,
                   baseline_iv, deviation_pct, status, volume, quote_time,
                   price_source, reliable, updated_at
            FROM option_live_quote
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY right_type ASC, strike_price ASC"

        cols = ["expiry_type", "expiry_date", "strike_price", "right_type", "session",
                "underlying_price", "market_price", "fair_price", "implied_vol",
                "baseline_iv", "deviation_pct", "status", "volume", "quote_time",
                "price_source", "reliable", "updated_at"]
        return [dict(zip(cols, row)) for row in self.query(sql, params)]

    def count(self, table: str) -> int:
        if table not in ("option_live_quote", "option_iv_history"):
            raise ValueError(f"不認得的表格名稱: {table}")
        return int(self.query(f"SELECT COUNT(*) FROM {table}")[0][0])


# ---------------------------------------------------------------------------
# 自我測試(用SQLite，不需要MySQL server)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta

    print("=" * 72)
    print("測試1：建表")
    print("=" * 72)
    db = OptionDB.connect_sqlite(":memory:")
    db.create_tables()
    db.create_tables()      # 重複執行不該出錯(IF NOT EXISTS)
    assert db.count("option_live_quote") == 0
    assert db.count("option_iv_history") == 0
    print("  ✓ 兩張表建立成功，重複建表不會出錯")
    print("✅ 建表測試通過\n")

    print("=" * 72)
    print("測試2：SQL方言轉換")
    print("=" * 72)
    sqlite_db = OptionDB.connect_sqlite(":memory:")
    mysql_like = OptionDB(None, "mysql")

    s1 = "SELECT * FROM t WHERE a = %s AND b = %s"
    assert mysql_like._translate(s1) == s1, "MySQL方言不該被改動"
    assert sqlite_db._translate(s1) == "SELECT * FROM t WHERE a = ? AND b = ?"
    print("  ✓ 佔位符 %s → ?")

    s2 = ("INSERT INTO t (a,b) VALUES (%s,%s) "
          "ON DUPLICATE KEY UPDATE b = VALUES(b), c = VALUES(c)")
    out = sqlite_db._translate(s2)
    assert "ON CONFLICT DO UPDATE SET" in out, out
    assert "excluded.b" in out and "excluded.c" in out, out
    assert "VALUES(b)" not in out, out
    print("  ✓ ON DUPLICATE KEY UPDATE → ON CONFLICT DO UPDATE SET")
    print(f"    轉換結果: {out.strip()}")
    print("✅ 方言轉換測試通過\n")

    print("=" * 72)
    print("測試3：即時報價寫入與upsert")
    print("=" * 72)
    exp = date(2026, 9, 16)
    rows = [
        LiveQuoteRow("month", exp, 45000, "C", "day", 45085.0, 300.0, 290.0,
                     0.2900, 0.2850, 3.45, "fair", 120, "134500", "last", True),
        LiveQuoteRow("month", exp, 45100, "C", "day", 45085.0, 250.0, 230.0,
                     0.3000, 0.2840, 8.70, "expensive", 80, "134501", "last", True),
        LiveQuoteRow("month", exp, 45000, "P", "day", 45085.0, 210.0, 220.0,
                     0.2800, 0.2860, -4.55, "fair", 60, "134502", "mid", True),
    ]
    n = db.save_live_quotes(rows)
    assert n == 3 and db.count("option_live_quote") == 3
    print(f"  ✓ 寫入 {n} 筆")

    # 同一個key再寫一次，應該是更新不是新增
    rows[0].market_price = 305.0
    rows[0].status = "expensive"
    db.save_live_quotes([rows[0]])
    assert db.count("option_live_quote") == 3, "upsert不該增加筆數"
    got = db.load_live_quotes(expiry_type="month", right_type="C")
    row0 = [r for r in got if float(r["strike_price"]) == 45000][0]
    assert float(row0["market_price"]) == 305.0, "upsert沒有更新到值"
    assert row0["status"] == "expensive"
    print("  ✓ 重複寫入同一合約 → 更新而非新增(upsert正確)")

    # 日盤/夜盤要能分開存(session在主鍵裡)
    night = LiveQuoteRow("month", exp, 45000, "C", "night", 44475.0, 280.0, 275.0,
                         0.2950, 0.2900, 1.82, "fair", 40, "231500", "last", True)
    db.save_live_quotes([night])
    assert db.count("option_live_quote") == 4, "日夜盤應該分開存，不該互相覆蓋"
    print("  ✓ 日盤/夜盤分開儲存(session已納入主鍵)")
    print("✅ 即時報價寫入測試通過\n")

    print("=" * 72)
    print("測試4：查詢與篩選")
    print("=" * 72)
    assert len(db.load_live_quotes()) == 4
    assert len(db.load_live_quotes(right_type="C")) == 3
    assert len(db.load_live_quotes(right_type="P")) == 1
    assert len(db.load_live_quotes(session="night")) == 1
    assert len(db.load_live_quotes(status="expensive")) == 2
    print("  ✓ 依買賣權/盤別/狀態篩選都正確")

    db.save_live_quotes([LiveQuoteRow(
        "month", exp, 55000, "C", "day", 45085.0, 0.1, 0.0, 1.5, 0.30,
        999.99, "expensive", 1, "134503", "last", False)])   # reliable=False
    assert len(db.load_live_quotes(only_reliable=True)) == 4
    assert len(db.load_live_quotes()) == 5
    print("  ✓ only_reliable 能濾掉不可信的判斷")

    # 極端偏差值要能存進去(這就是為什麼欄位要開DECIMAL(8,2)而不是(6,4))
    extreme = db.load_live_quotes(only_reliable=False)
    ext = [r for r in extreme if float(r["strike_price"]) == 55000][0]
    assert abs(float(ext["deviation_pct"]) - 999.99) < 0.01
    print("  ✓ 偏差999.99%能正確存取(原schema的DECIMAL(6,4)會溢位)")
    print("✅ 查詢測試通過\n")

    print("=" * 72)
    print("測試5：IV歷史與5日移動平均")
    print("=" * 72)
    base = date(2026, 8, 10)
    ivs = [0.14, 0.15, 0.16, 0.145, 0.155]      # 平均 0.15
    hist = [IvHistoryRow(base + timedelta(days=i), "day", "month", exp,
                         17500, "C", 17480, 150.0, iv) for i, iv in enumerate(ivs)]
    assert db.save_iv_history(hist) == 5
    got = db.get_monthly_baseline_iv(17500, "C", "day", exp)
    print(f"  5日均IV = {got:.6f} (應為 0.150000)")
    assert abs(got - 0.15) < 1e-9

    # 只取最近5天：多塞3天更早的資料，結果不該被影響
    older = [IvHistoryRow(base - timedelta(days=i + 1), "day", "month", exp,
                          17500, "C", 17480, 150.0, 0.99) for i in range(3)]
    db.save_iv_history(older)
    got2 = db.get_monthly_baseline_iv(17500, "C", "day", exp)
    print(f"  塞入3筆更早的異常資料(IV=0.99)後 → {got2:.6f} (應仍為 0.150000)")
    assert abs(got2 - 0.15) < 1e-9, "應該只取最近5天"

    # upsert：同一天重寫應更新不新增
    n_before = db.count("option_iv_history")
    db.save_iv_history([IvHistoryRow(base, "day", "month", exp, 17500, "C",
                                     17480, 150.0, 0.20)])
    assert db.count("option_iv_history") == n_before, "同一天重寫不該新增筆數"
    print("  ✓ 同一天重跑腳本不會產生重複資料")

    # 冷啟動：查沒有資料的履約價要回None
    assert db.get_monthly_baseline_iv(99999, "C", "day", exp) is None
    print("  ✓ 查無歷史時回傳None(讓呼叫端決定fallback)")
    print("✅ IV歷史測試通過\n")

    print("=" * 72)
    print("測試6：批次撈baseline(避免每檔各查一次DB)")
    print("=" * 72)
    more = []
    for strike, center in ((17600, 0.16), (17700, 0.17)):
        for i, d in enumerate([base + timedelta(days=j) for j in range(5)]):
            more.append(IvHistoryRow(d, "day", "month", exp, strike, "C",
                                     17480, 150.0, center))
    db.save_iv_history(more)
    batch = db.get_baseline_iv_batch("C", "day", exp)
    print(f"  一次撈回 {len(batch)} 個履約價的5日均: "
          f"{ {k: round(v, 4) for k, v in sorted(batch.items())} }")

    # 17500 的預期值要把測試5最後那次upsert算進去：
    # 原本5天是 [0.14, 0.15, 0.16, 0.145, 0.155]，
    # 後來把第一天(base)從0.14改成0.20，所以平均是 0.162 而不是 0.15。
    expected_17500 = (0.20 + 0.15 + 0.16 + 0.145 + 0.155) / 5
    assert abs(batch[17500] - expected_17500) < 1e-9, \
        f"17500的5日均應為{expected_17500}，實際{batch[17500]}"
    assert abs(batch[17600] - 0.16) < 1e-9
    assert abs(batch[17700] - 0.17) < 1e-9
    print(f"  ✓ 17500={batch[17500]:.4f}(含測試5的upsert), "
          f"17600={batch[17600]:.4f}, 17700={batch[17700]:.4f}")

    # 最重要的不變式：批次撈跟逐筆查，結果必須完全一致。
    # (批次是為了效能才存在的，一旦跟逐筆算出不同答案就失去意義)
    for k in batch:
        single = db.get_monthly_baseline_iv(k, "C", "day", exp)
        assert abs(single - batch[k]) < 1e-9, \
            f"批次與逐筆結果不一致 K={k}: 批次{batch[k]} vs 逐筆{single}"
    print("  ✓ 批次結果與逐筆查詢完全一致(這是批次版本存在的前提)")
    print("✅ 批次撈取測試通過\n")

    db.close()
    print("=" * 72)
    print("全部測試通過 ✅  (以上用SQLite驗證邏輯；正式環境用MySQL跑的是同一套程式碼路徑)")
    print("=" * 72)
