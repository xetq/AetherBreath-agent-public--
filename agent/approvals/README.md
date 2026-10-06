# approvals/ —— 审批规范包（策略外置）

**一类审批 = 一个文件。** 引擎（`agent/approval.py`）启动时自动发现本目录下的
所有模块并加载；新增一类审批**不改引擎、不改接线、不改前端**。

现在的六类：

| KIND | 文件 | 管什么 | 取向 |
|---|---|---|---|
| `cdrive.files` | `fs_drive.py` | 系统盘（C:）的写/删/移 | quiet / ask / block |
| `engine.selfmodify` | `engine_selfmodify.py` | 审批链与引擎关键文件（含工具实现、技能注入、身份红线）| **只问不拒** |
| `net.egress` | `egress.py` | 把数据发往外部（上传/POST/传输/远程执行）| 只问带载荷的出站 |
| `opaque.pipe` | `opaque_pipe.py` | 把编码内容灌进解释器执行 | 只问编码这一类 |
| `skill.install` | `skill_install.py` | 安装第三方技能（会随注册表注入系统提示）| **只问不拒** |
| `mcp.spawn` | `mcp_spawn.py` | 启动 MCP station（本地子进程）| 只问首次启动 |

> **2026-10：`outzone.write` 与 `secrets.read` 已删除。** 四档会话级权限模式上线后，
> 「项目外写入」由**工作区模式**的硬边界接管（区外写/删/移直接拒，连弹卡都不给），
> 不再需要一条「盘外一律问」的规范；「读凭据」也不再是问题 —— 问题在于会不会**发出去**，
> 那由 `net.egress` 管。
>
> 权限模式**不在**这张表里，因为规范只能**加**判定，而模式还要能**压掉**规范
> （工作区内免问、完全模式整体自动同意）—— 那要的是引擎级覆盖权。
> 见 `agent/permission_modes.py` 与 `approval._apply_permission_mode()`。

计划中的：邮件发送确认（`mail` / `sendmail` 已在 `net.egress` 覆盖内）……
都往这里放。

---

## 一、文件约定

| 文件名 | 行为 |
|---|---|
| `xxx.py` | **会被加载**，成为一类活跃的审批规范 |
| `_xxx.py` | **跳过**（下划线前缀）。`_template.py` 靠这条不会误生效 |
| `README.md` | 忽略（只收集 `.py`） |

一个规范模块**必须**提供五个成员，缺一不可：

| 成员 | 类型 | 作用 |
|---|---|---|
| `KIND` | `str` | 稳定标识，写进账本、给作用域（免审规则）用。一旦发布**不要改名** |
| `TITLE` | `str` | 审批卡标题里的动词短语，如 `"在系统盘改动文件"` |
| `RISK` | `int` | `0` 低 / `1` 中 / `2` 高。影响卡片配色与排序（高风险排前） |
| `applies(ctx)` | `-> bool` | 这类审批与本次调用是否相关。**只做便宜的初筛**，别在这里判动作 |
| `finding(ctx)` | `-> dict \| None` | 命中就返回给主人看的描述；不相关返回 `None` |

缺成员或 import 失败**不会静默跳过** —— 会记进 `SPECS_LOAD_ERRORS`，由
`approval.self_check()` 与界面徽标暴露。门悄悄拆了比门没装更危险。

---

## 二、引擎注入的上下文 `ctx`

