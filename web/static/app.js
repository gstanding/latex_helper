/* ─── Theme toggle ──────────────────────────────────────────────────── */
(function () {
  const root = document.documentElement;
  const saved = localStorage.getItem('theme');
  if (saved) root.setAttribute('data-theme', saved);

  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', function () {
      const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
      root.setAttribute('data-theme', next);
      localStorage.setItem('theme', next);
    });
  }
})();

/* ─── State ─────────────────────────────────────────────────────────── */
const state = {
  file: null,
  editor: null,
  pdflatexOk: false,
  pdfUrl: null,
  figures: {},        // {filename: base64} from screenshot mode
  sseReader: null,    // active SSE reader (for cancellation)
  selectedConverter: '', // '' = use server default
  outputFormat: 'latex',  // 'latex' or 'markdown'
};

/* ─── DOM refs ──────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const dropTarget      = $('drop-target');
const fileInput       = $('file-input');
const fileInfo        = $('file-info');
const convertBtn      = $('convert-btn');
const cancelBtn       = $('cancel-btn');
const clearBtn        = $('clear-btn');
const progressWrap    = $('progress-wrap');
const progressToken   = $('progress-token');
const uploadSection   = $('upload-section');
const editorSection   = $('editor-section');
const converterWrap   = $('converter-wrap');
const figureModeWrap  = $('figure-mode-wrap');
const monacoContainer = $('monaco-container');
const pdfPreview      = $('pdf-preview');
const compileBtn      = $('compile-btn');
const downloadBtn     = $('download-btn');
const downloadPdfBtn  = $('download-pdf-btn');
const reuploadBtn     = $('reupload-btn');
const compileLog      = $('compile-log');
const logContent      = $('log-content');
const llmBadge        = $('llm-badge');
const footerLlm       = $('footer-llm');
const footerPdflatex  = $('footer-pdflatex');
const streamStatus    = $('stream-status');
const compileOverlay  = $('compile-overlay');
// Preview tabs
const tabPdfBtn       = $('tab-pdf-btn');
const tabMdBtn        = $('tab-md-btn');
const pdfTabContent   = $('pdf-tab-content');
const mdTabContent    = $('md-tab-content');
const markdownPreview = $('markdown-preview');

// Modal
const modalOverlay = $('modal-overlay');
const modalTitle   = $('modal-title');
const modalBody    = $('modal-body');
const modalInput   = $('modal-input');
const modalCancel  = $('modal-cancel');
const modalConfirm = $('modal-confirm');
const modalClose   = $('modal-close');
let _modalResolve  = null;

/* ─── Init ──────────────────────────────────────────────────────────── */
(async function init() {
  initMonaco();
  initMarked();   // 配置 marked 数学扩展
  initDragDrop();
  initButtons();
  checkDraft();
  Promise.all([fetchLlmInfo(), fetchPdflatexHealth()]);
})();

async function fetchLlmInfo() {
  try {
    const r = await fetch('/health/converters');
    const { default: defaultId, converters } = await r.json();

    const defaultInfo = converters.find(c => c.id === defaultId) || converters[0];
    if (defaultInfo) {
      llmBadge.textContent = defaultInfo.label;
      footerLlm.textContent = `引擎: ${defaultInfo.label}`;
    } else {
      llmBadge.textContent = 'offline';
    }

    if (converters.length > 1) {
      converterWrap.innerHTML = '';
      const convLabel = $('converter-label');
      if (convLabel) convLabel.hidden = false;
      converters.forEach(c => {
        const label = document.createElement('label');
        label.className = 'figure-mode-option';
        const radio = document.createElement('input');
        radio.type = 'radio';
        radio.name = 'converter';
        radio.value = c.id;
        radio.checked = c.id === defaultId;
        radio.addEventListener('change', () => onConverterChange(c.id));
        const titleEl = document.createElement('span');
        titleEl.className = 'fm-title';
        const dot = document.createElement('span');
        dot.className = 'fm-dot';
        titleEl.appendChild(dot);
        titleEl.appendChild(document.createTextNode(c.label));
        const descEl = document.createElement('span');
        descEl.className = 'fm-desc';
        descEl.textContent = _converterTooltip(c);
        label.appendChild(radio);
        label.appendChild(titleEl);
        label.appendChild(descEl);
        converterWrap.appendChild(label);
      });
      converterWrap.hidden = false;
      state.selectedConverter = defaultId;
      onConverterChange(defaultId, /* silent */ true);
    }
  } catch {
    llmBadge.textContent = 'offline';
  }
}

