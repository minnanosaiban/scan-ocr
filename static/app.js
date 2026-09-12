const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const dropHint = document.getElementById("dropHint");
const filenameEl = document.getElementById("filename");

const inputFolderPathEl = document.getElementById("inputFolderPath");
const pickInputFolderBtn = document.getElementById("pickInputFolderBtn");
const clearInputFolderBtn = document.getElementById("clearInputFolderBtn");

const outputChks = document.querySelectorAll(".outputChk");
const outputHint = document.getElementById("outputHint");
const nameTemplateInput = document.getElementById("nameTemplate");
const folderPathEl = document.getElementById("folderPath");
const pickFolderBtn = document.getElementById("pickFolderBtn");
const folderPathHint = document.getElementById("folderPathHint");
const liteMode = document.getElementById("liteMode");
const startBtn = document.getElementById("startBtn");

const uploadCard = document.getElementById("uploadCard");
const progressCard = document.getElementById("progressCard");
const resultCard = document.getElementById("resultCard");
const errorCard = document.getElementById("errorCard");
const progressText = document.getElementById("progressText");
const progressFill = document.getElementById("progressFill");
const resultSummary = document.getElementById("resultSummary");
const savedNote = document.getElementById("savedNote");
const resultItems = document.getElementById("resultItems");
const errorText = document.getElementById("errorText");
const resetBtn = document.getElementById("resetBtn");

const FILE_ICON = `<svg class="icon" xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 16 16" aria-hidden="true"><path d="M14 14V4.5L9.5 0H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2M9.5 3A1.5 1.5 0 0 0 11 4.5h2V14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1h5.5z"/></svg>`;
const DOWNLOAD_ICON = `<svg class="icon" xmlns="http://www.w3.org/2000/svg" fill="currentColor" viewBox="0 0 16 16" aria-hidden="true"><path d="M.5 9.9a.5.5 0 0 1 .5.5v2.5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2.5a.5.5 0 0 1 1 0v2.5a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2v-2.5a.5.5 0 0 1 .5-.5"/><path d="M7.646 11.854a.5.5 0 0 0 .708 0l3-3a.5.5 0 0 0-.708-.708L8.5 10.293V1.5a.5.5 0 0 0-1 0v8.793L5.354 8.146a.5.5 0 1 0-.708.708z"/></svg>`;

const OUTPUT_META = {
  pdf: { label: "テキスト乗せPDF", desc: "元の見た目のまま、文字を検索・コピーできます" },
  md: { label: "Markdown", desc: "全ページ結合のテキスト（見出しは自動でH1）" },
  json: { label: "JSON", desc: "段落・表・座標などの構造化データ" },
};

let selectedFile = null;
let inputFolder = "";
let selectedFolder = "";
let pollTimer = null;

function showCard(card) {
  for (const c of [uploadCard, progressCard, resultCard, errorCard]) c.hidden = (c !== card);
}

function selectedOutputs() {
  return Array.from(outputChks).filter((c) => c.checked).map((c) => c.value);
}

function updateStartEnabled() {
  const hasOutputs = selectedOutputs().length > 0;
  outputHint.hidden = hasOutputs;
  startBtn.disabled = !((selectedFile || inputFolder) && hasOutputs);
}

function setFile(file) {
  if (!file) return;
  clearInputFolder();
  selectedFile = file;
  dropHint.hidden = true;
  filenameEl.hidden = false;
  filenameEl.textContent = file.name;
  updateStartEnabled();
}

function setInputFolder(path) {
  if (!path) return;
  resetFileSelection();
  inputFolder = path;
  inputFolderPathEl.textContent = path;
  clearInputFolderBtn.hidden = false;
  folderPathHint.hidden = false;
  updateStartEnabled();
}

function clearInputFolder() {
  inputFolder = "";
  inputFolderPathEl.textContent = "未選択";
  clearInputFolderBtn.hidden = true;
  folderPathHint.hidden = true;
  updateStartEnabled();
}

function resetFileSelection() {
  selectedFile = null;
  fileInput.value = "";
  dropHint.hidden = false;
  filenameEl.hidden = true;
}

fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
outputChks.forEach((c) => c.addEventListener("change", updateStartEnabled));

dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});

async function askServerForFolder(title) {
  const form = new FormData();
  form.append("title", title);
  const res = await fetch("/api/pick-folder", { method: "POST", body: form });
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  return data.path; // 空文字はキャンセル
}

pickInputFolderBtn.addEventListener("click", async (e) => {
  e.preventDefault();
  pickInputFolderBtn.disabled = true;
  try {
    const path = await askServerForFolder("処理するフォルダを選択（直下のPDFすべてが対象）");
    if (path) setInputFolder(path);
  } catch (e) {
    // キャンセル・失敗時は静かに元のまま
  } finally {
    pickInputFolderBtn.disabled = false;
  }
});
clearInputFolderBtn.addEventListener("click", (e) => { e.preventDefault(); clearInputFolder(); });

pickFolderBtn.addEventListener("click", async (e) => {
  e.preventDefault();
  pickFolderBtn.disabled = true;
  try {
    const path = await askServerForFolder("保存先フォルダを選択");
    if (path) {
      selectedFolder = path;
      folderPathEl.textContent = path;
    }
  } catch (e) {
    // キャンセル・失敗時は静かに元のまま
  } finally {
    pickFolderBtn.disabled = false;
  }
});

