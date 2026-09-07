cd C:\Users\ASUS\Desktop\Option
python serve_dashboard.py --open


# 台股選擇權貴賤判斷工具 — 使用說明

## 檔案清單與用途

| 檔案 | 狀態 | 用途 |
|---|---|---|
| `black76_iv.py` | ✅已測試 | 核心計算：定價、反推IV、貴/合理/便宜判斷 |
| ↳ 2026/08/18 修正 | | 價格下界改用「折現後內含價值」，修正長天期深價內合約被誤判為異常，詳見文末 |
| `iv_baseline.py` | ✅已測試 | 算baseline_iv：月選5日移動平均、週選當週ATM |
| `iv_skew.py` | ✅已測試 | **波動率偏斜曲線配適**，用曲線值當baseline，消除skew造成的假訊號 |
| `taifex_fetch.py` | ✅已測試 | 歷史行情回補 + **即時報價(已接上真實API，實測可用)** |
| `mock_live_quotes.py` | ✅已測試 | 模擬即時報價，格式跟真實API介面一致，先拿來測pipeline |
| `run_demo_pipeline.py` | ✅已測試 | 端對端demo(**模擬資料**)，離線可跑，用來驗證計算邏輯 |
| `run_live_pipeline.py` | ✅已測試 | 端對端pipeline(**真實資料**)，實際連期交所抓即時報價 |
| `pricing_service.py` | ✅已測試 | **計算流程的共用核心**，CLI跟網頁後端都走這裡，判斷邏輯只有一份 |
| `serve_dashboard.py` | ✅已測試 | **網頁後端**，把儀表板接上真實資料(標準函式庫，不需裝Flask) |
| `options_dashboard.html` | ✅已測試 | **網頁前端**，手機比例的選擇權鏈畫面 |

---

## Step 1：環境安裝

需要 Python 3.10 以上(程式裡用了 `float | None` 這種新版語法)。

```bash
pip install scipy pandas requests pymysql --break-system-packages
```

- `scipy`：算常態分布、求根(black76_iv.py要用)
- `pandas`：解析期交所回傳的HTML表格(taifex_fetch.py要用)
- `requests`：發HTTP請求
- `pymysql`：之後接正式MySQL資料庫用的，先裝起來備用

所有 `.py` 檔案(以及 `options_dashboard.html`)放在同一個資料夾裡（它們互相 import，位置要一致）。

---

## Step 2：先跑demo，確認邏輯沒問題

```bash
python3 run_demo_pipeline.py
```

會印出兩張模擬表格(月選Call、週選Put)，可以看到每個履約價的市場價、IV、合理價、貴/合理/便宜狀態。這一步**不需要網路連線**，全部用模擬資料，純粹驗證計算邏輯。

也可以單獨跑其他兩個檔案的自我測試：

```bash
python3 black76_iv.py     # 驗證定價<->反推IV互相一致
python3 iv_baseline.py    # 驗證5日均、ATM挑選邏輯
```

---

## Step 3：接上真實資料源（你要做的部分）

### 3-1｜歷史行情回補(相對簡單)
`taifex_fetch.py` 裡的 `fetch_daily_options_report()` 已經照期交所官方查詢頁面的POST格式寫好，直接跑：

```python
from taifex_fetch import fetch_daily_options_report
from datetime import date
df = fetch_daily_options_report(date(2026, 8, 14), "day")
print(df.head())
```

**第一次執行務必先檢查 `df.head()` 印出來的欄位對不對**，期交所偶爾會調整表格版面。如果欄位對不上，要調整 `fetch_daily_options_report()` 裡 `tables[2]` 的索引、或欄位篩選的邏輯。

### 3-2｜即時報價 ✅ 已完成

原本這裡要你自己開F12找API，**現在已經做完了**。API是從 mis.taifex.com.tw 的前端JS
(`/futures/_nuxt/*.js`，Nuxt打包的檔案裡直接寫死了端點名稱)反查出來，並且實際打過驗證回傳格式。

直接跑真實資料版的pipeline：

