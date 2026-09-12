# CLAUDE.md — 台指選擇權貴賤判斷工具

給 Claude Code 的專案指南。使用說明、實測數據、踩坑紀錄在 `README.md`(很詳細，動到對應
模組時先去讀那一章)。這份只寫「動手前要先知道的事」。

## 這是什麼

抓期交所台指選擇權即時報價 → Black-76 反推 IV → 跟基準 IV 比 → 判斷每檔**偏貴/合理/便宜**。
有 CLI 表格版跟網頁儀表板兩種介面，計算共用同一個核心。

## 架構

```
options_dashboard.html      畫面(純HTML/CSS/JS，無框架、無打包工具)
        │ fetch /api/expiries, /api/quotes
serve_dashboard.py          HTTP後端(標準函式庫 http.server + TTL快取)
        │                             ┌─ run_live_pipeline.py  CLI表格版
        └──────► pricing_service.py ◄─┘  ← 判斷邏輯只有這一份
                        │
     ┌──────────────────┼───────────────────┬─────────────┐
taifex_fetch.py    black76_iv.py       iv_skew.py      db.py
(期交所API)        (定價/反推IV/判斷)   (偏斜曲線基準)   (MySQL/SQLite)
                                       iv_baseline.py
                                       (5日均/ATM基準)
```

| 檔案 | 用途 |
|---|---|
| `pricing_service.py` | **計算核心** `analyze_chain()`，不印任何字、只回 `ChainAnalysis` |
| `run_live_pipeline.py` | CLI，只負責把 `ChainAnalysis` 印成表格 + 寫DB |
| `serve_dashboard.py` | 網頁後端，`/api/expiries`、`/api/quotes`、`/api/health` |
| `options_dashboard.html` | 前端，60秒自動更新 |
| `taifex_fetch.py` | 期交所 mis API + 歷史回補；到期別代碼解析(`parse_expiry_code` / `ContractMonth`) |
| `black76_iv.py` | 定價、反推IV、`evaluate_option()` 貴賤判斷 |
| `iv_skew.py` | 波動率偏斜曲線配適(消除skew假訊號) |
| `iv_baseline.py` | 月選5日均 / 週選ATM基準 |
| `db.py` | `OptionDB`，SQL寫MySQL方言、跑SQLite時自動轉換 |
| `run_demo_pipeline.py` / `mock_live_quotes.py` | 離線demo，不需網路 |

## 常用指令

```bash
# 網頁版(預設 http://127.0.0.1:8000)
python serve_dashboard.py --open
python serve_dashboard.py --db quotes.db --write-db   # 順便累積歷史，養月選的5日均基準

# CLI版
python run_live_pipeline.py --list
python run_live_pipeline.py --expiry 202609 --right P --window 8
python run_live_pipeline.py --min-volume 10 --max-age 300    # 盤中建議這樣用

# 自我測試(前四個不需網路)
python black76_iv.py && python iv_baseline.py && python iv_skew.py
python db.py && python run_demo_pipeline.py
python taifex_fetch.py        # 這個會實際連期交所
```

## 動手前的硬規則

1. **判斷邏輯只能改 `pricing_service.py`**。CLI 跟網頁後端都走 `analyze_chain()`，
   不要在任何一邊複製一份計算 —— 基準IV優先序、陳舊報價過濾這些規則走鐘的代價很大。
2. **基準優先序不要動**：資料庫5日均 > 偏斜曲線 > ATM單點。
   挑基準的報價池必須是「15分鐘內 + 有成交量」(`BASELINE_MAX_AGE`/`BASELINE_MIN_VOL`)。
   實測基準被一筆3.6小時前的報價污染，整條鏈幾乎每檔都被誤標偏貴。
3. **Call/Put 必須用同一個 F、同一批報價**。所以 `analyze_chain()` 的 `underlying_price`
   是呼叫端傳進去的，不是它自己抓。
4. **資料品質不能靜靜吞掉**。陳舊報價、深價外、曲線外插出來的基準，都要標記
   (`stale` / `reliable=False`)讓畫面淡化顯示，不要當成可用訊號印出來。