| 字段 | 说明 |
|---|---|
| `ctx["tool"]` | 工具名，如 `execute_shell` / `execute_python` / `read_file` |
| `ctx["kwargs"]` | 该次调用的原始参数 |
| `ctx["paths"]` | 全部候选路径（已归一化，小写、正斜杠） |
| `ctx["operands"]` | **操作数层**：真正被读/写/删/移的对象（该动的） |
| `ctx["mentions"]` | **提及层**：正文/注释/日志里出现的路径（不该动的） |
| `ctx["control"]` | **指令层**文本：命令词、flag、重定向、函数名链 |
| `ctx["facts"]` | 词法层产出：`cmdwords` / `open_modes` / `redirect_write` / `failed` … |
| `ctx["actions"]` | **动作点**：`{lang, word, args, kw, redirect, mode, recv_paths, cwd}` —— 动词与它的**直接实参**绑在一起 |
| `ctx["root"]` | 项目根（归一化、小写、正斜杠）。判"引擎自留地"用，**不要硬编码绝对路径** |
| `ctx["cwd"]` | 本次调用的工作目录；相对路径必须先在它下面绝对化 |
| `ctx["resolve"]` | 引擎提供的函数：`resolve(文本, cwd) -> 归一化绝对路径`（归一规则只有这一处）|
| `ctx["opaque_paths"]` | 读不懂的代码体里出现过的覆盖面内路径（只是提示，**不是**已确认目标）|
| `ctx["drive"]` | 系统盘根，形如 `c:/`。**永远用这个，不要硬编码盘符** |
| `ctx["forbidden_path"]` | 路径片段型绝对禁区（读它合法，写它才致命） |
| `ctx["mode"]` | `smart` / `strict` / `off` |

**动作点是最重要的那个约定**：目标必须是某个动词的直接实参，不是「这段文本里
出现过的路径」。共现式判定（旧法）造成的三条真实误报见 §六。规范层请用
`_impact.targets_of(动作点)` 取目标，不要自己去扫 `control` 全文。

判定分层的意义（详见 `agent/approval_lex.py`）：
**角色由命令的性质决定，不由引号决定。** 引号里是被搬运的文本时属载荷；
带空格的路径用引号包着仍是操作数。动作只信指令层，目标优先信操作数层。

---

## 三、`finding()` 的三种返回

```python
return None
# 与本次调用无关 —— 引擎继续问下一个规范，最终可能直接放行

return {"block": True, "targets": [...], "intent": "...", "reason": "..."}
# 绝对禁区：不给授权入口，界面不显示「允许」按钮。手滑一次就没了的东西放这

return {"quiet": True, "action": "...", "targets": [...], "reason": "..."}
# 免打扰：记账放行。适用于「量大且无害」（如临时目录、缓存目录）

return {"quiet": False, "action": "...", "targets": [...],
        "intent": "...", "reason": "...", "critical": bool}
# 申请人工审批。intent 给主人看「到底想干什么」，reason 给「为什么被问」
```

`critical=True` 会让卡片措辞更重（系统关键目录），前端默认不勾选、必须主动勾。

---

## 四、新增一类审批：三步

1. `cp _template.py email_send.py`（去掉下划线前缀 = 启用）
2. 改 `KIND` / `TITLE` / `RISK` 与 `applies` / `finding` 的判据
3. 跑测试：`venv/Scripts/python -m pytest tests/ -q`

```bash
# 自检（看是否加载成功、有没有失败项）
venv/Scripts/python -X utf8 -c "import sys,json; sys.path.insert(0,'agent'); import approval; print(json.dumps(approval.self_check(), ensure_ascii=False, indent=1))"
```

---

## 五、写规范时的三条纪律（都是踩过的坑）

1. **动作词只认「函数名精确等值」，绝不裸子串。**
   `k in w` 会让 `json.dumps` 命中「dump」、`csv.writer` 命中「write」，
   把只读代码判成写盘 —— 误报训练主人闭眼点「允许」，比不弹更坏。
   取点号后最后一段再比（见 `cdrive_files._word_hit`）。

2. **先定动作，再取目标。**
   动作判不出来时，路径提得再准也没用（实测 `ctypes…DeleteFileW(盘内路径)`
   曾因动作词缺失而静默放行）。

3. **目标默认只认操作数层；只有操作数层完全为空才回退提及层。**
   回退是为了兜住「路径经变量传递」（`p = 盘内路径; SHFileOperationW(p)`）；
   operands 非空时提及层多半是文档里提到的内容，回退会误报。

---

## 六、真机事故存档

词法分层与批次语义的用例在 `tests/test_approval_engine.py`；
**动词—目标绑定与后果分区**的用例在 `tests/test_approval_binding.py`。