```bash
python run_live_pipeline.py                    # 自動判斷日/夜盤，抓最近月的月選買權
python run_live_pipeline.py --list             # 列出目前掛牌的所有到期別
python run_live_pipeline.py --right P          # 看賣權
python run_live_pipeline.py --expiry 202609W1  # 指定週選(W=週三結算)
python run_live_pipeline.py --expiry 202609F1  # 指定週選(F=週五結算)
python run_live_pipeline.py --min-volume 10    # 只看成交量>=10的合約(建議加，見下方說明)
python taifex_fetch.py                         # 跑抓取模組的自我測試
```

#### 到期別代碼怎麼看(週選有「週三組」跟「週五組」兩套)

台指選擇權的到期別代碼有兩種形狀：

| 代碼 | 類型 | 結算日 |
|---|---|---|
| `202609` | 月選 | 該月**第三個週三** |
| `202609W1` `202609W2` `202609W4` `202609W5` | 週選(W系列) | 該月第 n 個**週三** |
| `202609F1` `202609F2` `202609F3` `202609F4` | 週選(F系列) | 該月第 n 個**週五** |

⚠ **沒有 W3**：當月第三個週三就是月選的結算日，那一週的週三合約就是月選本身。
所以一個月大致長這樣(2026/09)：

```
W1 09/02(三)  F1 09/04(五)  W2 09/09(三)  F2 09/11(五)
月選 09/16(三)              F3 09/18(五)
W4 09/23(三)  F4 09/25(五)  W5 09/30(三)
```

期交所一次只掛最近幾個週選，所以 `--list` 出來的清單裡 W 跟 F 是**交錯**的
(例：`202608F4` → `202609W1` → `202609F1` → `202609W2` → 月選 `202609`)。
只看到期日很容易把週三那檔跟週五那檔搞混，所以：

- `parse_expiry_code()` (在 `taifex_fetch.py`) 把代碼拆成 `(week/month, 週別代碼)`，
  `ContractMonth.week_code` 就是 `"W1"`/`"F1"`，月選是 `None`。
- **星期幾一律用真實到期日推**(`ContractMonth.weekday_zh`)，不從代碼字母猜——
  期交所之後如果再加別的星期(例如週一)會冒出新字母，靠字母猜就會標錯。
  程式碼裡不寫死只認 W/F：**有字母就是週選**。
- CLI `--list` 跟網頁的到期日選單都會把 `W1`/`F1` 跟星期標出來
  (網頁上 W 是藍色標籤、F 是紫色標籤)。
- 資料庫的 `expiry_type` 欄位仍然只有 `week`/`month` 兩種值——
  W 跟 F 的到期日不會相同，主鍵裡有 `expiry_date` 就足以區分，不需要改schema。

#### 用到的API(都是 POST + JSON body)

| 端點 | 用途 |
|---|---|
| `https://mis.taifex.com.tw/futures/api/getCmdyMonthDDLItemByKind` | 列出掛牌中的到期別，**附真實到期日** |
| `https://mis.taifex.com.tw/futures/api/getQuoteList` | 撈某到期別底下全部履約價的即時報價 |

主要參數：`MarketType`(0=日盤/1=夜盤)、`SymbolType`("O"選擇權/"F"期貨)、`KindID`("1"股價指數類)、
`CID`(TXO/TXF)、`ExpireMonth`(如`202609`或`202608W4`)、`RowSize`(`"全部"`=不分頁)。

#### 幾個實測踩到的坑(之後期交所改版時可以對照)

1. **payload有中文要自己encode**：`RowSize="全部"`，必須 `json.dumps(...).encode("utf-8")` 再送，
   否則期交所會回 `Invalid UTF-8 start byte` 的400錯誤。
2. **SymbolID只能解析、不能自己拼**：格式是 `前綴+履約價+月份碼+年碼-盤別`，
   但前綴會隨到期別變(月選`TXO`、週選`TX4`/`TXX`/`TXY`…)，期交所新增週別就會冒出新前綴。
