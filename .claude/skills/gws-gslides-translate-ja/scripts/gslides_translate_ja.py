#!/usr/bin/env python3
"""既存の Google スライドをコピーして、日本語版を作る

gws-gslides-translate-ja スキルが plan → (翻訳) → check → create → thumbs → (fix → thumbs) → clean の順で使う。訳は Claude が書く。
翻訳元のスライドは読むだけで、決して変えない。変えるのは、create がドライブのコピーで作った翻訳版だけ。
翻訳版に書き込む前に、毎回、翻訳元と ID が違うことと、create が付けたしるし (appProperties) があることを確かめる。

使い方:
  gslides_translate_ja.py plan <URL> [--name <名前>] [--folder <フォルダ>]
      gws・認証・翻訳元・保存先を確かめて、作るファイルの名前・保存先と、翻訳する段落の数を表示する。
      翻訳する段落を .tmp/gws-gslides-translate-ja/texts.json に書く (Google Workspace は読むだけ)。
      前の作業のファイルが別のスライドか変わる前の翻訳元のものなら、訳を取り違えないように先に消す
  gslides_translate_ja.py check <URL> [--name <名前>] [--folder <フォルダ>]
      plan と同じ確認をして、.tmp/gws-gslides-translate-ja/translations.json の訳がそろっているかを確かめ、
      訳で文字があふれそうな枠と、1 行から折り返す段落を表示する (Google Workspace は読むだけ)
  gslides_translate_ja.py create <URL> [--name <名前>] [--folder <フォルダ>]
      check と同じ確認をしてから、翻訳元を保存先にコピーし (名前は yyyymmdd_<元の名前>_ja、--name があれば yyyymmdd_<名前>)、
      コピーの段落を訳に置き換える (訳が元と同じ段落は書き換えない)。訳で文字があふれそうな枠は、文字を小さくする (元の 70%・8pt まで)。
      見た目の同じ枠は、同じ割合にそろえる。翻訳版の ID を .tmp/gws-gslides-translate-ja/state.json に書く。
      最後の行に {"presentationId", "url", "folder", "folderUrl", "shrunk", "review", "pages"}
        shrunk: 文字を小さくした枠  review: 小さくしても、あふれそうな枠  pages: 画像で確かめるページ
  gslides_translate_ja.py thumbs [--pages 1,3-5 | --review] [--source]
      翻訳版 (--source なら翻訳元) のスライドの画像を .tmp/gws-gslides-translate-ja/thumb-<ja|src>-NN.png に書く。
      --source は plan の後から使える。--review は、create の pages だけにする
  gslides_translate_ja.py fix
      .tmp/gws-gslides-translate-ja/fixes.json の直し (訳の差し替え・文字の大きさ) を、翻訳版だけに適用する。
      書式は翻訳元から写すので、翻訳元が plan の後に変わっていたらやめる。
      直した枠が今の訳であふれそうなら [注意] を出す。最後の行に {"presentationId", "texts", "scale", "review"}
  gslides_translate_ja.py clean
      .tmp/gws-gslides-translate-ja/ の中のファイルをすべて消す

翻訳の単位は段落。書式の違う部分は ⟦1⟧…⟦/1⟧ で囲んで示し、訳でも同じ印で囲むと同じ書式になる (印の外は段落の主な書式)。
段落の中の改行は \\n で示す。段落の区切り・箇条書き・段落の書式は、元のまま残る。
訳の中の ¶ は段落の区切りで、元の段落と同じ書式・箇条書きの段落を足す。訳を null にすると、段落を消す
(枠の最後の段落は空にする)。足した・消した段落の数は state.json に書き、fix で段落を対応させるのに使う。
翻訳しないもの: 数字・記号だけの段落、かなを含む日本語だけの段落、ページ番号などの自動の文字を含む段落、
  画像・グラフに埋め込まれた文字、レイアウトとマスターの文字、スピーカーノート。

texts.json:       {"title", "sourceId", "slides": [{"slide": N, "boxes": [{"box": "b1", "where": "枠|表 R行C列", "paras": {"1": "<元の段落>"}}]}],
                   "marks": {"<段落の番号>": {"1": "<印の書式が主な書式とどう違うか (例: 太字・色が違う)>"}}}
translations.json: {"1": "<訳>", "2": null, ...}  texts.json のすべての段落の番号について書く
                   (訳さないものは元のまま。"" は段落を空にし、null は段落を消す。¶ で段落を足す)。
                   translations-1.json・translations-2.json … に分けてもよい
fixes.json:        {"texts": {"<段落の番号>": "<新しい訳>"}, "scale": {"<枠>": 0.9}}  texts は translations.json と同じ書き方
                   (消した段落は戻せない)。scale は今の文字の大きさに掛ける

終了コード: 10=gws が無い 20=未認証/認証切れ 30=翻訳元を使えない (URL でない・読めない・スライドでない・コピーできない)
  35=訳を使えない (plan の前・無い・足りない・印の誤り・texts.json が別のスライドのもの・翻訳元が plan の後に変わった) 40=保存先を使えない
  45=翻訳版を使えない (create の前・しるしが無い・翻訳元と同じ) 2=引数の誤り 1=そのほかの API の失敗

Google Workspace の操作はすべて gws CLI で行う。認証情報はこのスクリプトでは扱わない (gws が管理する)。
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

SKILL = "gws-gslides-translate-ja"
SKILL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REPO_ROOT = os.path.normpath(os.path.join(SKILL_DIR, "..", "..", ".."))
WORK_DIR = os.path.join(REPO_ROOT, ".tmp", SKILL)  # 作業のファイル (Git 管理対象外。clean で消す)
WORK_REL = os.path.relpath(WORK_DIR, REPO_ROOT) + "/"
TEXTS, TRANSLATIONS, FIXES, STATE = "texts.json", "translations.json", "fixes.json", "state.json"
TRANSLATIONS_RE = re.compile(r"translations(-\d+)?\.json")  # translations.json か、分けた translations-1.json など
OUTPUT_FOLDERS = ["gws-skills", SKILL]  # 既定の保存先 (マイドライブからのパス)
DEFAULT_FOLDER = "マイドライブ/" + "/".join(OUTPUT_FOLDERS)
FOLDER_MIME = "application/vnd.google-apps.folder"
SLIDES_MIME = "application/vnd.google-apps.presentation"
MARK_SKILL, MARK_SOURCE = "gwsSkill", "translatedFrom"  # 翻訳版に付けるしるし (appProperties)
MAX_BODY = 500_000  # バイト / 1 回の batchUpdate の JSON の大きさ (コマンドの引数で渡すため)

# 文字のあふれの見積もり。描画の正確な寸法は API で得られないので、元の文と訳を同じ式で見積もって比べる
EMU_PER_PT = 12700
DEFAULT_SIZE = 18  # PT / 継承元に大きさが無いときの文字の大きさ (Slides の既定)
CELL_SIZE = 14  # PT / 表のセルで大きさが無いときの文字の大きさ (Slides の既定)
INSET_X, INSET_Y = 7.2, 7.2  # PT / 枠の内側の片側の余白 (Slides の既定の 0.1 インチ)
LINE_HEIGHT = 1.2  # 行の高さ / 文字の大きさ
MIN_SCALE = 0.7  # 自動で小さくするのは元の 70% まで
MIN_PT = 8  # PT / 自動で小さくするのはこの大きさまで (元から小さい文字は小さくしない)
SCALE_STEP = 0.05
TIGHT = 0.9  # 幅をこの割合にすると行が増える訳は、折り返しの手前 (見積もりの誤差で折り返しうる) として画像で見る
THUMB_WORKERS = 4  # スライドの画像を同時に取る数 (画像の作成は API の上限が低いので、多くしない)
MARK_RE = re.compile(r"⟦(\d+)⟧(.*?)⟦/\1⟧", re.S)
PARA_MARK = "¶"  # 訳の中の段落の区切り (段落を足すとき)
TEXT_FIELDS = ("bold,italic,underline,strikethrough,smallCaps,baselineOffset,fontFamily,weightedFontFamily,"
               "fontSize,foregroundColor,backgroundColor,link")
URL_RE = re.compile(r"/presentation/d/([\w-]+)")


class GwsError(Exception):
    pass


class Fail(Exception):
    """[NG] で終える。(終了コード, 内容, 案内)"""


# --------------------------------------------------------------------------- gws

def gws(*args, params=None, body=None):
    cmd = ["gws", *args]
    if params is not None:
        cmd += ["--params", json.dumps(params, ensure_ascii=False)]
    if body is not None:
        cmd += ["--json", json.dumps(body, ensure_ascii=False)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise GwsError(f"gws {' '.join(args)} が失敗しました:\n{res.stderr.strip()}\n{res.stdout.strip()}")
    return json.loads(res.stdout) if res.stdout.strip() else {}


def require_auth():
    """gws があり認証済みかを確かめて、アカウントを返す。"""
    if not shutil.which("gws"):
        raise Fail(10, "gws がありません", "Google Workspace CLI (gws) をインストールしてもらってください")
    try:
        about = gws("drive", "about", "get", params={"fields": "user(emailAddress)"})
    except GwsError as e:
        raise Fail(20, f"gws が認証されていません\n{e}",
                   "ユーザーに `! gws auth login` の実行を依頼し、済んだらやり直してください")
    return about.get("user", {}).get("emailAddress", "?")


INHERITED_FIELDS = "pageElements(objectId,size,transform,shape(placeholder(parentObjectId),text(textElements(textRun(style(fontSize))))))"
PRESENTATION_FIELDS = (  # スライドの全体を取ると大きい (レイアウト・マスターが大半) ので、使う項目だけを取る
    "slides(pageElements(objectId,size,transform,shape,table,elementGroup)),"
    f"layouts({INHERITED_FIELDS}),masters({INHERITED_FIELDS})")


def get_presentation(pid):
    """スライドの文字の枠と、プレースホルダーの継承元 (レイアウト・マスターの大きさと文字の大きさ) を読む。"""
    return gws("slides", "presentations", "get", params={"presentationId": pid, "fields": PRESENTATION_FIELDS})


def check_target(state):
    """翻訳版に書いてよいかを確かめる。翻訳元と同じ ID や、create が付けたしるしの無いファイルには書かない。"""
    copy_id, source_id = state.get("copyId"), state.get("sourceId")
    if not copy_id or not source_id or copy_id == source_id:
        raise Fail(45, "翻訳版の ID が翻訳元と同じか、ありません。書き込みをやめました", "create からやり直してください")
    try:
        meta = gws("drive", "files", "get", params={"fileId": copy_id, "supportsAllDrives": True,
                                                     "fields": "appProperties,trashed,capabilities(canEdit)"})
    except GwsError as e:
        raise Fail(45, f"翻訳版を読めません\n{e}", "create からやり直してください")
    marks = meta.get("appProperties", {})
    if marks.get(MARK_SKILL) != SKILL or marks.get(MARK_SOURCE) != source_id:
        raise Fail(45, "翻訳版に create のしるしがありません。書き込みをやめました", "create からやり直してください")
    if meta.get("trashed") or not meta.get("capabilities", {}).get("canEdit"):
        raise Fail(45, "翻訳版がゴミ箱にあるか、編集できません", "create からやり直してください")
    return copy_id


def write_copy(state, requests):
    """翻訳版だけに batchUpdate する。リクエストは先頭から順に適用されるので、分けて送っても結果は変わらない。"""
    pid = check_target(state)
    chunk, size = [], 0
    for r in requests:
        n = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
        if chunk and size + n > MAX_BODY:
            gws("slides", "presentations", "batchUpdate", params={"presentationId": pid}, body={"requests": chunk})
            chunk, size = [], 0
        chunk.append(r)
        size += n
    if chunk:
        gws("slides", "presentations", "batchUpdate", params={"presentationId": pid}, body={"requests": chunk})


# ------------------------------------------------------------------ work files

def work_path(name):
    return os.path.join(WORK_DIR, name)


def write_work(name, data):
    """自分だけが読めるように書く (ディレクトリ 0700・ファイル 0600。リンクはたどらない)。data は文字列かバイト列。"""
    os.makedirs(WORK_DIR, mode=0o700, exist_ok=True)
    os.chmod(WORK_DIR, 0o700)
    fd = os.open(work_path(name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with open(fd, "wb") as f:
        f.write(data.encode("utf-8") if isinstance(data, str) else data)


def save_state(state):
    write_work(STATE, json.dumps(state, ensure_ascii=False) + "\n")


def read_json(name, code, hint):
    try:
        with open(work_path(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise Fail(code, f"{WORK_REL}{name} がありません", hint)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise Fail(code, f"{WORK_REL}{name} を JSON として読めません: {e}", f"{name} を書き直してください")


# ------------------------------------------------------------------ extraction

def u16(s):
    """Slides API のインデックスは UTF-16 コード単位。"""
    return len(s.encode("utf-16-le")) // 2


def size_pt(dim):
    if not dim or "magnitude" not in dim:
        return None
    return dim["magnitude"] / EMU_PER_PT if dim.get("unit", "EMU") == "EMU" else dim["magnitude"]


def is_japanese_only(text):
    """かなを含み、文字がかな・漢字だけなら日本語とみなす (翻訳しない)。"""
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    kana = any("぀" <= c <= "ヿ" or "ｦ" <= c <= "ﾟ" for c in letters)
    return kana and all(is_wide(c) for c in letters)


def is_wide(ch):
    return unicodedata.east_asian_width(ch) in "WF"


def style_key(style):
    return json.dumps(style or {}, sort_keys=True)


def paragraphs(text):
    """text.textElements を段落の列にする。段落は dict(start, end, runs=[(start, content, style)], style, auto)。"""
    out, cur = [], None
    for el in text.get("textElements", []):
        if "paragraphMarker" in el:
            cur = {"start": el.get("startIndex", 0), "end": el["endIndex"], "runs": [],
                   "style": el["paragraphMarker"].get("style", {}), "auto": False,
                   "bullet": el["paragraphMarker"].get("bullet")}
            out.append(cur)
        elif cur is not None and "textRun" in el:
            cur["runs"].append((el.get("startIndex", 0), el["textRun"].get("content", ""), el["textRun"].get("style", {})))
        elif cur is not None and "autoText" in el:
            cur["auto"] = True
    return out


def segments(para):
    """段落の文字 (末尾の改行を除く) を、書式の同じ連続した部分 [(content, style)] にまとめる。空白だけの部分は主な書式にする。"""
    runs = []
    for _, content, style in para["runs"]:
        content = content.rstrip("\n")
        if content:
            runs.append([content, style])
    weight = {}
    for content, style in runs:
        if content.strip():
            weight[style_key(style)] = weight.get(style_key(style), 0) + len(content)
    base_key = max(weight, key=weight.get) if weight else style_key(runs[0][1] if runs else {})
    base = next((s for _, s in runs if style_key(s) == base_key), {})
    merged = []
    for content, style in runs:
        if not content.strip():
            style = base
        if merged and style_key(merged[-1][1]) == style_key(style):
            merged[-1][0] += content
        else:
            merged.append([content, style])
    return [tuple(m) for m in merged], base


def marked_text(segs, base):
    """書式の違う部分を ⟦n⟧…⟦/n⟧ で囲んだ文。段落の中の改行 (\\v) は \\n で示す。styles: n -> 書式。"""
    styles, keys, out = {}, {}, ""
    for content, style in segs:
        content = content.replace("\v", "\n")
        k = style_key(style)
        if k == style_key(base):
            out += content
            continue
        if k not in keys:
            keys[k] = str(len(keys) + 1)
            styles[keys[k]] = style
        n = keys[k]
        out += f"⟦{n}⟧{content}⟦/{n}⟧"
    return out, styles


def transform_scale(el):
    t = el.get("transform", {})
    return abs(t.get("scaleX", 1)), abs(t.get("scaleY", 1))


def walk(elements, path=(), scale=(1, 1)):
    """要素をグループの中までたどる。scale: 親のグループの拡大率 (グループの中の要素の transform はグループからの相対値)。"""
    for i, el in enumerate(elements):
        yield path + (i,), el, scale
        if "elementGroup" in el:
            sx, sy = transform_scale(el)
            yield from walk(el["elementGroup"].get("children", []), path + (i,), (scale[0] * sx, scale[1] * sy))


def inherited(index, el, key):
    """プレースホルダーの継承元を順にたどって、key(el) が None でない最初の値。index: objectId -> 要素。"""
    seen = set()
    while el is not None and el.get("objectId") not in seen:
        seen.add(el.get("objectId"))
        v = key(el)
        if v is not None:
            return v
        el = index.get(el.get("shape", {}).get("placeholder", {}).get("parentObjectId"))
    return None


def first_font_size(el):
    for t in el.get("shape", {}).get("text", {}).get("textElements", []):
        size = size_pt(t.get("textRun", {}).get("style", {}).get("fontSize"))
        if size:
            return size
    return None


def geometry(el, scale=(1, 1)):
    """要素の幅と高さ (PT)。scale: 親のグループの拡大率。"""
    w, h = size_pt(el.get("size", {}).get("width")), size_pt(el.get("size", {}).get("height"))
    if not w or not h:
        return None
    sx, sy = transform_scale(el)
    return w * sx * scale[0], h * sy * scale[1]


def extract(pres):
    """翻訳の対象を集める。boxes: スライドの文字の枠 (図形・表のセル) の列。枠は段落 (paras) を持つ。
    スピーカーノートは訳さない (原文のまま残す) ので集めない。

    段落の番号と枠の番号は、同じスライドからは同じに決まる (翻訳版はコピーなので、翻訳元と同じ番号になる)。
    """
    boxes, para_seq = [], 0
    index = {el.get("objectId"): el for page in pres.get("slides", []) + pres.get("layouts", []) + pres.get("masters", [])
             for _, el, _ in walk(page.get("pageElements", []))}
    for si, slide in enumerate(pres.get("slides", [])):
        for path, el, scale in walk(slide.get("pageElements", [])):
            # プレースホルダーの大きさ・文字の大きさが無いときは、継承元 (レイアウト・マスター) のものを使う
            parent = index.get(el.get("shape", {}).get("placeholder", {}).get("parentObjectId"))
            containers = []
            if "shape" in el and "text" in el["shape"]:
                containers.append((None, el["shape"]["text"], geometry(el, scale) or inherited(index, parent, geometry)))
            elif "table" in el:
                t = el["table"]
                sx, sy = transform_scale(el)
                sx, sy = sx * scale[0], sy * scale[1]
                cols = [size_pt(c.get("columnWidth")) or 0 for c in t.get("tableColumns", [])]
                rows = [size_pt(r.get("rowHeight")) or 0 for r in t.get("tableRows", [])]
                for r, row in enumerate(t.get("tableRows", [])):
                    for c, cell in enumerate(row.get("tableCells", [])):
                        if "text" not in cell:
                            continue
                        loc = cell.get("location", {"rowIndex": r, "columnIndex": c})
                        ri, ci = loc.get("rowIndex", 0), loc.get("columnIndex", 0)
                        # 結合したセルは、結合した列の幅・行の高さの合計
                        w = sum(cols[ci:ci + cell.get("columnSpan", 1)]) * sx
                        h = sum(rows[ri:ri + cell.get("rowSpan", 1)]) * sy
                        containers.append(({"rowIndex": ri, "columnIndex": ci}, cell["text"], (w, h) if w and h else None))
            autofit = el.get("shape", {}).get("shapeProperties", {}).get("autofit", {})
            auto_scale = autofit.get("fontScale", 1) if autofit.get("autofitType") == "TEXT_AUTOFIT" else 1
            base_size = (CELL_SIZE if "table" in el else inherited(index, parent, first_font_size)) or DEFAULT_SIZE
            for cell, text, geo in containers:
                box = {"box": f"b{len(boxes) + 1}", "slide": si + 1,
                       "where": f"表 {cell['rowIndex'] + 1}行{cell['columnIndex'] + 1}列" if cell else "枠",
                       "objectId": el["objectId"], "path": [si, *path], "cell": cell, "geo": geo,
                       "autoScale": auto_scale, "baseSize": base_size, "paras": []}
                for pi, para in enumerate(paragraphs(text)):
                    segs, base = segments(para)
                    plain = "".join(c for c, _ in segs)
                    entry = {"index": pi, "para": para, "segs": segs, "base": base, "id": None}
                    if (not para["auto"] and any(unicodedata.category(c).startswith("L") for c in plain)
                            and not is_japanese_only(plain)):
                        para_seq += 1
                        entry["id"] = str(para_seq)
                        entry["marked"], entry["styles"] = marked_text(segs, base)
                    box["paras"].append(entry)
                boxes.append(box)
    return boxes


def align(src_box, dst_box, counts):
    """翻訳元の段落ごとに、翻訳版の段落の列を対応させる。合わなければ None。

    counts: 段落の番号 -> 翻訳版での段落の数 (訳で ¶ で分けた・null で消した段落だけ。ほかは 1)。
    """
    out, i = [], 0
    for e in src_box["paras"]:
        k = counts.get(e["id"], 1) if e["id"] else 1
        out.append((e, dst_box["paras"][i:i + k]))
        i += k
    return out if i == len(dst_box["paras"]) else None


def same_layout(src_boxes, dst_boxes, counts):
    if [(b["path"], b["cell"]) for b in src_boxes] != [(b["path"], b["cell"]) for b in dst_boxes]:
        return False
    return all(align(s, d, counts) is not None for s, d in zip(src_boxes, dst_boxes))


# ------------------------------------------------------------------ estimation

def char_width(c):
    """文字の幅 / 文字の大きさ (おおよそ)。描画した画像で測った値 (フォントによって ±5% ほど違う)。"""
    if is_wide(c):
        return 0.95
    if c == " ":
        return 0.25
    if c.isupper() or c.isdigit():
        return 0.62
    if c.isalpha():
        return 0.48
    return 0.3


def count_lines(text, avail, size):
    """幅 avail に、大きさ size の文字で text を並べたときの行数。ラテン文字は単語で、全角の文字は 1 字ずつ折り返す。"""
    lines = 0
    for part in text.split("\n"):
        tokens = re.findall(r"[^\s　-鿿＀-￯]+\s*|\s+|.", part)
        n, x = 1, 0.0
        for tok in tokens:
            w = sum(char_width(c) for c in tok) * size
            if x > 0 and x + w > avail:
                n += 1
                x = 0.0
            if w > avail:  # 1 行に収まらない長い語 (URL など) は、その幅で折り返す
                n += int(w // avail)
                w %= avail
            x += w
        lines += n
    return lines


def run_size(style, box):
    return size_pt(style.get("fontSize")) or box["baseSize"]


def text_width(box, style, size):
    """段落の文字を並べられる幅 (PT)。枠の幅から内側の余白と字下げを引く。"""
    return max(box["geo"][0] - INSET_X * 2 - (size_pt(style.get("indentStart")) or 0), size)


def estimate(box, texts, scale):
    """枠の文字の高さ (PT)。texts: 段落ごとの (文, 文字の大きさ)。文が None (消す段落) は数えず、¶ で分けた文は別の段落として数える。"""
    total = INSET_Y * 2
    for entry, (text, size) in zip(box["paras"], texts):
        if text is None:
            continue
        st = entry["para"]["style"]
        size *= scale
        avail = text_width(box, st, size)
        line = size * LINE_HEIGHT * st.get("lineSpacing", 100) / 100
        for part in text.split(PARA_MARK):
            total += count_lines(part, avail, size) * line
            total += (size_pt(st.get("spaceAbove")) or 0) + (size_pt(st.get("spaceBelow")) or 0)
    return total


def para_size(entry, box):
    sizes = [run_size(s, box) for c, s in entry["segs"] if c.strip()]
    return (max(sizes) if sizes else box["baseSize"]) * box["autoScale"]


def fit(boxes, translations):
    """訳があふれないように、枠ごとに文字を小さくする割合を決める。

    元の文が枠に収まっていれば枠の高さまで、元からあふれていれば元の文の高さまでを許す (表のセルは、同じ行でいちばん高いセルまで)。
    小さくするのは元の MIN_SCALE 倍・MIN_PT までで、それでもあふれそうなら over にする。
    見た目をそろえるため、次の枠は同じ割合 (いちばん小さいもの) にする:
      同じ表のセル / 同じスライドで見た目が同じ枠 / 寸法と見た目が同じ枠 (別のスライドでも)
    返り値: 枠の番号 -> {"box", "scale", "own" (そろえる前の割合), "floor" (小さくできる下限), "over",
      "grown": [(段落の番号, 元の行数, 訳の行数)], "tight": 折り返しの手前の段落, "added": ¶ で段落を足した段落,
      "removed": 消す段落}  寸法のわからない枠は含まない
    """
    measured = []  # (枠, 元の文, 元の文の高さ)。段落の中の改行は、元の文では \v、訳では \n。どちらも \n にそろえて数える
    for box in boxes:
        if box["geo"]:
            src = [("".join(c for c, _ in e["segs"]).replace("\v", "\n"), para_size(e, box)) for e in box["paras"]]
            measured.append((box, src, estimate(box, src, 1)))

    def row_of(box):
        return box["objectId"], box["cell"]["rowIndex"]

    # 表の行は、いちばん高いセルに合わせて広がるので、セルは同じ行でいちばん高い元の文の高さまで使える
    row_height = {}
    for box, _, height in measured:
        if box["cell"]:
            row_height[row_of(box)] = max(row_height.get(row_of(box), 0), height)

    info = {}
    for box, src, src_height in measured:
        dst = [(plain_of(translations[e["id"]]) if e["id"] in translations else t, s)
               for e, (t, s) in zip(box["paras"], src)]  # 消す段落は None
        sizes = [s for _, s in src] or [box["baseSize"]]
        floor = max(MIN_SCALE, min(1.0, MIN_PT / min(sizes)))
        allowed = max(box["geo"][1], src_height, row_height[row_of(box)] if box["cell"] else 0)
        scale = 1.0
        while round(scale - SCALE_STEP, 2) >= floor - 1e-9 and estimate(box, dst, scale) > allowed:
            scale = round(scale - SCALE_STEP, 2)
        grown, tight = [], []
        for e, (ts, s), (td, _) in zip(box["paras"], src, dst):
            if td is None or not e["id"] or td == ts:
                continue
            width = text_width(box, e["para"]["style"], s)
            # 元の文は今の見た目で収まっているので、見積もりの誤差を見込んで少なめに数える (枠を 1 割広く見る)。
            # ¶ で分けた訳は、分けたそれぞれの段落が折り返すかを見る
            ls = count_lines(ts, width / TIGHT, s)
            ld = max(count_lines(p, width, s) for p in td.split(PARA_MARK))
            if ld > ls:
                grown.append((e["id"], ls, ld))
            elif max(count_lines(p, width * TIGHT, s) for p in td.split(PARA_MARK)) > ls:
                tight.append(e["id"])  # 見積もりの誤差で、実際には折り返しているかもしれない
        added = [e["id"] for e, (td, _) in zip(box["paras"], dst) if td and PARA_MARK in td]
        removed = [e["id"] for e, (td, _) in zip(box["paras"], dst) if e["id"] and td is None]
        # 見た目: 文字の大きさの組と、最初の段落の書式 (リンクは除く)
        base = {k: v for k, v in (box["paras"][0]["base"] if box["paras"] else {}).items() if k != "link"}
        look = tuple(sorted({round(s, 1) for s in sizes})), style_key(base)
        info[box["box"]] = {"box": box, "scale": scale, "own": scale, "floor": floor, "look": look,
                            "dst": dst, "allowed": allowed, "grown": grown, "tight": tight,
                            "added": added, "removed": removed}

    # 見た目をそろえる枠をまとめる (union-find)
    parent = {k: k for k in info}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    first = {}
    for k, v in info.items():
        b, look = v["box"], v["look"]
        w, h = b["geo"]
        # 同じ要素 (表のセル) / 同じスライドで見た目が同じ / 寸法と見た目が同じ
        for key in (("element", b["objectId"]), ("slide", b["slide"], look), ("geo", round(w), round(h), look)):
            if key in first:
                parent[root(k)] = root(first[key])
            else:
                first[key] = k
    least = {}
    for k, v in info.items():
        r = root(k)
        least[r] = min(least.get(r, 1.0), v["own"])
    for k, v in info.items():
        v["scale"] = max(least[root(k)], v["floor"])
        v["over"] = estimate(v["box"], v["dst"], v["scale"]) > v["allowed"]  # そろえた後の割合で見る
    return info


# --------------------------------------------------------------- translations

def parse_marked(text, styles, base):
    """訳を [(文, 書式)] にする。印の外は主な書式。段落の中の改行は \\v、段落の区切り (¶) は \\n にする。印の誤りは ValueError。"""
    out, pos = [], 0
    for m in MARK_RE.finditer(text):
        if m.group(1) not in styles:
            raise ValueError(f"元に無い印 ⟦{m.group(1)}⟧ があります")
        if PARA_MARK in m.group(2):
            raise ValueError(f"印 ⟦{m.group(1)}⟧ の中に段落の区切り {PARA_MARK} があります")
        out.append((text[pos:m.start()], base))
        out.append((m.group(2), styles[m.group(1)]))
        pos = m.end()
    out.append((text[pos:], base))
    out = [(t.replace("\n", "\v").replace(PARA_MARK, "\n"), s) for t, s in out if t]
    if any("⟦" in t or "⟧" in t for t, _ in out):
        raise ValueError("閉じていない印 ⟦ ⟧ があります")
    return out


def plain_of(text):
    return None if text is None else MARK_RE.sub(lambda m: m.group(2), text)


def num_key(k):
    """段落の番号 ("2" < "10") や訳のファイル名 (translations-2 < translations-10) を、数の順に並べる鍵。"""
    return len(k), k


def read_translations(code):
    """translations.json (大きなデッキでは translations-1.json などに分けてよい) をまとめて読む。"""
    names = sorted((n for n in os.listdir(WORK_DIR) if TRANSLATIONS_RE.fullmatch(n)), key=num_key) \
        if os.path.isdir(WORK_DIR) else []
    if not names:
        raise Fail(code, f"{WORK_REL}{TRANSLATIONS} がありません", "texts.json の段落を訳して書いてください")
    merged = {}
    for n in names:
        data = read_json(n, code, "")
        if not isinstance(data, dict):
            raise Fail(code, f"{n} の形が違います", '{"<段落の番号>": "<訳>"} の形にしてください')
        dup = sorted(k for k in data if k in merged and merged[k] != data[k])
        if dup:
            raise Fail(code, f"{n} に、前のファイルと訳の違う段落があります: {', '.join(dup[:20])}",
                       "同じ段落の訳は 1 つにしてください")
        merged.update(data)
    return merged, names


def validate_translations(boxes, data, name, code, require_all):
    """訳を確かめる。訳は文字列か null (段落を消す)。require_all なら、すべての段落の訳を求める。"""
    if not isinstance(data, dict) or not all(v is None or isinstance(v, str) for v in data.values()):
        raise Fail(code, f"{name} の形が違います", '{"<段落の番号>": "<訳>" か null} の形にしてください')
    entries = {e["id"]: e for b in boxes for e in b["paras"] if e["id"]}
    unknown = sorted(set(data) - set(entries), key=num_key)
    if unknown:
        raise Fail(code, f"{name} に texts.json に無い段落の番号があります: {', '.join(unknown[:20])}",
                   "plan からやり直したなら、texts.json の番号で書き直してください")
    if require_all:
        missing = sorted(set(entries) - set(data), key=num_key)
        if missing:
            raise Fail(code, f"{name} に訳の無い段落があります ({len(missing)} 個): {', '.join(missing[:20])}",
                       "すべての段落の訳を書いてください (訳さないものは元のまま書く)")
    errors = []
    for k, v in data.items():
        if v is None:
            continue
        e = entries[k]
        try:
            parse_marked(v, e["styles"], e["base"])
        except ValueError as ex:
            errors.append(f"{k}: {ex}")
    if errors:
        raise Fail(code, f"{name} の訳に誤りがあります:\n  " + "\n  ".join(errors[:20]),
                   f"印は texts.json の段落にある ⟦n⟧…⟦/n⟧ だけを、開きと閉じをそろえて使い、{PARA_MARK} は印の外に置いてください")
    return data


# --------------------------------------------------------------------- writing

def location(box):
    loc = {"objectId": box["objectId"]}
    if box["cell"]:
        loc["cellLocation"] = box["cell"]
    return loc


def text_range(s, e):
    return {"type": "FIXED_RANGE", "startIndex": s, "endIndex": e}


def with_size(style, size):
    st = dict(style)
    if size is not None:
        st["fontSize"] = {"magnitude": round(size, 1), "unit": "PT"}
    return st


def split_spacing(src_box, src, loc, start, text, regrouped):
    """¶ で分けた段落の前後の間隔を直すリクエスト。

    分けた段落は、どれも元の段落の書式 (前後の間隔を含む) を受け継ぐ。元の段落がリストの最後の項目で下の間隔が広いと、
    分けた段落の間にもその間隔が入ってしまうので、分けた段落の間は、同じ枠のほかの段落 (箇条書きかどうかが同じもの) の間隔にする。
    最初の段落の上と最後の段落の下は、元の段落の間隔にする (前に分けた段落をまとめ直したときも、元に戻す)。
    regrouped: 置き換える前の翻訳版で、この段落が 2 つ以上に分かれていたか。
    """
    if "\n" not in text and not regrouped:
        return []
    others = [e for e in src_box["paras"] if e is not src and bool(e["para"]["bullet"]) == bool(src["para"]["bullet"])]
    sib = min(others, key=lambda e: abs(e["index"] - src["index"]))["para"]["style"] if others else {}
    own = src["para"]["style"]
    reqs, pos, pieces = [], start, text.split("\n")
    for i, piece in enumerate(pieces):
        end = pos + u16(piece) + 1
        style = {}
        for key, from_ in (("spaceAbove", own if i == 0 else sib), ("spaceBelow", own if i == len(pieces) - 1 else sib)):
            if from_.get(key):
                style[key] = from_[key]
        reqs.append({"updateParagraphStyle": {**loc, "textRange": text_range(pos, end), "style": style,
                                              "fields": "spaceAbove,spaceBelow"}})
        pos = end
    return reqs


def replace_requests(src_box, dst_box, texts, scale_of, counts):
    """dst_box (翻訳版の今の枠) の段落を texts (段落の番号 -> 訳) に置き換えるリクエスト。

    訳の ¶ は段落の区切りで、足した段落は元の段落と同じ段落の書式・箇条書きになる。訳が null なら段落を消す
    (枠の最後の段落は、末尾の改行を消せないので空にする)。counts: 今の翻訳版での段落の数 (align を参照)。
    書式は翻訳元 (src_box) の段落から写す。scale_of(src_entry, dst_entries): 文字の大きさに掛ける割合 (None なら書式のまま)。
    後ろの段落から処理して、前の段落のインデックスが変わらないようにする。
    返り値: (リクエスト, {段落の番号: 置き換えた後の段落の数})
    """
    reqs, loc, new_counts = [], location(dst_box), {}
    last = dst_box["paras"][-1] if dst_box["paras"] else None
    for src, dsts in reversed(align(src_box, dst_box, counts)):
        scale = scale_of(src, dsts)
        if src["id"] in texts:
            text = texts[src["id"]]
            if not dsts:  # 前に消した段落 (戻す訳は、fix が先に止める)
                new_counts[src["id"]] = 0
                continue
            s = dsts[0]["para"]["start"]
            e = dsts[-1]["para"]["end"] - 1  # 段落の末尾の改行は残す (段落の書式と箇条書きが残る)
            if text is None:
                if dsts[-1] is last:
                    if e > s:
                        reqs.append({"deleteText": {**loc, "textRange": text_range(s, e)}})
                    new_counts[src["id"]] = 1
                else:
                    reqs.append({"deleteText": {**loc, "textRange": text_range(s, e + 1)}})
                    new_counts[src["id"]] = 0
                continue
            parts = parse_marked(text, src["styles"], src["base"])
            if e > s:
                reqs.append({"deleteText": {**loc, "textRange": text_range(s, e)}})
            if parts:  # 訳が空なら、段落を空にする
                reqs.append({"insertText": {**loc, "insertionIndex": s, "text": "".join(t for t, _ in parts)}})
            pos = s
            for t, style in parts:
                size = run_size(style, src_box) * scale if scale is not None else None
                for piece in t.split("\n"):  # 段落の区切りの改行には書式 (リンクなど) を付けない
                    if piece:
                        reqs.append({"updateTextStyle": {**loc, "textRange": text_range(pos, pos + u16(piece)),
                                                         "style": with_size(style, size), "fields": TEXT_FIELDS}})
                    pos += u16(piece) + 1
                pos -= 1
            reqs += split_spacing(src_box, src, loc, s, "".join(t for t, _ in parts), len(dsts) > 1)
            new_counts[src["id"]] = text.count(PARA_MARK) + 1
        elif scale is not None:  # 訳さない段落も、同じ枠なら同じ割合で小さくする
            reqs += resize_requests(dst_box, dsts, scale)
    return reqs, new_counts


def resize_requests(box, paras, factor):
    """枠 box の段落 paras の文字の大きさに、今の大きさに対して factor を掛けるリクエスト。"""
    loc, reqs = location(box), []
    for p in paras:
        for start, content, style in p["para"]["runs"]:
            n = u16(content.rstrip("\n"))
            if n:
                reqs.append({"updateTextStyle": {**loc, "textRange": text_range(start, start + n),
                                                 "style": with_size({}, run_size(style, box) * factor),
                                                 "fields": "fontSize"}})
    return reqs


# ------------------------------------------------------------------------ main

def source_id_of(url):
    m = URL_RE.search(url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[\w-]{20,}", url):
        return url
    raise Fail(30, f"Google スライドの URL ではありません: {url}",
               "https://docs.google.com/presentation/d/<ID>/... の形の URL をもらってください")


def check_source(sid):
    hint = "URL が正しいか、自分のアカウントで開けるかを、ユーザーに確かめてもらってください"
    try:
        meta = gws("drive", "files", "get", params={"fileId": sid, "supportsAllDrives": True,
                                                     "fields": "id,name,mimeType,capabilities(canCopy)"})
    except GwsError as e:
        raise Fail(30, f"翻訳元 ({sid}) を読めません\n{e}", hint)
    if meta.get("mimeType") != SLIDES_MIME:
        raise Fail(30, f"翻訳元 ({meta.get('name')}) が Google スライドではありません", hint)
    if not meta.get("capabilities", {}).get("canCopy"):
        raise Fail(30, f"翻訳元 ({meta.get('name')}) はコピーが禁じられています", "コピーできるようにしてもらうか、別のスライドにしてください")
    return meta


def folder_url(fid):
    return f"https://drive.google.com/drive/folders/{fid}"


def check_folder(value):
    """--folder で指定したフォルダの (ID, 名前)。使えなければ Fail (40)。"""
    m = re.search(r"/folders/([\w-]+)", value)
    fid = m.group(1) if m else value
    hint = "内容をユーザーに伝え、別の保存先 (フォルダの URL) を聞くか、--folder を外して既定の保存先にしてください"
    try:
        meta = gws("drive", "files", "get", params={"fileId": fid, "supportsAllDrives": True,
                                                     "fields": "name,mimeType,capabilities(canAddChildren)"})
    except GwsError as e:
        raise Fail(40, f"保存先 ({fid}) を読めません\n{e}", hint)
    if meta.get("mimeType") != FOLDER_MIME:
        raise Fail(40, f"保存先 ({meta.get('name')}) がフォルダではありません", hint)
    if not meta.get("capabilities", {}).get("canAddChildren"):
        raise Fail(40, f"保存先 ({meta.get('name')}) にファイルを作る権限がありません", hint)
    return fid, meta.get("name")


def output_folder(create):
    """作成物を置くフォルダ (マイドライブ/gws-skills/<スキル名>) の ID。create=False なら、無いときは None を返す。"""
    parent = "root"
    for name in OUTPUT_FOLDERS:
        found = gws("drive", "files", "list", params={
            "q": f"name = '{name}' and mimeType = '{FOLDER_MIME}' and '{parent}' in parents and trashed = false",
            "fields": "files(id)", "orderBy": "createdTime", "pageSize": 1}).get("files", [])
        if found:
            parent = found[0]["id"]
        elif create:
            parent = gws("drive", "files", "create", params={"fields": "id"},
                         body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]})["id"]
            print(f"[OK] フォルダを作成: {name}", file=sys.stderr)
        else:
            return None
    return parent


def prepare(args):
    """plan・check・create の共通の確認 (終了コードの順) と読み込み。(アカウント, 翻訳元のメタデータ, スライド, 枠, 保存先)"""
    account = require_auth()
    sid = source_id_of(args.url)
    meta = check_source(sid)
    folder = check_folder(args.folder) if args.folder else None
    pres = get_presentation(sid)
    return account, meta, pres, extract(pres), folder


def file_name(args, meta):
    return f"{datetime.date.today():%Y%m%d}_" + (args.name or f"{meta['name']}_ja")


STYLE_NAMES = (("bold", "太字"), ("italic", "斜体"), ("underline", "下線"), ("strikethrough", "取り消し線"),
               ("smallCaps", "小型大文字"), ("baselineOffset", "上付き・下付き"), ("fontFamily", "フォント"),
               ("fontSize", "大きさ"), ("foregroundColor", "色"), ("backgroundColor", "背景色"), ("link", "リンク"))


def describe(style, base):
    """印の書式が、段落の主な書式とどう違うか (訳す人が、どの語を囲むかを決めるため)。"""
    out = []
    for key, name in STYLE_NAMES:
        a, b = style.get(key), base.get(key)
        if a == b:
            continue
        if isinstance(a, bool) or isinstance(b, bool) or key == "link":  # 有り無しの書式
            out.append(name if a else f"{name}でない")
        else:
            out.append(f"{name}が違う")
    return "・".join(out) or "書式が少し違う"


def slides_of(boxes):
    """texts.json の slides (翻訳する段落)。翻訳元が plan の後に変わっていないかを、これを比べて確かめる。"""
    slides = {}
    for b in boxes:
        paras = {e["id"]: e["marked"] for e in b["paras"] if e["id"]}
        if paras:
            slides.setdefault(b["slide"], []).append({"box": b["box"], "where": b["where"], "paras": paras})
    return [{"slide": n, "boxes": v} for n, v in sorted(slides.items())]


def texts_json(meta, boxes):
    marks = {e["id"]: {n: describe(s, e["base"]) for n, s in e["styles"].items()}
             for b in boxes for e in b["paras"] if e["id"] and e["styles"]}
    return {"title": meta["name"], "sourceId": meta["id"], "slides": slides_of(boxes), "marks": marks}


def check_texts(source_id, boxes):
    """texts.json が、この翻訳元の今の段落から作ったものかを確かめる (別の資料の訳や、古い訳を使わないため)。"""
    texts = read_json(TEXTS, 35, "plan からやり直してください")
    if texts.get("sourceId") != source_id:
        raise Fail(35, f"{TEXTS} は別のスライドのものです", "この URL で plan からやり直してください")
    if texts.get("slides") != slides_of(boxes):
        raise Fail(35, "翻訳元が plan の後に変わりました", "plan からやり直し、訳を書き直してください")


def cmd_plan(args):
    account, meta, pres, boxes, folder = prepare(args)
    if folder:
        where = f"{folder[1]} ({folder_url(folder[0])})"
    else:
        fid = output_folder(create=False)
        where = DEFAULT_FOLDER + (f" ({folder_url(fid)})" if fid else " (無いので作る)")
    data = texts_json(meta, boxes)
    try:
        with open(work_path(TEXTS), encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = None
    if os.path.isdir(WORK_DIR) and os.listdir(WORK_DIR) and (
            not isinstance(old, dict) or (old.get("sourceId"), old.get("slides")) != (data["sourceId"], data["slides"])):
        # 別のスライドか、変わった翻訳元の作業のファイル (訳・画像など) は、取り違えないように消す
        print("前の作業のファイルは、別のスライドか変わる前の翻訳元のものなので消します")
        cmd_clean(args)
    write_work(TEXTS, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    entries = [e for b in boxes for e in b["paras"] if e["id"]]
    chars = sum(len(plain_of(e["marked"])) for e in entries)
    print(f"アカウント: {account}")
    print(f"翻訳元:     {meta['name']}")
    print(f"ファイル名: {file_name(args, meta)}")
    print(f"保存先:     {where}")
    print(f"スライド:   {len(pres.get('slides', []))} 枚")
    print(f"翻訳する段落: {len(entries)} 個 ({chars} 字)")
    print(f"[OK] 翻訳する段落を {WORK_REL}{TEXTS} に書きました")
    print(f"次: 内容の確認を得てから、thumbs --source で翻訳元を見て、{WORK_REL}{TRANSLATIONS} に日本語版を書き、"
          "check (plan と同じ URL とオプション)")
    return 0


def prepare_translated(args):
    """check と create の共通の確認。plan の後に翻訳元が変わっていないことと、訳がそろっていることを確かめる。"""
    _, meta, _, boxes, folder = prepare(args)
    check_texts(meta["id"], boxes)
    data, names = read_translations(35)
    translations = validate_translations(boxes, data, "・".join(names), 35, require_all=True)
    return meta, boxes, folder, translations, fit(boxes, translations)


def short(text, n=40):
    if text is None:
        return "(消す)"
    text = plain_of(text).replace("\n", " ")
    return text if len(text) <= n else text[:n] + "…"


def cmd_check(args):
    _, boxes, _, translations, info = prepare_translated(args)
    print(f"[OK] 訳は {len(translations)} 個すべてそろっています")
    removed = [k for k, v in translations.items() if v is None]
    added = sum(v.count(PARA_MARK) for v in translations.values() if v)
    if removed or added:
        print(f"段落の増減: 消す {len(removed)} 個 ({', '.join(removed[:20])})、足す {added} 個")
    needs = [v for v in info.values() if v["own"] < 1 or v["over"]]
    need_ids = {v["box"]["box"] for v in needs}
    if not needs:
        print("あふれそうな枠: ありません")
    else:
        print(f"あふれそうな枠: {len(needs)} 個 (create で、括弧の割合まで文字を小さくする)")
        for v in needs:
            b = v["box"]
            note = f"{v['scale']:.2f}" + ("。それでもあふれそう" if v["over"] else "")
            print(f"  スライド {b['slide']} {b['box']} {b['where']} ({note}):")
            for no, ls, ld in v["grown"]:
                print(f"    {no}: {ls}→{ld} 行 「{short(translations[no])}」")
            for no in v["added"]:
                print(f"    {no}: 段落を足した 「{short(translations[no])}」")
            if not v["grown"] and not v["added"]:
                print("    (行は増えないが、高さが足りない)")
    wrapped = [(v["box"], g) for v in info.values() if v["box"]["box"] not in need_ids for g in v["grown"] if g[1] == 1]
    if wrapped:
        print(f"1 行から折り返す段落 (枠には収まりそう): {len(wrapped)} 個")
        for b, (no, ls, ld) in wrapped:
            print(f"  スライド {b['slide']} {b['box']} {no}: {ls}→{ld} 行 「{short(translations[no])}」")
    shared = [v["box"] for v in info.values() if v["scale"] < 1 and v["own"] == 1]
    if shared:
        slides = sorted({b["slide"] for b in shared})
        print(f"見た目をそろえるために小さくする枠: {len(shared)} 個 (スライド {', '.join(map(str, slides))})")
    if needs or wrapped:
        print(f"案内: 見出し・ラベルなどの短い文は、訳を短く言い換えると、文字を小さくせずにすみます。"
              f"{TRANSLATIONS} を直したら check をやり直してください")
    else:
        print("案内: 確認を得ていれば create に進んでください")
    return 0


def review_pages(info):
    """画像で確かめるページ。訳で見た目が変わりうる枠のあるスライド: 文字を小さくした・あふれそう・行が増えた・
    折り返しの手前・段落を足した・消した枠。ほかの枠は、訳が元と同じ行数に収まると見積もったもの。"""
    return sorted({v["box"]["slide"] for v in info.values()
                   if v["scale"] < 1 or v["over"] or v["grown"] or v["tight"] or v["added"] or v["removed"]})


def cmd_create(args):
    meta, boxes, folder, translations, info = prepare_translated(args)

    fid, fname = folder or (output_folder(create=True), DEFAULT_FOLDER)
    copied = gws("drive", "files", "copy", params={"fileId": meta["id"], "supportsAllDrives": True,
                                                   "fields": "id,name,webViewLink"},
                 body={"name": file_name(args, meta), "parents": [fid],
                       "appProperties": {MARK_SKILL: SKILL, MARK_SOURCE: meta["id"]}})
    state = {"sourceId": meta["id"], "copyId": copied["id"]}
    save_state(state)  # 書き込みの途中で失敗しても、翻訳版を追えるように先に書く (書く前の確かめは write_copy が行う)
    print(f"[OK] 翻訳元をコピー: {fname}/{copied.get('name')}", file=sys.stderr)

    dst_boxes = extract(get_presentation(state["copyId"]))
    if not same_layout(boxes, dst_boxes, {}):
        raise Fail(1, "コピーの構成が翻訳元と違います", "時間をおいて create からやり直してください")
    # 訳が元と同じ段落 (製品名など) は書き換えない。書き換えると、Slides の文字の自動調整が外れるため
    marked = {e["id"]: e["marked"] for b in boxes for e in b["paras"] if e["id"]}
    changed = {k: v for k, v in translations.items() if v != marked[k]}
    reqs, counts = [], {}
    for src, dst in zip(boxes, dst_boxes):
        total = info[src["box"]]["scale"] * src["autoScale"] if src["box"] in info else src["autoScale"]
        if not any(e["id"] in changed for e in src["paras"]) and abs(total - src["autoScale"]) < 1e-6:
            continue  # 訳も大きさも変わらない枠は触らない
        # 自動調整で縮んでいた枠は、書き換えると縮みが外れるので、縮んだ大きさを書いておく
        r, c = replace_requests(src, dst, changed, lambda s, d: total if abs(total - 1) > 1e-6 else None, {})
        reqs += r
        counts.update(c)
    # paras (段落を足した・消した数) は fix で段落を対応させるのに、scales (小さくした割合) は fix での見積もりに、
    # pages は thumbs --review に使う
    state["paras"] = {k: v for k, v in counts.items() if v != 1}
    state["scales"] = {k: v["scale"] for k, v in info.items() if v["scale"] < 1}
    state["pages"] = review_pages(info)
    save_state(state)
    write_copy(state, reqs)
    print(f"[OK] {len(changed)} 個の段落を訳に置き換えました (訳が元と同じ {len(translations) - len(changed)} 個は、そのまま)",
          file=sys.stderr)

    shrunk = [{"slide": v["box"]["slide"], "box": k, "scale": v["scale"]} for k, v in info.items() if v["scale"] < 1]
    review = [{"slide": v["box"]["slide"], "box": k} for k, v in info.items() if v["over"]]
    url = copied.get("webViewLink") or f"https://docs.google.com/presentation/d/{state['copyId']}/edit"
    print(json.dumps({"presentationId": state["copyId"], "url": url, "folder": fname, "folderUrl": folder_url(fid),
                      "shrunk": shrunk, "review": review, "pages": state["pages"]}, ensure_ascii=False))
    return 0


def pages_of(spec, count):
    if not spec:
        return list(range(1, count + 1))
    out = set()
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", part)
        if not m:
            raise Fail(2, f"--pages の形が違います: {spec}", "1,3-5 のように書いてください")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        out.update(range(a, b + 1))
    bad = [p for p in out if not 1 <= p <= count]
    if bad:
        raise Fail(2, f"スライドは {count} 枚です: {', '.join(map(str, sorted(bad)))}", "範囲の中のページにしてください")
    return sorted(out)


def cmd_thumbs(args):
    if args.review and args.source:
        raise Fail(2, "--review は翻訳版だけに使えます", "--source を外すか、--pages でページを指定してください")
    require_auth()
    if args.source:  # 翻訳元の画像は、訳す前 (plan の後) にも取れる
        pid, tag = read_json(TEXTS, 35, "plan の後に実行してください").get("sourceId"), "src"
        if not pid:
            raise Fail(35, "翻訳元がわかりません", "plan からやり直してください")
    else:
        state = read_json(STATE, 45, "create の後に実行してください")
        pid, tag = check_target(state), "ja"
    slides = gws("slides", "presentations", "get", params={"presentationId": pid, "fields": "slides(objectId)"})
    slides = slides.get("slides", [])
    if args.review:
        pages = state.get("pages", [])
        if not pages:
            print("[OK] 画像で確かめるページはありません")
            return 0
    else:
        pages = pages_of(args.pages, len(slides))

    def fetch(n):
        for attempt in range(4):  # 画像の作成は API の上限が低いので、失敗したら待ってやり直す
            try:
                t = gws("slides", "presentations", "pages", "getThumbnail", params={
                    "presentationId": pid, "pageObjectId": slides[n - 1]["objectId"],
                    "thumbnailProperties.thumbnailSize": "MEDIUM"})
                break
            except GwsError:
                if attempt == 3:
                    raise
                time.sleep(10 * (attempt + 1))
        # 画像は curl で取る (python.org の Python には証明書が入っていないことがあるため。URL は 30 分で切れる)
        res = subprocess.run(["curl", "-sSfL", "--max-time", "60", t["contentUrl"]], capture_output=True)
        if res.returncode != 0:
            raise Fail(1, f"{n} 枚目の画像を取れません: {res.stderr.decode(errors='replace').strip()}", "時間をおいてやり直してください")
        name = f"thumb-{tag}-{n:02d}.png"
        write_work(name, res.stdout)
        return name

    # 1 枚ずつ取ると時間がかかるので、THUMB_WORKERS 枚ずつ同時に取る
    with concurrent.futures.ThreadPoolExecutor(max_workers=THUMB_WORKERS) as pool:
        written = list(pool.map(fetch, pages))
    print(f"[OK] {len(written)} 枚の画像を {WORK_REL} に書きました: {', '.join(written)}")
    print("案内: Read で見てください")
    return 0


def size_ratio(src_box, dst_box, src, dsts):
    """翻訳版の今の文字の大きさと、翻訳元の大きさの比。翻訳版の段落が空で測れなければ None。"""
    src_sizes = [run_size(st, src_box) for c, st in src["segs"] if c.strip()]
    dst_sizes = [run_size(st, dst_box) for d in dsts for c, st in d["segs"] if c.strip()]
    return dst_sizes[0] / src_sizes[0] if src_sizes and dst_sizes else None


def cmd_fix(args):
    require_auth()
    state = read_json(STATE, 45, "create の後に実行してください")
    copy_id = check_target(state)
    # 書式は翻訳元から写すので、翻訳元が plan の後に変わっていないことを確かめる
    src_boxes = extract(get_presentation(state["sourceId"]))
    check_texts(state["sourceId"], src_boxes)
    fixes = read_json(FIXES, 35, f"{WORK_REL}{FIXES} に直しを書いてください")
    shape_hint = '{"texts": {"<段落の番号>": "<訳>"}, "scale": {"<枠>": 0.9}} の形にしてください'
    if not isinstance(fixes, dict) or set(fixes) - {"texts", "scale"} or not isinstance(fixes.get("scale", {}), dict):
        raise Fail(35, f"{FIXES} の形が違います", shape_hint)
    texts = validate_translations(src_boxes, fixes.get("texts", {}), FIXES, 35, require_all=False)
    scale = fixes.get("scale", {})
    known = {b["box"] for b in src_boxes}
    bad = [k for k, v in scale.items()
           if k not in known or isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.3 <= v <= 2]
    if bad:
        raise Fail(35, f"{FIXES} の scale に誤りがあります: {', '.join(bad)}", "texts.json の枠 (b1 など) に 0.3〜2 の数を書いてください")
    if not texts and not scale:
        raise Fail(35, f"{FIXES} に直しがありません", shape_hint)

    counts = state.get("paras", {})
    restored = sorted(k for k, v in texts.items() if v is not None and counts.get(k) == 0)
    if restored:
        raise Fail(35, f"消した段落は戻せません: {', '.join(restored)}", "前後の段落の訳に含めてください")

    dst_boxes = extract(get_presentation(copy_id))
    if not same_layout(src_boxes, dst_boxes, counts):
        raise Fail(45, "翻訳版の構成が翻訳元と違います (手で段落や枠が足し引きされています)", "手で直してもらうよう、ユーザーに伝えてください")
    scales = state.get("scales", {})
    if texts:
        reqs = []
        for sb, db in zip(src_boxes, dst_boxes):
            if any(e["id"] in texts for e in sb["paras"]):
                def keep_ratio(s, ds, sb=sb, db=db):
                    # 差し替える段落は、create で小さくした割合を保つ。差し替えない段落の大きさは変えない。
                    # 測れない (空の段落) か、大きさが書かれていない (自動調整のまま) ときは、create の割合を使う
                    if s["id"] not in texts:
                        return None
                    r = size_ratio(sb, db, s, ds)
                    if r is None or abs(r - 1) < 1e-6:
                        r = scales.get(sb["box"], 1) * sb["autoScale"]
                    return r if abs(r - 1) > 1e-6 else None
                r, c = replace_requests(sb, db, texts, keep_ratio, counts)
                reqs += r
                counts.update(c)
        state["paras"] = {k: v for k, v in counts.items() if v != 1}
        state["texts"] = {**state.get("texts", {}), **texts}  # fix での見積もりに使う (translations.json は変えない)
        save_state(state)
        write_copy(state, reqs)
        print(f"[OK] {len(texts)} 個の段落の訳を差し替えました", file=sys.stderr)
    if scale:
        if texts:  # 訳を差し替えたので、インデックスを読み直す
            dst_boxes = extract(get_presentation(copy_id))
        reqs = []
        for db in dst_boxes:
            if db["box"] in scale:
                reqs += resize_requests(db, db["paras"], scale[db["box"]])
                scales[db["box"]] = round(scales.get(db["box"], 1) * scale[db["box"]], 3)
        state["scales"] = scales
        save_state(state)
        write_copy(state, reqs)
        print(f"[OK] {len(scale)} 個の枠の文字の大きさを変えました", file=sys.stderr)

    # 直した枠が、今の訳であふれそうかを見積もる (create と同じ式。直すかどうかは画像を見て決める)
    try:
        data, _ = read_translations(35)
    except Fail:
        data = {}
    info = fit(src_boxes, {**data, **state.get("texts", {})})
    touched = {b["box"] for b in src_boxes if any(e["id"] in texts for e in b["paras"])} | set(scale)
    warn = []
    for k in sorted(touched & set(info), key=lambda k: int(k[1:])):
        need = info[k]["own"] / scales.get(k, 1)
        if need < 1 - 1e-6 or info[k]["over"]:
            warn.append({"slide": info[k]["box"]["slide"], "box": k, "scale": round(need, 2)})
            print(f"[注意] スライド {info[k]['box']['slide']} {k}: 今の訳ではあふれそう"
                  f" (今の大きさの {need:.2f} 倍まで小さくするか、言い換える)", file=sys.stderr)
    print(json.dumps({"presentationId": copy_id, "texts": sorted(texts, key=num_key),
                      "scale": scale, "review": warn}, ensure_ascii=False))
    return 0


def cmd_clean(args):
    names = sorted(os.listdir(WORK_DIR)) if os.path.isdir(WORK_DIR) else []
    for n in names:
        p = os.path.join(WORK_DIR, n)
        if os.path.isfile(p) or os.path.islink(p):
            os.remove(p)
        else:
            raise Fail(1, f"{WORK_REL}{n} はファイルではないので消しません", "ユーザーに伝えてください")
    if os.path.isdir(WORK_DIR):
        os.rmdir(WORK_DIR)
    print(f"[OK] {WORK_REL} の {len(names)} 個のファイルを消しました")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    opts = argparse.ArgumentParser(add_help=False)
    opts.add_argument("url", help="翻訳元の Google スライドの URL")
    opts.add_argument("--name", help="ファイル名の yyyymmdd_ より後 (既定: <元の名前>_ja)")
    opts.add_argument("--folder", help=f"保存先のフォルダの URL か ID (既定: {DEFAULT_FOLDER})")
    sub.add_parser("plan", parents=[opts], help="作る内容を表示し、翻訳する段落を書く (読み取りのみ)")
    sub.add_parser("check", parents=[opts], help="訳を確かめ、文字があふれそうな枠を示す (読み取りのみ)")
    sub.add_parser("create", parents=[opts], help="翻訳元をコピーして、訳に置き換える")
    p = sub.add_parser("thumbs", help="スライドの画像を書く")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--pages", help="ページ (例: 1,3-5。既定: すべて)")
    which.add_argument("--review", action="store_true",
                       help="create が画像で確かめるとしたページだけにする (翻訳版のみ)")
    p.add_argument("--source", action="store_true", help="翻訳元の画像にする (比べるため)")
    sub.add_parser("fix", help="fixes.json の直しを翻訳版に適用する")
    sub.add_parser("clean", help=f"{WORK_REL} のファイルを消す")
    args = ap.parse_args()

    try:
        return {"plan": cmd_plan, "check": cmd_check, "create": cmd_create, "thumbs": cmd_thumbs,
                "fix": cmd_fix, "clean": cmd_clean}[args.cmd](args)
    except Fail as e:
        code, message, hint = e.args
        print(f"[NG] {message}")
        print(f"案内: {hint}")
        return code
    except GwsError as e:
        print(f"[NG] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
