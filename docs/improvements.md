# 待改进清单

2026-10-27 路线图第 1 步的进度：医生目录 `providers.sqlite` 已用真实数据建好并上传到 Railway `/data`（NPPES API 抓了价格库里全部 68,333 个 NPI，68,325 个在册；97,115 个执业地点，92.4% 有 Census 坐标；校验通过）。`find_providers` 线上可用。问价格（`estimate_cost`）和 Arena（`compare_plans`）里的**普通门诊和专科**改成按用户邮编附近的具体医生定价：问价格给最近 5 位各自的价格，Arena 每个计划用其网络里最近一位有价格的医生；查不到就如实说，不用中位数顶替。注意：现有 9 个计划的门诊和专科都是固定挂号费、不走免赔额，所以医生价格目前不改变用户要付的钱，改的是透明度。还没做：① 急诊、化验、影像、心理咨询、远程问诊仍按全网中位数（影像和化验最需要按机构定价，因为走免赔额和比例付费）；② HMO 先选医疗集团再选医生；③ 问卷里"愿意去多远"（现在固定 25 英里内找）；④ 医生现在是否还收这个保险，没有核实，只靠提醒用户先打电话。⑤ 医生的分类靠 NPPES 自报的专科猜：普通门诊只找只登记了家庭医学科、内科或全科的医生（这类医生里 38% 还登记了细分专科、住院医生或受训，保险公司多半按专科算），所有搜索都排除受训的（390200000X）。价格文件按合同整批列价，有价格不代表做这项服务。更可靠的是 Medicare "by Provider and Service" 数据（每个 NPI 实际做过哪些服务、多少次），和路线图第 2 步一起接。

记录于 2026-10-18，2026-10-20 更新。按优先级排列，每条写清楚现状、问题和建议做法。做完一条就把它挪到文末的"已完成"，或者直接删掉。

现状概要：产品只部署成 Railway 上的一个服务 `agent-api`（https://agent-api-production-adc8.up.railway.app），从 `main` 分支自动部署。功能一（一次看病的自付金额，`estimate_cost`）和功能二（Arena 对比，`compare_plans`）都已做完。保单库 `data/plans/policy.sqlite` 有 9 个 plan（2026-10-24 删掉了洛杉矶买不到的 2 个），都有带证据的网络对接（桥 1）。价格库 `rates.sqlite`（3.1 GB，Railway 磁盘卷 `/data`）运行时直接查。普通门诊和专科按附近具体医生的价格算（2026-10-27），其余场景暂时按网络里所有洛杉矶医生的中位数算。2026-10-20 删掉了 legacy 模式、搜索 worker 和 CSV 价格汇总表，现在只有一条路径。相关 issue：#5（人和计划的年度匹配）、#6（保单 × 价格对接）。

---

## 路线图（2026-10-20 定，2026-10-26 更新）

**2026-10-26 到 11-04 是 Meta 黑客松**，这段时间的范围、分工表和日程以 `docs/hackathon-meta-2026.md` 为准。黑客松要做的，按顺序：
- **A. 按医生定价**：下面第 1 步。Arena 也改成按医生算，去掉中位数。
- **B. 账单审核**（新加，见下面第 1.5 步）。
- **C. 补全费用计算**：只补 demo 用到的缺口（P2 第 5 条）；HPV、生孩子先不做；急诊和药品不在产品范围内（2026-10-27 定，见 `PRODUCT.md`）。
- **D. 前端去 AI 化**，加上医生卡和账单审核结果卡。
- 有余力再做：E. 找保单的搜索（P1 第 1 条），F. 微调一个读账单的开源小模型。
  - **F 的进度（2026-10-28）**：Fireworks 暂停了数据集上传（API 返回 "dataset uploads are temporarily disabled"），改在 Modal 上用 Axolotl 训（`scripts/bills/modal_sft.py`）。Muse Glimmer 30B，QLoRA，只训语言层，900 张生成的账单图片，2 个 epoch，1 张 H100 约 37 分钟。eval-100 上的 loss 从 0.145 降到 0.003。LoRA 在 Modal volume `bills-sft` 的 `/outputs/bills-sft-glimmer-v1/`。坑：HF `datasets` 会把内容是 JSON 字符串的 assistant 回答解析成 dict，模板于是丢掉这一轮、loss 全为 0，所以字符串内容一律写成 text 列表；`train` 开 GPU 前会先跑 `check_labels` 检查。**eval-100 准确率**（`scripts/bills/modal_eval.py`、`eval_spark.py`、`score.py`；Python 打分，逐行按位置比较）：整张账单全对 没训练的 Glimmer 73%、微调后 86%、Muse Spark（`muse-spark-open`）76%；所有字段合计 97.4% / 99.2% / 85.2%。微调前后差异显著（只有微调后对 16 张，只有训练前对 3 张，McNemar p=0.004）。Muse Spark 拒答了 6 张（说账单含 HIV、性病检测等敏感健康信息），这正是自己部署小模型的理由。微调后剩下的错多是单个数字或字符读错（金额 7 处、NPI 4 处），审核时可以让 Python 用"各行加起来等于总额"、NPI 校验位来拦一部分。还没做：真实账单上的准确率，以及部署。