3. **買賣權看月份碼**：買權`A~L`=1~12月，賣權`M~X`=1~12月。
   結尾的`-O`是「盤別=日盤」不是「Option/買權」，不要搞混。
4. **後綴隨盤別變**(這個最容易寫錯)：

   | | 日盤 | 夜盤 |
   |---|---|---|
   | 期貨 | `-F` | `-M` |
   | 選擇權 | `-O` | `-N` |
   | 現貨 | `-S` | `-P` |

   所以程式裡是用「SymbolID有沒有帶月份碼」來區分期貨與現貨，不是用後綴。
5. **`TradingRights` 參數送 "C" 不會在server端過濾**，回傳還是call+put全部，要自己在本地篩。
6. **到期日不用自己算**：`dispName`欄位直接給了(如`202609(2026/09/16)`)，
   不需要自己算「第三個星期三」或處理國定假日順延。

#### ⚠ 資料品質：務必過濾低流動性合約

實測發現的重要問題(2026/08/18 夜盤實際抓取)：

- 很多履約價**整天沒有成交**，`CLastPrice`是0。程式已處理：沒成交價時改用買賣中價(`price_source`欄位會標示`last`還是`mid`)。
- 更麻煩的是**陳舊報價**：API不會告訴你最後成交價是多久以前的。實測23:21抓到的資料裡，
  有的成交時間是17:17——**那個價格已經6小時前**，中間台指期已經跑掉幾百點。
  拿這種價格反推IV，會出現「Put價格不隨履約價遞增」這種明顯不合理的結果。
- 用 `quote_age_seconds(q.quote_time)` 可以算出報價過了幾秒(已處理夜盤跨午夜)。
  `run_live_pipeline.py` 的表格有「報價年齡」欄位，超過10分鐘會標 `*`，
  也可以用 `--max-age 300` 直接濾掉。
- **IV收斂率實測**：全鏈 72.6%，但**只看有成交量的合約是 99.1%~100%**。
  失敗的幾乎都是零成交量的深價內合約(買賣中價低於內含價值)。

  → **結論：排程正式跑的時候，一定要用成交量或報價時間過濾，不要全鏈照單全收。**
  建議用法：`python run_live_pipeline.py --min-volume 10 --max-age 300`

#### ⚠⚠ 特別注意：基準IV(baseline)本身也會被陳舊報價污染

這是實測才發現、而且殺傷力最大的一個問題：

`get_weekly_baseline_iv()` 是單純挑「履約價離台指期最近」的那一檔當基準，**不管它報價新不新鮮**。
實測夜盤週選時，最接近價平的那檔剛好是「**3.6小時前、只成交1口**」的僵滯報價，
基準IV被它拉到 0.2072(正確值應該是 0.2853)，結果**整條鏈幾乎每一檔都被誤標成「偏貴」**。

基準是整條鏈的比較標準，被污染的殺傷力比單一檔報價爛大得多。

`run_live_pipeline.py` 已經處理：挑基準時只用「15分鐘內 + 有成交量」的報價，
真的都沒有才退回全部並在畫面上標示警告。實測前後對照：

| | 基準IV | 判斷結果 |
|---|---|---|
| 修正前(挑到3.6小時前的1口報價) | 0.2072 | 偏貴6 / 合理3 / 便宜0 |
| 修正後(只用新鮮報價挑) | 0.2853 | 偏貴0 / 合理6 / 便宜2 |

**如果你之後自己寫排程、直接呼叫 `get_weekly_baseline_iv()`，記得比照辦理**，
先把報價過濾過再傳進去，不然基準會歪掉。

#### 關於 F(台指期點數)的選擇

`fetch_underlying_futures_price()` 預設抓**近月**期貨。如果想讓月選用同月份期貨定價
(理論上更精確)，可以傳 `expire_month="202609"`。用期貨而非現貨是因為Black-76的標的
就是期貨價，用現貨會多出基差(basis)造成IV系統性偏移。

---

## Step 4：接上MySQL(取代目前demo用的sqlite記憶體資料庫)