function _converterTooltip(c) {
  if (c.type === 'ocr')     return 'SimpleTex 专用 OCR，精准识别数学公式';
  if (c.type === 'doc_ocr') return 'SimpleTex 文档 OCR，秒级返回 Markdown + 公式预览';
  if (c.type === 'vlm')     return 'MiniMax 视觉语言模型，支持图文混排';
  return 'Anthropic Claude 大模型，支持复杂文档转换';
}

/* ─── Preview tab switching ─────────────────────────────────────────── */
function switchPreviewTab(tab) {
  const isPdf = (tab === 'pdf');
  tabPdfBtn.classList.toggle('active', isPdf);
  tabMdBtn.classList.toggle('active', !isPdf);
  pdfTabContent.classList.toggle('active', isPdf);
  mdTabContent.classList.toggle('active', !isPdf);
}

/* ─── marked.js 初始化：注册数学扩展，在 inline 处理前拦截 $...$ ──────── */
function initMarked() {
  if (typeof marked === 'undefined') return;

  // 扩展必须在 marked.use() 中注册，renderer 在 parse() 时被调用，
  // 届时 KaTeX 已就绪（defer 脚本在页面解析完后立即执行，用户操作更晚）
  marked.use({
    extensions: [
      // 块级公式：$$...$$（必须比 inline 先注册）
      {
        name: 'mathBlock',
        level: 'block',
        start(src) { return src.indexOf('$$'); },
        tokenizer(src) {
          const m = /^\$\$([\s\S]+?)\$\$/.exec(src);
          if (m) return { type: 'mathBlock', raw: m[0], math: m[1].trim() };
        },
        renderer(token) {
          try {
            return '<div class="math-display">' +
              katex.renderToString(token.math, { displayMode: true, throwOnError: false }) +
              '</div>\n';
          } catch { return `<div class="math-display">$$${token.math}$$</div>\n`; }
        },
      },
      // 行内公式：$...$
      {
        name: 'mathInline',
        level: 'inline',
        start(src) { return src.indexOf('$'); },
        tokenizer(src) {
          // 不跨行，不含 $$
          const m = /^\$([^$\n]+?)\$/.exec(src);
          if (m) return { type: 'mathInline', raw: m[0], math: m[1] };
        },
        renderer(token) {
          try {
            return katex.renderToString(token.math, { displayMode: false, throwOnError: false });
          } catch { return `$${token.math}$`; }
        },
      },
      // 块级公式：\[...\]（marked 会把 \ 当转义符吃掉，必须在扩展层拦截）
      {
        name: 'mathBlockBracket',
        level: 'block',
        start(src) { return src.indexOf('\\['); },
        tokenizer(src) {
          const m = /^\\\[([\s\S]+?)\\\]/.exec(src);
          if (m) return { type: 'mathBlockBracket', raw: m[0], math: m[1].trim() };
        },
        renderer(token) {
          try {
            return '<div class="math-display">' +
              katex.renderToString(token.math, { displayMode: true, throwOnError: false }) +
              '</div>\n';
          } catch { return `<div class="math-display">\\[${token.math}\\]</div>\n`; }
        },
      },
      // 行内公式：\(...\)
      {
        name: 'mathInlineParen',
        level: 'inline',
        start(src) { return src.indexOf('\\('); },
        tokenizer(src) {
          const m = /^\\\(([^\n]+?)\\\)/.exec(src);
          if (m) return { type: 'mathInlineParen', raw: m[0], math: m[1] };
        },
        renderer(token) {
          try {
            return katex.renderToString(token.math, { displayMode: false, throwOnError: false });
          } catch { return `\\(${token.math}\\)`; }
        },
      },
      // 块级数学环境：\begin{equation}...\end{equation} 等
      {
        name: 'mathEnv',
        level: 'block',
        start(src) { return src.indexOf('\\begin{'); },
        tokenizer(src) {
          const m = /^(\\begin\{([a-zA-Z]+\*?)\}[\s\S]+?\\end\{\2\})/.exec(src);
          if (!m) return;
          const mathEnvs = [
            'equation', 'equation*', 'align', 'align*', 'aligned',
            'gather', 'gather*', 'gathered', 'multline', 'multline*',
            'split', 'cases', 'bmatrix', 'pmatrix', 'vmatrix',
            'Bmatrix', 'Vmatrix', 'matrix', 'array',
          ];
          if (mathEnvs.includes(m[2])) {
            return { type: 'mathEnv', raw: m[0], math: m[1] };
          }
        },
        renderer(token) {
          try {
            return '<div class="math-display">' +
              katex.renderToString(token.math, { displayMode: true, throwOnError: false }) +
              '</div>\n';
          } catch { return `<div class="math-display">${token.math}</div>\n`; }
        },
      },
    ],
    // 保留换行，图片允许外链
    breaks: false,
    gfm: true,
  });
}

