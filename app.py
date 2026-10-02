"""
app.py — scan-ocr のローカルWebサーバー。

起動:
    python app.py
    (または run.bat をダブルクリック)
既定で http://127.0.0.1:8791 を開く。
"""

import functools
import os
import shutil
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from ocr_pipeline import (
    process_document,
    process_rendered,
    SUPPORT_INPUT_EXT,
    SUPPORT_OUTPUTS,
    DEFAULT_NAME_TEMPLATE,
)
import redact as redact_mod

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

JOB_MAX_AGE_SEC = 24 * 3600


# OCRモデル(DocumentAnalyzer)は全ジョブで共有していて並行利用に対して無防備、かつCPUも食うので、
# OCRを行うジョブは1つずつ順番に実行する（待っている間は status="queued" のまま）。
_OCR_LOCK = threading.Lock()


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _OCR_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _rmtree_quiet(path: Path):
    shutil.rmtree(path, ignore_errors=True)


def _rm_quiet(path: Path):
    try:
        path.unlink()
    except OSError:
        pass


def _purge_jobs(max_age_sec: float | None):
    """jobs/ の作業ファイル(アップロード原本・墨消し前のページ画像・OCR全文など)を消す。
    max_age_sec=None なら全削除(起動時: メモリ上のJOBSは空なので、残りは全て孤児)。
    実行中のジョブを巻き込まないよう、それ以外は更新から一定時間たったものだけ消す。"""
    now = time.time()
    for d in JOBS_DIR.iterdir():
        try:
            if max_age_sec is None or now - d.stat().st_mtime > max_age_sec:
                _rmtree_quiet(d) if d.is_dir() else _rm_quiet(d)
        except OSError:
            pass


def _safe_upload_name(filename: str | None) -> str:
    """クライアント指定のファイル名からディレクトリ成分を落とす（`..\` による jobs 外への書き込み防止）。"""
    name = Path((filename or "").replace("\\", "/")).name
    return name if name not in ("", ".", "..") else "upload"

HOST = "127.0.0.1"
PORT = 8791

app = FastAPI()

# このサーバーは自分のPC専用。他サイトのページ（クロスサイトのフォームPOST）や、
# DNSリバインディングで別名からのアクセスを、Host/Originの検査で拒否する。
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
_ALLOWED_ORIGINS = {f"http://{h}" for h in _ALLOWED_HOSTS}


@app.middleware("http")
async def local_only_guard(request, call_next):
    if request.headers.get("host", "") not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "許可されていないHostです"}, status_code=403)
    origin = request.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        return JSONResponse({"detail": "許可されていないOriginです"}, status_code=403)
    return await call_next(request)

# job_id -> 下記いずれか
#   単発: {mode:"single", status, pages_total, pages_done, elapsed_sec, error,
#          outputs, files, saved_dir, save_warning}
#   フォルダ一括: {mode:"batch", status, file_index, file_total, current_file,
#                 pages_total, pages_done, elapsed_sec, error, outputs, results, saved_dir}
JOBS: dict = {}


def _parse_outputs(outputs: str) -> set:
    return {o.strip() for o in outputs.split(",") if o.strip()} & SUPPORT_OUTPUTS


@_serialized
def run_job(job_id: str, input_path: Path, output_dir: str | None, name_template: str):
    job = JOBS[job_id]
    job["status"] = "processing"
    t0 = time.time()

    def on_progress(done, total):
        job["pages_done"] = done
        job["pages_total"] = total

    try:
        outdir = JOBS_DIR / job_id / "output"
        result = process_document(
            input_path,
            outdir,
            lite=job["lite"],
            outputs=job["outputs"],
            name_template=name_template,
            on_progress=on_progress,
        )
        job["files"] = {k: str(result[k]) for k in job["outputs"]}
        job["pages_total"] = result["pages"]
        job["pages_done"] = result["pages"]
        job["elapsed_sec"] = time.time() - t0

        if output_dir:
            try:
                dest = Path(output_dir)
                for path in job["files"].values():
                    shutil.copy2(path, dest / Path(path).name)
                job["saved_dir"] = str(dest)
            except Exception as e:
                job["save_warning"] = f"保存先フォルダへのコピーに失敗しました: {e}"

        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        _rm_quiet(input_path)  # アップロード原本は処理後に残さない（出力は output/ に残る）