`iv_baseline.py` 跟 `taifex_fetch.py` 裡的資料庫存取函式(`get_monthly_baseline_iv`、`save_session_close_iv`)已經是寫給正式SQL用的(用`%s`參數化查詢)，先在MySQL建表：

```sql
CREATE TABLE option_iv_history (
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
    UNIQUE KEY uniq_contract_session (trade_date, session, expiry_date, strike_price, right_type)
);

CREATE TABLE option_live_quote (
    expiry_type ENUM('week','month') NOT NULL,
    expiry_date DATE NOT NULL,
    strike_price DECIMAL(10,2) NOT NULL,
    right_type ENUM('C','P') NOT NULL,
    market_price DECIMAL(10,4) NOT NULL,
    fair_price DECIMAL(10,4) NOT NULL,
    baseline_iv DECIMAL(8,6) NOT NULL,
    deviation_pct DECIMAL(6,4) NOT NULL,
    status ENUM('expensive','fair','cheap') NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (expiry_type, expiry_date, strike_price, right_type)
);
```

用 `pymysql.connect(...)` 拿到的連線物件，直接傳給 `get_monthly_baseline_iv(db_conn, ...)` 這些函式就能用。

---

## Step 5：組成每分鐘排程

真實API接好、資料庫建好之後，排程的邏輯就是把已經寫好、測過的模組串起來（虛擬碼）：

```python
import schedule, time
from taifex_fetch import fetch_underlying_futures_price, fetch_live_quotes
from black76_iv import implied_vol, evaluate_option
from iv_baseline import get_monthly_baseline_iv, get_weekly_baseline_iv

def job():
    F = fetch_underlying_futures_price()
    for expiry_type, expiry_date in your_active_expiries():
        for right in ("C", "P"):
            quotes = fetch_live_quotes(expiry_type, expiry_date)  # 篩出right對應的
            for q in quotes:
                iv = implied_vol(q.last_price, F, q.strike_price, T, r, right)
                if not iv.converged:
                    continue
                if expiry_type == "month":
                    baseline = get_monthly_baseline_iv(db_conn, q.strike_price, right, session, expiry_date)
                else:
                    baseline = get_weekly_baseline_iv(quotes_this_expiry, F)
                verdict = evaluate_option(q.last_price, F, q.strike_price, T, r, right, baseline)
                # 寫進 option_live_quote 表

schedule.every(1).minutes.do(job)
while True:
    schedule.run_pending()
    time.sleep(1)
```

前端就是每分鐘打你自己後端的API，撈 `option_live_quote` 依篩選條件(月/週、C/P、到期月份)顯示表格。

---

## Step 6：網頁儀表板 ✅ 已完成

```bash
python serve_dashboard.py            # 然後瀏覽器開 http://127.0.0.1:8000
python serve_dashboard.py --open     # 順便自動開瀏覽器
```

畫面上會有：台指期即時點數、月選/週選切換、到期日選單、
以及ATM上下各10檔的Call/Put對照表(市場價 + 合理價 + 偏貴/便宜標籤)，每60秒自動更新。

### 三個檔案的分工

```
options_dashboard.html   畫面(純HTML/CSS/JS，沒有任何框架或打包工具)
        ↓ fetch /api/expiries, /api/quotes
serve_dashboard.py       HTTP後端(Python標準函式庫 http.server)
        ↓ analyze_chain()
pricing_service.py       計算核心 ← run_live_pipeline.py(CLI) 也是走這裡
        ↓
taifex_fetch / black76_iv / iv_skew / iv_baseline / db
```

**`pricing_service.py` 是這次整合的重點**：原本判斷邏輯全寫在 `run_live_pipeline.run()`
裡面、跟print混在一起。網頁後端要用同一套邏輯，如果各寫一份，
「基準IV優先序」「陳舊報價不能拿來當基準」這些踩坑調出來的規則遲早會走鐘。
所以先把計算抽成不印任何東西、只回傳 `ChainAnalysis` 的 `analyze_chain()`，
CLI 只留下把結果印成表格的部分 —— **網頁上看到的判斷跟 `run_live_pipeline.py` 跑出來的一定一致**。

