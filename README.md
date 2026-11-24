# 桥梁振动记录仪 · 纸本 UTC 时刻核查页

桥梁巡检员用纸本 UTC 时刻核对旧振动记录仪计数、定位抄错时刻的 Flask 网页。

## 业务规则与建模

* 记录仪计数每毫秒 +1，满 **65536** 归零；相邻事件间隔 **1～90000 ms**。
* 对相邻事件，计数模差 `r = (c₂-c₁) mod 65536`：
  * `r=0`：只能是绕了一整圈，间隔固定为 65536；
  * `1 ≤ r ≤ 24464`：间隔模糊，候选为 `r` 与 `r+65536`；
  * `r > 24464`：`r+65536 > 90000` 超界，间隔固定为 `r`。
* 任意两事件 `i<j` 的可达总耗时是等差数列：
  **首值 `low`、末值 `low + amb×65536`、步长 65536**，
  其中 `low` 为区间内最小候选之和，`amb` 为模糊间隔条数。
* 纸本锚点对的时差落在该数列内即“对得上”；所有相邻锚点对都对得上则全账一致。
* 不一致时，逐个假设“只有该锚点抄错”：其余锚点对必须全部对得上，
  再由前后邻居解出该锚点可替换时刻的**首值、末值、步长（65536）**；
  没有任何锚点满足则提示 **单处更正无法对平**。

### 为什么核查不用重读整份事件

导入 CSV 时即为每个事件预算前缀量 `prefix_min`、`prefix_amb` 并落 SQLite。
核查任意事件对只做两次按事件号的点查，复杂度与事件总数无关（O(锚点数)）。

## 目录

```
app/reconcile.py     核查算法（间隔候选、前缀量、一致性、单处更正候选）
app/csv_loader.py    CSV 解析与校验
app/db.py            SQLite 持久化（dataset/event/anchor 三表）
app/__init__.py      Flask 路由与事务（更正+复核同事务，失败回滚保留原账）
app/templates/       单页前端（上传/补登记/核查/确认更正）
demo/                演示数据生成器、事件 CSV、三套锚点账、演示脚本
tests/               37 个单元/端到端测试
Dockerfile, docker-compose.yml
```

## 用 Docker Compose 启动

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
# SQLite 数据保存在命名卷 inspection-data (/data/inspection.db)
```

## 本地启动（无 Docker 时）

```bash
pip install -r requirements.txt
python run.py            # 默认 http://0.0.0.0:8000，库文件 data/inspection.db
```

## 演示流程

```bash
python demo/make_demo.py demo     # 生成 events.csv 与三套锚点账
bash demo/demo.sh                 # 对运行中的网页跑完整 HTTP 演示
```

页面操作：上传 `demo/events.csv` → 照 `demo/anchors_good.csv` 登记 3 条锚点 →
核查显示“一致”；换成 `anchors_bad_single.csv` 重建并登记 → 显示不一致及候选
首值/末值/步长 → 用候选值“确认更正并复核”（区间外的值会被拒绝并回滚）；
`anchors_bad_double.csv` 演示“单处更正无法对平”。核查期间可随时补登记锚点，
确认更正始终按**当时全部锚点**重新核对。

## 自测

```bash
python -m unittest discover -s tests -v   # 37 tests
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 核查页 |
| GET | `/api/state` | 当前账与全部锚点 |
| POST | `/api/dataset` | 上传 CSV 重建账（清空旧锚点，单事务） |
| POST | `/api/anchors` | 补登记锚点（重复事件号 409，原账不动） |
| DELETE | `/api/anchors/<id>` | 删除锚点 |
| POST | `/api/verify` | 按全部锚点核查 |
| POST | `/api/correct` | 确认更正：改写后按当时全部锚点复核，不过即回滚 |
