# 待改进清单

记录于 2026-10-18，2026-10-20 更新。按优先级排列，每条写清楚现状、问题和建议做法。做完一条就把它挪到文末的"已完成"，或者直接删掉。

现状概要：产品只部署成 Railway 上的一个服务 `agent-api`（https://agent-api-production-adc8.up.railway.app），从 `main` 分支自动部署。功能一（一次看病的自付金额，`estimate_cost`）和功能二（Arena 对比，`compare_plans`）都已做完。保单库 `data/plans/policy.sqlite` 有 11 个 plan，价格库 `rates.sqlite`（3.1 GB）放在 Railway 磁盘卷的 `/data`，目前运行时不读它。2026-10-20 删掉了 legacy 模式和搜索 worker，现在只有一条路径。

---

## P0：现在就坏的

### 线上服务对所有问题都报错

2026-10-20 实测：线上对任何问题都返回 `AuthenticationError: 401 Invalid bearer token`。AMD 的 API key 已经失效。换成新的供应商以后，在 Railway 上把变量改成 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`（旧的 `AMD_*` 已经不读了）。

### "远程看诊"场景在 8 个计划上崩溃

2026-10-20 把 11 个计划 × 17 个场景全部跑了一遍：140 个算出结果，39 个按设计要追问用户（选网络档位、报剩余自付额），**8 个崩溃**：Health Net 和 L.A. Care 的全部 8 个计划，一算 `telehealth_visit` 就抛 `ValueError`。原因是 `data/mrf/allowed_amounts.csv` 里这个场景的价格是空字符串，`plan_db.allowed_amounts()`（`float(row["allowed_amount"])`）没有处理。空值应该返回 `no_rates`，而不是抛异常。

### 数据自动校验

Covered CA 同一档次的标准计划，各家保险公司的 copay 应该完全一样，可以拿来自动交叉检查。2026-10-20 手工比了一次：大部分一致，不一致的集中在 L.A. Care Silver 70（见第 6 条，7 项被错标成要先过自付额）和 Anthem（同一服务多行，Anthem Gold 的 X 光有 22 行，其中 15 行算不了）。建议把这个比对写成 `build_seed.py` 的一步：不一致就打上 review_flags，没核对过的不给用户看。再准备一套固定问题和标准答案，每次改动后自动跑。

---

## P1：产品功能

### 1. 上传 PDF 当场抽取

**现状**：上传的 PDF 只读第 1 页认出名字，再去保单库里找（`harness/reader.py` 的 `db_catalog()`）。库里没有的 plan（比如 Prime 500）只能回答"算不了"。

**做法**：
1. 找不到对应的 plan 时，在服务里调用 `scripts/policy/extract_sbc.py` 的抽取流程。先把它改成可以 import 的函数，进度通过 `search_step` 事件推给前端面板（复用现在的 "Agent computer" 面板）。
2. 抽出的数据存成**这次对话专用的临时 plan**，不写进公共保单库，回答时注明"由模型从你的上传中读取，未经人工核对"。`plan_db.PlanDB` 要支持同时查公共库和这次对话的临时 plan，比如为每个对话建一个内存 SQLite，并 ATTACH 公共库。
3. 网络匹配：模型从保单原文里找出网络名（只做摘录），Python 再和 `data/plan_profiles.v1.json` 的网络名单比对，比对上就生成 `plan_networks` 行，自动接上协商价。比对不上时只能算固定 copay。
4. 定位福利页：SBC 用现成的 `find_pages()`，很稳；长手册或 EOC 要按 "Schedule of Benefits"、"Copayment"、"Coinsurance" 等关键词的密度给每页打分，挑出连续的高分页段。

**耗时**：标准 SBC 3–5 分钟，65 页以上的长保单会更久。
**工作量**：约半天，主要花在长保单的测试上。

### 2. 接上 3.1 GB 价格库，支持按具体医生查价

**现状**：`plan_db.py` 读的是 `data/mrf/allowed_amounts.csv`。这是 `scripts/mrf/report.py` 从 3.1 GB 的库预先算好的汇总：每个网络 × 每个就诊场景一个典型协商价（UCLA Health 价或洛杉矶中位数），附 p10/p90 区间。线上卷里的 `rates.sqlite` 目前运行时没人读（以前只有已删除的 legacy pricing 技能用它）。

**做法**：
- `estimate_cost` 增加一个可选参数 `provider_npi`。传了 NPI 时，用 `scripts/mrf/allowed.py` 的 `fetch_candidates` 在 `rates.sqlite` 里按 plan_key + 收费代码 + NPI 查真实协商价，查不到再退回汇总值。
- 前端估价卡片标明价格来源：是这位医生的协商价，还是网络中位数。
- 回答里可以给出 p10–p90 区间，而不只是一个数。

### 3. 问卷改版

用户提过要改，内容还没讨论。现状：`harness/skills/questionnaire/bank.py` 还是旧的题库，Arena 已经能用上里面的权重题（保费、看病成本、坏年份保障）。
**要对齐的地方**：每道题的 `maps_to` 应该对应到新的输入，也就是 `estimate_cost` 的场景、档位、剩余自付额，以及 `compare_plans` 的场景组合。可以考虑加一道"一年里预计会用到哪些医疗服务、各几次"的题，直接生成 Arena 用的场景列表。

### 4. 对话速度

**现状**：一轮对话 25 秒到 4 分钟，主要是模型开着思考模式（`server.py` 的 `agent_loop` 用的是 responses API）。抽取那边关掉思考后快了十倍以上。
**做法**：拿几组固定问题，对比开和关思考时的回答质量和用时，再决定 agent 主循环要不要关。也可以只在第一步（理解问题、选工具）开思考，后面写回复时关掉。

---

## P2：算钱的准确度

### 5. 计算器还不支持的情况

这些行会被 `build_seed.py` 打上 review_flags，`calculable=0`，计算器返回 `needs_review`，不会算错，但也算不出来：
- **封顶**（`cap`）：比如"40% 自付，每张处方最多 $500"。两个 Bronze 计划的品牌药就卡在这里，导致这两个 plan 的 12 项标准服务只能算 11 项。
- **加罚**（`penalty`）：比如网外住院"加罚 25%"。
- **补差价**（`balance_billing`）：网外机构按账单金额减去协商价收取的差额。
- **自付上限**（OOP max）：目前完全没算进去。Arena 的年度对比在花费很高时会高估自付额。
- **同时有 copay 和比例自付，而且还要先过自付额**：仍然返回 `needs_review`。

### 6. 抽取数据里的已知问题

- **L.A. Care Silver 70**：急诊诊所和心理门诊的 copay 行被判为"要先过自付额"。原因是 SBC 第 1 页只点名了初级诊疗、专科和预防保健；表头又写着"所有费用均为过自付额之后的金额"。Covered CA 的统一标准里，这些服务其实不走自付额。所以 Arena 里它比另外两个 Silver 贵（"自付额没付"时 $789.71，另外两个是 $665）。可以考虑用 `data/policy_pdfs/B_coveredca_designs.pdf`（Covered CA 统一档次设计）做交叉检查。
- **UC SHIP 网外初级诊疗和专科走不走自付额**：Perplexity 版填的是 no。SBC 第 1 页免自付额清单里的 "network" 修饰到哪里有歧义，需要查手册（`A1_ucship_booklet.pdf`）定下来。
- **Anthem EOC 的零售和邮寄两行**：Tier 2 药品有零售（$60）和邮寄（$180）两行。`plan_db.benefit()` 会降低邮寄行的优先级，取零售行，所以结果对。但抽取时可以再加一个字段区分这两种情况。
- **"走不走自付额"填 NOT FOUND 的行**：每个 plan 有 1–25 行（Anthem Gold 最多），这些行算不了钱。

### 7. 场景表（`plan_db.SCENARIOS`）

- 急诊崴脚拍片里的 X 光按 X 光福利单独计费，急诊诊所看流感时的快速流感检测按化验单独计费。有些计划会把这类检查算进急诊或诊所的 copay 里，这样估出来会偏高。目前是保守做法，可以按各 plan 的 SBC 说明细化。
- 还没对应上的场景：`pt_evaluation`（物理治疗评估，对应 `other` 里的 Rehabilitation services）、`spirometry`（肺功能检查）。
- 没有药品场景：价格库里没有药价，只能算纯 copay 的药。可以加"仿制药一次"、"品牌药一次"这类只看 copay 的场景。
- 没有住院、生孩子这类大额场景。价格库里有些代码，但场景表还没定义。

---

## P3：数据覆盖

### 8. 缺的 plan

- **Anthem Bronze 60、Platinum 90**：官网的 SBC 下载失败（见 `data/plans/sbc-2026-bundle-manifest.csv`），需要另找来源，或者用 EOC 抽。
- **UHC Choice Plus、Cigna National PPO**：价格库里有这两个网络，但加州没有对应的、面向本地人的个人计划（见 `docs/research/sbc-2026-part-b-network-candidates.md`）。UHC 唯一的候选是 Saint Mary's College 的学生保险，它的 SBC 要登录才能下载。
- **Prime 500**（国际学生计划）：不在产品的目标用户范围内。如果要用来演示，可以用 `extract_sbc.py` 抽一遍，网络对应 `uhc_ca_choice_plus`。
- **其他 UC 校区的 UC SHIP**：只收了 UCLA。`db_catalog()` 匹配时要求出现 "Los Angeles"，所以其他校区的 SBC 不会被误认成 UCLA 的。

### 9. 保费

Arena 没有保费数据，所以只比较看病的花费。`data/policy_pdfs/C_coveredca_rates.pdf` 是 Covered CA 2026 的保费表，可以抽成 SQL 表，按年龄和地区查保费，让 Arena 给出"保费 + 预计看病花费"的年度总成本。

---

## P4：工程和运维

- **服务器上的压缩包**：Railway 磁盘卷里还有 `/rates.sqlite.xz`（273 MB），已经没用了。按 Railway 的规定只能本人删除：`railway volume files --volume rates-data delete /rates.sqlite.xz`
- **搜索**：旧的搜索 worker 已删除，新的搜索还没定，候选方向见 `PRODUCT.md` 的"搜索"一节。
- **会话存在内存里**：`server.py` 的 `SESSIONS = {}` 永远不清理，重新部署就全部丢失，也不能开多个实例。至少要改成 SQLite 存储并设过期时间。
- **legacy 剩下的**：`scripts/policy/review_sheet.py`（和 `tests/test_review_sheet.py`）只处理已删除的 `import_policy.py` 产出的规则 JSON，可以删。`scripts/policy/import_plan_csv.py` 自己的输出（`policies/*.json`）也没人用了，只有 `build_seed.py` 还在用它的 CSV 辅助函数，可以把那几个函数挪进 `build_seed.py` 后删掉。前端仍支持确认卡片（`confirm`），后端已经不发了。
- **`agent-api` 上用不到的变量**：`CORS_ORIGINS`。前后端已经同域，可以删掉。
- **本地分支**：本地还有两个只在本机的分支（`mr-jude-justin/amd-hackathon-plan`、`mr-jude-justin/latest-version-status`），被 Conductor 工作区占用着，各有 27–29 个没合进 main 的提交。
- **之前用 AMD 接口（Qwen）抽取时踩过的坑**，换供应商后可能还会遇到：
  - 开着思考模式时，一页福利表要十几分钟，一定要用 chat API 关掉思考（Qwen 是 `LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}`）；
  - 并发请求太多会返回 429，所以一次只抽一个 plan，并带退避重试；
  - 温度为 0 时偶尔陷入重复输出的死循环，所以要设 `max_tokens`，并在重试时调高温度。