### 常用參數

```bash
python serve_dashboard.py --port 8080
python serve_dashboard.py --session night     # 強制用夜盤資料(預設依現在時間自動判斷)
python serve_dashboard.py --window 12         # ATM上下各12檔(預設10)
python serve_dashboard.py --min-volume 10     # 只看有成交量的合約
python serve_dashboard.py --ttl 30            # 報價快取秒數(預設20)
python serve_dashboard.py --db quotes.db --write-db
```

`--db quotes.db --write-db` 會把每次更新的**全鏈**(不只畫面上那幾檔)寫進 `option_live_quote`，
表也會自動建。累積幾天之後，月選的基準就會自動從偏斜曲線切換成
「同履約價自己的5日IV移動平均」(優先序見Step 5)，畫面下方的「基準」那行會跟著變。
等於用看盤這件事本身把歷史資料養起來，不用另外寫排程。

### API規格(想自己接別的前端可以照這個)

| 端點 | 回傳 |
|---|---|
| `GET /api/expiries?type=month\|week` | `[{id, label, expiryDate, days, type}, ...]` |
| `GET /api/quotes?type=..&expiry=..` | `{underlying, atmStrike, rows, meta}` |
| `GET /api/health` | 後端是否活著、目前盤別 |

`rows` 每一列是 `{strike, call, put}`，
單邊是 `{price, status, fairPrice, deviationPct, iv, baselineIv, volume, priceSource, ageSec, stale, reliable}`。
**`call` 或 `put` 可能是 `null`** —— 真實資料裡常有單邊完全沒報價、或反推不出IV的履約價，
前端要留白而不是印成0(這點跟原本用模擬資料的假設不一樣，模擬資料每一格都一定有值)。

錯誤一律回 `{"error": "..."}` 加上對應的HTTP狀態碼(404找不到到期別 / 502期交所API錯誤 /
503沒有可用報價)，前端直接把訊息顯示出來，比「抓資料失敗」有用得多。

### 畫面上的資料品質標示

README前面講的那些坑(陳舊報價、深價外偏差爆炸)在畫面上都有對應的標示，
不是靜靜地把爛資料當成訊號顯示：

| 畫面上 | 意思 |
|---|---|
| 價格顏色變淡 | 報價超過10分鐘，那個價格可能是幾小時前成交的，IV參考價值低 |
| 標籤變淡 + `偏貴?` | 偏差百分比在這一檔失去意義(深價外，或基準是曲線外插來的)，不要當訊號 |
| `—` | 這一邊沒報價或反推不出IV |
| 滑鼠移到價格上 | 顯示即時IV、基準IV、合理價、偏差、成交量、報價幾分鐘前 |
| 最下面那幾行 | 到期日、盤別、**基準IV是哪來的**、全鏈偏貴/合理/便宜的分布 |

「基準是哪來的」特別值得看一眼：同樣一個「偏貴」，
基準是**資料庫5日均**跟基準是**ATM單點**，可信度差很多。

### 幾個實作上的注意事項

1. **Call跟Put一定要用同一個F、同一批報價**。分兩次抓的話，中間台指期跑掉幾點，
   兩邊的合理價就對不起來了。所以 `analyze_chain()` 的 `underlying_price` 是呼叫端傳進去的，
   不是它自己去抓。
2. **後端有TTL快取(預設20秒)**。前端60秒更新一次，但重新整理、切到期別、多開分頁都會打進來，
   沒有快取的話一個下午就會對期交所送出遠比需要更多的請求。
3. **ATM是哪一列由後端決定**。前端原本是把台指期點數四捨五入到百位，
   但履約價間距不是固定100(近月價平附近有50點間距)，那樣會標錯行。
   現在後端直接給 `atmStrike`(實際掛牌履約價裡離F最近的那檔)。
4. **只監聽 127.0.0.1，沒有任何身分驗證**，不要直接開到公網上。
5. 更新失敗(非交易時段、斷網、期交所維護)時，畫面**保留上一次成功的資料**，
   只在下面標一行紅字說明，不會整個清空。

