# verify-probe —— 验证"检查器真的会失败"

**解决的问题**：`verify.py` 的语法检查层曾**静默失效** —— `py_compile(..., quiet=2)`
在 Python 3.11.15 上会吞掉语法错，一路报"合格"（见 `NOTES.md` 2026-09-17 第一条）。
检查类代码最隐蔽的坏法就是"永远返回通过" —— 它每天都看起来正常。

**怎么用**（项目根）：

    venv/Scripts/python.exe self_maintenance/packs/verify-probe/probe_compile.py

**副作用**：在 `self_maintenance/workbench/` 下临时造两个文件（一个故意语法错、一个正常），
跑完自动删除。**不碰生产文件、不联网、不改配置**。

**为什么必须双向**：
- 只喂坏文件 → 漏掉"永远报错"的反向坏法
- 只喂好文件 → 漏掉"永远通过"的正向坏法
- 所以两个都喂：坏文件必须 False、好文件必须 True，缺一不可。

**输出判据**：`PASS` / `FAIL`（退出码 0 / 1，可手工跑，也可进 CI）。
