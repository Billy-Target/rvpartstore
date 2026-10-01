# rvpartstore

TheRVPartStore（`thervpartstore.myshopify.com`）新店的自动化脚本。**取代**旧的
`francis-shopify`（RV Marines / ibs-rvmarines）自动化 —— 只做三件事：

1. 价格 + 库存同步（拉供应商 feed、Amazon repricer、RMA 库存，算出新价格/库存/供应商，写回 Shopify）
2. 拉取 Shopify 未发货订单，写入 MySQL 订单表
3. 给 `tracking_automation` 提供一个可直接替换的 `ship_orders(order_id, sku, tracking, carrier)`
4. 拉取 Brakex（`brakex.myshopify.com`）的未发货订单，写入 MySQL（2026-10-01 决定：旧项目完全停用，
   Brakex 拉单从旧的 `rvmarines_main.py` 搬到这里，见下方"Brakex 订单拉取"）

新店没有"建商品"的流程（商品已存在），也不处理没有 UPC 的商品定价（见下方
"UPC-less 商品"）。Google Merchant 同步已移植但默认关闭。

不要修改 `francis-shopify`、`tracking_automation` 等既有项目 —— 本项目是完全独立的新
代码库，只在 cut-over 当天对 `tracking_automation` 做**一行 import 的改动**（见下方
"割接 checklist"）。

> **路径提醒**：本文档里出现的所有绝对路径（例如
> `C:\Users\billy\PycharmProjects\rvpartstore`）都是**开发这台机器**的路径。部署机器
> 是 Francis 的电脑（跟 `francis-shopify`/`run_rvmarines.bat` 用的是同一台，路径形如
> `C:\Users\francis\PycharmProjects\...`）。部署到那台机器时，`run_rvpartstore.bat`
> 里写死的路径、以及本 README"割接 checklist"里举例用的路径，都需要相应改成部署机
> 器自己的路径 —— 不要照抄 `billy` 这个路径。

## 目录结构

```
rvpartstore/
  rvpartstore/            Python package（包内一律用相对 import）
    config.py              .env 配置（手写解析，不依赖 python-dotenv）
    shopify_client.py       Shopify GraphQL / REST / bulk query 客户端
    db.py                   MySQL 连接封装
    snapshot.py              Shopify 商品快照（bulk query -> DataFrame）
    sources.py                pricing 用的各数据源（feed / amazon / RMA / MAP ...）
    pricing.py                 定价核心逻辑（老代码的忠实移植）
    shopify_writes.py           写 Shopify（价格/库存/供应商 metafield/tracked）
    orders.py                     拉订单写 MySQL（RV）
    brakex_orders.py                拉订单写 MySQL（Brakex，B1-B5；与 orders.py 共用部分辅助函数）
    tracking.py                    ship_orders() drop-in 替换
    google_merchant.py             Google Merchant 同步（默认关闭）
    amazon_sp_api.py, google_sheets.py   移植的第三方接口封装
    app_logging.py, job_runner.py         日志 + 任务运行框架
  main.py                  命令行入口：python main.py <job> [flags]
  run_rvpartstore.bat       Task Scheduler 用的启动脚本
  .env / .env.example       配置（.env 本机专用，不进 git）
  requirements.txt
  data/                    运行时输出（dry-run CSV、amazon 报表），不进 git
  tests/
```

## 环境搭建

```
py -3.9 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   REM 然后手填真实的 key（本机已经有一份填好的 .env）
```

`token.json` / `credentials.json`（Google OAuth）需要放在项目根目录，本机已从
`francis-shopify` 拷贝过来一份（同一个 Google 账号，共用授权）。两个文件都在
`.gitignore` 里，不会被提交。

## 任务（jobs）与参数

```
python main.py upload               每小时跑一次：snapshot -> 拉订单 -> 算价格/库存/供应商变更 -> 写 Shopify
                                     -> 最后一步：BRAKEX_ENABLED=true 时拉 Brakex 订单（B4，独立 try/except，
                                     RV 那部分不管成功失败都会跑；日志行 "brakex orders: inserted=n skipped=n errors=n"）
python main.py enable_tracking      割接用的一次性工具：把 tracked=false 的 variant 改成 tracked=true
python main.py tracking_audit       每天跑一次：只读，核对"已发货但 Shopify 上还没关"的订单，写 ERROR 日志
python main.py delete_disapproved   删除 Google Merchant 上被拒登的商品（GOOGLE_MERCHANT_ENABLED=false 时是空操作）
python main.py brakex_orders        单独拉一次 Brakex 订单（跟 upload 里跑的是同一段逻辑，方便单独测试/补拉）
```

通用 flag：

- `--dry-run`：强制 DRY_RUN=true（不管 .env 里怎么设），不调用 Shopify 写接口/不写
  数据库，价格/库存/供应商/订单/enable_tracking 的变更会写到
  `data/dry_run/<时间戳>/<kind>.csv`，方便上线前检查。