- 还要做：第 1 天把模型换成 Meta Model API（先确认支持 `server.py` 用的 Responses API），最后一天录 demo 视频和提交。

按顺序做：

1. **按位置推荐医生（geographic matching）**：不取中位数，只有两条路：用户指定了医生，就按这位医生的协商价算；没指定，就找出计划网络里合适专科的候选医生，每位都按自己的协商价算出用户要付多少，由 agent 结合用户的全部信息明确推荐 1–5 位。
   - Python 负责查找和算钱（harness 里的工具），agent 负责判断和解释。
   - 医生是谁、在哪：用价格库里的 NPI 查 NPPES（政府免费接口：姓名、地址、电话、专科、执照号）。注意价格库里的 `tin_business_name` 是收款方（税号登记名），不一定是看病的医生。
   - 位置：问卷加一道"愿意去多远"的选择题。距离具体怎么算，以后再定。
   - HMO：按每个计划自己的规则筛选（先选医疗集团，再选集团里的医生）。L.A. Care 的价格文件里网络名就是医疗集团。
   - `allowed.component()` 已经支持按 NPI 取这位医生的价格（`provider_rate`）。
1.5. **账单审核（看病后）**：用户上传账单或 EOB，多模态模型只抽取每一行（医生、日期、收费编号、金额，JSON schema 约束），Python 用第 1 步的按 NPI 查价和计算器逐行核对，标出多收、差价、重复收费、急诊按网外收费；agent 解释并起草申诉信。价格库里没有的编号或医生如实标"没有数据"。产品说明见 `PRODUCT.md` 的"账单审核"。测试用程序生成的样例账单，答案已知，同时也是微调的评测集。
2. **医生水平（rate my doctor）**：给每位医生建一份档案，每一项都有公开来源，agent 按用户在意的点比较。具体做法再商量。候选来源：加州医学委员会（处分记录，按执照号）、CMS Care Compare（MIPS 绩效分，不是每个医生都有，例如 NPI 1205190659 Jun Chung 就没有；医学院和毕业年份）、Medicare 服务量、CMS 医院星级、搜索（职称、执业医院）。查不到的项说"没有公开数据"，不能编。水平低的医生要如实告诉用户。
3. **更深的 computer search**：用 Computer Use 查医生和医院的评价（例如 Google 评价）。注意网站条款对自动抓取的限制。
4. **登录**
5. **个人信息的长期记忆**：登录以后，记住用户的病情、常看的医生、计划等，下次不用再问。

前提：桥 1（计划对网络）必须先全部确认。2026-10-20 已完成：当时的 11 个计划都有证据（`data/plans/plan_networks.csv` 的 `notes`）。以后加计划按 `db/README.md` 的"Adding a plan"。

---

## P0：现在就坏的

### issue #6（保单 × 价格对接）的状态

2026-10-21 做完：默认价格不再用 UCLA 价；区间算对；对接代码不再崩溃；桥 1 全部有证据；桥 2 审过一遍（`SCENARIOS` 和 `encounters.csv`，依据是 SBC 的联邦费用示例和 Covered CA 2026 统一设计）；copay 不超过服务价格（Covered CA 脚注 6，原来算错）；Ashe Center 看流感按普通门诊算；远程看诊没有 98005 价格时用 99213；样本太少的价格会带警告；`scripts/mrf/check_rates.py` 做跨网络检查。

