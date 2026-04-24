/* ─── State ─────────────────────────────────────────────────────────── */
const state = {
  file: null,
  editor: null,          // Monaco instance (or null if CDN failed)
  pdflatexOk: false,
};

/* ─── DOM refs ──────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const dropTarget   = $('drop-target');
const fileInput    = $('file-input');
const fileInfo     = $('file-info');
const convertBtn   = $('convert-btn');
const clearBtn     = $('clear-btn');
const progressWrap = $('progress-wrap');
const errorMsg     = $('error-msg');
const uploadSection  = $('upload-section');
const editorSection  = $('editor-section');
const monacoContainer = $('monaco-container');
const katexPreview = $('katex-preview');
const pdfPreview   = $('pdf-preview');
const compileBtn   = $('compile-btn');
const downloadBtn  = $('download-btn');
const compileLog   = $('compile-log');
const logContent   = $('log-content');
const llmBadge     = $('llm-badge');
const footerLlm    = $('footer-llm');

/* ─── Init ──────────────────────────────────────────────────────────── */
(async function init() {
  await Promise.all([fetchLlmInfo(), fetchPdflatexHealth()]);
  initMonaco();
  initDragDrop();
  initButtons();
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
  dropTarget.addEventListener('click', () => fileInput.click());
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
    showError('Only PDF or image files (PNG, JPG, GIF, WebP) are supported.');
    return;
  }
  state.file = file;
  fileInfo.textContent = `${file.name}  (${formatBytes(file.size)})`;
  fileInfo.hidden = false;
  convertBtn.disabled = false;
  clearBtn.hidden = false;
  hideError();
}

/* ─── Buttons ───────────────────────────────────────────────────────── */
function initButtons() {
  convertBtn.addEventListener('click', startConvert);
  clearBtn.addEventListener('click', resetUpload);
  downloadBtn.addEventListener('click', downloadTex);
  compileBtn.addEventListener('click', compilePdf);
  $('close-log').addEventListener('click', () => { compileLog.hidden = true; });

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}

/* ─── Conversion (SSE) ──────────────────────────────────────────────── */
async function startConvert() {
  if (!state.file) return;

  convertBtn.disabled = true;
  clearBtn.hidden = true;
  progressWrap.hidden = false;
  hideError();

  const formData = new FormData();
  formData.append('file', state.file);

  let response;
  try {
    response = await fetch('/convert', { method: 'POST', body: formData });
  } catch (e) {
    showError(`Network error: ${e.message}`);
    resetProgress();
    return;
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    showError(`Server error ${response.status}: ${text || response.statusText}`);
    resetProgress();
    return;
  }

  let accumulated = '';

  // Show editor immediately so text streams in
  uploadSection.style.display = 'none';
  editorSection.hidden = false;
  if (state.editor) state.editor.setValue('');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete last line

      let errorEventDetected = false;
      for (const line of lines) {
        if (line.startsWith('event: error')) {
          errorEventDetected = true;
        } else if (line.startsWith('data: ')) {
          if (errorEventDetected) {
            errorEventDetected = false;
            try {
              const err = JSON.parse(line.slice(6));
              showError(err.message || 'Unknown error');
              if (state.editor) state.editor.setValue('');
            } catch { showError('Unknown error occurred'); }
          } else {
            const chunk = JSON.parse(line.slice(6));
            accumulated += chunk;
            if (state.editor) {
              state.editor.setValue(accumulated);
              const model = state.editor.getModel?.();
              if (model) state.editor.revealLine?.(model.getLineCount());
            }
          }
        } else if (line.startsWith('event: done')) {
          renderKatex();
          progressWrap.hidden = true;
        }
      }
    }
  } catch (e) {
    showError(`Stream error: ${e.message}`);
  } finally {
    resetProgress();
  }
}

/* ─── KaTeX preview ─────────────────────────────────────────────────── */
function renderKatex() {
  const latex = state.editor ? state.editor.getValue() : '';
  // Set as text first (HTML-escapes everything)
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
    showError('pdflatex is not installed on this server. Install TeX Live to enable PDF compilation.');
    return;
  }

  const latex = state.editor ? state.editor.getValue() : '';
  if (!latex.trim()) return;

  compileBtn.disabled = true;
  compileBtn.textContent = '⚙ Compiling…';
  compileLog.hidden = true;

  try {
    const resp = await fetch('/compile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ latex }),
    });

    if (resp.ok) {
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      // Open in new tab — more reliable than iframe across browsers
      window.open(url, '_blank');
    } else {
      const detail = await resp.json().catch(() => ({ message: resp.statusText }));
      let parsed = detail;
      // detail.detail might be a JSON string
      if (typeof detail.detail === 'string') {
        try { parsed = JSON.parse(detail.detail); } catch { parsed = { message: detail.detail }; }
      }
      if (parsed.log) {
        logContent.textContent = parsed.log;
        compileLog.hidden = false;
      } else {
        showError(parsed.message || `Compilation error (${resp.status})`);
      }
    }
  } catch (e) {
    showError(`Network error: ${e.message}`);
  } finally {
    compileBtn.disabled = false;
    compileBtn.textContent = '⚙ Compile PDF';
  }
}

/* ─── Download ──────────────────────────────────────────────────────── */
function downloadTex() {
  const latex = state.editor ? state.editor.getValue() : '';
  if (!latex) return;
  const blob = new Blob([latex], { type: 'text/x-tex' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'document.tex';
  a.click();
  URL.revokeObjectURL(url);
}

/* ─── Tab switching ─────────────────────────────────────────────────── */
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  katexPreview.classList.toggle('active', tab === 'katex');
  katexPreview.style.display = tab === 'katex' ? 'block' : 'none';
  pdfPreview.hidden = tab !== 'pdf';
  if (tab === 'pdf') pdfPreview.style.display = 'block';
}

/* ─── Helpers ───────────────────────────────────────────────────────── */
function showError(msg) {
  errorMsg.textContent = msg;
  errorMsg.hidden = false;
}
function hideError() { errorMsg.hidden = true; }

function resetProgress() {
  progressWrap.hidden = true;
  convertBtn.disabled = false;
  clearBtn.hidden = false;
}

function resetUpload() {
  state.file = null;
  fileInput.value = '';
  fileInfo.hidden = true;
  fileInfo.textContent = '';
  convertBtn.disabled = true;
  clearBtn.hidden = true;
  hideError();
  uploadSection.style.display = '';
  editorSection.hidden = true;
  if (state.editor) state.editor.setValue('');
  katexPreview.textContent = '';
  pdfPreview.src = 'about:blank';
  compileLog.hidden = true;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
