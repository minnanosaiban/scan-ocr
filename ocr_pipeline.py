"""
ocr_pipeline.py — スキャンPDF/画像 → テキスト乗せPDF + Markdown + JSON。

OCR(YomiToku)は1ページにつき1回だけ実行し、その結果を3形式へ書き出す。
将来の拡張（墨消し・見出し目次・低信頼度フラグ等の "+α"）はここに足していく想定。
"""

import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from yomitoku.document_analyzer import DocumentAnalyzer
from yomitoku.data.functions import load_pdf, load_image
from yomitoku.export import convert_markdown
from yomitoku.utils.searchable_pdf import create_searchable_pdf

SUPPORT_INPUT_EXT = {"pdf", "jpg", "jpeg", "png", "bmp", "tiff", "tif"}
SUPPORT_OUTPUTS = {"pdf", "md", "json"}
JST = timezone(timedelta(hours=9))

# ファイル名の既定パターン。プレースホルダーは {stem}(元のファイル名) / {date}(実行日 YYYYMMDD)。
# 末尾に必ず何か付け足す既定にしておくことで、出力フォルダ=入力フォルダの時に元のスキャンPDFへ
# 誤って上書きするのを防ぐ（{stem}だけを指定した場合はprocess_document側で弾く）。
DEFAULT_NAME_TEMPLATE = "{stem}_ocr"
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def resolve_base_name(name_template: str, stem: str) -> str:
    """ファイル名パターンを実際の文字列へ展開する（拡張子は含まない）。"""
    template = (name_template or "").strip() or DEFAULT_NAME_TEMPLATE
    try:
        base = template.format(stem=stem, date=datetime.now(JST).strftime("%Y%m%d"))
    except (KeyError, IndexError) as e:
        raise ValueError(f"ファイル名の設定に使えないプレースホルダーです: {{{e}}}")
    base = _INVALID_FILENAME_CHARS.sub("_", base).strip()
    if not base:
        raise ValueError("ファイル名が空になります。設定を見直してください")
    return base


_analyzer_cache: dict = {}


def get_analyzer(lite: bool = False, device: str = "cpu") -> DocumentAnalyzer:
    """DocumentAnalyzerはモデル初期化が重いので (lite, device) ごとに使い回す。"""
    key = (lite, device)
    if key not in _analyzer_cache:
        configs: dict = {}
        if lite:
            configs = {"ocr": {"text_recognizer": {"model_name": "parseq-tiny"}}}
            if device == "cpu":
                configs["ocr"]["text_detector"] = {"infer_onnx": True}
        _analyzer_cache[key] = DocumentAnalyzer(configs=configs, visualize=False, device=device)
    return _analyzer_cache[key]


def process_document(
    input_path: Path,
    outdir: Path,
    dpi: int = 200,
    lite: bool = False,
    device: str = "cpu",
    outputs: Optional[set] = None,
    name_template: str = DEFAULT_NAME_TEMPLATE,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """1つの文書をOCRし、outdir に選ばれた形式だけを書き出す。

    outputs: 作る形式（{"pdf","md","json"}の部分集合）。Noneなら全部作る。
             OCR自体(analyzer呼び出し)は形式によらず1回だけ行い、選ばれなかった形式の
             変換・書き出し処理だけを省く。
    name_template: 出力ファイル名（拡張子抜き）のパターン。{stem}/{date}が使える。

    戻り値: {"pages": int, <選ばれた形式>: Path, ...}
    """
    ext = input_path.suffix[1:].lower()
    if ext not in SUPPORT_INPUT_EXT:
        raise ValueError(f"未対応の形式です: .{ext}")

    outputs = SUPPORT_OUTPUTS if outputs is None else (outputs & SUPPORT_OUTPUTS)
    if not outputs:
        raise ValueError("出力形式が1つも選ばれていません")

    base = resolve_base_name(name_template, input_path.stem)
    if "pdf" in outputs and (outdir / f"{base}.pdf").resolve() == input_path.resolve():
        raise ValueError(
            "出力ファイル名が元のファイルと同じで、上書きしてしまいます。"
            "ファイル名の設定を変えてください（既定: {stem}_ocr）"
        )

    imgs = load_pdf(input_path, dpi=dpi) if ext == "pdf" else load_image(input_path)
    analyzer = get_analyzer(lite, device)

    results = []
    pil_images = []
    md_parts = []
    json_pages = []

    total = len(imgs)
    for i, img in enumerate(imgs):
        page_no = i + 1
        result, _ocr_vis, _layout_vis = analyzer(img)

        if "pdf" in outputs:
            results.append(result)
            pil_images.append(Image.fromarray(img[:, :, ::-1]))

        if "md" in outputs:
            md, _elements = convert_markdown(result, out_path="", img=img, export_figure=False)
            md_parts.append(f"<!-- page {page_no} -->\n\n{md}")

        if "json" in outputs:
            json_pages.append({"page": page_no, **result.model_dump()})

        if on_progress:
            on_progress(page_no, total)

    outdir.mkdir(parents=True, exist_ok=True)
    out_paths: dict = {"pages": total}

    if "md" in outputs:
        md_path = outdir / f"{base}.md"
        md_path.write_text("\n\n---\n\n".join(md_parts), encoding="utf-8")
        out_paths["md"] = md_path

    if "json" in outputs:
        json_path = outdir / f"{base}.json"
        json_doc = {
            "source": input_path.name,
            "page_count": total,
            "generated_at": datetime.now(JST).isoformat(),
            "engine": "yomitoku",
            # ここに将来の "+α"（低信頼度フラグ・見出し目次・墨消しログ等）を追加していく。
            "pages": json_pages,
        }
        json_path.write_text(json.dumps(json_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        out_paths["json"] = json_path

    if "pdf" in outputs:
        pdf_path = outdir / f"{base}.pdf"
        create_searchable_pdf(pil_images, results, output_path=str(pdf_path))
        out_paths["pdf"] = pdf_path

    return out_paths