/* ─── Markdown 渲染 ─────────────────────────────────────────────────── */
function renderMarkdown(mdText) {
  if (!markdownPreview) return;
  // marked 已配置数学扩展，直接 parse 即可
  markdownPreview.innerHTML = (typeof marked !== 'undefined')
    ? marked.parse(mdText)
    : '<pre style="white-space:pre-wrap">' +
        mdText.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</pre>';
  // \[...\]、\(...\) 和 \begin{env} 已由 marked 扩展处理，无需 renderMathInElement
}

/* ─── Markdown → LaTeX client-side conversion ───────────────────────── */
function markdownToLatex(md) {
  const preamble = [
    '\\documentclass{article}',
    '\\usepackage{amsmath}',
    '\\usepackage{amssymb}',
    '\\usepackage{amsfonts}',
    '\\usepackage{geometry}',
    '\\geometry{margin=2.5cm}',
    '\\begin{document}',
    '',
  ].join('\n');

  // Handle page separators inserted for multi-page PDFs
  const pages = md.split(/\n---\n/);
  const body = pages.map(_convertMdPage).join('\n\\newpage\n\n');
  return preamble + body + '\n\n\\end{document}\n';
}

function _convertMdPage(md) {
  const lines = md.split('\n');
  const out = [];
  for (const line of lines) {
    const t = line.trim();
    if      (/^#### (.+)/.test(t)) out.push(`\\paragraph{${_mdInline(t.slice(5))}}`);
    else if (/^### (.+)/.test(t))  out.push(`\\subsubsection{${_mdInline(t.slice(4))}}`);
    else if (/^## (.+)/.test(t))   out.push(`\\subsection{${_mdInline(t.slice(3))}}`);
    else if (/^# (.+)/.test(t))    out.push(`\\section{${_mdInline(t.slice(2))}}`);
    else                            out.push(_mdInline(line));
  }
  return out.join('\n');
}

function _mdInline(line) {
  // Protect math from inline formatting substitution
  const parts = [];
  let s = line.replace(/\$\$[\s\S]+?\$\$|\$[^$\n]+?\$/g, m => {
    parts.push(m);
    return `\x00M${parts.length - 1}\x00`;
  });
  // Bold, italic, inline code
  s = s
    .replace(/\*\*(.+?)\*\*/g, '\\textbf{$1}')
    .replace(/\*(.+?)\*/g,     '\\textit{$1}')
    .replace(/`(.+?)`/g,       '\\texttt{$1}');
  // Restore math
  s = s.replace(/\x00M(\d+)\x00/g, (_, i) => parts[parseInt(i)]);
  return s;
}

function onConverterChange(id, silent = false) {
  state.selectedConverter = id;
  figureModeWrap.hidden = (id === 'simpletex' || id === 'simpletex_doc' || id === 'simpletex_doc_v2');
  if (!silent) {
    const info = converterWrap.querySelector(`input[value="${id}"]`)?.closest('label')?.querySelector('span');
    if (info) {
      llmBadge.textContent = info.textContent;
      footerLlm.textContent = `引擎: ${info.textContent}`;
    }
  }
}

async function fetchPdflatexHealth() {
  try {
    const r = await fetch('/health/pdflatex');
    const { available } = await r.json();
    state.pdflatexOk = available;
    if (footerPdflatex) footerPdflatex.hidden = !available;
    if (!available) {
      compileBtn.title = 'pdflatex not installed — install TeX Live to enable';
      compileBtn.style.opacity = '0.45';
    }
  } catch { /* ignore */ }
}

/* ─── Monaco setup ──────────────────────────────────────────────────── */
function initMonaco() {
  if (typeof require === 'undefined') {
    useFallbackEditor();
    return;
  }
  require.config({
    paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/vs' },
  });
  require(['vs/editor/editor.main'], () => {
    state.editor = monaco.editor.create(monacoContainer, {
      value: '',
      language: 'latex',
      theme: 'vs-dark',
      wordWrap: 'on',
      minimap: { enabled: false },
      fontSize: 13,
      lineNumbers: 'on',
      scrollBeyondLastLine: false,
      automaticLayout: true,
    });
  });
}

function useFallbackEditor() {
  monacoContainer.innerHTML =
    '<textarea id="fallback-editor" spellcheck="false"></textarea>';
  const ta = $('fallback-editor');
  state.editor = {
    getValue: () => ta.value,
    setValue: v => { ta.value = v; },
  };
}

/* ─── Editor helpers ────────────────────────────────────────────────── */
function editorAppend(chunk) {
  if (state.editor.getModel) {
    const model = state.editor.getModel();
    const lc = model.getLineCount();
    const col = model.getLineMaxColumn(lc);
    model.applyEdits([{ range: new monaco.Range(lc, col, lc, col), text: chunk }]);
    state.editor.revealLine(model.getLineCount());
  } else {
    const ta = $('fallback-editor');
    if (ta) { ta.value += chunk; ta.scrollTop = ta.scrollHeight; }
  }
}

function editorGoToLine(line) {
  if (!line || !state.editor.revealLineInCenter) return;
  state.editor.revealLineInCenter(line);
  state.editor.setPosition({ lineNumber: line, column: 1 });
}

/* ─── Drag & drop / file selection ─────────────────────────────────── */
function initDragDrop() {
  dropTarget.addEventListener('dragover', e => {
    e.preventDefault();
    dropTarget.classList.add('drag-over');
  });
  dropTarget.addEventListener('dragleave', () => dropTarget.classList.remove('drag-over'));
  dropTarget.addEventListener('drop', e => {
    e.preventDefault();
    dropTarget.classList.remove('drag-over');
    handleFile(e.dataTransfer.files[0]);
  });
  dropTarget.addEventListener('click', e => {
    if (e.target.closest('label') || e.target === fileInput) return;
    fileInput.click();
  });
  dropTarget.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') fileInput.click();
  });
  fileInput.addEventListener('change', () => handleFile(fileInput.files[0]));
}

function handleFile(file) {
  if (!file) return;
  const ok =
    file.type === 'application/pdf' ||
    file.type.startsWith('image/') ||
    file.name.toLowerCase().endsWith('.pdf');
  if (!ok) {
    showErrorModal('文件格式不支持', '仅支持 PDF 或图片文件（PNG、JPG、GIF、WebP）。');
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    showErrorModal('文件过大', '请上传小于 20MB 的文件。');
    return;
  }
  state.file = file;
  fileInfo.textContent = `${file.name}  (${formatBytes(file.size)})`;
  fileInfo.hidden = false;
  convertBtn.disabled = false;
  clearBtn.hidden = false;
}

/* ─── Buttons ───────────────────────────────────────────────────────── */
function initButtons() {
  convertBtn.addEventListener('click', startConvert);
  cancelBtn.addEventListener('click', cancelConvert);
  clearBtn.addEventListener('click', resetUpload);
  reuploadBtn.addEventListener('click', resetUpload);
  downloadBtn.addEventListener('click', downloadTex);
  downloadPdfBtn.addEventListener('click', downloadPdf);
  compileBtn.addEventListener('click', compilePdf);
  $('close-log').addEventListener('click', () => { compileLog.hidden = true; });
  // Preview tab switching
  if (tabPdfBtn) tabPdfBtn.addEventListener('click', () => switchPreviewTab('pdf'));
  if (tabMdBtn)  tabMdBtn.addEventListener('click',  () => switchPreviewTab('md'));

  // Modal wiring
  modalClose.addEventListener('click', () => closeModal(null));
  modalCancel.addEventListener('click', () => closeModal(null));
  modalConfirm.addEventListener('click', () => {
    closeModal(modalInput.hidden ? true : modalInput.value);
  });
  modalOverlay.addEventListener('click', e => {
    if (e.target === modalOverlay) closeModal(null);
  });
  modalInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') closeModal(modalInput.value);
    if (e.key === 'Escape') closeModal(null);
  });
}