还剩（`check_rates.py` 报出的就是这些）：
- **HIV 检测**：现在常用 87389，但当初只导入了 86701，Health Net 没有 86701 的价格。要把 87389 加进 `data/mrf/billing_codes.csv` 再跑一次导入。

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

### 1.6 账单照片和 Gmail 申诉信（2026-10-26）

- **前端和 mock 已做**（Lovable 项目 "Health Coverage Hub"，见 `web/README.md`）：上传照片、账单审核页、可编辑的邮件卡片、"Send from Gmail"。协议见 `docs/frontend-protocol.md` 最后一节。
- **后端**：`/upload` 已收照片，但模型还读不了照片，现在会如实说读不了，并用问卷问服务、医生和账单金额。待做：多模态逐行读账单（路线图 B），`draft_email` 工具和 `email_draft` / `email_status` 事件，Gmail OAuth（每个用户自己的 token）和 `/integrations` 的真实状态。
- **对比页的默认数字**：计划卡片显示滑块选中的场景，默认是中间那个（`healthy`，只含保费），不是 `expected`。要改的话在 Lovable 里改。

### 2. 按具体医生查价

已并入上面路线图第 1 步。

### 3. 问卷改版

2026-10-24 按队友反馈（`docs/feedback-2026-10-24.md`）改了一轮：就诊次数题由 Python 传给 `compare_plans` 按次计价；四道优先级打分题由 `plan_db.rank_plans` 按公式排名，并加了题干 "What matters most to you?"；预算题让超预算的计划排在后面；"想保留的医生或医院"改成多选。
还没做的：`keep_providers` 的选项写死了 UCLA 的两家机构，不适合全美用户；"保留医生"这个优先级暂时不计分（没有医生在不在网络的数据，等路线图第 1 步）；`network_types_ok` 收集了但没用来筛选。

### 4. 对话速度

2026-10-24 换成 Kimi（`kimi-for-coding`）后实测：简单的问价一轮 5–12 秒（以前 25 秒到 4 分钟），带问卷的 Arena 一轮大约 30–60 秒。暂时不用再调。

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

- 急诊诊所（urgent care）看流感时的快速流感检测按化验单独计费。有些计划会把这类检查算进诊所的 copay 里，这样估出来会偏高。目前是保守做法，可以按各 plan 的 SBC 说明细化。
- 还没对应上的场景：`pt_evaluation`（物理治疗评估，对应 `other` 里的 Rehabilitation services）、`spirometry`（肺功能检查）。
- 急诊和药品不做（2026-10-27 定）：去掉了急诊场景 `er_ankle_xray`、问卷的急诊次数和常用药两题；`encounters.csv` 里急诊的几行留给 MRF 管道。
- 没有住院、生孩子这类大额场景。价格库里有些代码，但场景表还没定义。

---

## P3：数据覆盖

### 8. 缺的 plan

- **Anthem Bronze 60、Platinum 90**：官网的 SBC 下载失败（见 `data/plans/sbc-2026-bundle-manifest.csv`），需要另找来源，或者用 EOC 抽。
- **UHC Choice Plus、Cigna National PPO**：价格库里有这两个网络，但加州没有对应的、面向本地人的个人计划（见 `docs/research/sbc-2026-part-b-network-candidates.md`）。UHC 唯一的候选是 Saint Mary's College 的学生保险，它的 SBC 要登录才能下载。
- **Prime 500**（国际学生计划）：不在产品的目标用户范围内。如果要用来演示，可以用 `extract_sbc.py` 抽一遍，网络对应 `uhc_ca_choice_plus`。
- **其他 UC 校区的 UC SHIP**：只收了 UCLA。`db_catalog()` 匹配时要求出现 "Los Angeles"，所以其他校区的 SBC 不会被误认成 UCLA 的。

### 9. 保费