---

## 目前還沒做、之後可以討論的部分

- ~~前端頁面本身~~ → 已完成，見Step 6(`options_dashboard.html` + `serve_dashboard.py`)
- ~~判斷日盤/夜盤的session切換邏輯~~ → 已在 `run_live_pipeline.py` 的 `detect_session()`
  實作(日盤08:45-13:45、夜盤15:00-次日05:00，已處理跨日)。`is_market_open()` 也已處理週末
  (注意凌晨那段是前一天開的盤，所以**週六凌晨00:00-05:00是有開的**，那是週五夜盤)。
  **但還沒接國定假日/颱風假**，也還沒處理夜盤的「歸屬交易日」
  (8/18夜盤跨到8/19凌晨，期交所歸在8/19這個交易日)。
- 交易日曆：**選擇權到期日已經不用自己算**(期交所API直接給)，但 `backfill_monthly_iv_history()`
  往前找歷史交易日時，仍是用「往前推日曆天、查無資料就跳過」的簡化方式
  (已加 `max_calendar_days` 上限，抓不到資料時不會無限往回走)。
- `backfill_monthly_iv_history()` 目前**不能直接用**：它需要 `underlying_close_fn`
  (簽名 `(trade_date, session) -> 台指期收盤價`)才跑得動，沒給會 raise NotImplementedError。
  原因是舊版拿履約價當F，反推出來的IV是假的，而那批數字會寫進 `option_iv_history`，
  接著被 `analyze_chain()` 當成**最高優先序**的基準靜默採用 —— 比直接報錯危險得多。
- ~~波動率微笑(skew)會影響判斷準確度~~ → 已用 `iv_skew.py` 配偏斜曲線解決，見下方說明。

---

## 波動率偏斜(skew)：問題與解法 ✅ 已處理

### 問題

實測真實資料時看到的現象(2026/08/18夜盤，202608買權)：

| 履約價 | 即時IV |
|---|---|
| 44100 | 0.3133 |
| 44450 (ATM) | 0.2707 |
| 44800 | 0.2516 |

IV隨履約價單調下降 —— 這就是教科書上的**波動率偏斜(skew)**，是選擇權市場的
正常結構性現象，**不是定價錯誤**。

原本週選的設計是「用ATM那一檔的IV當整條鏈的baseline」，
結果價內/價外的合約會因為skew被系統性誤標。而且**越價外誤差越誇張**：
深度價外選擇權用ATM的IV算出來的合理價趨近於0，
相對偏差 `(市價-合理價)/合理價` 就會爆炸。實測看到過 **+2573%** 這種數字。

### 解法：配偏斜曲線當基準(`iv_skew.py`)

對整條鏈的IV配一條平滑曲線，用**曲線值**當各履約價的baseline，
判斷就變成「這一檔偏離**它自己該有的IV水準**多少」，skew成分自動被吸收掉。

技術重點(細節見 `iv_skew.py` 檔頭)：

1. **在對數價性 k = ln(K/F) 的空間配適**，不是直接用履約價 —
   曲線形狀跟指數點數高低無關(台指在17000或45000都適用)，這是業界標準做法。
2. **二次多項式** `IV = a + b·k + c·k²`，只有3個參數。
   刻意不用cubic spline那種柔軟的曲線 —— 它會把「真正的定價異常」也一起吸收進去，
   那就失去偵測的意義了。
3. **穩健配適**：配一次 → 算殘差 → 剔掉離群點 → 重配。
   這是必要的不是加分項，否則陳舊報價會把曲線拉歪，
   等於重蹈「基準被污染」的覆轍。離群門檻用MAD估(標準差本身會被離群值影響)。
4. **配適權重 = 成交量 × 報價新鮮度**(`quality_weight()`)，
   成交量取log壓縮差距，新鮮度用指數衰減(每15分鐘權重減半)。

### 實測效果(同一份資料快照，兩種基準直接對照)

**202609 賣權(29天)**：

