# K12 自适应学习引擎（骨架）

基于 `k12-knowledge-points` 数据集的自适应学习引擎实现骨架：
**知识点先修图谱 + 掌握度建模（BKT/Elo）+ 错题归因 + 分阶段学习路径规划 + 复习调度**。

```
adaptive-engine/
├── ale/                  引擎核心包
│   ├── dataset.py        数据集懒加载（按学段×科目）+ 全局 ID 索引 + 派生索引缓存
│   ├── graph.py          先修 DAG：拓扑序、祖先回溯、分层、枢纽识别、环检测
│   ├── mastery.py        MasteryModel 接口 + BKT / Elo 两个实现（可插拔）
│   ├── llm.py            OpenAI 兼容 LLM 客户端（纯标准库；.env 三行配置 + 内置 Provider 表）
│   ├── classroom.py      班级与作业：教师建班/邀请码、作业布置、全班成绩矩阵
│   ├── quiz.py           出题：LLM 命题（带完整上下文的命题规范）→ 缓存 → 标注模板兜底
│   ├── accounts.py       账号：PBKDF2 口令哈希 + HMAC 签名会话 Cookie（纯标准库）
│   ├── store_sqlite.py   学生档案 SQLite 存储（WAL；与文件版同一套接口）
│   ├── diagnose.py       错题归因：沿先修链回溯 + 贪心集合覆盖找根因
│   ├── planner.py        路径规划：先修闭包 → 剔除已掌握 → 分阶段输出
│   ├── review.py         SM-2 变体复习调度
│   ├── state.py          学生状态（作答历史/掌握度/复习队列）+ 文件版存储（原子写 + 文件锁）
│   ├── engine.py         AdaptiveEngine 门面（CLI 与 API 共用）
│   ├── figures.py        交互图形注册表（知识点 UUID → 图形）
│   └── config.py         全部可调参数（阈值/衰减/回溯深度…）
├── cli.py                命令行入口（含 llm-check：一条命令验证 LLM 连通并出样题）
├── server.py             FastAPI 服务 + 账号端点 + 归属守卫 + 托管前端/图形资源
├── web/                  单页前端（知识链视图默认；SVG 全景图可选；无框架依赖；
│                         本地内置 KaTeX 渲染数学公式——vendor/katex，无需外网）
├── figures/              交互图形（离线构建的静态 HTML/SVG）
│   ├── registry.json     图形注册表：知识点 UUID → 图形
│   └── ohm-lab/          欧姆定律实验台（model / geom / live / build）
├── tests/                130 个测试：合成图单测 + 真实数据集端到端 + LLM 出题链路
│                         + 账号/存储 + 班级/作业 + LLM 设置 + 回归
├── .env.example          LLM 配置样例（复制为 .env 填 Key；.env 已 gitignore）
├── pyproject.toml        pytest 配置（pythonpath，任意目录可跑）
└── data/                 运行时目录（app.db、secret.key、students/、quiz_cache/、cache/，均 gitignore）
```

## 快速开始

```bash
# 依赖（仓库根目录已建好 .venv，或自行 pip install -r requirements.txt）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 跑测试（含真实数据集冒烟）；pyproject.toml 里配了 pythonpath，任意目录可跑
.venv/bin/python -m pytest tests/ -q

# 预热派生索引（可选：首次请求会自动建，之后走缓存）
.venv/bin/python cli.py prebuild-index

# CLI 演示
.venv/bin/python cli.py search 待定系数法 --band 初中
.venv/bin/python cli.py new-student 小明 --model bkt
.venv/bin/python cli.py simulate <SID> --band 初中 --subject 数学 --n 30
.venv/bin/python cli.py diagnose <SID>          # 错题归因
.venv/bin/python cli.py plan <SID> <goal_kp_id> # 分阶段学习路径
.venv/bin/python cli.py next <SID> --goal <goal_kp_id>

# LLM 出题质检：一条命令验证连通性并出样题（需先复制 .env.example 为 .env 填 Key）
.venv/bin/python cli.py llm-check

# Web 服务（浏览器打开 http://localhost:8000）
.venv/bin/python server.py
```

数据集路径默认取仓库 `../split`，可用环境变量 `K12_DATA_DIR` 覆盖。