| 事故 | 根因 | 已修 |
|---|---|---|
| 桌面文件被删而闸门报 `pass` | `python - <<'PY' … os.remove(路径) … PY` 的 heredoc 体没被解析 | heredoc 收成整块并按解释器递归解析 |
| `bash -c "rm …"` 静默放行 | `-c` 按 flag 选解析器，被送进 python 解析器后 SyntaxError | 改为按**解释器**选解析器（未知时两种都试） |
| `ctypes…DeleteFileW(路径)` 放行 | 动词表没有 Windows 原生 API | 补 `deletefilew`/`shfileoperationw`/`movefileexw` 等 |
| `p = 路径; SHFileOperationW(p)` 放行 | 路径经变量赋值落在提及层 | 操作数层为空时回退提及层 |
| 整句中文被当成文件目标 | 路径正则没按 CJK 标点收尾 | 正则收尾处理 |

---

## 七、`_impact.py`：所有碰文件系统的规范共用它

下划线前缀 = 不是规范（loader 跳过），但可以被规范 import。它承诺三件事：

| 成员 | 作用 |
|---|---|
| `targets_of(动作点) -> (路径, 依据)` | 把「动词 + 实参」翻译成「要动哪些文件」。方向表 `DEST_LAST` / `FIRST_ONLY` / `ALL_TARGET` 决定取第几个实参 |
| `category_of(动词) -> 删除/移动/写入` | 措辞用的动作类别。**认不出就返回空串**，规范层不许编一句给人看 |
| `self_hit(路径, root)` / `zone(路径, ctx)` | 落在哪个后果区：`drive` 系统盘 / `self` 引擎自留地 / `work` 工作区 / `other` 其它 |

三条取向（都是重放 1700+ 条历史调用量出来的，不要随手改回去）：

1. **只读动词带 `>` 时，被改动的是重定向目标，源只是被读。**
   `cat 源 > 目标` 不许把源列进 targets。
2. **`2>` 是 stderr，不是写。** 曾因此让 `grep x 自留地文件 2>/dev/null` 变成
   「改动引擎文件」，重放时一次制造几十条误报。
3. **认不出类别、又没有写信号的动词不算改动。**
   最初我按「未知动词保守取全部实参」写，历史重放立刻冒出 125 条弹窗 ——
   `python x.py` / `git diff y` / `pytest z` 全是引用。**引用文件不是改动文件。**

现有的审批（完整清单见文首表格）：

| KIND | 文件 | 管什么 | 取向 |
|---|---|---|---|
| `cdrive.files` | `fs_drive.py` | 系统盘文件的写/删/移 | quiet / ask / block（关键系统文件）|
| `engine.selfmodify` | `engine_selfmodify.py` | 审批链与引擎自身的文件 | **只问不拒** —— 若改引擎不可授权，规则一旦写错我就再也修不了自己 |

| 事故（2026-09-11 重放 1700+ 条历史调用查出） | 根因 | 现在 |
|---|---|---|
| 脚本里一行 `SD = '盘符'` 常量被弹成「对该盘执行写入」 | 操作数提取不到相对路径 → 触发「operands 空则采纳 mentions」回退 → 常量变目标 | 回退删除；变量传路径改由词法层常量表 `_const_vars` 负责 |
| `cmd.lower().replace('a','b')` 被判「移动文件」 | 动词与路径共现即成立；`_DATA_RECV` 不覆盖链式调用 | 动作点绑定 + `_recv_is_text` 认链式 |
| `cp 盘内/x D:/backup` 谎称「对盘内/x 执行写入」 | 不区分源与目的地 | `DEST_LAST` 取目的地；读走的不算改动 |
| `cat a > b` 把源 a 也列进目标 | 只读动词带重定向时未收窄 | 只读动词 + 重定向 → 只有目标算改动 |
| `grep x 自留地 2>/dev/null` 判成改动引擎文件 | `2>` 被 lstrip("012") 后当成写重定向 | 文件描述符分开判，`2>` 不算写 |
| `rd('…/bridge.py')` 判成「删除引擎文件」 | shell 命令名撞上 python 同名局部函数 | `SHELL_ONLY` 表：py 动作点上不认 shell 裸命令名 |
| `python x.py`/`git diff y` 一类引用共 125 条弹窗 | 「未知动词保守取全部实参」 | 取向反转：认不出类别且无写信号 = 不算改动 |
| 改 `agent/approval.py`、往免审规则追加、删工作区全部静默放行 | 审批入口判据是「路径在系统盘」→ 执行者可无损改写审查者 | 新增 `engine.selfmodify` 规范（只问不拒）|
| 规范内部抛异常时整道门消失（写系统盘判 pass） | `spec_error` 分支 `continue` = 残留 fail-open | 异常升级为 ask，并记 `spec_fail_closed` |
| 代码语法解析失败（`python -c '坏代码'`）静默放行 | 切断 operands 降级后无人管它 | `opaque` 动作点 → ask，明写「目标未能确定，只是提示」 |

