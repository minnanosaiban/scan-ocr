// redact.js — 墨消しページのフロントエンド。
// 流れ: ① ファイル+辞書を送って候補検出用OCR → ② ページ画像の上で候補を確認・調整
//       → ③ 確定した矩形を送って黒塗り→再OCR → ④ 結果ダウンロード

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const dropHint = document.getElementById("dropHint");
const filenameEl = document.getElementById("filename");
const dictionaryInput = document.getElementById("dictionaryInput");
const liteMode = document.getElementById("liteMode");
const startBtn = document.getElementById("startBtn");

const uploadCard = document.getElementById("uploadCard");
const progressCard = document.getElementById("progressCard");
const progressText = document.getElementById("progressText");
const progressFill = document.getElementById("progressFill");
const reviewCard = document.getElementById("reviewCard");
const applyProgressCard = document.getElementById("applyProgressCard");
const applyProgressFill = document.getElementById("applyProgressFill");
const resultCard = document.getElementById("resultCard");
const errorCard = document.getElementById("errorCard");
const errorText = document.getElementById("errorText");
const resetBtn = document.getElementById("resetBtn");

const prevPageBtn = document.getElementById("prevPageBtn");
const nextPageBtn = document.getElementById("nextPageBtn");
const pageIndicator = document.getElementById("pageIndicator");
const canvasWrap = document.getElementById("canvasWrap");
const pageImg = document.getElementById("pageImg");
const overlay = document.getElementById("overlay");
const candidateList = document.getElementById("candidateList");

const outputChks = document.querySelectorAll(".outputChk");
const nameTemplateInput = document.getElementById("nameTemplate");
const folderPathEl = document.getElementById("folderPath");
const pickFolderBtn = document.getElementById("pickFolderBtn");
const applyBtn = document.getElementById("applyBtn");

const resultSummary = document.getElementById("resultSummary");
const savedNote = document.getElementById("savedNote");
const resultItems = document.getElementById("resultItems");
const leftoverBox = document.getElementById("leftoverBox");
const leftoverList = document.getElementById("leftoverList");

let selectedFile = null;
let selectedFolder = "";
let jobId = null;
let pageCount = 0;
let currentPage = 1;
let pagesState = {}; // pageNo -> [{id, box:[x0,y0,x1,y1], text, reason, kind, included}]
let manualCounter = 0;
let scale = 1;
let dragging = false;
let dragStart = null;

const KIND_COLOR = {
  dict: "#e0433d",
  pattern: "#e0433d",
  manual: "#e0433d",
  low_confidence: "#e0b400",
};

function showCard(card) {
  for (const c of [uploadCard, progressCard, reviewCard, applyProgressCard, resultCard, errorCard]) {
    c.hidden = (c !== card);
  }
}

function showError(message) {
  errorText.textContent = message;
  showCard(errorCard);
}

// ── ① ファイル選択 ──────────────────────────────────────────────
function setFile(file) {
  if (!file) return;
  selectedFile = file;
  dropHint.hidden = true;
  filenameEl.hidden = false;
  filenameEl.textContent = file.name;
  startBtn.disabled = false;
}

fileInput.addEventListener("change", () => setFile(fileInput.files[0]));
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("dragover");
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});

startBtn.addEventListener("click", async () => {
  if (!selectedFile) return;
  showCard(progressCard);
  progressText.textContent = "アップロード中…";
  progressFill.style.width = "0%";

  const form = new FormData();
  form.append("file", selectedFile);
  form.append("lite", liteMode.checked ? "true" : "false");

  try {
    const res = await fetch("/api/redact/start", { method: "POST", body: form });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    jobId = data.job_id;
  } catch (e) {
    showError("開始に失敗しました: " + e.message);
    return;
  }

  pollLinesStatus();
});