- `--apply`：仅用于 `enable_tracking`。不加这个 flag 只统计 tracked=false 的数量，
  不做任何修改。
- `--ignore-quiet`：仅用于 `upload`。跳过 23:30–06:30 的静默时段限制（R14）。
- `--force-breaker`：仅用于 `upload`。跳过熔断器（circuit breaker）限制 —— 熔断器
  会在下列情况下自动中止写入（不做任何改动）：
  - 超过 `BREAKER_ZERO_PCT`（默认 20%）的"当前有库存"商品会被清零；
  - 超过 `BREAKER_PRICE_PCT`（默认 20%）的商品价格会变动超过 30%。
  **第一次真实上线预计一定会触发熔断**（新店所有价格都是导入值，第一次同步几乎
  相当于全量改价/改库存），README 特此提醒：第一次跑 upload 时需要加
  `--force-breaker`。

除了熔断器，upload 还有几个"守护检查"（不能用 flag 跳过）：

- 9 张 `merged_feed_*` 表里任何一张今天 0 行 -> 直接中止，不写任何东西；
- Amazon 报表下载失败 / 文件缺失或为空 / 下载后超过 3 小时才用 -> 中止；
- RMA 表没有一行带 UPC / `restricted_skus` 或 `ca_map_price_sheet` 是空表 -> 中止；
- 商品匹配到今天 feed 的比例低于 `FEED_MATCH_MIN`（默认 19000）-> 中止（这个检查
  **不能**用 `--force-breaker` 跳过，故意设计成更严格）。

## Task Scheduler 设置

- **upload**：每小时整点触发一次。"如果任务已在运行" 选 **不要启动新实例**
  （Do not start a new instance）—— 因为一次 dry-run/正式跑可能耗时较久（写入量大时
  第一次跑可能要 40 分钟左右），绝不能并发跑两个 upload。
- **tracking_audit**：每天跑一次（建议放在业务低峰期，比如凌晨）。只读，不会改
  任何东西，失败了也不影响发货。
- `run_rvpartstore.bat <job> [flags]` 是 Task Scheduler 调用的入口，里面写死了**本
  开发机**的路径（`C:\Users\billy\PycharmProjects\rvpartstore`）。**部署到 Francis
  的电脑之前，必须先把这个文件里的三处路径改成部署机器上项目的实际路径**（跟
  `francis-shopify\run_rvmarines.bat` 里写死 `C:\Users\francis\PycharmProjects\shopify`
  是同样的道理）——这是本项目唯一允许写死完整路径的地方，但写死的是**这台开发机**
  的路径，不是部署机的路径，照抄运行会直接报"找不到文件"。

## Brakex 订单拉取（B1-B5，2026-10-01 加入）

`brakex.myshopify.com` 用的是写死的 Admin API token（不是 client-credentials），
对应 `ShopifyClient.with_static_token(shop, api_version, access_token)`（B2）——跟 RV
那边用 `.env` 里 `SHOPIFY_CLIENT_ID`/`SHOPIFY_CLIENT_SECRET` 走 client-credentials
拿 token 是两条不同的认证路径，但重试/节流/分页逻辑完全一样。Brakex 的 401 不会重试
（没有 client_id/secret 可以用来换新 token），直接报错。

`BRAKEX_ENABLED`（默认 `true`）控制开关：

- `true`：`upload` 的最后一步、以及独立的 `python main.py brakex_orders`，都会去拉
  Brakex 未发货订单写入 `BRAKEX_ORDER_TABLE`（默认 `shopify_brakex_order`，28 列，
  跟 RV 的订单表结构类似但多一列 `PaymentMethod`）。
- `false`：两边都变成空操作（log 一行然后直接返回）。

Brakex 跟 RV 共用 `TxnID`/`riskLevel`/`current_quantity` 兜底/`shipping_address`
保护客户数据等规则（R4/R10/R15/V7），`orders.py` 里对应的辅助函数被
`brakex_orders.py` 直接复用（没有改 RV 那边的行为）。Brakex 专属的部分：

- `TxnID = BRAKEX_TXN_PREFIX + order_number`（默认前缀 `BRX_SP_37`）；
- vendor 永远是 `BRX`，Promise_Date 固定 2 天起算（跟其它供应商一样套用周末调整）；
- 订单金额/明细金额：paypal 支付且用 USD 结算时，按 `BRAKEX_USD_TO_CAD`（默认
  1.35）换算成 CAD，否则直接用 CAD 金额；
- 旧代码里对 `shopify_brakex_sku_upc_mapping` 的查询被**去掉**了——它唯一的用途是
  一个库存扣减，但旧代码里这个扣减从来没真正生效过，反而只要有一个 product 没映射
  就会让整个 run 崩掉。本项目不做这个查询，也不做这个扣减。

## 割接（cut-over）checklist