@app.post("/api/process")
async def api_process(
    file: UploadFile = File(...),
    lite: str = Form("false"),
    outputs: str = Form("pdf,md,json"),
    output_dir: str = Form(""),
    name_template: str = Form(DEFAULT_NAME_TEMPLATE),
):
    upload_name = _safe_upload_name(file.filename)
    ext = Path(upload_name).suffix[1:].lower()
    if ext not in SUPPORT_INPUT_EXT:
        raise HTTPException(400, f"未対応の形式です: .{ext}")

    output_set = _parse_outputs(outputs)
    if not output_set:
        raise HTTPException(400, "出力形式を1つ以上選んでください")

    _purge_jobs(JOB_MAX_AGE_SEC)
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    input_path = job_dir / upload_name
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    JOBS[job_id] = {
        "mode": "single",
        "status": "queued",
        "pages_total": 0,
        "pages_done": 0,
        "elapsed_sec": 0,
        "error": None,
        "lite": lite.lower() == "true",
        "outputs": output_set,
        "files": {},
        "saved_dir": None,
        "save_warning": None,
    }

    thread = threading.Thread(
        target=run_job,
        args=(job_id, input_path, output_dir.strip() or None, name_template),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


def _list_batch_pdfs(folder: Path):
    # 前回このアプリが書き出した(既定名 "*_ocr.pdf")と思われるファイルは対象から除く。
    # （出力先=入力フォルダのまま繰り返し実行すると、自分の生成物を再OCRしてしまうのを防ぐ。
    #  ただしファイル名パターンを変更した場合はこの限りではない。）
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf" and not p.stem.endswith("_ocr")
    )


@_serialized
def run_batch_job(job_id: str, folder: Path, output_dir: str | None, name_template: str):
    job = JOBS[job_id]
    job["status"] = "processing"
    t0 = time.time()
    out_root = Path(output_dir) if output_dir else folder

    def on_progress(done, total):
        job["pages_done"] = done
        job["pages_total"] = total

    pdfs = _list_batch_pdfs(folder)
    job["file_total"] = len(pdfs)

    for i, pdf in enumerate(pdfs):
        job["file_index"] = i + 1
        job["current_file"] = pdf.name
        job["pages_done"] = 0
        job["pages_total"] = 0
        try:
            result = process_document(
                pdf,
                out_root,
                lite=job["lite"],
                outputs=job["outputs"],
                name_template=name_template,
                on_progress=on_progress,
            )
            job["results"].append({"file": pdf.name, "status": "done", "pages": result["pages"]})
        except Exception as e:
            job["results"].append({"file": pdf.name, "status": "error", "error": str(e)})

    job["elapsed_sec"] = time.time() - t0
    job["saved_dir"] = str(out_root)
    job["status"] = "done"


@app.post("/api/process-folder")
async def api_process_folder(
    folder: str = Form(...),
    lite: str = Form("false"),
    outputs: str = Form("pdf,md,json"),
    output_dir: str = Form(""),
    name_template: str = Form(DEFAULT_NAME_TEMPLATE),
):
    folder_path = Path(folder)
    if not folder_path.is_dir():
        raise HTTPException(404, "フォルダが見つかりません")

    output_set = _parse_outputs(outputs)
    if not output_set:
        raise HTTPException(400, "出力形式を1つ以上選んでください")

    if not _list_batch_pdfs(folder_path):
        raise HTTPException(400, "このフォルダにはPDFがありません")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "mode": "batch",
        "status": "queued",
        "file_index": 0,
        "file_total": 0,
        "current_file": None,
        "pages_total": 0,
        "pages_done": 0,
        "elapsed_sec": 0,
        "error": None,
        "lite": lite.lower() == "true",
        "outputs": output_set,
        "results": [],
        "saved_dir": None,
    }

    thread = threading.Thread(
        target=run_batch_job,
        args=(job_id, folder_path, output_dir.strip() or None, name_template),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "ジョブが見つかりません")

    base = {
        "mode": job["mode"],
        "status": job["status"],
        "pages_total": job["pages_total"],
        "pages_done": job["pages_done"],
        "elapsed_sec": job["elapsed_sec"],
        "error": job["error"],
        "outputs": sorted(job["outputs"]),
        "saved_dir": job["saved_dir"],
    }
    if job["mode"] == "single":
        base["save_warning"] = job["save_warning"]
    else:
        base["file_index"] = job["file_index"]
        base["file_total"] = job["file_total"]
        base["current_file"] = job["current_file"]
        base["results"] = job["results"]
    return base


FMT_MEDIA = {
    "pdf": "application/pdf",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
}


@app.get("/api/download/{job_id}/{fmt}")
async def api_download(job_id: str, fmt: str):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done" or job["mode"] != "single":
        raise HTTPException(404, "ファイルがまだありません")
    path = job["files"].get(fmt)
    if not path:
        raise HTTPException(404, f"この形式は生成されていません: {fmt}")
    return FileResponse(path, media_type=FMT_MEDIA.get(fmt), filename=Path(path).name)


import json as _json