> **置信度过滤（重要）**：默认 `min_confidence=0` 全量加载。音乐、体育等科目的
> 条目大部分来自「目录标题抽取」，`confidence` 只有 0.2~0.4；若按 0.6 硬过滤，
> 整个科目会静默消失（接口返回空图）。低置信条目在 UI 上以虚线弱化显示，
> 详情页会提示「仅按目录标题抽取」。需要严格模式时设 `K12_MIN_CONFIDENCE=0.6`。

## 核心设计

### 掌握度：BKT 与 Elo 可插拔

统一 `MasteryModel` 接口（`prior / update / decay / p`），学生档案里记录用哪个模型，
`create_model()` 工厂注册，新增模型（如 DKT 神经网络）只需实现同一接口。

- **BKT**：四参数贝叶斯知识追踪，知识点难度调制先验/学习率/失误率/蒙对率。
  *对经典公式的修正：学习转移只在答对后应用*——原版对答错也套转移，
  低先验时会出现"答错反而涨掌握度"的反直觉结果（测试 `test_bkt_update_direction` 守住这一点）。
- **Elo**：知识点难度映射为"题目分"，学生对每个知识点维护能力分，掌握概率即胜率；
  K 值随练习次数收敛。
  *状态语义修正*：能力分与胜率分开存放（`MasteryState.ability`）。旧实现把胜率
  写进 `p_mastery`，下一次 `update()` 又把它当能力分乘一遍 span，能力估计系统性
  偏低——"连对 10 次"的偏差达 8.6 个百分点。修复后 Elo 的学习轨迹与"能力分独立
  存放"的标准写法逐位一致（测试 `test_elo_trajectory_matches_reference_implementation`）。
- 两者都实现了指数遗忘（`decay`），参数在 `config.py`。
- **阈值按模型分派**：BKT 用 `mastered_threshold`（0.85），Elo 用
  `elo_mastered_threshold`（0.80）。走 `config.mastered_for(model.name)` 统一取，
  接口层、规划器、归因器用的是同一个口径——此前 `elo_mastered_threshold` 是死配置，
  所有判断都落到了 0.85 上。

### 出题：LLM 命题为主，标注兜底

在 `adaptive-engine/` 复制 `.env.example` 为 `.env`、填三项 OpenAI 兼容配置
（`K12_LLM_BASE_URL / K12_LLM_API_KEY / K12_LLM_MODEL`）即启用 AI 命题：
`cli.py llm-check` 一条命令验证连通并出样题。

**为什么不用模板出题**：标注原文是标签式残句（「认为…」「能…」「混淆…」），
直接当选项读起来生硬。LLM 路径把知识点的完整材料打包成上下文——
知识点名/学段/教材位置/`learningObjectives`（标准答案素材）/`misconceptions`
（干扰项素材）——并按命题规范要求：

1. 四个选项**必须改写成完整自然的陈述句**，禁止动词开头的标注残句；
2. 至少一个干扰项来自误区，且要**改写成看起来合理**的说法（误区之所以是
   常见误区，就是因为它骗得到人，选项不能一眼假）；
3. 正确项与课标一致、具体可判断；不出「以上都对/都不对」，选项互不包含；
4. 解析要说清正确项为什么对、错误项踩了哪条误区。

生成路径三级回退：**缓存 → LLM → 模板**。同一 `(kp_id, seed, model)` 的题落盘
`data/quiz_cache/`（原子写；换模型或改提示词自动换一批），标题答耗时从秒级降到 0。
LLM 输出强校验（4 选项唯一、`answer_index` 合法、解析长度），不合格直接走兜底，
不会把坏题发给学生。

**防串题**：响应带选项哈希 `qhash`，作答时随答案带回；服务端按 `(kp_id, seed)`
在读缓存里重建同一道题并核对哈希——缓存被清或换了模型会立即以 **409** 提示
前端重新抽题，而不是拿另一道题悄悄判分。下发接口永远不含 `answer_index`。

**判分兜底（答案库）**：每道 AI 题生成时把答案/解析/误区按 qhash 持久化到
`data/question_bank.jsonl`。出题缓存被清后判分仍按原题执行——学生答完一定
看到「对 + 正确答案推导 + 误区归因」，而不是被告知「题目已刷新」。

**公式排版**：命题规范要求所有数学式走 LaTeX（`$...$`，禁用 √ ² 等字符混写），
解析须贴合学科特点（数学分步推导、物理定律代入、化学守恒）。前端内置 KaTeX
本地渲染题干/选项/解析，无需外网 CDN；公式失败退回原样显示，不白屏。

