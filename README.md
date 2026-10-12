# 10-K → Markdown

把本项目中的每份 SEC 10-K 单独转换为一个 Markdown 文件，并计算转换前后的 token 数。转换完全在本地完成，不调用 LLM、不按关键词筛选章节，也不上传文件。原始文件不被修改。

借鉴 [Jiahao-Grinnell/10-k-analysis](https://github.com/Jiahao-Grinnell/10-k-analysis) 的转换、独立审计和 token 报告流程。本实现使用 `lxml` 编写保守的转换器，而不是直接包装 `sec2md`，以兼容当前语料中的 SGML 完整提交、早期定宽文本、HTML 和 inline XBRL，并保留复杂财务表格。

## 使用

需要 Python 3.12 或更新版本。当前工作区已经安装依赖、缓存 tokenizer，可以直接运行：

```powershell
& '.\.venv\Scripts\python.exe' convertmd.py --input '10K_Filings' --output 'Markdown_10K' --workers 8 --resume
```

在新环境中先安装依赖：

```powershell
python -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
```

第一次加载 `tiktoken` 编码表需要联网；之后使用 `.cache/tiktoken` 中的本地缓存。转换本身不下载附件，不需要 API key。

单独测试一个原始文件：

```powershell
& '.\.venv\Scripts\python.exe' convertmd.py --input '某份10-K.htm' --output 'test_outputs/single'
```

其他选项：

- `--limit 10`：仅转换前 10 份，用于试运行。
- `--workers 1`：严格串行本地转换；默认就是 1。多个 worker 只并行执行本地格式转换，不是 LLM batch processing。
- `--tokenizer o200k_base --tokenizer cl100k_base`：分别统计两种编码；默认使用 `o200k_base`。
- `--resume`：校验原始内容、转换代码、tokenizer 和输出文件的 SHA-256，再复用已有结果。1.1 还允许复用指定兼容的 1.0 版本中正文 / 数字 / 表格检查全部通过的输出；保留实际转换代码的 hash，并另记当前校验版本。检查失败或内容损坏的输出会重新生成。

## 输入与输出

有 `filings.csv` 时，按照清单读取 `downloaded` / `already_present` 文件，并保留公司目录结构；没有清单时递归读取 `.htm`、`.html`、`.txt`、`.sgml`、`.sgm`。格式由内容识别，不依赖扩展名。

输出包括：

- 每份报告的 `.md` 文件，以及同名 `.audit.json` 审计记录。
- `conversion_report.csv`：逐份 token 数、节省比例、检查结果。
- `conversion_summary.json`：全量统计。
- `conversion_report.md`：可读的汇总报告。
- `conversion_progress.jsonl`：追加式运行记录；多次运行可能包含重复记录，最终统计以 summary / CSV 为准。

遇到转换错误会继续处理其他文件，并在报告中记录错误。退出码：`0` 全部完成；`1` 至少一份转换错误；`2` 没有转换错误，但原始文件有缺失正文的情况。只有 SEC 文件头，或者错误下载为 EX-3 等附件而缺少 10-K 主文档的文件，会输出明确的占位 Markdown，不算作成功提取，也不纳入节省比例。

## 保留规则

- 保留整份报告的可读正文，包括目录、风险因素、财务报表、附注等；不根据 intangible investment 关键词删选章节。
- SGML `<DOCUMENT>` / `<TEXT>` 先分离，再转换。保留 10-K / 10-K405 主文档以及可读文本附件，包括可能包含完整年报的 EX-13。
- 排除技术附件 EX-27、EX-101 / XML 和二进制图片等，并在审计中记录。正文中引用的图片保留引用和替代文字，但不 OCR、不联网补下载附件。
- HTML 删除样式属性、脚本、技术头部、隐藏 XBRL 等格式负担；可见的 inline XBRL 数字按原文保留，不重新缩放、加减或计算。
- 普通矩形表格转换为 Markdown 表格。合并单元格、嵌套或不规则表格保留精简 HTML，维持单元格顺序和 `rowspan` / `colspan`。因此 Markdown 内可以包含原生 HTML 表格。
- 旧式 TXT 的 ASCII 表格和正文使用 fenced text，保留间距、负数括号、符号和脚注标记。纯 TXT 的 token 数可能小幅增加。
- UTF-8、UTF-16、声明的字符集以及 CP1252 / Latin-1 有独立解码处理，不用 `errors=ignore` 丢弃字符。

## 检查与局限

每个保留的文档分别检查：清理后原始可见文本与重新渲染 Markdown 的文本是否一致（忽略空白）、数字出现次数是否缺失、表格单元格顺序及合并结构是否一致。普通 Markdown 转换失败时，自动回退为精简 HTML 正文，再检查一次。`CHECKED_TEXT` 表示这些程序检查通过；图片、编码 / 解析诊断、绝对定位或检查不通过会标为 `REVIEW_REQUIRED`。

这些检查不代表浏览器渲染完全一致，也不替代财务含义核对。清理使用 HTML 树和内联隐藏样式，不执行 JavaScript 或完整 CSS 布局。原始文件中的图像内容、链接到外部但未下载的材料不包含在文本完整性证明中。通过全部文本检查的报告仍可能因图片缺失而需要复核。

运行测试：

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q
& '.\.venv\Scripts\python.exe' smoke_conversion.py
& '.\.venv\Scripts\python.exe' verify_outputs.py
```

单元测试覆盖表格合并 / 嵌套、数字与符号、旧式文本、SGML 附件、XBRL、编码、编号转义、保存与恢复、缺失正文和加权统计；真实样本测试覆盖当前语料中的主要格式和最大文件。

`verify_outputs.py` 在全量转换结束后，逐份重新校验原始文件与下载清单的 SHA-256、输出文件与审计记录的 SHA-256、当前转换代码版本、正文 / 数字 / 表格检查结果以及汇总 token 数，输出 `corpus_validation.json`。

## 如何理解 token 节省

报告同时提供两个基准：

1. **原始文件 → Markdown**：原始文件字符串包含 HTML / SGML 标签、隐藏 XBRL、编码附件等。
2. **保留文档的原始正文 → Markdown**：只比较保留的主报告及文本附件，区分“删除技术附件”与“去除格式负担”的节省。

全量比例是 `1 − 全部 Markdown tokens / 全部基准 tokens`，不是各文件节省比例的简单平均。缺失正文的占位文件不参与比较。

这是用 `tiktoken` 对实际字符串进行的本地测量。它适用于比较原始 HTML 文本与 Markdown 文本作为模型输入的大小，不等于 ChatGPT 文件上传后的实际账单或内部 token 数；ChatGPT 本身可能先解析 HTML。提示词、输出、推理及图像 tokens 未计入。

后续 LLM 提取可以严格按“读取一份 Markdown → 独立请求 → 保存结果 → 下一份”执行。本转换器不实现 LLM 提取或浏览器自动化。