2026-10-24 接入：`scripts/policy/extract_premiums.py` 从官方 PDF 解析出 `data/plans/premiums.csv`、`age_curve.csv`、`rating_regions.csv`，`build_seed.py` 建进 `policy.sqlite`。Arena 现在按"一年保费 + 看病花费"（`year_total`）排名；用户给优先级打了分时按加权分排名（2026-10-24，见 `docs/feedback-2026-10-24.md`）。年龄、ZIP、本科/研究生由 Python 从问卷答案里填，缺了就自动发表单。还没做的：
- **补贴**：保费是补贴前的标价，没算 Covered CA 的保费税收抵免（APTC）。低收入的学生拿到补贴后，Covered CA 的计划可能便宜很多，这时排名会变。要算的话需要家庭收入和人数这两道题，加上第二便宜 Silver 计划（benchmark）的保费。
- **初步费率**：费率册是 2025-08-01 的初步版。Shop & Compare 上的最终价可能有几块钱的差别，找到最终版的公开文件就换掉。
- **2027 年**：2027 年的开放注册从 2026-11-01 开始，到时候计划和保费都要换成 2027 年的。
- **洛杉矶买不到的计划（2026-10-24 已删）**：费率册的洛杉矶页面没有 Anthem EPO，也没有 Health Net HMO 的 Bronze，所以 Anthem Silver 70 EPO 和 Health Net Bronze 60 HMO 已经从数据库删掉了，抽取的 CSV 和网络对接的证据都还在 git 历史里。注意 `sbc-2026-bundle-manifest.csv` 里写着 Ambetter HMO 覆盖洛杉矶县，但那说的是这个产品线，不是 Bronze 这一档。以后要支持洛杉矶以外的地区，可以从历史里把它们恢复回来。
- **UC SHIP 的其他项目**：只收了 UCLA 按季度注册的本科生和研究生。法学院、医学院和暑期项目的保费不一样（日历上都有），还没收。
- **年龄换算**：除了 25 岁和 40 岁，其他年龄都是用 40 岁的价格按 CMS 联邦默认年龄曲线换算的。用 25 岁的实际价格校验过，最多差 $0.72。

---

## P4：工程和运维

2026-10-24 做完：会话存进 SQLite（`workflow/sessions.py`，Railway 上在卷里 `/data/sessions.sqlite`，上传的 PDF 在 `/data/uploads`）。重新部署也不丢对话，保留 24 小时（`SESSION_TTL`），过期的连同上传文件每小时清理一次。删掉了 `review_sheet.py`、`import_plan_csv.py` 和它们的测试。Railway 上不用的变量都删了。"免赔额已经付完"现在算作用户说了 0。

还剩：
- **服务器上的压缩包**：Railway 磁盘卷里还有 `/rates.sqlite.xz`（273 MB），已经没用了。只能本人删除：`railway volume files --volume rates-data delete /rates.sqlite.xz`
- **换 key**：Kimi key 在聊天里发过，旧的 AMD key 也在命令输出里完整显示过一次。要换成新 key，然后改 Railway 上的 `LLM_API_KEY`。
- **前端的确认卡片**（`confirm`）：后端已经不发了，前端还支持，可以删。`web/` 现在有别人在改，等那边提交了再动。
- **多实例**：会话已经能多个实例共用，但 SQLite 文件在一个卷上，Railway 的卷只能挂一个服务实例，真要扩容得换成 Postgres 之类。现在一个实例够用。
- **搜索**：旧的搜索 worker 已删除，新的搜索还没定，候选方向见 `PRODUCT.md` 的"搜索"一节。
- **本地分支**：本地还有两个只在本机的分支（`mr-jude-justin/amd-hackathon-plan`、`mr-jude-justin/latest-version-status`），被 Conductor 工作区占用着，各有 27–29 个没合进 main 的提交。
- **之前用 AMD 接口（Qwen）抽取时踩过的坑**，换供应商后可能还会遇到：
  - 开着思考模式时，一页福利表要十几分钟，一定要用 chat API 关掉思考（Qwen 是 `LLM_EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}`）；
  - 并发请求太多会返回 429，所以一次只抽一个 plan，并带退避重试；
  - 温度为 0 时偶尔陷入重复输出的死循环，所以要设 `max_tokens`，并在重试时调高温度。

## 前端改版后测出的问题（2026-10-24）

- **Python 自动发的表单不预填**：用户在聊天里说了"22 岁、本科生、90024"，`compare_plans` 缺这三项时自动发表单（`server.py` 的 `auto_ask` → `build_form`），但表单是空的，用户得重新填一遍。应该用问卷已有的 `_stated_value` 检查，把用户说过的值预填上。

## 数据阶段要看的回复问题

- 2026-10-24 实测：Kimi 在 Arena 的回复里说了"L.A. Care 的网络里 UCLA 的医生有限"，这句话没有数据支持。要么在提示里禁止对网络大小下判断，要么等路线图第 1 步有了医生数据再说。