# ── 墨消し ──────────────────────────────────────────────────────────
# job_id -> {phase:"lines"|"apply", status, pages_done, pages_total, page_count,
#            error, dir(Path), stem, files, saved_dir, save_warning, leftover}
REDACT_JOBS: dict = {}


def _review_ready(job) -> bool:
    """確認画面の操作（照合・適用）ができる状態か。候補検出が完了している、
    または墨消し適用に失敗してやり直す場合（元の画像は残してある）。"""
    return (job["phase"] == "lines" and job["status"] == "done") or            (job["phase"] == "apply" and job["status"] == "error")


@_serialized
def run_redact_lines_job(job_id: str, input_path: Path, lite: bool, dpi: int = 200):
    job = REDACT_JOBS[job_id]
    job["status"] = "processing"
    try:
        imgs = redact_mod.render_pages(input_path, dpi=dpi)
        _rm_quiet(input_path)  # 以降はページ画像(pages/)だけを使う。原本は残さない
        job["page_count"] = len(imgs)
        redact_dir = job["dir"]
        redact_mod.save_page_images(imgs, redact_dir / "pages")

        def on_progress(done, total):
            job["pages_done"] = done
            job["pages_total"] = total

        pages_lines = redact_mod.ocr_lines(imgs, lite=lite, on_progress=on_progress)
        (redact_dir / "lines.json").write_text(
            _json.dumps(pages_lines, ensure_ascii=False), encoding="utf-8"
        )
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.post("/api/redact/start")
async def api_redact_start(
    file: UploadFile = File(...),
    lite: str = Form("false"),
):
    upload_name = _safe_upload_name(file.filename)
    ext = Path(upload_name).suffix[1:].lower()
    if ext not in SUPPORT_INPUT_EXT:
        raise HTTPException(400, f"未対応の形式です: .{ext}")

    _purge_jobs(JOB_MAX_AGE_SEC)
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    redact_dir = job_dir / "redact"
    redact_dir.mkdir(parents=True, exist_ok=True)

    input_path = job_dir / upload_name
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    REDACT_JOBS[job_id] = {
        "phase": "lines",
        "status": "queued",
        "pages_done": 0,
        "pages_total": 0,
        "page_count": 0,
        "error": None,
        "dir": redact_dir,
        "stem": Path(upload_name).stem,
        "input_name": upload_name,
        "input_path": input_path,
        "files": {},
        "saved_dir": None,
        "save_warning": None,
        "leftover": None,
    }

    thread = threading.Thread(
        target=run_redact_lines_job,
        args=(job_id, input_path, lite.lower() == "true"),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/redact/status/{job_id}")
async def api_redact_status(job_id: str):
    job = REDACT_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "ジョブが見つかりません")
    return {
        "phase": job["phase"],
        "status": job["status"],
        "pages_done": job["pages_done"],
        "pages_total": job["pages_total"],
        "page_count": job["page_count"],
        "error": job["error"],
        "files": sorted(job["files"].keys()),
        "saved_dir": job["saved_dir"],
        "save_warning": job["save_warning"],
        "leftover": job["leftover"],
    }


@app.get("/api/redact/page/{job_id}/{page_no}")
async def api_redact_page(job_id: str, page_no: int):
    job = REDACT_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "ジョブが見つかりません")
    path = job["dir"] / "pages" / f"page_{page_no:03d}.png"
    if not path.exists():
        raise HTTPException(404, "ページ画像が見つかりません")
    return FileResponse(path, media_type="image/png")


@app.post("/api/redact/match")
async def api_redact_match(job_id: str = Form(...), dictionary: str = Form("")):
    job = REDACT_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "ジョブが見つかりません")
    if not _review_ready(job):
        raise HTTPException(409, "候補検出用OCRがまだ完了していません")

    lines_path = job["dir"] / "lines.json"

    def _match():
        pages_lines = _json.loads(lines_path.read_text(encoding="utf-8"))
        terms = redact_mod.load_dictionary(dictionary)
        return redact_mod.find_candidates(pages_lines, terms)

    # 辞書が大きいと照合はCPUを食うので、イベントループを塞がないようスレッドで実行する
    candidates = await run_in_threadpool(_match)
    return {
        "page_count": job["page_count"],
        "candidates": [c.to_dict() for c in candidates],
    }