function pollLinesStatus() {
  const timer = setInterval(async () => {
    try {
      const res = await fetch(`/api/redact/status/${jobId}`);
      if (!res.ok) throw new Error(await res.text());
      const s = await res.json();

      if (s.status === "processing" || s.status === "queued") {
        const total = s.pages_total || 0;
        const done = s.pages_done || 0;
        progressText.textContent = total
          ? `候補を検出しています… ${done} / ${total} ページ`
          : "候補を検出しています…";
        progressFill.style.width = total ? `${Math.round((done / total) * 100)}%` : "8%";
      } else if (s.status === "done") {
        clearInterval(timer);
        progressFill.style.width = "100%";
        await startReview(s.page_count);
      } else if (s.status === "error") {
        clearInterval(timer);
        showError(s.error || "候補検出中にエラーが発生しました");
      }
    } catch (e) {
      clearInterval(timer);
      showError("状態の取得に失敗しました: " + e.message);
    }
  }, 1000);
}

// ── ② レビュー ──────────────────────────────────────────────────
async function startReview(count) {
  pageCount = count;
  const form = new FormData();
  form.append("job_id", jobId);
  form.append("dictionary", dictionaryInput.value);

  let candidates;
  try {
    const res = await fetch("/api/redact/match", { method: "POST", body: form });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    candidates = data.candidates;
  } catch (e) {
    showError("候補の照合に失敗しました: " + e.message);
    return;
  }

  pagesState = {};
  for (let p = 1; p <= pageCount; p++) pagesState[p] = [];
  candidates.forEach((c, i) => {
    pagesState[c.page].push({
      id: `auto-${i}`,
      box: c.box,
      text: c.text,
      reason: c.reason,
      kind: c.kind,
      included: c.kind !== "low_confidence",
    });
  });

  currentPage = 1;
  showCard(reviewCard);
  await loadPage(currentPage);
}

function updatePageIndicator() {
  pageIndicator.textContent = `ページ ${currentPage} / ${pageCount}`;
  prevPageBtn.disabled = currentPage <= 1;
  nextPageBtn.disabled = currentPage >= pageCount;
}

async function loadPage(pageNo) {
  currentPage = pageNo;
  updatePageIndicator();
  await new Promise((resolve, reject) => {
    pageImg.onload = resolve;
    pageImg.onerror = () => reject(new Error("ページ画像の読み込みに失敗しました"));
    pageImg.src = `/api/redact/page/${jobId}/${pageNo}?t=${Date.now()}`;
  });
  resizeCanvas();
  drawOverlay();
  renderCandidateList();
}

function resizeCanvas() {
  scale = pageImg.clientWidth / pageImg.naturalWidth;
  overlay.width = pageImg.clientWidth;
  overlay.height = pageImg.clientHeight;
  overlay.style.width = pageImg.clientWidth + "px";
  overlay.style.height = pageImg.clientHeight + "px";
}

function drawOverlay(tempRect) {
  const ctx = overlay.getContext("2d");
  ctx.clearRect(0, 0, overlay.width, overlay.height);

  for (const c of pagesState[currentPage] || []) {
    const [x0, y0, x1, y1] = c.box;
    const sx0 = x0 * scale, sy0 = y0 * scale, sw = (x1 - x0) * scale, sh = (y1 - y0) * scale;
    const color = KIND_COLOR[c.kind] || "#e0433d";
    ctx.lineWidth = 2;
    ctx.strokeStyle = color;
    if (c.included) {
      ctx.fillStyle = color + "55";
      ctx.fillRect(sx0, sy0, sw, sh);
    } else {
      ctx.setLineDash([4, 3]);
    }
    ctx.strokeRect(sx0, sy0, sw, sh);
    ctx.setLineDash([]);
  }

  if (tempRect) {
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#e0433d";
    ctx.strokeRect(tempRect.x, tempRect.y, tempRect.w, tempRect.h);
  }
}

function hitTest(sx, sy) {
  const list = pagesState[currentPage] || [];
  for (let i = list.length - 1; i >= 0; i--) {
    const [x0, y0, x1, y1] = list[i].box;
    const bx0 = x0 * scale, by0 = y0 * scale, bx1 = x1 * scale, by1 = y1 * scale;
    if (sx >= bx0 && sx <= bx1 && sy >= by0 && sy <= by1) return list[i];
  }
  return null;
}