/* ─── Modal ─────────────────────────────────────────────────────────── */
function showErrorModal(title, message, log = null) {
  return new Promise(resolve => {
    _modalResolve = resolve;
    modalTitle.textContent = title;
    modalBody.innerHTML = '';
    modalBody.className = 'is-error';

    const p = document.createElement('p');
    p.textContent = message;
    modalBody.appendChild(p);

    if (log) {
      const pre = document.createElement('pre');
      pre.textContent = log;
      modalBody.appendChild(pre);
    }

    modalInput.hidden = true;
    modalCancel.hidden = true;
    modalConfirm.textContent = '关闭';
    modalOverlay.hidden = false;
  });
}

function showConfirmModal(title, message) {
  return new Promise(resolve => {
    _modalResolve = resolve;
    modalTitle.textContent = title;
    modalBody.innerHTML = '';
    modalBody.className = '';

    const p = document.createElement('p');
    p.textContent = message;
    modalBody.appendChild(p);

    modalInput.hidden = true;
    modalCancel.hidden = false;
    modalCancel.textContent = '忽略';
    modalConfirm.textContent = '恢复';
    modalOverlay.hidden = false;
  });
}

function showInputModal(title, message, defaultValue = '') {
  return new Promise(resolve => {
    _modalResolve = resolve;
    modalTitle.textContent = title;
    modalBody.innerHTML = '';
    modalBody.className = '';

    if (message) {
      const p = document.createElement('p');
      p.textContent = message;
      modalBody.appendChild(p);
    }

    modalInput.hidden = false;
    modalInput.value = defaultValue;
    modalCancel.hidden = false;
    modalCancel.textContent = '取消';
    modalConfirm.textContent = '确定';
    modalOverlay.hidden = false;

    setTimeout(() => { modalInput.select(); modalInput.focus(); }, 50);
  });
}