@_serialized
def run_redact_apply_job(
    job_id: str,
    boxes_per_page: dict,
    dictionary_text: str,
    outputs: set,
    output_dir: str | None,
    name_template: str,
    lite: bool,
):
    job = REDACT_JOBS[job_id]
    job["status"] = "processing"
    t0 = time.time()
    try:
        page_paths = sorted((job["dir"] / "pages").glob("page_*.png"))
        imgs = redact_mod.load_page_images(page_paths)
        redacted_imgs = redact_mod.burn_boxes(imgs, boxes_per_page)

        def on_progress(done, total):
            job["pages_done"] = done
            job["pages_total"] = total

        outdir = job["dir"] / "output"
        result = process_rendered(
            redacted_imgs,
            job["stem"],
            outdir,
            lite=lite,
            outputs=outputs,
            name_template=name_template,
            on_progress=on_progress,
            source_name=job["input_name"],
            extra_json_fields={"redacted": True},
        )
        job["files"] = {k: str(result[k]) for k in outputs if k in result}
        job["elapsed_sec"] = time.time() - t0

        # 検証: 墨消し後の画像を再OCRし、辞書語・パターンが残っていないか確認する
        # 辞書が空でも、パターン(電話番号・郵便番号・日付)の残りは確認する
        terms = redact_mod.load_dictionary(dictionary_text)
        verify_lines = redact_mod.ocr_lines(redacted_imgs, lite=lite)
        job["leftover"] = redact_mod.verify_no_leftover(verify_lines, terms)

        if output_dir:
            try:
                dest = Path(output_dir)
                for path in job["files"].values():
                    shutil.copy2(path, dest / Path(path).name)
                job["saved_dir"] = str(dest)
            except Exception as e:
                job["save_warning"] = f"保存先フォルダへのコピーに失敗しました: {e}"

        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        # 成功したら、墨消し前のページ画像・OCR全文は不要なので消す（墨消し済みの出力 output/ だけ残す）。
        # 失敗したときは、確認画面に戻って再実行できるよう残す（24時間後/次回起動時に削除される）。
        if job["status"] == "done":
            _rmtree_quiet(job["dir"] / "pages")
            _rm_quiet(job["dir"] / "lines.json")


@app.post("/api/redact/apply")
async def api_redact_apply(
    job_id: str = Form(...),
    boxes: str = Form(...),  # JSON文字列: {"1": [[x0,y0,x1,y1], ...], "2": [...]}
    dictionary: str = Form(""),
    outputs: str = Form("pdf,md,json"),
    output_dir: str = Form(""),
    name_template: str = Form(DEFAULT_NAME_TEMPLATE),
    lite: str = Form("false"),
):
    job = REDACT_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "ジョブが見つかりません")
    if not _review_ready(job):
        raise HTTPException(409, "候補検出用OCRがまだ完了していません")

    try:
        boxes_raw = _json.loads(boxes)
        boxes_per_page = {int(k): v for k, v in boxes_raw.items()}
    except Exception:
        raise HTTPException(400, "矩形データの形式が不正です")

    output_set = _parse_outputs(outputs)
    if not output_set:
        raise HTTPException(400, "出力形式を1つ以上選んでください")

    # 状態の切り替えはここ(リクエスト処理中)で同期的に行う。スレッド側に任せると、連打で二重に
    # 走ったり、開始直後のポーリングが候補検出フェーズの "done" を拾って空の結果を出したりする。
    job["phase"] = "apply"
    job["status"] = "queued"
    job["error"] = None
    job["files"] = {}
    job["leftover"] = None
    job["pages_done"] = 0
    job["pages_total"] = 0

    thread = threading.Thread(
        target=run_redact_apply_job,
        args=(
            job_id, boxes_per_page, dictionary, output_set,
            output_dir.strip() or None, name_template, lite.lower() == "true",
        ),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/redact/download/{job_id}/{fmt}")
async def api_redact_download(job_id: str, fmt: str):
    job = REDACT_JOBS.get(job_id)
    if not job or job["status"] != "done" or job["phase"] != "apply":
        raise HTTPException(404, "ファイルがまだありません")
    path = job["files"].get(fmt)
    if not path:
        raise HTTPException(404, f"この形式は生成されていません: {fmt}")
    return FileResponse(path, media_type=FMT_MEDIA.get(fmt), filename=Path(path).name)


def _ask_directory(title: str) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title=title) or ""
    finally:
        root.destroy()


@app.post("/api/pick-folder")
async def api_pick_folder(title: str = Form("フォルダを選択")):
    """サーバー(=このPC)側でOSネイティブのフォルダ選択ダイアログを開く。
    ローカル専用アプリなので、サーバーとクライアントは常に同じPCという前提。"""
    try:
        path = await run_in_threadpool(_ask_directory, title)
    except Exception as e:
        raise HTTPException(500, f"フォルダ選択ダイアログを開けませんでした: {e}")
    return {"path": path}  # 空文字はキャンセル


@app.post("/api/open-folder")
async def api_open_folder(path: str = Form(...)):
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(404, "フォルダが見つかりません")
    os.startfile(str(p))
    return {"ok": True}


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    _purge_jobs(None)  # 前回までの作業ファイル(墨消し前の画像など)を持ち越さない
    threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run(app, host=HOST, port=PORT)
