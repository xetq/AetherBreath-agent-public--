# 第三方来源与声明

本仓库的绝大部分代码由 AetherBreath 自行编写。以下部分**借鉴了外部项目的方法论或设计思路**
（均为独立重写，不是代码拷贝），在此致谢并声明来源。

---

## Hermes Agent

`agent_tools/` 中的以下模块，在**工具语义与设计思路**上借鉴了 Hermes Agent：

| 本仓库文件 | 借鉴的内容 |
|---|---|
| `agent_tools/web_extract.py` | 参考 Hermes `tools/web_tools.py` 中 `web_extract_tool()` 的工具语义（入参形状、返回结构、超长内容的头尾截断与全文落盘策略）。本项目为**纯本地重实现**：requests + BeautifulSoup + 自写 HTML→Markdown，不依赖 firecrawl / tavily / exa 等第三方抓取服务，**无需任何 API key** |
| `agent_tools/url_safety.py` | 参考 Hermes `tools/url_safety.py` 的 URL 安全校验思路：疑似密钥/令牌拦截、内网与回环地址拦截（SSRF 防护） |
| `agent_tools/skillhub_download.py` | 与 Hermes skillhub 生态（技能仓库）的对接方式 |

> Hermes Agent 的代码与许可归其原作者所有。**本仓库不包含 Hermes 的源代码**，
> 上述模块均为在本项目内独立实现的版本。

---

## 其他第三方资源

- `agent_skills/` 中的技能，如来自外部，各自保留其原始许可与来源说明（技能文件夹内一般有出处注释）。
- `agent_integration_packs/` 中的集成包同理，详见各包的 `PACK.md`。
- `agent_webui/frontend/` 使用 npm 生态依赖（React / Vite / marked 等），各自遵循其 `package.json` 与 `node_modules` 中声明的许可。
- Python 侧依赖见 `requirements.txt`，各自遵循其发行许可。

---

## 若有遗漏或标注有误

我们尽力标注每一处外部来源，但**如果仍有遗漏、标注不准，或你认为某处应当补充声明**，
请通过 GitHub Issue 联系作者，我们会尽快补正：

<https://github.com/xetq/AetherBreath-agent-public--/issues>