function showCompileErrorModal(title, message, log = null) {
  return new Promise(resolve => {
    _modalResolve = resolve;
    modalTitle.textContent = title;
    modalBody.innerHTML = '';
    modalBody.className = 'is-error';

    const p = document.createElement('p');
    p.textContent = message;
    modalBody.appendChild(p);

    if (log) {
      const pre = document.createElement('pre');
      pre.textContent = log;
      modalBody.appendChild(pre);
    }

    modalInput.hidden = true;
    modalCancel.hidden = false;
    modalCancel.textContent = '关闭';
    modalConfirm.textContent = '🔧 自动修复';
    modalOverlay.hidden = false;
  });
}

function closeModal(value) {
  modalOverlay.hidden = true;
  if (_modalResolve) {
    _modalResolve(value);
    _modalResolve = null;
  }
}

/* ─── Conversion (SSE) ──────────────────────────────────────────────── */
async function startConvert() {
  if (!state.file) return;

  convertBtn.disabled = true;
  clearBtn.hidden = true;
  cancelBtn.hidden = false;
  progressWrap.hidden = false;
  progressToken.hidden = false;
  progressToken.textContent = '';
  state.figures = {};

  const figureMode = document.querySelector('input[name="figure-mode"]:checked')?.value || 'draw';

  const formData = new FormData();
  formData.append('file', state.file);
  formData.append('figure_mode', figureMode);
  if (state.selectedConverter) formData.append('converter', state.selectedConverter);

  state.outputFormat = 'latex'; // reset each conversion
  const t0 = performance.now();
  let firstChunkAt = null;
  console.log('[convert] uploading…');

  let response;
  try {
    response = await fetch('/convert', { method: 'POST', body: formData });
  } catch (e) {
    showErrorModal('网络错误', e.message);
    resetProgress();
    return;
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    showErrorModal(`服务器错误 ${response.status}`, text || response.statusText);
    resetProgress();
    return;
  }

  console.log(`[convert] fetch OK ${((performance.now() - t0) / 1000).toFixed(2)}s, reading SSE…`);
  uploadSection.style.display = 'none';
  editorSection.hidden = false;
  streamStatus.hidden = false;
  if (state.editor) state.editor.setValue('');

  const reader = response.body.getReader();
  state.sseReader = reader;
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      let pendingEvent = null;
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          pendingEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const raw = line.slice(6);
          if (!raw) { pendingEvent = null; continue; }

          if (pendingEvent === 'format') {
            pendingEvent = null;
            try {
              state.outputFormat = JSON.parse(raw); // 'latex' or 'markdown'
              if (state.outputFormat === 'markdown') {
                // Show markdown tab, update editor pane title
                tabMdBtn.hidden = false;
                const titleEl = document.querySelector('#editor-toolbar .toolbar-title');
                if (titleEl) titleEl.lastChild.textContent = ' OCR 结果';
                // Hide compile btn (can't compile raw markdown directly)
                compileBtn.style.display = 'none';
              }
            } catch { /* ignore */ }
          } else if (pendingEvent === 'error') {
            pendingEvent = null;
            try {
              const err = JSON.parse(raw);
              showErrorModal('转换失败', err.message || '未知错误');
              if (state.editor) state.editor.setValue('');
            } catch {
              showErrorModal('转换失败', '发生未知错误');
            }
          } else if (pendingEvent === 'progress') {
            pendingEvent = null;
            try {
              const p = JSON.parse(raw);
              progressToken.textContent = `已生成 ${p.chars || 0} 字符…`;
            } catch { /* ignore */ }
          } else if (pendingEvent === 'replace') {
            pendingEvent = null;
            try {
              const fullText = JSON.parse(raw);
              if (state.editor) state.editor.setValue(fullText);
              if (state.outputFormat === 'markdown') {
                // Render markdown in preview pane and switch to it
                renderMarkdown(fullText);
                switchPreviewTab('md');
              }
              console.log(`[convert] replace ${((performance.now() - t0) / 1000).toFixed(2)}s`);
            } catch { /* ignore */ }
          } else if (pendingEvent === 'images') {
            pendingEvent = null;
            try { state.figures = JSON.parse(raw); } catch { /* ignore */ }
          } else if (pendingEvent === 'done') {
            pendingEvent = null;
            saveDraft();
            progressWrap.hidden = true;
            streamStatus.hidden = true;
            console.log(`[convert] done ${((performance.now() - t0) / 1000).toFixed(2)}s total`);
            if (state.outputFormat !== 'markdown') autoPreview();
          } else {
            // Regular streaming chunk
            pendingEvent = null;
            const chunk = JSON.parse(raw);
            if (firstChunkAt === null) {
              firstChunkAt = performance.now();
              console.log(`[convert] first token ${((firstChunkAt - t0) / 1000).toFixed(2)}s`);
            }
            editorAppend(chunk);
          }
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError' && !e.message?.includes('cancel')) {
      showErrorModal('流式传输错误', e.message);
    }
  } finally {
    state.sseReader = null;
    cancelBtn.hidden = true;
    progressToken.hidden = true;
    streamStatus.hidden = true;
    resetProgress();
  }
}