| 履約價 | 成交量 | 即時IV | ATM單點基準的判斷 | 曲線基準的判斷 |
|---|---|---|---|---|
| 39000 | 100 | 0.3564 | 偏貴 **+275.4%** | 合理 +0.1% |
| 40000 | 330 | 0.3395 | 偏貴 **+142.4%** | 合理 +1.9% |

**202608W4 賣權(8天)**：

| 履約價 | 成交量 | 即時IV | ATM單點基準的判斷 | 曲線基準的判斷 |
|---|---|---|---|---|
| 40700 | 10 | 0.3626 | 偏貴 **+2573.6%** | 便宜 −18.4% |
| 41900 | 47 | 0.3367 | 偏貴 **+492.6%** | 合理 +1.2% |

整體分布的變化(202609買權，61檔)：

| | 偏貴 | 合理 | 便宜 |
|---|---|---|---|
| ATM單點基準 | 7 | 10 | **44** |
| 偏斜曲線基準 | 23 | 23 | 15 |

舊做法把61檔買權中的44檔標成「便宜」——那全是skew造成的假訊號(價外買權IV天生低於ATM)。

**合成資料驗證**(`python iv_skew.py` 測試3、4)：
- 用一條已知曲線生成**完全沒有定價異常**的資料，理想上所有偏差都該是0%：
  ATM單點基準最大偏差 2.1%(全是假訊號)，曲線基準 0.3%。
- 把某一檔的IV人為拉高15%模擬真正的異常 → 曲線基準仍抓出 **+15.3%**，
  證明它沒有把真正的異常也一起吸收掉。

### 用法

```bash
python run_live_pipeline.py                  # 預設就是用曲線基準
python run_live_pipeline.py --baseline atm   # 想看舊做法的話
python run_live_pipeline.py --skew-range 0.08  # 只用價平±8%的履約價配曲線
python iv_skew.py                            # 跑曲線模組的自我測試(不需網路)
```

曲線配適品質不佳時(點數太少或RMSE過大)會自動退回ATM單點基準，並在畫面上標示。

### 還沒處理的部分

- **月選接上資料庫後不需要曲線**：月選用「同一履約價自己的5日IV移動平均」當基準，
  拿自己跟自己比，skew會自動抵銷。程式裡的優先序是
  **資料庫5日均 > 偏斜曲線 > ATM單點**，接上DB後月選會自動改用5日均。
- **深度價外的相對偏差仍然偏敏感**：即使用了曲線，深價外合約的合理價本身很小，
  IV差3%可能就變成價格差18%。如果覺得這個放大效應困擾，
  可以考慮改成直接比較「IV的偏差」而不是「價格的偏差」，這個還沒做。

---

## 修正紀錄

### 2026/08/18｜`black76_iv.py`：價格下界改用折現後內含價值

**問題**：`implied_vol()` 原本用「未折現內含價值 `max(F-K, 0)`」當作合法價格的下界，
但 Black-76 在 sigma→0 時價格會收斂到 `exp(-r*T) * max(F-K, 0)`。
期貨式選擇權的payoff是到期日才交割、要折現回今天，所以理論最低價本來就比未折現的低。

**影響**：天期越長差距越大(r=1.5%時，1天差0.4點、30天差12點、**210天差87點**)。
價格落在「折現後下界」與「未折現內含價值」之間的長天期深價內合約，
明明有合法的IV，卻被錯誤地當成「資料異常」丟掉。

**驗證**：用同一份真實資料快照，對全部8個到期別共2260檔合約做新舊規則A/B對照：

| | 收斂檔數 | 收斂率 |
|---|---|---|
| 舊規則(未折現下界) | 2023 | 89.51% |
| 新規則(折現後下界) | 2028 | 89.73% |

- **多救回 5 檔**，全部集中在長天期(64天1檔、120天3檔、211天1檔)
- 短天期(1~29天)完全不受影響 — 符合理論預期，因為折現因子趨近1
- **新增誤放 0 檔** — 每一檔被放行的都確實能反推出IV，沒有放進任何算不出來的髒資料

`python black76_iv.py` 的自我測試已加上這個邊界的回歸測試。
