"""
redact.py — 墨消し（人名・住所などの自動候補検出＋手動確認→黒塗り適用）。

流れ:
  1) 元画像をレンダリング（yomitokuのload_pdf/load_imageを流用、OCRはまだしない）
  2) 軽量OCR（yomitoku）で行単位のテキスト＋座標を取得（候補検出専用。この結果は最終出力に使わない）
  3) 辞書（人名リスト）＋パターン（電話番号・郵便番号・日付など）で候補行を検出
  4) 人がレビューUIで候補を取捨選択・追加（app.py 側でHTML配信）
  5) 確定した矩形を「OCR前の画像」に黒塗りしてから、あらためてOCRし直して最終出力を作る
     → 墨消しした文字は最終PDF/MD/JSONのどこにも一切出てこない（テキスト乗せPDFの
       透明テキスト層にも、墨消し前の文字は入らない）
  6) 検証: 墨消し後の再OCR結果に辞書語・パターンがまだ残っていないか再チェックする
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw

from ocr_pipeline import get_analyzer, SUPPORT_INPUT_EXT

# 訴訟資料でよく出る旧字体・異体字の正規化。辞書語・OCR結果の双方に適用してから比較する。
# （網羅は狙わず、代表的なものだけ。取りこぼしは低信頼度フラグや目視検査でカバーする想定）
_VARIANTS = {
    "髙": "高", "﨑": "崎", "邊": "辺", "邉": "辺", "齋": "斎", "齊": "斎",
    "櫻": "桜", "德": "徳", "澤": "沢", "廣": "広", "眞": "真", "萬": "万",
    "國": "国", "當": "当", "藝": "芸", "數": "数", "壽": "寿", "龍": "竜",
    "彌": "弥", "祐": "祐", "邉": "辺", "槇": "槙", "渡邊": "渡辺", "渡邉": "渡辺",
}

# 辞書に無くても機械的に見つけられるもの（電話番号・郵便番号・生年月日など）
PATTERNS: dict[str, re.Pattern] = {
    "電話番号": re.compile(r"0\d{1,4}-?\d{1,4}-?\d{3,4}"),
    "郵便番号": re.compile(r"〒?\s*\d{3}-\d{4}"),
    "日付・生年月日": re.compile(
        r"(明治|大正|昭和|平成|令和)?\s*\d{1,4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?"
    ),
}

LOW_CONFIDENCE_THRESHOLD = 0.5
FUZZY_THRESHOLD = 0.82


def normalize(text: str) -> str:
    """全半角統一・異体字統一・空白除去。辞書語とOCR結果テキストの両方をこれで揃えてから比較する。"""
    text = unicodedata.normalize("NFKC", text or "")
    for src, dst in _VARIANTS.items():
        text = text.replace(src, dst)
    return re.sub(r"\s+", "", text)


@dataclass
class Candidate:
    page: int
    box: list  # [x0, y0, x1, y1]（元画像のピクセル座標）
    text: str  # 該当した行のOCR文字列（レビュー画面での確認用）
    reason: str  # "辞書: 山田太郎" / "パターン: 電話番号" / "低信頼度（要目視確認）"
    kind: str  # "dict" | "pattern" | "low_confidence"

    def to_dict(self) -> dict:
        return {"page": self.page, "box": self.box, "text": self.text,
                "reason": self.reason, "kind": self.kind}


def load_dictionary(text: str) -> list[str]:
    """1行1語（`/`で表記ゆれ・別名を列挙可、`#`以降はコメント）のテキストから検索語リストを作る。

    例:
        山田太郎 / 山田 / 太郎 / ヤマダタロウ
        株式会社〇〇products  # 会社名
    """
    terms = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0]
        for term in line.split("/"):
            term = term.strip()
            if term:
                terms.append(term)
    # 長い語から先に判定させたいので長さ降順（短い語が長い語の一部を横取りして
    # reasonの表示だけ変わる、程度の影響だが分かりやすさのため）
    return sorted(set(terms), key=len, reverse=True)


def render_pages(input_path: Path, dpi: int = 200):
    """PDF/画像を、yomitokuが受け取れるBGR numpy配列のリストにする（OCRはまだしない）。"""
    from yomitoku.data.functions import load_pdf, load_image

    ext = input_path.suffix[1:].lower()
    if ext not in SUPPORT_INPUT_EXT:
        raise ValueError(f"未対応の形式です: .{ext}")
    return load_pdf(input_path, dpi=dpi) if ext == "pdf" else load_image(input_path)


def save_page_images(imgs, out_dir: Path) -> list[Path]:
    """レビューUIで表示する用に、各ページをPNGとして保存する。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, img in enumerate(imgs):
        p = out_dir / f"page_{i + 1:03d}.png"
        Image.fromarray(img[:, :, ::-1]).save(p)  # BGR -> RGB
        paths.append(p)
    return paths


