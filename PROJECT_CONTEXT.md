# LaTeX Helper — 项目全量上下文

## 项目简介

**LaTeX Helper** 是一个将 PDF / 图片文档自动转换为 LaTeX 源码并支持在线编译的 Web 工具。
- 本地运行，通过浏览器访问
- 支持 Anthropic（Claude）和 MiniMax VLM 两种 LLM 后端
- 转换结果可在 Monaco 编辑器中编辑，支持 KaTeX 预览和 pdflatex/xelatex 编译

---

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11, FastAPI, uvicorn, aiofiles |
| LLM | Anthropic SDK (`anthropic`) / MiniMax VLM REST API |
| PDF 处理 | PyMuPDF (`fitz`) |
| 前端 | 纯 HTML/CSS/JS，Monaco Editor（CDN），KaTeX（CDN） |
| LaTeX 编译 | 系统安装的 pdflatex / xelatex（TeX Live） |

---

## 目录结构

```
latex_helper/
├── run.py                        # 启动入口
├── requirements.txt
├── latex_helper/
│   ├── prompts.py                # LLM system prompt
│   ├── converter.py              # LLM 调用逻辑（AnthropicConverter / MinimaxVLMConverter）
│   └── utils.py                  # 工具函数，含 postprocess_latex()
└── web/
    ├── app.py                    # FastAPI 路由
    └── static/
        ├── index.html
        ├── style.css
        └── app.js
```

---

## 启动方式

```bash
# Anthropic
ANTHROPIC_API_KEY=xxx python3.11 run.py

# MiniMax
LLM_PROVIDER=minimax MINIMAX_API_KEY=xxx python3.11 run.py

# 可选环境变量
LLM_MODEL=claude-opus-4-7        # 覆盖默认模型
MINIMAX_API_HOST=https://...      # 覆盖 MiniMax host
```

服务启动后访问 `http://127.0.0.1:8000`

---

## 后端核心逻辑

### `web/app.py` — 路由

| 路由 | 说明 |
|---|---|
| `POST /convert` | 接收 PDF/图片，流式返回 LaTeX（SSE） |
| `POST /compile` | 接收 LaTeX JSON，调用 xelatex/pdflatex，返回 PDF bytes |
| `GET /health/llm` | 返回当前 LLM provider 和 model |
| `GET /health/pdflatex` | 检查 pdflatex/xelatex 是否可用 |

**`/convert` 关键流程**：
```python
# 接受表单字段 figure_mode: str（draw/skip/screenshot，默认 draw）

# screenshot 模式预提取图像（在调 LLM 之前，以确定 figure_count）
if figure_mode == "screenshot" and file_type == "pdf":
    raw_figures = extract_pdf_figures(content)       # {name: bytes}
    figure_count = len(raw_figures)
    figures_b64 = {k: base64.b64encode(v).decode() for k, v in raw_figures.items()}

# 1. 完整收集 LLM 输出（缓冲后处理）
async for chunk in converter.stream_latex(..., figure_mode=figure_mode, figure_count=figure_count):
    full_latex += chunk

# 2. 后处理
full_latex = postprocess_latex(full_latex)

# 3. 分块推给前端
for i in range(0, len(full_latex), 512):
    yield f"data: {json.dumps(full_latex[i:i+512])}\n\n"

# 4. screenshot 模式额外推送图像数据
if figures_b64:
    yield f"event: images\ndata: {json.dumps(figures_b64)}\n\n"

yield "event: done\ndata: \n\n"
```

**`/compile` PDF 有效性校验**（不用 returncode，因为 LaTeX warning 也会导致非零退出码）：
```python
def _is_valid_pdf(path: str) -> bool:
    # 1. 文件大小 > 256 bytes
    # 2. 前5字节 == b"%PDF-"
    # 3. 末尾1024字节包含 b"%%EOF"
```

### `latex_helper/converter.py` — LLM 调用

- `AnthropicConverter`：用 Anthropic SDK，原生 PDF block，真正流式
- `MinimaxVLMConverter`：把 PDF 每页渲染成 PNG，逐页调用 MiniMax VLM REST API，每页返回完整 LaTeX，多页拼接
- `get_converter()` 根据 `LLM_PROVIDER` 环境变量自动选择

### `latex_helper/utils.py` — 工具函数

#### `extract_pdf_figures(file_bytes) -> dict[str, bytes]`
从 PDF 中提取嵌入的光栅图像，返回 `{"figure1.png": png_bytes, ...}`。  
优先提取 XObject 嵌入图（`page.get_images()`），若无嵌入图则退回到渲染含矢量绘图的页面区域（`page.get_drawings()`）。  
忽略小于 64×64px 的装饰性小图。供 `screenshot` 模式使用。

#### `postprocess_latex(latex: str) -> str` — 后处理器（重要）

在每次转换后自动修复四类模型输出问题：

1. **自动补全未定义颜色**  
   扫描 `\color{}`、`\textcolor{}`、TikZ 选项中所有颜色名，与 `\definecolor` 已声明的对比，对缺失的按名称关键词推断色值（`headerblue` → 深蓝，`textyellow` → 黄色等）自动插入 `\definecolor`