function canvasPoint(e) {
  const rect = overlay.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}

overlay.addEventListener("mousedown", (e) => {
  const pt = canvasPoint(e);
  const hit = hitTest(pt.x, pt.y);
  if (hit) {
    hit.included = !hit.included;
    drawOverlay();
    renderCandidateList();
    return;
  }
  dragging = true;
  dragStart = pt;
});

overlay.addEventListener("mousemove", (e) => {
  if (!dragging) return;
  const pt = canvasPoint(e);
  const rect = {
    x: Math.min(dragStart.x, pt.x), y: Math.min(dragStart.y, pt.y),
    w: Math.abs(pt.x - dragStart.x), h: Math.abs(pt.y - dragStart.y),
  };
  drawOverlay(rect);
});

window.addEventListener("mouseup", (e) => {
  if (!dragging) return;
  dragging = false;
  const pt = canvasPoint(e);
  const sx0 = Math.min(dragStart.x, pt.x), sy0 = Math.min(dragStart.y, pt.y);
  const sw = Math.abs(pt.x - dragStart.x), sh = Math.abs(pt.y - dragStart.y);
  if (sw < 6 || sh < 6) { drawOverlay(); return; }

  const box = [sx0 / scale, sy0 / scale, (sx0 + sw) / scale, (sy0 + sh) / scale];
  pagesState[currentPage].push({
    id: `manual-${manualCounter++}`,
    box, text: "(手動で指定した範囲)", reason: "手動追加", kind: "manual", included: true,
  });
  drawOverlay();
  renderCandidateList();
});

function renderCandidateList() {
  const list = pagesState[currentPage] || [];
  candidateList.innerHTML = "";
  if (!list.length) {
    candidateList.innerHTML = `<div class="candidate-empty">このページに候補はありません（必要なら画像をドラッグして手動で追加できます）</div>`;
    return;
  }
  list.forEach((c) => {
    const row = document.createElement("label");
    row.className = "candidate-row";
    row.innerHTML = `
      <input type="checkbox" ${c.included ? "checked" : ""}>
      <span class="candidate-swatch ${c.kind}"></span>
      <span class="candidate-text"><span class="ctext">${escapeHtml(c.text)}</span><br><span class="creason">${escapeHtml(c.reason)}</span></span>
    `;
    row.querySelector("input").addEventListener("change", (e) => {
      c.included = e.target.checked;
      drawOverlay();
    });
    candidateList.appendChild(row);
  });
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : s;
  return d.innerHTML;
}

prevPageBtn.addEventListener("click", () => { if (currentPage > 1) loadPage(currentPage - 1); });
nextPageBtn.addEventListener("click", () => { if (currentPage < pageCount) loadPage(currentPage + 1); });
window.addEventListener("resize", () => { if (!reviewCard.hidden) { resizeCanvas(); drawOverlay(); } });

// ── 出力設定 ──────────────────────────────────────────────────
async function askServerForFolder(title) {
  const form = new FormData();
  form.append("title", title);
  const res = await fetch("/api/pick-folder", { method: "POST", body: form });
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  return data.path;
}

pickFolderBtn.addEventListener("click", async (e) => {
  e.preventDefault();
  pickFolderBtn.disabled = true;
  try {
    const path = await askServerForFolder("保存先フォルダを選択");
    if (path) { selectedFolder = path; folderPathEl.textContent = path; }
  } catch (e) {
    // キャンセル・失敗時は静かに元のまま
  } finally {
    pickFolderBtn.disabled = false;
  }
});

