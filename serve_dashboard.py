# -*- coding: utf-8 -*-
"""
serve_dashboard.py

把 options_dashboard.html 接上真實資料的小型後端
------------------------------------------------
前端(options_dashboard.html)本來就是照「打兩支API」寫的，這支程式就是那兩支API：

    GET /                       → 送出 options_dashboard.html
    GET /api/expiries?type=month|week
                                → [{id, label, expiryDate, days, type}, ...]
    GET /api/quotes?type=..&expiry=..
                                → {underlying:{...}, atmStrike, rows:[...], meta:{...}}

計算完全走 pricing_service.analyze_chain()，跟 CLI(run_live_pipeline.py) 是同一套邏輯，
所以網頁上看到的判斷跟指令列跑出來的會一致。

用法：
    python serve_dashboard.py                    # http://127.0.0.1:8000
    python serve_dashboard.py --port 8080
    python serve_dashboard.py --session night    # 強制用夜盤資料
    python serve_dashboard.py --window 12        # ATM上下各12檔(預設10)
    python serve_dashboard.py --db quotes.db --write-db
                                                 # 順便把每次更新寫進 option_live_quote，
                                                 # 累積幾天之後月選就會自動改用5日IV均當基準

刻意只用Python標準函式庫(http.server)，不引入Flask/FastAPI —— 這是一個人在本機看的
儀表板，每分鐘一次請求，多裝一個框架只是多一份要維護的東西。

注意事項：
  - 期交所的API有速率考量，前端每60秒更新一次，後端這邊再加一層TTL快取(預設20秒)，
    重新整理網頁、或多開分頁都不會變成對期交所連發請求。
  - 一次更新裡Call跟Put用「同一個F、同一批報價」，兩邊的合理價才對得起來。
  - 只監聽 127.0.0.1，沒有做任何身分驗證，不要直接開到公網上。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse, parse_qs

from db import OptionDB, LiveQuoteRow
from pricing_service import (
    ChainAnalysis,
    NoQuoteDataError,
    analyze_chain,
    pick_contract,
)
from run_live_pipeline import detect_session, is_market_open
from taifex_fetch import (
    ContractMonth,
    TaifexApiError,
    fetch_live_quotes,
    fetch_underlying_quote,
    list_contract_months,
)

HERE = Path(__file__).resolve().parent
HTML_FILE = HERE / "options_dashboard.html"

MONTHS_TTL = 300.0     # 掛牌到期別清單很少變，5分鐘撈一次就夠
DEFAULT_QUOTES_TTL = 20.0


# ---------------------------------------------------------------------------
# 快取
# ---------------------------------------------------------------------------
# 為什麼需要：前端60秒更新一次，但使用者重新整理、切到期別、多開分頁都會打進來。
# 沒有快取的話，一個下午就會對期交所送出遠比需要更多的請求。
#
# _lock 同時也把「對期交所抓資料 + 用資料庫連線」整段序列化了：
# ThreadingHTTPServer 每個請求一條執行緒，而 sqlite3/pymysql 的連線都不是
# 天生執行緒安全的，序列化最省事也最不會出錯(反正就一個人在看)。

class TTLCache:
    def __init__(self) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        # 必須是RLock不是Lock：快取的計算函式裡面還會再查快取
        # (quotes_payload 要先拿到期別清單)，用普通Lock會當場自己鎖死自己
        self._lock = threading.RLock()

    def get_or_compute(self, key: Any, ttl: float, fn) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and (time.monotonic() - hit[0]) < ttl:
                return hit[1]
            value = fn()
            self._data[key] = (time.monotonic(), value)
            return value


_cache = TTLCache()


# ---------------------------------------------------------------------------
# 執行參數(由 main() 填入，handler 讀)
# ---------------------------------------------------------------------------

class Config:
    session: Optional[Literal["day", "night"]] = None   # None=依現在時間自動判斷
    window: int = 10
    min_volume: int = 0
    max_age_sec: Optional[int] = None
    quotes_ttl: float = DEFAULT_QUOTES_TTL
    db: Optional[OptionDB] = None
    write_db: bool = False


CFG = Config()


def current_session() -> Literal["day", "night"]:
    return CFG.session or detect_session()


# ---------------------------------------------------------------------------
# 資料組裝
# ---------------------------------------------------------------------------

def _hhmmss(raw: str) -> str:
    """API的CTime是 "134500" 這種格式，轉成 "13:45:00" 給人看"""
    s = (raw or "").strip().zfill(6)
    if len(s) != 6 or not s.isdigit():
        return "--"
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def expiry_label(m: ContractMonth) -> str:
    """
    到期別在畫面上的名稱。跨年的月選要標年份，不然「3月」看不出是明年的。

    週選一定要標星期：同一週會有週三(W系列)跟週五(F系列)兩個合約，
    只寫「09/02 (5天)」看不出是哪一組。週別代碼(W1/F1)由前端另外做成標籤，
    所以這裡不重複寫。
    """
    days = (m.expiry_date - date.today()).days
    if m.expiry_type == "month":
        year = "" if m.expiry_date.year == date.today().year else f"{m.expiry_date.year % 100}年"
        return f"{year}{m.expiry_date.month}月 ({m.expiry_date:%m/%d} {m.weekday_zh})"
    when = "今天到期" if days <= 0 else ("明天" if days == 1 else f"{days}天")
    return f"{m.expiry_date:%m/%d} {m.weekday_zh} ({when})"


def get_months(session: str) -> list[ContractMonth]:
    return _cache.get_or_compute(
        ("months", session), MONTHS_TTL,
        lambda: list_contract_months("TXO", session=session),
    )


def expiries_payload(expiry_type: Literal["week", "month"]) -> list[dict]:
    session = current_session()
    months = [m for m in get_months(session) if m.expiry_type == expiry_type]
    months.sort(key=lambda m: m.expiry_date)
    return [
        {
            "id": m.code,
            "label": expiry_label(m),
            "expiryDate": m.expiry_date.isoformat(),
            "days": (m.expiry_date - date.today()).days,
            "type": m.expiry_type,
            # 週選才有：W1=第1個週三、F1=第1個週五(月選是null)
            "weekCode": m.week_code,
            "weekday": m.weekday_zh,          # 例如 "週三"
            "kindLabel": m.kind_label,        # 例如 "週選W1(週三)"
        }
        for m in months
    ]


def _side_payload(e) -> dict:
    """單邊(Call或Put)一格的資料。前端只認 price/status/fairPrice，其餘是附加資訊。"""
    return {
        "price": round(e.market_price, 2),
        "status": e.status,
        "fairPrice": round(e.fair_price, 2),
        "deviationPct": round(e.deviation_pct, 1),
        "iv": round(e.implied_vol, 4),
        "baselineIv": round(e.baseline_iv, 4),
        "volume": e.volume,
        "priceSource": e.price_source,   # last=成交價 / mid=買賣中價
        "ageSec": e.age_sec,
        "stale": e.stale,                # 報價超過10分鐘，IV參考價值低
        "reliable": e.reliable,          # False=偏差百分比失去意義，不要當訊號看
    }


def _chain_meta(side: Optional[ChainAnalysis]) -> dict:
    if side is None:
        return {"available": False}
    curve = side.curve
    return {
        "available": True,
        "baselineSource": side.baseline_source,
        "baselineQualityNote": side.baseline_quality_note,
        "curveRejected": side.curve_rejected,
        "curve": None if curve is None else {
            "atmIv": round(curve.atm_iv(), 4),
            "skewSlope": round(curve.skew_slope, 4),
            "nUsed": curve.n_used,
            "nDropped": curve.n_dropped,
            "rmse": round(curve.rmse, 4),
        },
        "atmIv": round(side.baseline_iv_atm, 4),
        # counts=全鏈(含不可信的)、countsReliable=只算可信的。
        # 畫面統計行要用 countsReliable，不然表頭說「偏貴6檔」但表格裡只有2檔紅的。
        "counts": side.counts,
        "countsReliable": side.reliable_counts,
        "nEvaluated": len(side.evals),
        "nFailedIv": side.n_failed_iv,
        "nUnreliable": side.n_unreliable,
        "dbBaselineCount": side.db_baseline_count,
    }


def _save_to_db(target: ContractMonth, session: str, F: float,
                sides: dict[str, Optional[ChainAnalysis]]) -> None:
    """把這次算出來的全鏈寫進 option_live_quote(月選累積幾天就有5日IV均可用)"""
    rows = []
    for right, side in sides.items():
        if side is None:
            continue
        rows.extend(
            LiveQuoteRow(
                expiry_type=target.expiry_type,
                expiry_date=target.expiry_date,
                strike_price=e.strike,
                right_type=right,
                session=session,
                underlying_price=F,
                market_price=e.market_price,
                fair_price=e.fair_price,
                implied_vol=e.implied_vol,
                baseline_iv=e.baseline_iv,
                deviation_pct=e.deviation_pct,
                status=e.status,
                volume=e.volume,
                quote_time=e.quote_time,
                price_source=e.price_source,
                reliable=e.reliable,
            )
            for e in side.evals
        )
    if rows:
        CFG.db.save_live_quotes(rows)


def quotes_payload(expiry_type: Literal["week", "month"],
                   expiry_code: Optional[str],
                   window: int) -> dict:
    """
    組出前端要的一整包資料。

    Call跟Put是「同一次抓的報價、同一個F」算出來的 —— 分兩次抓的話，
    中間台指期跑掉幾點，兩邊的合理價就對不起來了。
    """
    session = current_session()
    months = get_months(session)
    target = pick_contract(months, expiry_code, expiry_type)
    if target is None:
        raise LookupError(f"找不到到期別 {expiry_code or f'(最近的{expiry_type})'}")

    u = fetch_underlying_quote(session=session)
    quotes = fetch_live_quotes(
        target.expiry_type, target.expiry_date,
        session=session, expire_month=target.code,     # 不指定right_type，call+put一次拿
    )

    sides: dict[str, Optional[ChainAnalysis]] = {}
    errors: dict[str, str] = {}
    for right in ("C", "P"):
        try:
            sides[right] = analyze_chain(
                target, right, session, underlying_price=u.price, quotes=quotes,
                min_volume=CFG.min_volume, max_age_sec=CFG.max_age_sec, db=CFG.db,
            )
        except NoQuoteDataError as exc:
            # 一邊算不出來(例如深夜週選整排沒報價)不該讓另一邊也看不到
            sides[right] = None
            errors[right] = str(exc)

    if sides["C"] is None and sides["P"] is None:
        raise NoQuoteDataError(" / ".join(errors.values()) or "沒有可用的報價")

    if CFG.write_db and CFG.db is not None:
        try:
            _save_to_db(target, session, u.price, sides)
        except Exception as exc:      # 寫不進去不該讓畫面掛掉
            errors["db"] = f"{type(exc).__name__}: {exc}"

    # 用ATM附近的視窗，Call跟Put各取一段之後取聯集(兩邊的履約價通常一樣，
    # 但流動性差的時候會有單邊算不出IV的情況)
    call_map = {e.strike: e for e in (sides["C"].window(window) if sides["C"] else [])}
    put_map = {e.strike: e for e in (sides["P"].window(window) if sides["P"] else [])}

    rows = [
        {
            "strike": int(k) if float(k).is_integer() else k,
            "call": _side_payload(call_map[k]) if k in call_map else None,
            "put": _side_payload(put_map[k]) if k in put_map else None,
        }
        for k in sorted(set(call_map) | set(put_map))
    ]

    ref_side = sides["C"] or sides["P"]
    change_ref = u.ref_price if u.ref_price > 0 else u.price

    return {
        "underlying": {
            "price": u.price,
            "prevClose": change_ref,     # 期交所給的參考價=前一交易日結算價
            "updatedAt": _hhmmss(u.quote_time),
            "priceSource": u.price_source,
        },
        "atmStrike": ref_side.atm_strike,
        "rows": rows,
        "meta": {
            "session": session,
            "marketOpen": is_market_open(),
            "expiry": {
                "id": target.code,
                "label": expiry_label(target),
                "type": target.expiry_type,
                "weekCode": target.week_code,
                "weekday": target.weekday_zh,
                "kindLabel": target.kind_label,
                "date": target.expiry_date.isoformat(),
                "days": ref_side.days_to_expiry,
            },
            "call": _chain_meta(sides["C"]),
            "put": _chain_meta(sides["P"]),
            "filters": {"minVolume": CFG.min_volume, "maxAgeSec": CFG.max_age_sec},
            "serverTime": datetime.now().strftime("%H:%M:%S"),
            "errors": errors or None,
            "disclaimer": "判斷僅供參考，不構成投資建議；沒成交量的報價可靠度低",
        },
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "TxoDashboard/1.0"

    # 預設的log每個請求印一行，60秒一次的輪詢會把畫面洗掉，只留錯誤
    def log_message(self, fmt: str, *args) -> None:
        pass

    def log_error(self, fmt: str, *args) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {self.address_string()} {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # 使用者在載入中途按了重新整理，正常現象

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        def arg(name: str, default: Optional[str] = None) -> Optional[str]:
            v = query.get(name, [default])[0]
            return v if v not in ("", None) else default

        try:
            if path in ("/", "/index.html", "/options_dashboard.html"):
                if not HTML_FILE.exists():
                    self._send(404, f"找不到 {HTML_FILE.name}".encode("utf-8"),
                               "text/plain; charset=utf-8")
                    return
                # 每次都重讀檔案，改完HTML重新整理就看得到，不用重啟server
                self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
                return

            if path == "/api/expiries":
                etype = arg("type", "month")
                if etype not in ("month", "week"):
                    self._send_json({"error": "type 只能是 month 或 week"}, 400)
                    return
                data = _cache.get_or_compute(
                    ("expiries", etype, current_session()), MONTHS_TTL,
                    lambda: expiries_payload(etype),
                )
                if not data:
                    self._send_json({"error": f"目前沒有掛牌中的{etype}合約"}, 503)
                    return
                self._send_json(data)
                return

            if path == "/api/quotes":
                etype = arg("type", "month")
                if etype not in ("month", "week"):
                    self._send_json({"error": "type 只能是 month 或 week"}, 400)
                    return
                code = arg("expiry")
                # 夾在合理範圍內：負數會讓 ChainAnalysis.window() 切出空清單
                # (實測 ?window=-3 回傳0列，畫面空白且沒有任何錯誤訊息)，
                # 過大的值則等於把幾百檔全塞給前端。順便也擋住「用任意window值
                # 灌爆TTL快取、每個值各打一次期交所」這種繞過快取的用法。
                window = max(0, min(int(arg("window", str(CFG.window))), 100))
                key = ("quotes", current_session(), etype, code, window,
                       CFG.min_volume, CFG.max_age_sec)
                data = _cache.get_or_compute(
                    key, CFG.quotes_ttl,
                    lambda: quotes_payload(etype, code, window),
                )
                self._send_json(data)
                return

            if path == "/api/health":
                self._send_json({
                    "ok": True,
                    "session": current_session(),
                    "marketOpen": is_market_open(),
                    "serverTime": datetime.now().isoformat(timespec="seconds"),
                })
                return

            self._send_json({"error": f"沒有這個路徑: {path}"}, 404)

        except LookupError as e:
            self._send_json({"error": str(e)}, 404)
        except NoQuoteDataError as e:
            self._send_json({"error": str(e)}, 503)
        except TaifexApiError as e:
            self._send_json({"error": f"期交所API錯誤: {e}"}, 502)
        except ValueError as e:
            self._send_json({"error": f"參數錯誤: {e}"}, 400)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)


def main() -> None:
    # Windows的終端機預設是cp950(Big5)，印到 ⚠ 這種不在Big5裡的字會直接
    # UnicodeEncodeError 讓程式掛掉。改成印不出來的字用?代替，不要因為一個符號而中斷。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="台指選擇權貴賤判斷 — 網頁儀表板後端")
    p.add_argument("--host", default="127.0.0.1", help="監聽位址 (預設只開本機)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--session", choices=["day", "night"], default=None,
                   help="強制指定盤別 (預設依現在時間自動判斷)")
    p.add_argument("--window", type=int, default=10, help="ATM上下各給幾檔 (預設10)")
    p.add_argument("--min-volume", type=int, default=0, help="只看成交量>=N的合約")
    p.add_argument("--max-age", type=int, default=None,
                   help="只看報價在N秒內的合約(例如300=5分鐘)")
    p.add_argument("--ttl", type=float, default=DEFAULT_QUOTES_TTL,
                   help=f"報價快取秒數 (預設{DEFAULT_QUOTES_TTL:.0f}，避免對期交所連發請求)")
    p.add_argument("--db", default=None,
                   help="連資料庫。給 'env' 讀環境變數(MySQL)，或給檔案路徑用SQLite")
    p.add_argument("--write-db", action="store_true",
                   help="每次更新順便寫進 option_live_quote (需搭配 --db)")
    p.add_argument("--open", action="store_true", help="啟動後自動開瀏覽器")
    args = p.parse_args()

    CFG.session = args.session
    CFG.window = args.window
    CFG.min_volume = args.min_volume
    CFG.max_age_sec = args.max_age
    CFG.quotes_ttl = args.ttl
    CFG.write_db = args.write_db

    if args.db:
        try:
            if args.db == "env":
                CFG.db = OptionDB.connect_from_env()
            else:
                # ThreadingHTTPServer 是一個請求一條執行緒，sqlite預設會擋跨執行緒使用。
                # 所有DB存取都在 TTLCache 的鎖裡序列化，所以關掉這個檢查是安全的。
                conn = sqlite3.connect(args.db, check_same_thread=False)
                conn.execute("PRAGMA foreign_keys = ON")
                CFG.db = OptionDB(conn, "sqlite")
                CFG.db.create_tables()
        except Exception as e:
            print(f"資料庫連線失敗: {type(e).__name__}: {e}")
            return
    elif args.write_db:
        print("--write-db 需要搭配 --db 指定資料庫")
        return

    url = f"http://{args.host}:{args.port}/"
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    print("=" * 62)
    print("台指選擇權 貴賤判斷 — 網頁版")
    print("=" * 62)
    print(f"網址      : {url}")
    print(f"盤別      : {CFG.session or '自動判斷 (現在是 ' + detect_session() + ')'}"
          f"{'' if is_market_open() else '   ⚠ 目前非交易時段，看到的是上一盤最後報價'}")
    print(f"顯示範圍  : ATM上下各 {CFG.window} 檔")
    print(f"快取      : 報價 {CFG.quotes_ttl:.0f} 秒 / 到期別清單 {MONTHS_TTL:.0f} 秒")
    if CFG.db is not None:
        print(f"資料庫    : {args.db} ({CFG.db.dialect}){' 每次更新寫入' if CFG.write_db else ' 唯讀(月選5日均基準)'}")
    print("按 Ctrl+C 結束")
    print("=" * 62)

    if args.open:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        if CFG.db is not None:
            CFG.db.close()


if __name__ == "__main__":
    main()