function cancelConvert() {
  if (state.sseReader) {
    state.sseReader.cancel();
    state.sseReader = null;
  }
  cancelBtn.hidden = true;
  progressToken.hidden = true;
  streamStatus.hidden = true;
  resetProgress();
}

/* ─── Auto preview after conversion ────────────────────────────────── */
// Silently compile and show PDF; on failure leaves the iframe blank.
async function autoPreview() {
  if (!state.pdflatexOk) return;
  const latex = state.editor ? state.editor.getValue() : '';
  if (!latex.trim()) return;
  try {
    const t = performance.now();
    const result = await _doCompile(latex, state.figures);
    if (result.ok) {
      console.log(`[compile] auto-compile ${((performance.now() - t) / 1000).toFixed(2)}s`);
      _applyPdf(result.blob);
    } else {
      console.log(`[compile] auto-compile failed: ${result.message}`);
    }
  } catch { /* ignore */ }
}

/* ─── PDF compilation ───────────────────────────────────────────────── */

async function _doCompile(latex, images) {
  try {
    const resp = await fetch('/compile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ latex, images }),
    });
    if (resp.ok) {
      return { ok: true, blob: await resp.blob() };
    }
    const detail = await resp.json().catch(() => ({ message: resp.statusText }));
    let parsed = detail;
    if (typeof detail.detail === 'string') {
      try { parsed = JSON.parse(detail.detail); } catch { parsed = { message: detail.detail }; }
    }
    return {
      ok: false,
      message: parsed.message || `编译失败（HTTP ${resp.status}）`,
      log: parsed.log || '',
      line: parsed.line || null,
    };
  } catch (e) {
    return { ok: false, message: e.message, log: '', line: null };
  }
}

