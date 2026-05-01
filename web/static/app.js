/* ─── State ─────────────────────────────────────────────────────────── */
const state = {
  file: null,
  editor: null,
  pdflatexOk: false,
  pdfUrl: null,
  figures: {},     // {filename: base64} from screenshot mode
  sseReader: null, // active SSE reader (for cancellation)
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
const monacoContainer = $('monaco-container');
const katexPreview    = $('katex-preview');
const pdfPreview      = $('pdf-preview');
const compileBtn      = $('compile-btn');
const downloadBtn     = $('download-btn');
const downloadPdfBtn  = $('download-pdf-btn');
const reuploadBtn     = $('reupload-btn');
const compileLog      = $('compile-log');
const logContent      = $('log-content');
const llmBadge        = $('llm-badge');
const footerLlm       = $('footer-llm');
const streamStatus    = $('stream-status');
const compileOverlay  = $('compile-overlay');

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
  initDragDrop();
  initButtons();
  checkDraft();
  Promise.all([fetchLlmInfo(), fetchPdflatexHealth()]);
})();

async function fetchLlmInfo() {
  try {
    const r = await fetch('/health/llm');
    const { provider, model } = await r.json();
    const label = `${provider} / ${model}`;
    llmBadge.textContent = label;
    footerLlm.textContent = `LLM: ${label}`;
  } catch {
    llmBadge.textContent = 'offline';
  }
}

async function fetchPdflatexHealth() {
  try {
    const r = await fetch('/health/pdflatex');
    const { available } = await r.json();
    state.pdflatexOk = available;
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

    let debounce;
    state.editor.onDidChangeModelContent(() => {
      clearTimeout(debounce);
      debounce = setTimeout(renderKatex, 300);
    });
  });
}

function useFallbackEditor() {
  monacoContainer.innerHTML =
    '<textarea id="fallback-editor" spellcheck="false"></textarea>';
  const ta = $('fallback-editor');
  let debounce;
  ta.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(renderKatex, 300);
  });
  state.editor = {
    getValue: () => ta.value,
    setValue: v => { ta.value = v; },
  };
}

/* ─── Editor helpers ────────────────────────────────────────────────── */
function editorAppend(chunk) {
  if (state.editor.getModel) {
    // Monaco: use applyEdits to append without full re-render
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

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

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
          if (!raw) continue;

          if (pendingEvent === 'error') {
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
          } else if (pendingEvent === 'images') {
            pendingEvent = null;
            try { state.figures = JSON.parse(raw); } catch { /* ignore */ }
          } else if (pendingEvent === 'done') {
            pendingEvent = null;
            saveDraft();
            renderKatex();
            progressWrap.hidden = true;
            streamStatus.hidden = true;
          } else {
            pendingEvent = null;
            const chunk = JSON.parse(raw);
            editorAppend(chunk);
          }
        } else if (line.startsWith('event: done')) {
          saveDraft();
          renderKatex();
          progressWrap.hidden = true;
          streamStatus.hidden = true;
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

/* ─── KaTeX preview ─────────────────────────────────────────────────── */
function renderKatex() {
  const latex = state.editor ? state.editor.getValue() : '';
  katexPreview.textContent = latex || '(editor is empty)';

  if (typeof renderMathInElement === 'function') {
    renderMathInElement(katexPreview, {
      delimiters: [
        { left: '$$',  right: '$$',  display: true  },
        { left: '$',   right: '$',   display: false },
        { left: '\\[', right: '\\]', display: true  },
        { left: '\\(', right: '\\)', display: false },
        { left: '\\begin{equation}', right: '\\end{equation}', display: true },
        { left: '\\begin{align}',    right: '\\end{align}',    display: true },
        { left: '\\begin{align*}',   right: '\\end{align*}',   display: true },
      ],
      throwOnError: false,
      errorColor: '#f48771',
    });
  }
}

/* ─── PDF compilation ───────────────────────────────────────────────── */
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

  try {
    const resp = await fetch('/compile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ latex, images: state.figures }),
    });

    if (resp.ok) {
      const blob = await resp.blob();
      if (state.pdfUrl) URL.revokeObjectURL(state.pdfUrl);
      state.pdfUrl = URL.createObjectURL(blob);
      pdfPreview.src = state.pdfUrl;
      downloadPdfBtn.hidden = false;
      switchTab('pdf');
    } else {
      const detail = await resp.json().catch(() => ({ message: resp.statusText }));
      let parsed = detail;
      if (typeof detail.detail === 'string') {
        try { parsed = JSON.parse(detail.detail); } catch { parsed = { message: detail.detail }; }
      }
      const msg = parsed.message || `编译失败（HTTP ${resp.status}）`;
      const log = parsed.log || null;
      // Jump to error line in editor if available
      if (parsed.line) editorGoToLine(parsed.line);
      showErrorModal('编译失败', msg, log);
    }
  } catch (e) {
    showErrorModal('网络错误', e.message);
  } finally {
    compileOverlay.hidden = true;
    compileBtn.disabled = false;
  }
}

/* ─── Download ──────────────────────────────────────────────────────── */
async function downloadTex() {
  const latex = state.editor ? state.editor.getValue() : '';
  if (!latex.trim()) {
    showErrorModal('内容为空', '编辑器中没有可下载的内容。');
    return;
  }

  const defaultName = getDefaultFilename();
  const raw = await showInputModal('下载 LaTeX 文件', '输入文件名（无需加后缀）：', defaultName);
  if (raw === null) return;

  let name = (raw || defaultName).trim() || defaultName;
  name = name.replace(/[<>:"/\\|?*]/g, '_');
  if (!name.endsWith('.tex')) name += '.tex';

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

/* ─── Tab switching ─────────────────────────────────────────────────── */
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  katexPreview.classList.toggle('active', tab === 'katex');
  katexPreview.style.display = tab === 'katex' ? 'block' : 'none';
  pdfPreview.hidden = tab !== 'pdf';
  pdfPreview.style.display = tab === 'pdf' ? 'block' : '';
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
      // Editor may not be ready yet; wait a tick
      setTimeout(() => {
        state.editor.setValue(draft);
        renderKatex();
      }, 200);
    }
  } else {
    try { localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
  }
}

// Auto-save every 30s while editor has content
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
  katexPreview.textContent = '';
  if (state.pdfUrl) { URL.revokeObjectURL(state.pdfUrl); state.pdfUrl = null; }
  pdfPreview.src = 'about:blank';
  compileLog.hidden = true;
  try { localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ }
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