2. **注释掉占位图片**  
   `\includegraphics{xxx.png}` 如果引用的是本地不存在的文件（无绝对路径/子目录）→ 注释掉，避免编译报错

3. **删除冗余 CJK 环境**  
   使用 `ctexart/ctexbook` 时，模型有时会错误地加 `\begin{CJK*}...\end{CJK*}` → 自动删除

4. **修正 `\tikzset` 位置**  
   模型有时把 `\tikzset` 放在 `\usepackage{tikz}` 之前 → 自动移到正确位置

### `latex_helper/prompts.py` — System Prompt 要点

提供 `get_system_prompt(figure_mode, figure_count) -> str`，根据图形处理模式返回不同 prompt：

| `figure_mode` | 行为 |
|---|---|
| `"draw"`（默认） | 用 TikZ 重绘数学图形（四步方法论：分析→精确坐标→硬编码共享→只画原图有的） |
| `"skip"` | 跳过所有图形，不插入任何占位符 |
| `"screenshot"` | 引用预提取的 `figure1.png`、`figure2.png` 等文件 |

通用规则（所有模式）：
- 颜色必须先 `\definecolor` 再用；`\tikzset` 必须在 `\usepackage{tikz}` 后
- ctex 文档类下禁止 `\begin{CJK*}`
- 保留文本格式（`\textbf`、`\textit`、`\texttt`）

`SYSTEM_PROMPT` 保留为向后兼容的 `draw` 模式 prompt。

---

## 前端核心逻辑（`web/static/app.js`）

### SSE 解析（转换流）
```javascript
// 支持以下事件类型（pendingEvent 状态机解析）：
// event: error   → data: {"message": "..."}  显示错误弹窗
// event: images  → data: {filename: base64}  存入 state.figures（screenshot 模式）
// event: done    → 触发 KaTeX 渲染，隐藏 spinner
// 无 event 前缀   → data: "chunk"  累加到编辑器
```

### 状态管理
```javascript
const state = {
  file: null,        // 当前上传的文件
  editor: null,      // Monaco 实例（或 fallback textarea）
  pdflatexOk: false, // pdflatex 是否可用
  pdfUrl: null,      // 当前编译 PDF 的 blob URL（用于释放内存）
  figures: {},       // screenshot 模式下提取的图像 {filename: base64}
};
```

### 关键函数

| 函数 | 说明 |
|---|---|
| `startConvert()` | 上传文件，解析 SSE 流，显示转换中 spinner |
| `compilePdf()` | POST /compile，显示编译遮罩，成功后加载进 iframe |
| `downloadTex()` | 弹出文件名输入框（默认 `documentyyMMddhhmmss`），下载 .tex |
| `resetUpload()` | 重置回上传状态（重新上传按钮触发） |
| `showErrorModal(title, msg, log?)` | 统一错误弹窗，log 显示在 `<pre>` 块 |
| `showInputModal(title, msg, default)` | 带输入框的弹窗，返回 Promise\<string\|null\> |
| `postprocess_latex()` | 后端已处理，前端无需额外处理 |

### PDF 预览
编译成功后不打开新标签，而是加载进页面内的 `<iframe id="pdf-preview">` 并切换到 PDF 预览标签。

---

## 已知问题与历史决策

1. **MiniMax 模型会生成大量自定义颜色名但不定义** → 已由 `postprocess_latex()` 兜底，无需 prompt 完全解决
2. **LaTeX warning 导致 pdflatex 退出码非零** → 改用 PDF magic bytes + `%%EOF` 校验，不依赖退出码
3. **TikZ 图形精度问题（切线、交点）** → Prompt 要求先推导精确坐标再写 TikZ，hardcode 共享坐标
4. **`window.open` blob URL 在某些浏览器报"无法加载"** → 改用 iframe 内嵌
5. **`#stream-status` spinner 在页面加载时就显示** → CSS ID 选择器优先级高于 user-agent `[hidden]` 样式；改为 `display: none` 默认隐藏，通过 `:not([hidden])` 规则显示
6. **上传文件时弹窗出现两次** → `<label for="file-input">` 原生触发 input，同时事件冒泡到 dropTarget 的 click 监听器再次调用 `fileInput.click()`；在 click 处理器中加 `e.target.closest('label')` 判断跳过
7. **SSE 转换错误不可见于服务器日志** → uvicorn access log 只记录 HTTP 状态码，SSE 错误在响应体内；已在 `event_generator` 的 except 块加 `logger.error(..., exc_info=True)`，错误现在会出现在服务器控制台

---

## 依赖

```
anthropic>=0.40.0
httpx>=0.27.0
fastapi>=0.115.0
uvicorn[standard]>=0.32.0
python-multipart>=0.0.12
pymupdf>=1.25.0
aiofiles>=24.1.0
```

Python 3.11（macOS 上用 `python3.11`，系统默认 python3 是 3.6 不可用）

---

## Git 信息

- 仓库：`https://github.com/gstanding/latex_helper`
- 主分支：`claude/analyze-doc2latex-conversion-GURWp`（当前工作分支，也是 main 的 PR 来源）
- 最新 commit：Add figure mode toggle (draw/skip/screenshot) with PDF figure extraction