def load_page_images(paths: list[Path]):
    """save_page_imagesで保存したPNGを、yomitoku互換のBGR numpy配列に戻す。"""
    imgs = []
    for p in paths:
        pil = Image.open(p).convert("RGB")
        imgs.append(np.array(pil)[:, :, ::-1])  # RGB -> BGR
    return imgs


def ocr_lines(imgs, lite: bool = False, on_progress: Optional[Callable[[int, int], None]] = None):
    """候補検出専用の軽量OCR。各ページの行(word)単位のテキスト＋boxのみ返す（構造化はしない）。"""
    analyzer = get_analyzer(lite, "cpu")
    pages = []
    total = len(imgs)
    for i, img in enumerate(imgs):
        result, _ocr_vis, _layout_vis = analyzer(img)
        lines = []
        for w in result.words:
            xs = [pt[0] for pt in w.points]
            ys = [pt[1] for pt in w.points]
            lines.append({
                "box": [min(xs), min(ys), max(xs), max(ys)],
                "text": w.content,
                "rec_score": w.rec_score,
            })
        pages.append(lines)
        if on_progress:
            on_progress(i + 1, total)
    return pages


def _best_fuzzy_ratio(term: str, line: str) -> float:
    """lineの中でtermと同じ長さの部分文字列をスライドさせ、最も似ている箇所の類似度を返す。
    OCRの1〜2文字程度の誤認識（例: 「太郎」→「太朗」）を拾うための簡易あいまい一致。"""
    n = len(term)
    if len(line) < n:
        return SequenceMatcher(None, term, line).ratio()
    best = 0.0
    for i in range(len(line) - n + 1):
        ratio = SequenceMatcher(None, term, line[i:i + n]).ratio()
        if ratio > best:
            best = ratio
    return best


def find_candidates(pages_lines: list[list[dict]], terms: list[str]) -> list[Candidate]:
    """辞書＋パターンで墨消し候補行を検出する。低信頼度の行は別枠（要目視確認）で返す。"""
    candidates = []
    for page_no, lines in enumerate(pages_lines, start=1):
        for line in lines:
            norm_line = normalize(line["text"])
            if not norm_line:
                continue

            hit_reason = None
            hit_kind = None

            for term in terms:
                norm_term = normalize(term)
                if not norm_term:
                    continue
                if norm_term in norm_line:
                    hit_reason = f"辞書: {term}"
                    hit_kind = "dict"
                    break
                if len(norm_term) >= 3:
                    ratio = _best_fuzzy_ratio(norm_term, norm_line)
                    if ratio >= FUZZY_THRESHOLD:
                        hit_reason = f"辞書（あいまい一致 {ratio:.2f}）: {term}"
                        hit_kind = "dict"
                        break

            if not hit_reason:
                for label, pattern in PATTERNS.items():
                    if pattern.search(line["text"]):
                        hit_reason = f"パターン: {label}"
                        hit_kind = "pattern"
                        break

            if hit_reason:
                candidates.append(Candidate(
                    page=page_no, box=line["box"], text=line["text"],
                    reason=hit_reason, kind=hit_kind,
                ))
            elif line["rec_score"] < LOW_CONFIDENCE_THRESHOLD:
                candidates.append(Candidate(
                    page=page_no, box=line["box"], text=line["text"],
                    reason="低信頼度（要目視確認）", kind="low_confidence",
                ))
    return candidates


def burn_boxes(imgs, boxes_per_page: dict, padding: int = 4):
    """確定した矩形をページ画像に黒塗りして焼き込む。

    imgs: yomitoku形式のBGR numpy配列のリスト
    boxes_per_page: {page_no(int): [[x0,y0,x1,y1], ...]}
    戻り値: 黒塗り済みの同形式画像リスト（元のimgsは変更しない）
    """
    out = []
    for i, img in enumerate(imgs):
        page_no = i + 1
        pil = Image.fromarray(img[:, :, ::-1])  # BGR -> RGB
        draw = ImageDraw.Draw(pil)
        for box in boxes_per_page.get(page_no, []) or boxes_per_page.get(str(page_no), []):
            x0, y0, x1, y1 = box
            draw.rectangle(
                [x0 - padding, y0 - padding, x1 + padding, y1 + padding],
                fill=(0, 0, 0),
            )
        out.append(np.array(pil)[:, :, ::-1])  # RGB -> BGR
    return out


def verify_no_leftover(pages_lines: list[list[dict]], terms: list[str]) -> list[dict]:
    """墨消し後の再OCR結果に、辞書語・パターンがまだ残っていないか確認する
    （低信頼度フラグだけの行は誤検知が多いので除外する）。"""
    leftover = find_candidates(pages_lines, terms)
    return [c.to_dict() for c in leftover if c.kind in ("dict", "pattern")]