1. 先停掉旧店（`rvmarines_main.py` 对应的 Task）的 Task Scheduler 任务 ——
   **这是老项目最后一个还在跑的任务**，Brakex 拉单已经搬到本项目里了
   （见上面"Brakex 订单拉取"），所以这里是老项目**彻底停用**，不再是旧任务停了
   但 Brakex 还需要跟进的遗留问题。
2. `python main.py enable_tracking --apply`，把新店所有 `tracked=false` 的 variant
   改成 `tracked=true`（否则库存没法正常同步）。
3. 先跑一次 `python main.py upload --dry-run`，检查 `data/dry_run/` 下的 CSV
   （价格、库存、供应商、订单）看着都合理，再跑一次**不带** `--dry-run`（且大概率要
   加 `--force-breaker`，见上面熔断器说明）的正式 upload。
4. 切 `tracking_automation` 的 import：把
   ```python
   from shopify_tracking import ship_orders
   ```
   改成
   ```python
   import sys
   sys.path.insert(0, r"C:\Users\billy\PycharmProjects\rvpartstore")
   from rvpartstore.tracking import ship_orders
   ```
   这是**唯一**需要动 `tracking_automation` 代码的地方，而且只在割接当天改一次。
   旧店的在途订单（TxnID 不是 `RVM_SP_N` 开头）会被自动转发给
   `tracking_automation` 自己原有的 `shopify_tracking.ship_orders`（R1/V1），所以
   旧店未发完的订单在切换后仍然能正常发货。
5. 给新项目排 Task Scheduler：`upload`（每小时）+ `tracking_audit`（每天）。

割接前后还要注意：

- **先下单测试**：新店目前还没有真实订单跑过，`orders.py` 里对"保护客户数据"
  （shipping_address 缺失）的处理（R10）没法在没有真实订单前验证，割接前务必先在
  新店下一笔测试订单，确认能正常拉到并写入数据库。
- 4 个重复条码（duplicate barcode）会同时显示供应商库存，这是已知问题，不在本项目
  处理范围内。
- UPC-exception 名单里的商品完全不会被本项目触碰（价格、库存都保持它们当前的值）。
- 旧店的 API app（client id/secret）**要保留着不要删**，直到旧店所有在途订单都已
  发货完毕（新旧两条 tracking 路径都可能还需要用到）。
- 关掉旧店前，建议先把旧店设成"需要密码访问"或者把它的库存清零，避免旧店继续被
  下单（老代码停了但店铺本身还开着的话，客人还是能在旧店下单）。

## UPC-less 商品（option b）

新店有约 2,949 个 variant 没有 UPC（barcode 为空，SKU 形如 `A1317142`）。这些商品
**完全跳过定价**（P2）：

- 价格、Google Merchant、供应商 metafield 都不会改；
- 只要它们当前库存不是 0（且 Shopify 上确实有库存记录），就会被强制清零，避免卖出
  没有供应商来源的商品。

## 已知的设计取舍 / 偏离老代码的地方

`pricing.py` 是老代码 `update_price_inventory.py::update_price_quantity_graphql`
的逐行移植，所有偏离都在代码里用 `# P1:` `# R8:` `# V6:` 这种注释标出对应的设计
文档编号，方便对照。比较值得一提的两处：

- **R13（$0 价格保护）+ V6（compareAtPrice 变化也算改价）+ R9（UPC 优先于 RMA）**
  三条规则叠加时，发现一个潜在问题：如果某个 variant 只在 RMA 名单里有价格、完全
  不在供应商 feed 里，"UPC 路径"对它其实没有任何真实数据、只是把店内原价原样带过
  ——但因为店内 compareAtPrice 目前全部是 NULL，"原样带过"也会被 V6 判定成"变了"，
  再加上 R9 规定"价格冲突时 UPC 赢"，就会出现 UPC 路径用未经处理的店内原价，覆盖掉
  RMA 路径算出来的正确价格。这个问题不在任何一条 R/V 编号里，是三条规则组合出的副
  作用，本项目加了一个不改变三条规则本身含义的小修正（只有"该 variant 确实在供应商
  feed 里匹配到"时，才允许 V6 的 compareAtPrice 差异触发改价）。详见 `pricing.py`
  里 `_get_changed_items` 函数的 `feed_matched` 相关注释。

## 测试

`tests/` 目录下是 test-author 角色根据设计文档编写的黑盒测试套件（不读实现代码）。
跑法：

```
.venv\Scripts\python.exe -m pytest tests -q
```

本项目实现这边自己做的是编译检查（`python -m compileall`）、每个模块的 import 检查、
`rvpartstore.tracking` 的 R3 隔离检查（子进程里 import 之后确认 `sys.modules` 里
没有 pandas/numpy/dotenv），以及针对 `pricing.compute_changes` / `orders.build_order_rows`
/ `brakex_orders.build_brakex_order_rows` / `tracking.build_fulfillment_input` /
`snapshot.parse_snapshot_rows` 等纯函数的一次性冒烟测试（未留在仓库里，不跟正式测试
套件混在一起）。