function _applyPdf(blob) {
  if (state.pdfUrl) URL.revokeObjectURL(state.pdfUrl);
  state.pdfUrl = URL.createObjectURL(blob);
  pdfPreview.src = state.pdfUrl;
  downloadPdfBtn.hidden = false;
}

async function compilePdf() {
  if (!state.pdflatexOk) {
    showErrorModal('pdflatex 不可用', '服务器未安装 pdflatex，请安装 TeX Live 后重试。');
    return;
  }

  const latex = state.editor ? state.editor.getValue() : '';
  if (!latex.trim()) return;

  compileBtn.disabled = true;
  compileOverlay.hidden = false;
  compileLog.hidden = true;
  downloadPdfBtn.hidden = true;

  const result = await _doCompile(latex, state.figures);
  compileOverlay.hidden = true;
  compileBtn.disabled = false;

  if (result.ok) {
    _applyPdf(result.blob);
  } else {
    if (result.line) editorGoToLine(result.line);
    const fix = await showCompileErrorModal('编译失败', result.message, result.log);
    if (fix) autoFix(latex, result.log, state.figures, 1, 3);
  }
}

/* ─── Auto-fix ──────────────────────────────────────────────────────── */

async function _streamFix(latex, log, images) {
  const resp = await fetch('/fix', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ latex, log, images }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `修复请求失败（HTTP ${resp.status}）`);
  }

  if (state.editor) state.editor.setValue('');
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '', accumulated = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();

    let pendingEvent = null;
    for (const line of lines) {
      if (line.startsWith('event: ')) {
        pendingEvent = line.slice(7).trim();
      } else if (line.startsWith('data: ')) {
        const raw = line.slice(6);
        if (!raw) { pendingEvent = null; continue; }
        if (pendingEvent === 'error') {
          const err = JSON.parse(raw);
          throw new Error(err.message || '修复失败');
        } else if (pendingEvent === 'replace') {
          accumulated = JSON.parse(raw);
          if (state.editor) state.editor.setValue(accumulated);
        } else if (pendingEvent !== 'done') {
          const chunk = JSON.parse(raw);
          accumulated += chunk;
          editorAppend(chunk);
        }
        pendingEvent = null;
      }
    }
  }
  return accumulated;
}

async function autoFix(latex, log, images, attempt, maxAttempts) {
  streamStatus.textContent = `🔧 修复中… ${attempt}/${maxAttempts}`;
  streamStatus.hidden = false;
  compileBtn.disabled = true;

  let fixedLatex;
  try {
    fixedLatex = await _streamFix(latex, log, images);
  } catch (e) {
    streamStatus.hidden = true;
    compileBtn.disabled = false;
    showErrorModal('自动修复出错', e.message);
    return;
  }

  streamStatus.textContent = `🔧 重新编译… ${attempt}/${maxAttempts}`;
  compileOverlay.hidden = false;
  const result = await _doCompile(fixedLatex, images);
  compileOverlay.hidden = true;
  streamStatus.hidden = true;
  compileBtn.disabled = false;

  if (result.ok) {
    _applyPdf(result.blob);
  } else if (attempt < maxAttempts) {
    if (result.line) editorGoToLine(result.line);
    const fix = await showCompileErrorModal(
      `修复后仍然失败（${attempt}/${maxAttempts}）`,
      result.message,
      result.log
    );
    if (fix) autoFix(fixedLatex, result.log, images, attempt + 1, maxAttempts);
  } else {
    showErrorModal(`自动修复失败（已尝试 ${maxAttempts} 次）`, result.message || '编译失败', result.log);
  }
}