5. **不要把 server 綁到 0.0.0.0**。目前沒有任何身分驗證。
6. **週選有兩組：W=週三結算、F=週五結算**，期交所交錯掛牌，一個月長這樣(2026/09)：

   ```
   W1 09/02(三)  F1 09/04(五)  W2 09/09(三)  F2 09/11(五)
   月選 09/16(三)              F3 09/18(五)
   W4 09/23(三)  F4 09/25(五)  W5 09/30(三)
   ```

   - **沒有W3**：當月第三個週三是月選的結算日，那一週的週三合約就是月選本身。
   - 畫面跟CLI一定要標出**週別代碼+星期**，只寫「週選」使用者分不出是週三還是週五那檔。
   - 代碼解析走 `taifex_fetch.parse_expiry_code()`，結果放 `ContractMonth.week_code`
     ("W1"/"F1"，月選是 `None`)；星期幾一律用 `ContractMonth.weekday_zh`
     (**從真實到期日推**，不要從代碼字母猜)。程式碼不寫死只認 W/F：**有字母就是週選**，
     期交所之後加別的星期(例如週一)會冒出新字母，靠字母猜就會標錯。
   - DB 的 `expiry_type` 維持只有 `week`/`month`：W 跟 F 的 `expiry_date` 不會相同，
     主鍵已經分得開，不要為了這個改schema。
   - 細節見 README「到期別代碼怎麼看」。
7. **畫面上「合理」是刻意不標徽章的**，不要當成 bug 去「修」。
   語意是：只標例外，不標正常。三種狀態在畫面上長這樣 ——

   | 判斷 | 畫面 |
   |---|---|
   | 偏貴 | 紅色徽章 + 左邊顯示合理價 |
   | 便宜 | 綠色徽章 + 右邊顯示合理價 |
   | 合理 | **只有價格數字**，沒徽章、合理價欄留白 |
   | 沒資料 | 價格顯示 `—` |

   「合理」跟「沒資料」的差別**只在有沒有價格數字**。動 `statusBadge()` 或
   `updateSide()` 時千萬別把這個區別弄不見了。實測 2026/09/08 夜盤 ATM±10 檔
   42 格裡有 35 格是合理 —— 大部分時候市場定價本來就合理，隨便就標得出偏貴
   反而代表基準算錯了。
8. **下面這幾個地方是踩過坑修好的，看起來多餘但不要「簡化」回去**：
   - `taifex_fetch.CLOCK_SKEW_TOLERANCE`：報價時間比本機快幾秒是時鐘沒對齊，
     不是跨午夜。少了這段容忍，差1秒就變成 age=86399，當下正在成交的那幾檔
     會被當成最陳舊的報價整批丟掉。
   - `pricing_service` 的 `n_out_of_range += 1` 必須在 `db_baseline` 覆寫**之後**，
     不然計數器會大於 `n_unreliable`，CLI 的 `n_tiny` 變負數。
   - `counts`(全鏈) vs `reliable_counts`(只算可信)：統計行一律用後者，
     不然表頭說「偏貴6檔」但表格裡只有2檔紅的。
   - `db.get_baseline_iv_batch()` 的 `trade_date >=` 下限 + `pricing_service` 的
     `BASELINE_DB_MAX_STALE_DAYS` 整組退掉，是兩道不同的守門，兩道都要在：
     下限擋「平均裡混進兩個月前的單筆」，整組退擋「排程斷了、五筆一起變舊」。
     只留一道會漏掉另一種。
   - `pricing_service` 沒有曲線時走的 `_within_atm_range()`：ATM單點對每個履約價
     都給同一個IV，深價外的翹尾完全沒被吸收。守門不能因為「資料更差、配不出曲線」
     就消失 —— 那正是最該擋的時候。
   - `backfill_monthly_iv_history()` 的 `underlying_close_fn` 是必填的，
     沒有它就只能拿履約價當F，反推出來的IV是假的 —— 而那批數字會被
     `analyze_chain()` 當成最高優先序的基準靜默採用。

## 環境與工具注意事項

- Windows 11 / Python 3.14 / 已裝 scipy、pandas、requests、pymysql。
- **終端機是 cp950(Big5)**：`⚠`、`・` 這類字直接 print 會 `UnicodeEncodeError` 中斷。
  `run_live_pipeline.py` 跟 `serve_dashboard.py` 的 `main()` 已加
  `stream.reconfigure(errors="replace")`，新寫的進入點記得比照辦理。
  各模組的 `__main__` 自我測試還沒加，用管線跑它們時要帶 `PYTHONIOENCODING=utf-8`，
  不然會假性失敗(測試其實是過的，只是印不出字)。
- **Bash 工具不要用 `&` 背景執行**(整個呼叫會卡到 timeout)，用 `run_in_background` 參數。
- 前端 JS 改完可以用 `node --check` 驗語法；要驗渲染流程可以抽出 `<script>` 內容，
  用最小 DOM stub 在 node 裡跑一遍(比開瀏覽器快)。