startBtn.addEventListener("click", async (e) => {
  e.preventDefault();
  if (!selectedFile && !inputFolder) return;
  showCard(progressCard);
  progressText.textContent = inputFolder ? "処理中…" : "アップロード中…";
  progressFill.style.width = "0%";

  const form = new FormData();
  form.append("lite", liteMode.checked ? "true" : "false");
  form.append("outputs", selectedOutputs().join(","));
  form.append("output_dir", selectedFolder);
  form.append("name_template", nameTemplateInput.value);

  let url = "/api/process";
  if (inputFolder) {
    url = "/api/process-folder";
    form.append("folder", inputFolder);
  } else {
    form.append("file", selectedFile);
  }

  let jobId;
  try {
    const res = await fetch(url, { method: "POST", body: form });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    jobId = data.job_id;
  } catch (e) {
    showError("開始に失敗しました: " + e.message);
    return;
  }

  pollTimer = setInterval(() => pollStatus(jobId), 1200);
});

async function pollStatus(jobId) {
  try {
    const res = await fetch(`/api/status/${jobId}`);
    if (!res.ok) throw new Error(await res.text());
    const s = await res.json();

    if (s.status === "processing" || s.status === "queued") {
      if (s.mode === "batch") {
        const ft = s.file_total || 0;
        const done = ft ? (s.file_index - 1) + (s.pages_total ? s.pages_done / s.pages_total : 0) : 0;
        progressText.textContent = s.current_file
          ? `処理中… ファイル ${s.file_index} / ${ft}（${s.current_file}） ページ ${s.pages_done} / ${s.pages_total || "?"}`
          : "処理中…";
        progressFill.style.width = ft ? `${Math.round((done / ft) * 100)}%` : "8%";
      } else {
        const total = s.pages_total || 0;
        const done = s.pages_done || 0;
        progressText.textContent = total
          ? `処理中… ${done} / ${total} ページ`
          : "処理中…";
        progressFill.style.width = total ? `${Math.round((done / total) * 100)}%` : "8%";
      }
    } else if (s.status === "done") {
      clearInterval(pollTimer);
      progressFill.style.width = "100%";
      if (s.mode === "batch") showBatchResult(s); else showResult(jobId, s);
    } else if (s.status === "error") {
      clearInterval(pollTimer);
      showError(s.error || "処理中にエラーが発生しました");
    }
  } catch (e) {
    clearInterval(pollTimer);
    showError("状態の取得に失敗しました: " + e.message);
  }
}

function renderSavedNote(savedDir, warning) {
  savedNote.hidden = true;
  savedNote.innerHTML = "";
  if (savedDir) {
    savedNote.hidden = false;
    const openBtn = document.createElement("button");
    openBtn.className = "btn-outline";
    openBtn.textContent = "フォルダを開く";
    openBtn.addEventListener("click", () => {
      const f = new FormData();
      f.append("path", savedDir);
      fetch("/api/open-folder", { method: "POST", body: f });
    });
    savedNote.append(
      `保存先: `,
      Object.assign(document.createElement("span"), { className: "path", textContent: savedDir }),
      openBtn,
    );
  } else if (warning) {
    savedNote.hidden = false;
    savedNote.textContent = warning + "（ダウンロードは可能です）";
  }
}

function showResult(jobId, s) {
  resultSummary.textContent = `完了（${s.pages_total}ページ、${s.elapsed_sec.toFixed(0)}秒）`;
  renderSavedNote(s.saved_dir, s.save_warning);

  resultItems.innerHTML = "";
  for (const fmt of s.outputs) {
    const meta = OUTPUT_META[fmt];
    if (!meta) continue;
    const item = document.createElement("div");
    item.className = "result-item";
    item.innerHTML = `
      <div>
        <div class="result-item-label">${FILE_ICON}${meta.label}</div>
        <div class="result-item-desc">${meta.desc}</div>
      </div>
      <a class="btn-outline" href="/api/download/${jobId}/${fmt}" download>${DOWNLOAD_ICON}ダウンロード</a>
    `;
    resultItems.appendChild(item);
  }

  showCard(resultCard);
}

function showBatchResult(s) {
  const okCount = s.results.filter((r) => r.status === "done").length;
  const errCount = s.results.length - okCount;
  resultSummary.textContent = errCount
    ? `完了（${s.results.length}件中 成功${okCount}件・失敗${errCount}件、${s.elapsed_sec.toFixed(0)}秒）`
    : `完了（${s.results.length}件すべて成功、${s.elapsed_sec.toFixed(0)}秒）`;
  renderSavedNote(s.saved_dir, null);

  const list = document.createElement("div");
  list.className = "batch-list";
  for (const r of s.results) {
    const row = document.createElement("div");
    row.className = "batch-list-item";
    if (r.status === "done") {
      row.innerHTML = `<span class="batch-status-ok">✓</span><span class="fname">${r.file}</span>`;
    } else {
      row.innerHTML = `<span class="batch-status-error">✕</span><span class="fname">${r.file}</span><span class="emsg">— ${r.error}</span>`;
    }
    list.appendChild(row);
  }
  resultItems.innerHTML = "";
  resultItems.appendChild(list);

  showCard(resultCard);
}

function showError(message) {
  errorText.textContent = message;
  showCard(errorCard);
}

resetBtn.addEventListener("click", resetForm);

function resetForm() {
  resetFileSelection();
  clearInputFolder();
  updateStartEnabled();
  showCard(uploadCard);
}