没有配 Key 时也不影响使用：模板路径照常出题（详情见旧模板规则），前端以
「📝 模板示例」徽标区分「🤖 AI 命题」。LLM 三要素可在前端「⚙ LLM 设置」
面板可视化配置：内置预设（OpenCode Go / DeepSeek / 通义 / 智谱 / Ollama 本地）、
连通性探测显示耗时与模型回复、保存即热生效并写回 `.env`；游客单向只读。

数据覆盖：`learningObjectives` 和 `misconceptions` 在 33,765 个知识点上的
覆盖率都是 99.8%，其中 81% 的 `assessability ≥ 0.7`。

### 账号体系：SQLite 落库

注册/登录/游客三种会话，全部落在 `data/app.db`（SQLite，WAL 模式，零部署）：

- **口令**：PBKDF2-HMAC-SHA256 + 每用户随机盐（12 万次迭代），纯标准库。
- **会话**：无状态 HMAC 签名 Cookie（`k12_session`，HttpOnly + SameSite=Lax），

  服务重启不掉登录态；签名密钥首次启动自动生成到 `data/secret.key`（0600），
  轮换该文件即全员下线。
- **归属**：学生档案带 `owner_id`。注册自动建档并绑定；写学生的所有端点
  （作答/路径/模拟…）都过归属守卫——**别人的正式档案按 404 处理（不暴露存在
  性），演示档案 403 只读**。首次启动若库为空且存在旧 JSON 档案，自动导入为
  演示档案（对所有人可见）。
- **游客模式**：不注册即可完整体验（一键游客账号 + 档案），但不设口令、
  不能再次登录；页面提示“注册后可长期保存”。

### 班级与作业：教师布置、学生作答、全班矩阵

「🏫 班级」面板把教师工作流搬进来：

1. **教师**（任何注册用户可建班，创建者即教师）新建班级，分发 6 位邀请码
   （去掉了 0/O/1/I 的易混字母表）；
2. **学生**凭码入班，答疑/掌握度仍落各自的学习档案，与自学数据互通；
3. **布置作业**：教师浏览图表时把当前知识点加入草稿（1~20 个），起标题生成
   作业。作业在布置时就固化 `(kp_id, seed)` 对——seed 由作业 id+题号确定性
   派生，**全班同一道题**，成绩可直接横向比较；
4. **学生作答**：面板点「去做题」→ 知识链视图按作业题固定 (kp, seed) 出题
   → 判分复用 `eng.answer_question`（qhash 核对、答案库兜底、掌握度更新、
   答错归因，全套一致）→ 结果登记进班级进度表；
5. **教师矩阵**：学生 × 题号的 ✓/✗/· 网格 + 每题全班对题数，一眼看出
   “哪道题全班都错”——那就是下周要讲的内容。

游客不参与班级（会话是一次性的，数据不保留）；作业相关写操作要求注册。

### 知识链视图（默认）与全景图

上万个知识点画成一张大图必然是一团线球。前端默认改为**知识链**：选中一个
知识点后，沿先修关系回溯出的「根 → 当前」链条以卡片逐张浮现
（CSS 交错动画），每张卡标出掌握百分比与状态色；下方列出该知识点的直接下游。
未选中知识时给「推荐下一步」入口。全景依赖图降级为可选
（「🗺 全景图」按钮，按需裁剪，见下文参数）。

### 错题归因：证据传播 + 贪心集合覆盖

1. 症状 = 做错的知识点集；
2. 沿 `prerequisite` 边向上 BFS，祖先获得证据权重 `(1 - p(祖先)) × 0.6^(距离)`——
   越弱、离症状越近，嫌疑越大；
3. 贪心选覆盖未解释症状最多的祖先为根因，附一条「根因 ← … ← 症状」解释链，
   直到症状全部覆盖或达到根因上限。

### 只用硬前置建 DAG

数据集有 `prerequisite / leads-to / related` 三类边。图的拓扑、分层、祖先回溯
**只认 `prerequisite`**，`leads-to` 以虚线留在可视化里，`related` 不参与推理——
否则弱边会污染先修深度（曾导致"最大深度 17 但最长链只有 1"的不一致）。

**环的处理**：LLM 抽取的先修边不保证无环（实测 39 个科目中 17 个存在环，
初中物理有一个 35 节点的大环）。所有图算法先在 SCC（强连通分量）上缩点：
拓扑序、深度、规划阶段都在缩点后的 DAG 上计算，环内节点共享同一深度与阶段
（它们互相依赖，本来就无法排序）。`graph.cycles()` 只报告真正的环。