- 期交所 API 有速率考量：後端已有 TTL 快取(報價20秒/到期別5分鐘)，debug 時不要繞過它連打。
- **版控**：https://github.com/SamuelPeng60/option_calculate (public，branch `main`)。
  `.gitignore` 擋掉 `*.db`(本機報價快照)、`.claude/settings.local.json`、`_*_tmp.py`。
  還**沒加 LICENSE** —— 公開 repo 沒授權條款等於「保留所有權利」，別人不能合法使用。

## 下次接手先看這裡 (更新於 2026-09-12)

**下一個日盤：2026-09-14(一) 08:45–13:45**，那才是這個工具真正該用的時段。

⚠ 2026-09-11 做過一次全面邏輯檢查，找到 8 個問題(3 個會實際誤標)。
**2026-09-12 已修掉 A 跟 C(兩個紅的)**，剩 6 個，
**動任何計算之前先看下面「下次要做：修剩下這 6 個邏輯問題」那一節** ——
還沒修的紅色是 **B(到期日當天 T)**，那個要先決定 T 怎麼算。
離線自我測試 5 支當時全過 —— 那些問題測試測不到。

起 server：

```bash
python serve_dashboard.py --host 127.0.0.1 --port 8000   # 然後開 http://127.0.0.1:8000
python serve_dashboard.py --min-volume 10 --max-age 600   # 想看乾淨訊號時
```

### 2026-09-08 00:08 夜盤實測到什麼

台指期 47320(前結算 47462)，月選 202609 剩8天，ATM=47300，基準走偏斜曲線。

**夜盤的資料幾乎不能用**：ATM±10 檔裡絕大多數報價是 3–8 小時前的(畫面標 `*` 淡化)，
那幾個「偏貴」全是陳舊報價造成的假訊號，量只有 1~5 口：

```
47150 Call 850.0 偏貴+10.4%  量1  8.7時前
47450 Call 675.0 偏貴 +9.7%  量4  6.8時前
47750 Call 535.0 偏貴+10.7%  量5  6.8時前
```

⚠ **不要拿夜盤的結果去判斷計算邏輯對不對** —— 看起來一堆偏貴不代表程式有問題，
那是流動性的問題。要驗邏輯請用日盤，或用 `run_demo_pipeline.py` 的合成資料。

另外全鏈有 85 檔 Call 反推不出IV被略過(Put只有3檔)，都在很深的價內/價外，
報價僵在很久以前、低於理論下界，數學上找不到對應的IV。這是正常的。

### 待你決定(我沒有自作主張)

- [ ] **「合理」要不要也給一個灰色徽章？** 現在是留白(見硬規則7)。留白的好處是
      4個真訊號不會被35個色塊淹掉；壞處是乍看像「沒算」。要改的話兩分鐘的事。
- [ ] **加不加 LICENSE？** repo 是 public 但沒有授權條款。
- [ ] **`_dbverify_tmp.py` 要留嗎？** 目前被 `.gitignore` 擋掉沒進 repo
      (它讀的 `_t.db` 也沒進去，推上去也跑不動)。要留跟我說。

## 下次要做：修剩下這 6 個邏輯問題 (2026-09-11 全面檢查找到的)

離線自我測試那 5 支當時全過，這些是「測試測不到、但會實際誤標」的問題。
A 跟 C 已於 2026-09-12 修掉(保留在下面當紀錄)，**下一個是 B**，
但 B 要先決定到期日當天 T 怎麼算，不是十行內能解決的。

### A. ~~`get_baseline_iv_batch()` 沒有時間下限~~ ✅ 已修 (2026-09-12)

**原本的問題**：`db.py` 的 SQL 只有 `ORDER BY trade_date DESC`，沒有 `WHERE trade_date >= ?`，
所以「5日均」其實是「最近5筆」，那5筆可以是兩個月前的。而它在 `analyze_chain()` 是
**最高優先序**基準(覆寫曲線、還把 `out_of_range` 取消掉標成可信)——
收盤後寫入的 job 一斷，整條鏈就被過期IV靜默評價。

**修法(兩道守門，都要在)**：

| 守門 | 位置 | 擋什麼 |
|---|---|---|
| `trade_date >= as_of - max_age_days` | `db.py` `BASELINE_HISTORY_MAX_AGE_DAYS = 15` | 平均裡混進兩個月前的**單筆** |
| 最新一筆 > N 天 → 整組不採用 | `pricing_service.py` `BASELINE_DB_MAX_STALE_DAYS = 5` | 排程斷掉、**五筆一起**變舊 |