// ── ③ 墨消し実行 ────────────────────────────────────────────────
applyBtn.addEventListener("click", async () => {
  const outputs = Array.from(outputChks).filter((c) => c.checked).map((c) => c.value);
  if (!outputs.length) { showError("作成するファイルを1つ以上選んでください"); return; }

  const boxes = {};
  let total = 0;
  for (const [pageNo, list] of Object.entries(pagesState)) {
    const included = list.filter((c) => c.included).map((c) => c.box);
    if (included.length) { boxes[pageNo] = included; total += included.length; }
  }

  const form = new FormData();
  form.append("job_id", jobId);
  form.append("boxes", JSON.stringify(boxes));
  form.append("dictionary", dictionaryInput.value);
  form.append("outputs", outputs.join(","));
  form.append("output_dir", selectedFolder);
  form.append("name_template", nameTemplateInput.value);
  form.append("lite", liteMode.checked ? "true" : "false");

  showCard(applyProgressCard);
  applyProgressFill.style.width = "5%";

  try {
    const res = await fetch("/api/redact/apply", { method: "POST", body: form });
    if (!res.ok) throw new Error(await res.text());
  } catch (e) {
    showError("墨消しの実行開始に失敗しました: " + e.message);
    return;
  }

  pollApplyStatus();
});

function pollApplyStatus() {
  const timer = setInterval(async () => {
    try {
      const res = await fetch(`/api/redact/status/${jobId}`);
      if (!res.ok) throw new Error(await res.text());
      const s = await res.json();

      if (s.status === "processing" || s.status === "queued") {
        const total = s.pages_total || 0;
        const done = s.pages_done || 0;
        document.getElementById("applyProgressText").textContent = total
          ? `墨消しを焼き込んでOCRし直しています… ${done} / ${total} ページ`
          : "墨消しを焼き込んでOCRし直しています…";
        applyProgressFill.style.width = total ? `${Math.round((done / total) * 100)}%` : "10%";
      } else if (s.status === "done") {
        clearInterval(timer);
        applyProgressFill.style.width = "100%";
        showResult(s);
      } else if (s.status === "error") {
        clearInterval(timer);
        showError(s.error || "墨消しの実行中にエラーが発生しました");
      }
    } catch (e) {
      clearInterval(timer);
      showError("状態の取得に失敗しました: " + e.message);
    }
  }, 1200);
}

const OUTPUT_META = {
  pdf: { label: "テキスト乗せPDF" },
  md: { label: "Markdown" },
  json: { label: "JSON" },
};

function showResult(s) {
  resultSummary.textContent = "墨消しが完了しました";

  savedNote.hidden = true;
  savedNote.innerHTML = "";
  if (s.saved_dir) {
    savedNote.hidden = false;
    const openBtn = document.createElement("button");
    openBtn.className = "btn-outline";
    openBtn.textContent = "フォルダを開く";
    openBtn.addEventListener("click", () => {
      const f = new FormData();
      f.append("path", s.saved_dir);
      fetch("/api/open-folder", { method: "POST", body: f });
    });
    savedNote.append(
      "保存先: ",
      Object.assign(document.createElement("span"), { className: "path", textContent: s.saved_dir }),
      openBtn,
    );
  } else if (s.save_warning) {
    savedNote.hidden = false;
    savedNote.textContent = s.save_warning + "（ダウンロードは可能です）";
  }

  resultItems.innerHTML = "";
  for (const fmt of s.files) {
    const meta = OUTPUT_META[fmt];
    if (!meta) continue;
    const item = document.createElement("div");
    item.className = "result-item";
    item.innerHTML = `
      <div class="result-item-label">${meta.label}</div>
      <a class="btn-outline" href="/api/redact/download/${jobId}/${fmt}" download>ダウンロード</a>
    `;
    resultItems.appendChild(item);
  }

  leftoverBox.hidden = !(s.leftover && s.leftover.length);
  leftoverList.innerHTML = "";
  if (s.leftover) {
    for (const item of s.leftover) {
      const li = document.createElement("li");
      li.textContent = `ページ ${item.page}: 「${item.text}」（${item.reason}）`;
      leftoverList.appendChild(li);
    }
  }

  showCard(resultCard);
}

resetBtn.addEventListener("click", () => {
  selectedFile = null;
  selectedFolder = "";
  jobId = null;
  pagesState = {};
  fileInput.value = "";
  dropHint.hidden = false;
  filenameEl.hidden = true;
  startBtn.disabled = true;
  folderPathEl.textContent = "未選択（ダウンロードのみ）";
  showCard(uploadCard);
});