### 路径规划

目标 → 先修闭包 → 按掌握度阈值剔除已掌握 → 拓扑排序 → 按"弱节点子图内最长前置深度"
分阶段。超长路径按「离目标近者优先」截断（保底包含目标），截断后重算阶段。
估时为骨架级启发式（8 + 难度×14 分钟，程序性知识 +4）。

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/groups` | 学段×科目清单 |
| GET | `/api/config` | 前端阈值下发（掌握度阈值、置信度提示阈值…） |
| GET | `/api/search?q=&band=&subject=` | 知识点搜索 |
| GET | `/api/kp/{id}?student=` | 详情（目标/误区/先修链/掌握度） |
| GET | `/api/kp/{id}/chain?student=` | 知识链：先修链（根→叶）+ 直接下游 + 掌握度 |
| GET | `/api/quiz/{id}?seed=` | 出一道题（响应不含答案；`source` 标明 AI 命题/模板） |
| POST | `/api/students/{sid}/answer` | 提交作答 `{kp_id, seed, choice, qhash}` → 核对哈希判分 + 写掌握度 + 答错归因；哈希不一致 409 |
| GET | `/api/graph/{band}/{subject}?student=&depth=&root=&spine=&max_nodes=` | 图谱可视化数据（支持按范围收窄，见下） |
| POST | `/api/auth/register` | 注册 `{username, password}` → 自动建档绑定，设会话 Cookie |
| POST | `/api/auth/login` | 登录 `{username, password}` |
| POST | `/api/auth/guest` | 游客模式（免注册，自动建游客账号+档案） |
| GET | `/api/auth/me` | 当前用户（未登录返回 `{user: null}`） |
| POST | `/api/auth/logout` | 退出（清 Cookie） |
| POST | `/api/classes` | 建班 `{name}`（创建者=教师，自动成为成员；需注册） |
| POST | `/api/classes/join` | 凭邀请码入班 `{code}`（需注册） |
| GET | `/api/classes` | 我的班级列表（教师含邀请码；学生不含） |
| GET | `/api/classes/{cid}/members` | 成员列表（成员可见） |
| POST | `/api/classes/{cid}/assignments` | 布置作业 `{title, kp_ids}`（教师；server 解析名称快照） |
| GET | `/api/classes/{cid}/assignments` | 作业列表 + 我的进度（教师含全班完成统计） |
| GET | `/api/classes/{cid}/assignments/{aid}` | 作业详情：固定 (kp, seed) 题目条目 + 我的状态 |
| POST | `/api/classes/{cid}/assignments/{aid}/answer` | 作业作答 `{item, choice, qhash}`：判分同普通答题 + 记进度（哈希不符 409） |
| GET | `/api/classes/{cid}/assignments/{aid}/results` | 全班成绩矩阵（教师） |
| GET | `/api/llm` | 当前 LLM 配置摘要（密钥只有末 4 位）+ Provider 预设 |
| POST | `/api/llm/save` | 保存 LLM 配置：热切换 + 写回 `.env`（`{base_url, api_key, model, max_tokens}`；游客 403） |
| POST | `/api/llm/test` | 连通性探测（不改配置不落盘；游客 403） |
| POST | `/api/students` | 建学生 `{name, model}`（**需登录**，归属当前用户） |
| POST | `/api/students/{sid}/attempt` | 记录作答 `{kp_id, correct}`（答错自动归因；需登录 + 归属校验） |
| POST | `/api/students/{sid}/attempts` | 批量记录作答（整批只写一次档案） |
| POST | `/api/students/{sid}/simulate` | 模拟练习 `{kp_ids, ability, noise, seed}` |
| POST | `/api/students/{sid}/diagnose` | 归因 `{kp_ids}` |
| GET | `/api/students/{sid}/diagnose/recent` | 最近错题自动归因 |
| POST | `/api/students/{sid}/plan` | 学习路径 `{goal_id}` |
| GET | `/api/students/{sid}/recommend?goal=` | 推荐下一步 |
| GET | `/api/students/{sid}/reviews` | 到期复习 |
| GET | `/api/figures` | 已注册的交互图形清单 |
| GET | `/figures/**` | 图形静态资源（构建产物） |
| GET | `/api/kp/{id}` 里的 `figure` 字段 | 该知识点挂着的交互图形（没有则无此字段） |

### 图谱范围收窄

大科目整图太大（小学语文 4851 个知识点 → 1.4 MB 响应 + 上万个 SVG 元素），
`/api/graph` 支持四个收窄参数，响应里的 `truncated / total_nodes / total_edges`
告诉前端是否被裁剪：

| 参数 | 含义 | 效果（小学语文）|
|---|---|---|
| `spine=true` | 只保留参与先修关系的节点 | 4851 → 2529 节点 |
| `depth=N` | 只保留深度 ≤ N | 按层级过滤 |
| `root=<kp_id>` | 只保留该节点所在连通分量 | 1683 → 14 节点（初中数学）|
| `max_nodes=N` | 节点数上限，按连通分量从大到小装 | 800 节点 / 648 条边 |

前端工具栏的「范围」下拉对应这些模式；节点超过 1200 个时只给有信息量的节点
（已作答 / 选中 / 根因 / 路径内）画文字标签，避免 DOM 爆炸。

## 性能与持久化

- **双后端学生档案**：默认 **SQLite**（`data/app.db`，WAL + busy_timeout，
  `store_sqlite.py` 与文件版同一套方法签名，`data/students/` 里旧的 JSON 档案
  首次启动自动导入为演示档案）；环境变量 `K12_STORE=file` 可切回旧文件版。
  账号表与学生表同库，`RLock` 保护进程内「读-改-写」（`mutate()` 持锁内部还要
  再进锁存档——`threading.Lock` 在这里会自锁死，测试
  `test_mutate_locks_and_persists` 守住）。
- **派生索引缓存**：`kp_id → (学段, 科目)` 与 `名称/别名 → kp_id` 两个索引会落盘到
  `data/cache/`，以 split 文件的 `(路径, 大小, mtime)` 指纹判断新鲜度，数据集变了
  自动重建。冷启动 `locate()` 从 0.39 s 降到 0.015 s，搜索从 0.41 s / 157 MB 降到
  0.17 s / 106 MB。`cli.py prebuild-index` 可手动预热。
- **学生档案原子写**：先写同目录临时文件再 `os.replace` 覆盖，中途崩溃不会留下
  半截 JSON；失败时清掉临时文件，原档案毫发无损。（SQLite 后端走语句级事务 +
  WAL，语义等价）
- **文件锁**（`K12_STORE=file` 后端）：`StudentStore.mutate()` 在 `fcntl` 独占锁
  内完成「读-改-写」，多 worker 并发作答不会互相覆盖。
- **损坏档案降级**：`get()` 遇到坏数据抛 `KeyError`（API 回 404 + 提示），
  列表接口跳过坏档案，不会整个挂掉。
- **LLM 出题缓存**：`data/quiz_cache/` 按 `(kp_id, seed, model, 提示词版本)`
  原子落盘；缓存命中后出题 0 开销、判分严格一致。


## 交互图形：欧姆定律实验台

`figures/ohm-lab/` 是一个**用物体解释概念**的交互图形：6 V 电源、0–20 Ω 滑线变阻器、
两个 10 Ω 电阻、电流表、电压表和一台 I–U 记录仪。拖动滑片 → 变阻器接入电阻变大 →
电流表、电压表指针回落 → 记录纸上的笔沿直线移动；换接法（单个 / 串联 / 并联），
笔臂倾角就是 1/R，三条参考线分别对应 5 Ω、10 Ω、20 Ω。

### 单一真源

| 文件 | 作用 |
|---|---|
| `model.mjs` | 纯电学模型 `solve()`：滑片位置 + 接法 + 开关 → U / I / R，指针角度、图线坐标、读数格式全部由它派生 |
| `geom.mjs` | 等距投影几何（与 Anatomy kit 逐字等价，构建时抽样对比，不一致直接构建失败） |
| `live.js` | 浏览器运行时：拖拽、接法切换、指针弹簧缓动、笔臂/记录线、巡演、键盘、reduced-motion |
| `build.mjs` | 离线构建：生成 `embed.html`（浅色，嵌前端用）、`embed-dark.html`、`ohm-lab-light.html`、`ohm-lab.html`（独立页） |

要点：

- **运行时不含任何 LLM 调用**，也不读网络：读数与图线是同一个纯模型算出来的，
  图形本身就是解释，不是装饰动画。
- 图内自评（「我看懂了这条线」/「还不太明白」）走**形成性评估**通道：
  答对加掌握度、答错触发根因归因，与主页面 ✓/✗ 是同一个作答接口。
- 暗色版与浅色版共用 `model.mjs` / `live.js`，只换主题 token；两版产物都在仓库里。

### 构建

```bash
cd adaptive-engine
node figures/ohm-lab/build.mjs        # 需要本机 Anatomy skill（ANATOMY_HOME 可覆盖）
node --test figures/ohm-lab/model.test.mjs   # 10 个模型断言
```

构建产物是纯静态 HTML（浅色嵌入页约 280 kB），直接被 `/figures` 托管。
构建产物可复现：同样输入重跑 `build.mjs`，产物逐字节一致。

### 注册表

`figures/registry.json` 决定「哪些知识点挂图形」：

```json
{
  "version": 1,
  "figures": {
    "ohm-lab": {
      "id": "ohm-lab",
      "title": "欧姆定律实验台",
      "subtitle": "串联滑线变阻器 · 三种接法 · I–U 记录纸",
      "embed_url": "/figures/ohm-lab/embed.html",
      "embed_dark_url": "/figures/ohm-lab/embed-dark.html",
      "standalone_url": "/figures/ohm-lab/ohm-lab-light.html",
      "standalone_dark_url": "/figures/ohm-lab/ohm-lab.html"
    }
  },
  "by_kp": { "<知识点 UUID>": "ohm-lab" }
}
```

`ale/figures.py` 的 `FigureRegistry` 读它；文件缺失或损坏时返回空（详情页不出现
图形入口），**不影响主引擎**。目前映射了 9 个初中物理知识点
（欧姆定律表达式与单位、I–U 图像分析、控制变量法实验设计、伏安法测电阻原理、
串联电流处处相等、伏安法接线规范与常见错误等）。

### 前端接入与 postMessage 协议

知识点详情页出现「打开实验台 →」入口，点开是全屏模态里的 iframe（~860 px 宽，
不用侧栏 380 px 去压一张 640 px 的图）。父子页面用 `postMessage` 通信，
只认同源：

| 方向 | 消息 | 说明 |
|---|---|---|
| iframe → 父页面 | `{type:"ready"}` | 图形就绪，父页面回送掌握度 |
| iframe → 父页面 | `{type:"height", height}` | 内容高度，父页面据此调 iframe 高度 |
| iframe → 父页面 | `{type:"attempt", kp_id, correct, reading}` | 图内自评，`reading` 带 U / I / R / 接法 / 滑片位置 |
| 父页面 → iframe | `{type:"mastery", kp_id, p}` | 当前掌握度，图形据此调整提示（低掌握先动滑片，高掌握先预测再验证） |

父页面收到 `attempt` 后：校验 `kp_id` 与当前选中一致 → `POST /api/students/{sid}/attempt`
→ 掌握度条与图谱重新着色 → 把新的 `p` 回送 iframe。图内作答与主页面 ✓/✗ 共用同一接口，
所以「在实验台里点懂了」和「在详情页点懂了」进入同一条学习轨迹。

## 已知边界（骨架阶段）

- **LLM 需自备 Key**：`.env` 三行配置（OpenAI 兼容），未配置时模板出题兜底；
  free-tier 网关（如 OpenCode 免费档）做了客户端锁定，只能用直连 Provider。
- 交互图形目前只有「欧姆定律实验台」一个，注册表可继续扩展；
  图形产物需要手工执行 `node figures/ohm-lab/build.mjs` 重建（不进 CI）。
- 关系不跨学科不跨学段（数据集本身如此），跨科归因暂不支持。
- 高中知识点 `grades` 常为空，展示层回退到教材模块名。
- 估时、推荐权重均为可解释的启发式，未用真实作答数据校准。
- SQLite 单文件库：单机/单机多进程没问题；跨机部署需迁 Postgres
  （`store_sqlite.py` 已把 SQL 收敛在一个模块，迁移只动这一处）。
- 会话令牌是无状态 HMAC 签名：没有服务端会话表，退出是删 Cookie；
  需要强制下线时轮换 `data/secret.key`。
- CORS 默认放开（本地演示用）；部署到公网请设 `K12_CORS_ORIGINS`
  （逗号分隔的来源列表）。

## License

数据集 © 2026 猫叔，[PolyForm Noncommercial 1.0.0](../LICENSE)——
本引擎代码同样仅限非商业用途。