- 新增 `db.get_iv_history_latest_date()`，讓 `analyze_chain()` 判斷「寫入排程是不是斷了」。
  刻意分成兩支查詢：「平均用哪幾筆」是資料層，「多舊就不採用」是判斷邏輯(硬規則1)。
  多一次查詢無所謂 —— 批次版當初要避免的是「每個履約價各打一次」那種幾百次往返。
- 退掉時**不靜默**：`baseline_source` 會接上
  `⚠ 資料庫5日均最新一筆是40天前(超過5天，整組不採用；收盤寫入的排程可能斷了)`，
  CLI 跟前端都看得到。連「撈得到資料卻查不到最新日期」也記成 `-1` 而不是 `None`，
  就是為了不讓它靜默退掉。
- `get_monthly_baseline_iv()` 同步改(兩支的行為必須一致，自我測試有不變式在擋)。
- 新舊呼叫相容：`as_of` / `max_age_days` 都是選填，預設 `date.today()`。
- 測試：`db.py` 測試5 加了「把 as_of 推到40天後 → 逐筆回None、批次回空dict」
  跟 `get_iv_history_latest_date()` 的驗證。

### B. 到期日當天 `T` 被灌水成 1/365 🔴

`pricing_service.py:172-173`：`T = max(days_to_expiry, 1) / 365`。
到期日 `days=0` → `T=1/365`，但實際只剩幾小時。

**同鏈基準會自我抵銷**(反推跟定價用同一個錯的 T)，所以偏斜曲線/ATM 那兩條路
判斷結果不受影響 —— 實測偏差 0.0%。但 **DB 5日均那條完全不抵銷**，
因為 IV 是與 T 無關的參數：

```
到期日 11:00、市場真實 IV=0.20
  程式反推出來的 IV = 0.0646          ← 畫面顯示的 IV 本身就是錯的
  A) 基準=同鏈曲線   → 合理價 63.76   fair 0.0%      ✅ 抵銷
  B) 基準=DB 5日均   → 合理價 197.53  cheap -67.7%   ❌ 整條鏈假性便宜
```

週選走不到 DB 那條所以現在還沒事，**月選一旦接上 MySQL，結算日當天就會中**。
另外不管哪條路徑，到期日畫面顯示的 IV 都是錯的。
要修得先決定 T 怎麼算(算到 13:30 收盤？還是算到隔天早上結算價決定的時點？
TXO 最後結算價是到期日開盤後30分鐘的加權平均，這件事會影響答案)。

### C. ~~`out_of_range` 保護只存在於偏斜曲線那條路~~ ✅ 已修 (2026-09-12)

**原本的問題**：`out_of_range` 初始 `False`，只有 `curve is not None` 才可能被設成 True。
所以 `curve.is_reliable()` 不過、退回ATM單點時(資料品質**更差**的時候)，
深價外反而一個守門都沒有 —— 直接回到 `iv_skew.SkewCurve.in_range` 檔頭那個坑
(深價外 37 檔 100% 全被標偏貴)。

**修法**：`pricing_service._within_atm_range()`，沒有曲線時用 `|ln(K/F)| <= k_range`
當守門，範圍跟曲線配適共用 `skew_k_range`(所以 `--skew-range` 一次放寬兩邊，
CLI 的提示文字也還是對的)。預設 `ATM_BASELINE_K_RANGE = 0.15`。

實測(合成資料，101檔履約價 47300±25000、真實IV 0.20)：

```
                        修正前            修正後
use_skew_curve=True     19 檔 out_of_range   19 檔   (本來就有守門，不受影響)
use_skew_curve=False     0 檔                18 檔   ← |ln(K/F)|>0.15 的正好 18 檔
曲線配不出來退回ATM單點     0 檔                 2 檔
```

CLI 的診斷文字跟著分兩種講法(`run_live_pipeline.py`)，
沒有曲線時不能再說「落在偏斜曲線的配適範圍外」。

不變式仍然成立：`out_of_range ⊆ unreliable`(設了旗標就會 `verdict.reliable = False`)，
所以 H 的 `n_tiny` 不會變負數。

### D. `mid` 報價的 `age_sec` 量錯東西了 🟡

`taifex_fetch.py:702` 不論 `price_source` 是 `last` 還是 `mid`，
`quote_time` 一律填 `CTime`(最後**成交**時間)。但 `mid` 的意思就是「今天還沒成交」，
它的 CTime 是很久以前甚至空的 —— 而 bid/ask 可能是此刻掛的。