/* ─── Download ──────────────────────────────────────────────────────── */
async function downloadTex() {
  const content = state.editor ? state.editor.getValue() : '';
  if (!content.trim()) {
    showErrorModal('内容为空', '编辑器中没有可下载的内容。');
    return;
  }

  const defaultName = getDefaultFilename();
  const raw = await showInputModal('下载 LaTeX 文件', '输入文件名（无需加后缀）：', defaultName);
  if (raw === null) return;

  let name = (raw || defaultName).trim() || defaultName;
  name = name.replace(/[<>:"/\\|?*]/g, '_');
  if (!name.endsWith('.tex')) name += '.tex';

  // Convert markdown to LaTeX if needed
  const latex = (state.outputFormat === 'markdown') ? markdownToLatex(content) : content;

  const blob = new Blob([latex], { type: 'text/x-tex' });
  triggerDownload(blob, name);
}

function downloadPdf() {
  if (!state.pdfUrl) return;
  const name = getDefaultFilename() + '.pdf';
  fetch(state.pdfUrl)
    .then(r => r.blob())
    .then(blob => triggerDownload(blob, name))
    .catch(() => { window.open(state.pdfUrl, '_blank'); });
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function getDefaultFilename() {
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const yy = String(now.getFullYear()).slice(-2);
  const MM = pad(now.getMonth() + 1);
  const dd = pad(now.getDate());
  const hh = pad(now.getHours());
  const mm = pad(now.getMinutes());
  const ss = pad(now.getSeconds());
  return `document${yy}${MM}${dd}${hh}${mm}${ss}`;
}

/* ─── Auto-save draft ───────────────────────────────────────────────── */
const DRAFT_KEY = 'latex_helper_draft';

function saveDraft() {
  const latex = state.editor ? state.editor.getValue() : '';
  if (latex.trim()) {
    try { localStorage.setItem(DRAFT_KEY, latex); } catch { /* storage full */ }
  }
}

async function checkDraft() {
  let draft;
  try { draft = localStorage.getItem(DRAFT_KEY); } catch { return; }
  if (!draft || !draft.trim()) return;

  const restore = await showConfirmModal('发现草稿', '检测到上次未保存的 LaTeX 草稿，是否恢复？');
  if (restore) {
    uploadSection.style.display = 'none';
    editorSection.hidden = false;
    if (state.editor) {
      setTimeout(() => { state.editor.setValue(draft); }, 200);
    }
  } else {
    try { localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
  }
}

setInterval(saveDraft, 30000);

/* ─── Helpers ───────────────────────────────────────────────────────── */
function resetProgress() {
  progressWrap.hidden = true;
  progressToken.hidden = true;
  convertBtn.disabled = false;
  clearBtn.hidden = false;
  cancelBtn.hidden = true;
}

function resetUpload() {
  state.file = null;
  state.figures = {};
  state.outputFormat = 'latex';
  fileInput.value = '';
  fileInfo.hidden = true;
  fileInfo.textContent = '';
  convertBtn.disabled = true;
  clearBtn.hidden = true;
  cancelBtn.hidden = true;
  uploadSection.style.display = '';
  editorSection.hidden = true;
  streamStatus.hidden = true;
  downloadPdfBtn.hidden = true;
  if (state.editor) state.editor.setValue('');
  if (state.pdfUrl) { URL.revokeObjectURL(state.pdfUrl); state.pdfUrl = null; }
  pdfPreview.src = 'about:blank';
  compileLog.hidden = true;
  // Reset markdown UI
  if (tabMdBtn) tabMdBtn.hidden = true;
  switchPreviewTab('pdf');
  if (markdownPreview) markdownPreview.innerHTML = '';
  compileBtn.style.display = '';
  const titleEl = document.querySelector('#editor-toolbar .toolbar-title');
  if (titleEl) titleEl.lastChild.textContent = ' LaTeX 源码';
  try { localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