### 2026-09-11 安全审计（prompt injection / 数据外泄 / 工具滥用）

审计报告：`workspace/security-audit-aetherbreath/AUDIT-REPORT.md`；回归网：
`tests/test_security_hardening.py`（42 条断言）。

| 事故 | 根因 | 现在 |
|---|---|---|
| 外发、读凭据、写 D 盘**全部静默放行**（15 条探针里 11 条 pass）| 覆盖面只有两类：系统盘写 + 引擎自留地。「读」「出网」「盘外」无人管 | 新增 `net.egress` / `outzone.write` / `secrets.read` 三类 |
| `printf x > $USERPROFILE/Desktop/x`、`~/x`、`//localhost/c$/…` 绕过系统盘审批（10/10 漏判）| 归一只认字面路径（`_RE_ENV` 只认 `%VAR%` 与 `$env:VAR`）| `unify()` 归一前展开 shell 家目录引用；本机管理共享映射回 `c:/` |
| `echo <base64> \| base64 -d \| bash` 绕过**block 级**禁区 | 管道符被词法层当分段符丢掉，判据看不到「编码后执行」| 新增 `opaque.pipe`（只问编码这一类，`cat x.sh \| bash` 仍不问）|
| `requests.get('…?d='+secret)` 判 pass | `net.egress` 的「纯 GET 不问」被「GET+URL 载荷」绕开 | 补 URL 拼接/命令替换判据（shell 与 python 两侧）|
| `fetch_url` 零守卫（同类 `web_extract` 有两道）| 守卫写在各工具内部 | 抽到 `agent_tools/url_safety.py`，两个工具同源 |
| 参数里没有路径的工具整条免审（`skillhub_install`）| `inspect_one` 在「无 paths/actions」处提前 return | 新增 `_tool_only_hit()`：无路径也要按工具名问一遍规范 |
| 改 `agent_tools/`、`skill_system.py`、自己的 `SOUL.md` 全放行 | `SELF_PREFIXES` 只列了「审批链相关」的少数文件 | 扩表：工具实现 / 技能注入 / 留痕 / 身份与红线 |
| `tests/test_approval_engine` 断言「规范恰好两类」| 断言写死数量，新增规范必红 | 改为「必需规范都在」——红多了就会被当噪音顺手改掉 |

### 已知限制（本轮未修，诚实记录）

1. **`python open(os.path.expanduser('~/…'),'w')` 仍漏判**：词法层把嵌套调用的路径参数
   归进 mentions，`open` 动作点的 args 只剩 `'w'`。修它要动 `approval_lex` 的参数归属，
   误报风险高，暂缓（宁可不做也不制造误报）。
2. **shell 变量中转仍漏判**：`D=$USERPROFILE/Desktop; printf x > $D/leak.txt`、
   `P="C:/…"; cp .env $P/x`（python 的常量表 `_const_vars` 只管 python 侧）。
3. **`cd X && 相对路径写` 不追踪 cwd**：`cd $USERPROFILE/Desktop && printf x > leak.txt`。
4. **多 agent 子系统（`subagents/`）不经过本引擎**：那里有独立的
   `shell_executor.py`（`shell=True` + 15 条子串黑名单 + **零审批**），是第二个执行入口。
5. **`NON_FS_TOOLS` 里的工具走不到规范层**（search / fetch_url / web_extract / rag_query …）：
   设计如此（避免把搜索词当路径误报）。`fetch_url`/`web_extract` 的 URL 侧风险由
   `url_safety.py` 承担；`execute_browser` 不受审批（浏览器本身就在访问网络）。