後果是這批報價被三重懲罰：畫面標 stale 淡化、被踢出基準池(`BASELINE_MAX_AGE`)、
曲線權重掉到 `0.5**(age/900)` ≈ 0。而深價外/深價內本來就是靠 mid 才有價格的區間。
API 沒給 bid/ask 的時戳，只能改成「mid 的 age 標成 None(未知)」而不是拿成交時間充數。

### E. `quote_age_seconds` 超過 24 小時會繞回來 🟡

`taifex_fetch.py:418` 只拿得到 HHMMSS，沒有日期：

```
CTime=094500，現在 09/11 10:00        → age=900秒
3天前 09:45 成交的僵滯報價 CTime 也是 094500 → age 一樣算成 900秒
```

三天沒成交的僵滯報價會偽裝成「15分鐘前」，直接通過 `BASELINE_MAX_AGE` 進基準池。
連假後第一天、以及流動性差的週選最容易中。
API 沒給日期，修不乾淨 —— 只能靠別的訊號佐證(例如 `volume==0` 就不要信 CTime)。

### F. 「不可信 + 合理」跟「真的合理」在畫面上完全一樣 🟡

`options_dashboard.html:600-606`：`status === "fair"` 時 `statusBadge()` 回空字串、
`fairEl` 留白、`priceEl` 的 class 是 `fair`(`unreliable` 那個三元式算出來也是 `fair`)。
所以 unreliable 的檔只有 tooltip 有差。

而 `black76_iv.py:187-188` 保證會產生這一格：`fair_price <= 0` → `deviation = 0.0`
→ `status = "fair"`。深價外理論價 3e-35 那些，畫面上會呈現成一個正常的「合理」。

硬規則7 的語意是「只標例外，不標正常」—— 這格其實是例外(根本沒判斷出東西)，
卻被畫成正常。**這跟「合理要不要加灰徽章」是兩件事，這個是漏標。**

### G. `BASELINE_MAX_AGE(900) > STALE_AGE(600)` 🟢

畫面上標 `*` 淡化、跟使用者說「這筆不可信」的報價，仍然可以被拿去當整條鏈的基準
(`pricing_service.py:47` vs `:50`)。兩個門檻的語意不一致，決定一個對齊。

### H. `n_tiny` 對重疊情況會少算 🟢

`run_live_pipeline.py:301`：`n_tiny = n_unreliable - n_out_of_range`。
一檔同時「合理價過小」又「在配適範圍外」時只算進 out_of_range，n_tiny 低報。
不會變負數(out_of_range ⊆ unreliable 有保證)，只是數字不準。

## 下次要做：部署

現在是「本機開著 terminal 才看得到」的狀態。要變成常態服務還缺：

- [ ] **MySQL**：建 `option_iv_history` / `option_live_quote` 兩張表(DDL 在 README Step 4)，
      用 `TAIFEX_DB_*` 環境變數，然後 `--db env`。密碼不要寫進程式碼。
- [ ] **常駐**：目前是前景跑。要嘛 Windows 工作排程器/NSSM 包成服務，要嘛丟到有 Linux 的地方。
      決定之前先確認一件事：serve_dashboard 是**被動觸發**(有人開網頁才抓資料)，
      如果要累積歷史資料，需要的是**主動排程**(README Step 5 的 job())，兩者不一樣。
- [ ] **收盤後寫 iv_history**：月選的5日均基準要靠這張表，現在只有 `--write-db` 順手寫的
      live_quote。需要一支收盤後跑一次、寫 `option_iv_history` 的排程。
- [ ] **交易日曆**：`detect_session()` 還沒接國定假日/颱風假，夜盤的「歸屬交易日」也還沒處理
      (8/18夜盤跨到8/19凌晨，期交所歸在8/19)。寫進資料庫的日期會錯。
- [ ] **要不要開放外網**：目前只監聽 127.0.0.1 且無驗證。要從手機看的話，
      先決定用內網/VPN/Tailscale 還是真的公開 —— 公開的話一定要先加驗證。

## 未決/已知限制

- 深度價外的相對偏差仍然敏感(合理價很小，IV差3%就變價格差18%)。
  考慮過改成比較「IV的偏差」而不是「價格的偏差」，還沒做。
- `backfill_monthly_iv_history()` 往前找交易日是用「推日曆天、查無資料就跳過」的簡化方式
  (已加 `max_calendar_days` 上限)。而且它現在需要傳 `underlying_close_fn` 才能跑 ——
  舊版拿履約價當F反推出來的IV是假的，那批數字會被 `analyze_chain()` 當成最高優先序的基準。
- `detect_session()`/`is_market_open()` 已處理週末，但還沒接國定假日。
- 判斷僅供參考，不構成投資建議 —— 畫面跟 CLI 都要保留這行。
