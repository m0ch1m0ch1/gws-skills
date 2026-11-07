#!/usr/bin/env python3
"""Markdown (手順書など) を Google ドキュメントにする

gws-md2gdoc スキルが plan → create の順で使う。

使い方:
  md2gdoc.py plan [<入力>] [--name <名前>] [--folder <フォルダ>]
      gws・認証・保存先を確かめてから、作るドキュメントの名前・保存先・タイトル・見出しを表示する (読み取りのみ)
  md2gdoc.py create [<入力>] [--name <名前>] [--folder <フォルダ>]
      plan と同じ確認をしてから、保存先に yyyymmdd_<タイトル> (--name があれば yyyymmdd_<名前>) の名前で
      空のドキュメントを作り、先頭にタイトル (入力の最初の見出し)、その後に本文を書き込む。
      最後の行に {"documentId", "title", "url", "folder", "folderUrl"}
  保存先: --folder で指定したフォルダ (URL か ID)。無ければマイドライブの gws-skills/gws-md2gdoc/ (無ければ作る)
終了コード (plan・create で共通。この順に確かめる):
  10=gws が無い 20=未認証/認証切れ 30=入力が決まらない (無い・複数ある・指定したものが無い)
  40=保存先を使えない (読めない・フォルダでない・作る権限が無い) 2=引数の誤り 1=そのほかの API の失敗

入力: input/gws-md2gdoc/ に置いた Markdown (.md / .markdown)。
  省略するとそこにある 1 つを使う。ファイル名だけを渡すとそこから探す。

Google Workspace の操作はすべて gws CLI で行う。認証情報はこのスクリプトでは扱わない (gws が管理する)。
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

SKILL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REPO_ROOT = os.path.normpath(os.path.join(SKILL_DIR, "..", "..", ".."))
INPUT_DIR = os.path.join(REPO_ROOT, "input", os.path.basename(SKILL_DIR))  # 入力を置く (Git 管理対象外)
INPUT_REL = os.path.relpath(INPUT_DIR, REPO_ROOT) + "/"
INPUT_EXTS = (".md", ".markdown")
OUTPUT_FOLDERS = ["gws-skills", os.path.basename(SKILL_DIR)]  # 既定の保存先 (マイドライブからのパス)
DEFAULT_FOLDER = "マイドライブ/" + "/".join(OUTPUT_FOLDERS)
FOLDER_MIME = "application/vnd.google-apps.folder"
OVERVIEW_HEADING = "概要"  # 最初の見出しより前の文章をまとめる見出し
MAX_LEVEL = 4  # 手順を見出しにするのは Markdown の見出しレベル 4 (Docs の見出し 3) まで

CODE_FONT = {"weightedFontFamily": {"fontFamily": "Roboto Mono"}}  # 本文は Docs の段落スタイルのフォントのまま
HANG = 18  # PT / リスト項目の 2 行目以降の字下げ (番号・記号の幅)。ネストしても字下げはしない (すべて左揃え)
MIN_FONT = 10  # PT / どこでもこれより小さくしない


def rgb(hex_):
    return {"color": {"rgbColor": {k: int(hex_[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}}}


# 色は無彩色のグレーと、注記の青・赤・橙だけにする
GRAY_BG = rgb("F0F0F0")
GRAY_LINE = rgb("D2D2D2")
LABEL_COLOR = rgb("6A6E73")
PROMPT_COLOR = rgb("8A8D90")
CODE_STYLES = {  # (背景, 文字, 左の線 (None なら背景と同じ), 外枠)
    "cmd": (rgb("1F1F1F"), rgb("F0F0F0"), None, None),      # 入力するコマンド: 端末風
    "file": (rgb("F5F5F5"), rgb("151515"), rgb("8A8D90"), None),  # ファイルの内容
    "output": (rgb("FFFFFF"), rgb("3C3F42"), None, GRAY_LINE),    # 出力例
}
ADMONITION_COLORS = {  # (左の線とラベル, 背景)
    "note": (rgb("0066CC"), rgb("EDF5FF")),
    "important": (rgb("EE0000"), rgb("FFF0F0")),
    "warning": (rgb("F0AB00"), rgb("FFF8E6")),
}
ADMONITION_KINDS = {
    "注記": "note", "注意": "warning", "警告": "warning", "重要": "important", "ヒント": "note",
    "note": "note", "tip": "note", "important": "important", "warning": "warning", "caution": "warning",
}


def pt(v):
    return {"magnitude": v, "unit": "PT"}


def u16(s):
    """Docs API のインデックスは UTF-16 コード単位。"""
    return len(s.encode("utf-16-le")) // 2


# --------------------------------------------------------------------------- gws

class GwsError(Exception):
    pass


class Fail(Exception):
    """[NG] で終える。(終了コード, 内容, 案内)"""


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


def batch_update(doc_id, requests, chunk=300):
    # リクエストは先頭から順に適用されるので、分割しても結果は変わらない
    for i in range(0, len(requests), chunk):
        gws("docs", "documents", "batchUpdate", params={"documentId": doc_id},
            body={"requests": requests[i:i + chunk]})


# ---------------------------------------------------------------------- inline

INLINE_RE = re.compile(r"(?P<ticks>`+)(.+?)(?P=ticks)|\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)|(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])")


def parse_inline(src):
    """インライン記法を (プレーンテキスト, [(start, end, textStyle, fields)]) に変換する。"""
    out, spans, pos = "", [], 0
    for m in INLINE_RE.finditer(src):
        out += src[pos:m.start()]
        _, code, bold, ltext, url, italic = m.groups()
        start = u16(out)
        if code is not None:
            out += code.strip() or code  # `` `x` `` の内側の空白は取る
            spans.append((start, u16(out), {**CODE_FONT, "fontSize": pt(MIN_FONT)},
                          "weightedFontFamily,fontSize"))
        elif bold is not None:
            inner, inner_spans = parse_inline(bold)
            out += inner
            spans += [(start + s, start + e, st, f) for s, e, st, f in inner_spans]
            spans.append((start, u16(out), {"bold": True}, "bold"))
        elif ltext is not None:
            out += ltext
            spans.append((start, u16(out), {"link": {"url": url}}, "link"))
        else:
            out += italic
            spans.append((start, u16(out), {"italic": True}, "italic"))
        pos = m.end()
    out += src[pos:]
    return out, spans


# ---------------------------------------------------------------------- blocks

LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])(\s+)(.*)$")
FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)\s*(\S*)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def is_wide(ch):
    """全角の文字 (日本語など) か。"""
    return ord(ch) > 0x2E7F


def join_lines(lines):
    """段落の途中の改行をつなぐ。全角の文字どうしの間には空白を入れない。"""
    out = ""
    for ln in lines:
        if out and not (is_wide(out[-1]) and is_wide(ln[0])):
            out += " "
        out += ln
    return out


def parse_markdown(text):
    """Markdown をブロックの列にする。各ブロックは所属するリスト項目のスタック (items) を持つ。"""
    lines = text.replace("\t", "    ").splitlines()
    blocks, stack, i = [], [], 0
    if lines and lines[0].strip() == "---":  # 先頭の YAML front matter は本文にしない
        end = next((k for k in range(1, len(lines)) if lines[k].strip() in ("---", "...")), None)
        if end is not None:
            i = end + 1
    item_seq = 0

    def pop_to(col):
        # マーカーより深く字下げした行は、その項目に属する (`10. ` の子を 3 文字の字下げで書いても子になる)
        while stack and stack[-1]["marker_col"] >= col:
            stack.pop()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        ind = indent_of(line)
        stripped = line.strip()
        if re.fullmatch(r"(-\s*){3,}|(\*\s*){3,}|(_\s*){3,}", stripped):  # 区切り線は使わない (見出しの線で区切る)
            i += 1
            continue

        m = HEADING_RE.match(line)
        if m and ind == 0:
            stack.clear()
            blocks.append({"kind": "heading", "level": len(m.group(1)), "text": m.group(2), "items": []})
            i += 1
            continue

        m = FENCE_RE.match(line)
        if m:
            pop_to(ind)
            fence, lang = m.group(2), m.group(3)
            body, i = [], i + 1
            # 開きと同じ文字が開き以上の長さで並ぶだけの行で閉じる
            while i < len(lines) and not (lines[i].strip().startswith(fence) and not lines[i].strip().strip(fence[0])):
                ln = lines[i]
                body.append(ln[min(ind, indent_of(ln)):])
                i += 1
            i += 1
            blocks.append({"kind": "code", "lang": lang, "text": "\n".join(body), "items": list(stack)})
            continue

        if stripped.startswith(">"):
            pop_to(ind)
            qlines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                qlines.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            blocks.append({"kind": "quote", "lines": qlines, "items": list(stack)})
            continue

        if stripped.startswith("|"):
            pop_to(ind)
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            blocks.append({"kind": "table", "rows": rows, "items": list(stack)})
            continue

        m = LIST_RE.match(line)
        if m:
            pop_to(ind)
            marker, rest = m.group(2), m.group(4)
            check = None
            cm = re.match(r"^\[([ xX])\]\s+(.*)$", rest)
            if cm:
                check, rest = cm.group(1) != " ", cm.group(2)
            item_seq += 1
            item = {"id": item_seq, "ordered": marker[0].isdigit(), "check": check, "marker_col": ind}
            blocks.append({"kind": "item", "text": rest, "item": item, "items": list(stack)})
            stack.append(item)
            i += 1
            continue

        # 段落 (連続行は結合)
        pop_to(ind)
        para = [stripped]
        i += 1
        while i < len(lines) and lines[i].strip() and not (
                HEADING_RE.match(lines[i]) or FENCE_RE.match(lines[i]) or LIST_RE.match(lines[i])
                or lines[i].strip()[0] in ">|"):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"kind": "para", "text": join_lines(para), "items": list(stack)})
    return blocks


# ----------------------------------------------------------------- restructure

def restructure(blocks):
    """読みやすさのために組み替える (Markdown の構成はそのまま使わない)。

    - 子の手順を持つ番号付きの項目は、1 つ下のレベルの見出しにする (番号は heading_numbers が見出しの順に振る)。
      一度見出しにした後の兄弟の項目も見出しにする
    - 子に番号付きの手順を持つ箇条書きの項目は、前置きの段落にする (ほかの入れ子の箇条書きはそのまま)
    これで、番号付きリストは見出しの下で 1 段だけになる。
    """
    items = [b for b in blocks if b["kind"] == "item" and b["items"]]
    step_parents = {b["items"][-1]["id"] for b in items if b["item"]["ordered"] and b["item"]["check"] is None}
    promoted, unwrapped, promoted_siblings = {}, set(), set()  # promoted: 見出しにした項目 id -> Markdown の見出しレベル
    out, hl, section = [], 1, 0
    for b in blocks:
        if b["kind"] == "heading":
            hl, section = b["level"], section + 1
            out.append(b)
            continue
        kept = [a for a in b["items"] if a["id"] not in promoted and a["id"] not in unwrapped]
        if b["kind"] == "item" and b["item"]["check"] is None:
            it = b["item"]
            sib = (section, b["items"][-1]["id"] if b["items"] else None)
            base = max((promoted[a["id"]] for a in b["items"] if a["id"] in promoted), default=hl)
            if it["ordered"] and base < MAX_LEVEL and (it["id"] in step_parents or sib in promoted_siblings):
                promoted[it["id"]] = base + 1
                promoted_siblings.add(sib)
                out.append({"kind": "heading", "level": base + 1, "text": b["text"].rstrip("。"), "items": []})
                continue
            if not it["ordered"] and it["id"] in step_parents:
                unwrapped.add(it["id"])
                out.append({"kind": "para", "text": b["text"], "items": kept})
                continue
        out.append({**b, "items": kept})
    return out


# ------------------------------------------------------------------- rendering

NUMBERED_RE = re.compile(r"\d+(\.\d+)*\.\s")


def heading_numbers(blocks):
    """最初の H1 を文書タイトル、H2/H3/H4 を Docs の HEADING_1/2/3 にして番号を振る。
    2 つ目以降の H1 と H5 以下は太字の段落にする。最初の H2 より前の文章は「概要」の見出しの下にまとめる。

    入力の見出しに番号が付いているもの (例: "## 1. 準備") があれば、入力の番号を活かして番号を振らない。
    """
    number = not any(b["kind"] == "heading" and 2 <= b["level"] <= 4 and NUMBERED_RE.match(b["text"])
                     for b in blocks)
    title, out, preamble, counters, seen_h2 = None, [], [], [0, 0, 0], False
    for b in blocks:
        if b["kind"] == "heading" and b["level"] == 1 and title is None:
            title = b["text"]
            continue
        if b["kind"] == "heading" and 2 <= b["level"] <= 4:
            if not seen_h2 and preamble:
                counters[0] += 1
                out.append({"kind": "heading", "style": "HEADING_1", "items": [],
                            "text": (f"{counters[0]}. " if number else "") + OVERVIEW_HEADING})
                out += preamble
            seen_h2 = True
            lvl = b["level"] - 2
            counters[lvl] += 1
            for j in range(lvl + 1, 3):
                counters[j] = 0
            num = ".".join(str(c) for c in counters[:lvl + 1]) + ". "
            out.append({"kind": "heading", "style": f"HEADING_{lvl + 1}", "items": [],
                        "text": (num if number else "") + b["text"]})
            continue
        if b["kind"] == "heading":  # 2 つ目以降の H1 / H5 以下
            b = {**b, "kind": "para", "text": f"**{b['text']}**"}
        (out if seen_h2 else preamble).append(b)
    if not seen_h2:
        out = preamble
    return title, out


def build_segments(blocks):
    """セグメントの列にする。セグメントは、表を挟まない連続した段落のリストか、表 ({"table": 行の列})。

    段落は dict(text, spans, style, role, list_level, group, ordered, indent_start, after_table, ...)。
    """
    segments, seg = [], []
    group, group_id, manual_counters = None, 0, {}

    def depth(b):
        return len(b["items"])

    for idx, b in enumerate(blocks):
        if not (b["kind"] == "item" or depth(b) > 0):
            group = None
        elif group is None:
            # リストのまとまり (グループ) ごとに番号付き / 箇条書きを決める
            group_id += 1
            j, ordered = idx, False
            while j < len(blocks) and blocks[j]["kind"] != "heading" and (
                    blocks[j]["kind"] == "item" or depth(blocks[j]) > 0):
                if blocks[j]["kind"] == "item" and blocks[j]["item"]["ordered"] and blocks[j]["item"]["check"] is None:
                    ordered = True
                j += 1
            group = {"id": group_id, "ordered": ordered}

        if b["kind"] == "table":
            segments.append(seg)
            segments.append({"table": [[parse_inline(c) for c in row] for row in b["rows"]]})
            seg = []
            continue

        p = {"style": "NORMAL_TEXT", "list_level": None, "group": group["id"] if group else None,
             "ordered": bool(group and group["ordered"]), "indent_start": 0, "role": "body",
             "after_table": not seg and bool(segments) and isinstance(segments[-1], dict)}
        prev = seg[-1] if seg else None
        if b["kind"] == "heading":
            p["style"], p["role"] = b["style"], "heading"
            text, spans = parse_inline(b["text"])
        elif b["kind"] == "para":
            text, spans = parse_inline(b["text"])
            if re.fullmatch(r"\*\*[^*]{1,30}\*\*", b["text"]):  # 「出力例」などの小見出し
                p["role"] = "label"
                spans.append((0, u16(text), {"fontSize": pt(MIN_FONT), "foregroundColor": LABEL_COLOR},
                              "fontSize,foregroundColor"))
        elif b["kind"] == "code":
            seg += code_paras(b, p, prev)
            continue
        elif b["kind"] == "quote":
            label, body = None, list(b["lines"])
            while body and not body[0].strip():
                body.pop(0)
            # ラベルは 1 行目の **注記** か、GitHub の [!NOTE]
            lm = re.fullmatch(r"\*\*(.+?)\*\*|\[!(\w+)\]", body[0].strip()) if body else None
            if lm:
                label = lm.group(1) or lm.group(2).capitalize()
                body.pop(0)
            paras_, cur = [], []  # 空の行で段落を分ける
            for ln in body + [""]:
                if ln.strip():
                    cur.append(ln.strip())
                elif cur:
                    paras_.append(join_lines(cur))
                    cur = []
            md = "\v".join(paras_)
            if label:
                md = f"**{label}**\v" + md
            text, spans = parse_inline(md)
            p["role"] = ADMONITION_KINDS.get((label or "").lower(), "note")
            if label:
                spans.append((0, u16(label), {"foregroundColor": ADMONITION_COLORS[p["role"]][0]}, "foregroundColor"))
        else:  # item
            it = b["item"]
            text, spans = parse_inline(b["text"])
            ordered_depth = sum(1 for a in b["items"] if a["ordered"] == group["ordered"] and a["check"] is None)
            native = it["check"] is None and it["ordered"] == group["ordered"]
            p["role"] = "step" if native and it["ordered"] else "bullet"
            if native:
                p["list_level"] = ordered_depth
                p["indent_start"] = HANG
            else:
                # グループの種類と違う項目は記号を文字で付ける
                if it["check"] is not None:
                    prefix = "☑ " if it["check"] else "☐ "
                elif it["ordered"]:
                    key = (group["id"], b["items"][-1]["id"] if b["items"] else 0)
                    manual_counters[key] = manual_counters.get(key, 0) + 1
                    prefix = f"{manual_counters[key]}. "
                else:
                    prefix = "• "
                shift = u16(prefix)
                text = prefix + text
                spans = [(s + shift, e + shift, st, f) for s, e, st, f in spans]
                p["indent_start"] = HANG
        p["text"], p["spans"] = text, spans
        seg.append(p)
    segments.append(seg)
    return [s for s in segments if s]


OUTPUT_LABEL_RE = re.compile(r"出力|結果|output|result", re.I)
SHELL_LANGS = ("terminal", "console", "shell", "bash", "sh", "zsh", "powershell", "ps1", "cmd", "bat")


def code_paras(b, base, prev):
    """コードブロックを 1 行 1 段落にする。折り返した行は 16pt 字下げして、続きの行だとわかるようにする。

    種類: 「出力例」などのラベルの直後は出力、シェルの言語か "$ " で始まるものはコマンド、ほかはファイルの内容。
    """
    lines = b["text"].split("\n")
    if prev and prev["role"] == "label" and OUTPUT_LABEL_RE.search(prev["text"]):
        kind = "output"
    elif b["lang"].lower() in SHELL_LANGS or lines[0].startswith("$ "):
        kind = "cmd"
    else:
        kind = "file"
    _, fg, _, _ = CODE_STYLES[kind]
    paras = []
    for i, line in enumerate(lines):
        spans = [(0, u16(line), {**CODE_FONT, "fontSize": pt(MIN_FONT),
                                 "foregroundColor": fg}, "weightedFontFamily,fontSize,foregroundColor")]
        if kind == "cmd" and line.startswith("$ "):
            spans.append((0, 1, {"foregroundColor": PROMPT_COLOR}, "foregroundColor"))
        paras.append({**base, "role": kind, "indent_start": 16, "text": line, "spans": spans,
                      "first": i == 0, "last": i == len(lines) - 1, "keep": len(lines) <= 20})
    return paras


def border(color, width, padding):
    return {"color": color, "width": pt(width), "padding": pt(padding), "dashStyle": "SOLID"}


def para_style(p):
    """役割ごとの段落スタイル。行間と前後の余白で、手順のまとまりが見えるようにする。"""
    role = p["role"]
    st = {"indentStart": pt(p["indent_start"]), "indentFirstLine": pt(0)}
    if role == "heading":
        if p["style"] == "HEADING_1":  # 章の区切りに細い線を引く
            st.update(borderBottom=border(GRAY_LINE, 0.75, 4))
        return st
    if role == "body":
        st.update(lineSpacing=130, spaceAbove=pt(4), spaceBelow=pt(4))
    elif role == "label":
        st.update(spaceAbove=pt(8), spaceBelow=pt(2), keepWithNext=True)
    elif role == "step":  # 手順の後のコマンドと同じページに置く
        st.update(lineSpacing=130, spaceAbove=pt(16), spaceBelow=pt(4), keepWithNext=True)
    elif role == "bullet":
        st.update(lineSpacing=130, spaceAbove=pt(2), spaceBelow=pt(2))
    elif role in CODE_STYLES:
        # 同じ枠の段落が続くと 1 つの枠にまとまる
        bg, _, accent, frame = CODE_STYLES[role]
        edge = border(frame or bg, 0.75, 8)
        st.update(shading={"backgroundColor": bg}, borderTop=edge, borderBottom=edge, borderRight=edge,
                  borderLeft=border(accent, 3, 10) if accent else edge, lineSpacing=115,
                  spaceAbove=pt(8 if p["first"] else 0), spaceBelow=pt(12 if p["last"] else 0),
                  keepWithNext=p["keep"] and not p["last"])
    else:  # 注記など
        line, bg = ADMONITION_COLORS[role]
        st.update(shading={"backgroundColor": bg}, borderLeft=border(line, 3, 10),
                  lineSpacing=130, spaceAbove=pt(10), spaceBelow=pt(10))
    if p.get("after_table"):  # 表のすぐ後が詰まらないようにする
        st["spaceAbove"] = pt(max(st.get("spaceAbove", pt(0))["magnitude"], 12))
    return st


RESET_TEXT_FIELDS = "bold,italic,underline,strikethrough,link,backgroundColor,foregroundColor,fontSize,weightedFontFamily"
RESET_PARA_FIELDS = ("namedStyleType,indentStart,indentFirstLine,shading,borderTop,borderBottom,"
                     "borderLeft,borderRight,spaceAbove,spaceBelow,lineSpacing,keepWithNext")


def rng(s, e):
    return {"startIndex": s, "endIndex": e}


def segment_requests(seg, at, width):
    """at の位置にセグメントを挿入するリクエスト列。width は本文の幅 (PT。表の列幅の合計)。"""
    if isinstance(seg, dict):
        return table_requests(seg["table"], at, width)

    # 1) 位置計算 (リスト項目は先頭タブでネストを指定する)
    pos, layout = at, []
    for p in seg:
        tabs = p["list_level"] or 0
        text = "\t" * tabs + p["text"] + "\n"
        layout.append((pos, pos + u16(text), tabs, text))
        pos += u16(text)

    reqs = [
        {"insertText": {"location": {"index": at}, "text": "".join(t for *_, t in layout)}},
        {"deleteParagraphBullets": {"range": rng(at, pos)}},
        {"updateParagraphStyle": {"range": rng(at, pos), "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                  "fields": RESET_PARA_FIELDS}},
        {"updateTextStyle": {"range": rng(at, pos), "textStyle": {},
                             "fields": RESET_TEXT_FIELDS}},
    ]
    for p, (s, e, tabs, _) in zip(seg, layout):
        if p["style"] != "NORMAL_TEXT":
            reqs.append({"updateParagraphStyle": {"range": rng(s, e), "paragraphStyle": {"namedStyleType": p["style"]},
                                                  "fields": "namedStyleType"}})
        for ss, se, st, f in p["spans"]:
            reqs.append({"updateTextStyle": {"range": rng(s + tabs + ss, s + tabs + se), "textStyle": st, "fields": f}})

    # 2) リストグループごとに箇条書きを作成 (後ろから処理してタブ削除の影響を避ける)
    groups = {}
    for p, (s, e, tabs, _) in zip(seg, layout):
        if p["group"] is not None:
            g = groups.setdefault(p["group"], {"first": None, "last": None, "ordered": p["ordered"]})
            if p["list_level"] is not None:
                g["first"] = s if g["first"] is None else g["first"]
                g["last"] = e
    for g in sorted((g for g in groups.values() if g["first"] is not None), key=lambda g: -g["first"]):
        preset = "NUMBERED_DECIMAL_ALPHA_ROMAN" if g["ordered"] else "BULLET_DISC_CIRCLE_SQUARE"
        reqs.append({"createParagraphBullets": {"range": rng(g["first"], g["last"]), "bulletPreset": preset}})

    # 3) タブ削除後の座標で、リスト外段落の箇条書き解除と段落スタイルを設定
    removed, adj = 0, []
    for s, e, tabs, _ in layout:
        adj.append((s - removed, e - removed - tabs))
        removed += tabs
    for p, (s, e) in zip(seg, adj):
        g = groups.get(p["group"])
        if p["list_level"] is None and g and g["first"] is not None:
            reqs.append({"deleteParagraphBullets": {"range": rng(s, e)}})
        style = para_style(p)  # deleteParagraphBullets が足す字下げもここで消す
        reqs.append({"updateParagraphStyle": {"range": rng(s, e), "paragraphStyle": style,
                                              "fields": ",".join(style)}})
    return reqs


def table_requests(rows, at, width):
    ncols = max(len(r) for r in rows)
    rows = [r + [("", [])] * (ncols - len(r)) for r in rows]
    t = at + 1  # insertTable は表の前に改行を 1 つ挿入する
    # 表の前に入る空の段落は消せないので、ごく小さくして目立たなくする
    reqs = [{"insertTable": {"rows": len(rows), "columns": ncols, "location": {"index": at}}},
            {"updateParagraphStyle": {"range": rng(at, at + 1),
                                      "paragraphStyle": {"namedStyleType": "NORMAL_TEXT", "lineSpacing": 100},
                                      "fields": RESET_PARA_FIELDS}},
            {"updateTextStyle": {"range": rng(at, at + 1), "textStyle": {"fontSize": pt(1)}, "fields": "fontSize"}}]
    for r in reversed(range(len(rows))):
        row_start = t + 1 + r * (1 + 2 * ncols)
        for c in reversed(range(ncols)):
            text, spans = rows[r][c]
            idx = row_start + 2 * c + 2
            if not text:
                continue
            reqs.append({"insertText": {"location": {"index": idx}, "text": text}})
            reqs.append({"updateTextStyle": {"range": rng(idx, idx + u16(text)),
                                             "textStyle": {"bold": r == 0},
                                             "fields": RESET_TEXT_FIELDS}})
            for ss, se, st, f in spans:
                reqs.append({"updateTextStyle": {"range": rng(idx + ss, idx + se), "textStyle": st, "fields": f}})

    def cells(rows_):
        return {"tableCellLocation": {"tableStartLocation": {"index": t}, "rowIndex": 0, "columnIndex": 0},
                "rowSpan": rows_, "columnSpan": ncols}
    line = {"color": GRAY_LINE, "width": pt(0.75), "dashStyle": "SOLID"}
    reqs.append({"updateTableCellStyle": {
        "tableRange": cells(len(rows)),
        "tableCellStyle": {"borderTop": line, "borderBottom": line, "borderLeft": line, "borderRight": line,
                           "paddingTop": pt(5), "paddingBottom": pt(5), "paddingLeft": pt(7), "paddingRight": pt(7)},
        "fields": "borderTop,borderBottom,borderLeft,borderRight,paddingTop,paddingBottom,paddingLeft,paddingRight"}})
    reqs.append({"updateTableCellStyle": {"tableRange": cells(1), "tableCellStyle": {"backgroundColor": GRAY_BG},
                                          "fields": "backgroundColor"}})
    if width:
        # 列幅は各列のいちばん長い文字列に比例させる (全角は 2 文字分。極端に狭く・広くならないように丸める)
        def width_of(text):
            return sum(2 if is_wide(ch) else 1 for ch in text)
        weights = [min(max(max(width_of(rows[r][c][0]) for r in range(len(rows))), 6), 60) for c in range(ncols)]
        for c, w in enumerate(weights):
            reqs.append({"updateTableColumnProperties": {
                "tableStartLocation": {"index": t}, "columnIndices": [c],
                "tableColumnProperties": {"widthType": "FIXED_WIDTH",
                                          "width": pt(round(width * w / sum(weights), 1))},
                "fields": "widthType,width"}})
    return reqs


def text_width(doc):
    """本文の幅 (PT)。ページの幅から左右の余白を引いたもの。表の列幅の合計にする。"""
    style = doc.get("documentStyle", {})
    width = style.get("pageSize", {}).get("width", {}).get("magnitude")
    if not width:
        return None
    return width - style.get("marginLeft", {}).get("magnitude", 72) - style.get("marginRight", {}).get("magnitude", 72)


# ------------------------------------------------------------------------ main

def resolve_input(name):
    """入力のパス。省略したら INPUT_DIR の .md (1 つだけのとき)、ファイル名だけなら INPUT_DIR から探す。"""
    if not name:
        found = sorted(f for f in os.listdir(INPUT_DIR) if f.lower().endswith(INPUT_EXTS)) \
            if os.path.isdir(INPUT_DIR) else []
        if len(found) == 1:
            return os.path.join(INPUT_DIR, found[0])
        if not found:
            raise Fail(30, f"{INPUT_REL} に入力 (.md) がありません",
                       f"変換する Markdown を {INPUT_REL} に置いてもらってください")
        raise Fail(30, f"{INPUT_REL} に入力が複数あります: " + ", ".join(found),
                   "どれを使うかユーザーに聞き、ファイル名を渡してください")
    for path in (name, os.path.join(INPUT_DIR, name)):
        if os.path.isfile(path):
            return path
    raise Fail(30, f"入力 {name} がありません", f"{INPUT_REL} に置いたファイルの名前を渡してください")


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


def prepare(args):
    """plan と create の共通の確認 (終了コードの順) と読み込み。

    (アカウント, 入力のパス, タイトル, ブロック, 保存先) を返す。保存先は --folder の (ID, 名前) か None (既定)。
    """
    account = require_auth()
    path = resolve_input(args.input)
    folder = check_folder(args.folder) if args.folder else None
    with open(path, encoding="utf-8") as f:
        title, blocks = heading_numbers(restructure(parse_markdown(f.read())))
    title = title or os.path.splitext(os.path.basename(path))[0]
    return account, path, title, blocks, folder


def file_name(args, title):
    return f"{datetime.date.today():%Y%m%d}_{args.name or title}"


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


def cmd_plan(args):
    account, path, title, blocks, folder = prepare(args)
    if folder:
        where = f"{folder[1]} ({folder_url(folder[0])})"
    else:
        fid = output_folder(create=False)
        where = DEFAULT_FOLDER + (f" ({folder_url(fid)})" if fid else " (無いので作る)")
    print(f"アカウント: {account}")
    print(f"入力:       {os.path.relpath(path, REPO_ROOT)}")
    print(f"ファイル名: {file_name(args, title)}")
    print(f"保存先:     {where}")
    print(f"タイトル:   {title}")
    print("見出し:")
    for b in blocks:
        if b["kind"] == "heading":
            print(("  " * int(b["style"][-1])) + b["text"])
    print(f"次: 内容の確認を得てから create {os.path.basename(path)} (plan と同じオプション)")
    return 0


def cmd_create(args):
    _, _, title, blocks, folder = prepare(args)
    fid, fname = folder or (output_folder(create=True), DEFAULT_FOLDER)
    created = gws("drive", "files", "create", params={"fields": "id,name,webViewLink", "supportsAllDrives": True},
                  body={"name": file_name(args, title), "parents": [fid],
                        "mimeType": "application/vnd.google-apps.document"})
    doc_id = created["id"]
    print(f"[OK] ドキュメントを作成: {fname}/{created.get('name')}", file=sys.stderr)
    width = text_width(gws("docs", "documents", "get", params={"documentId": doc_id}))

    # 空のドキュメントの先頭 (1) に、タイトルと本文を入れる。後ろのセグメントから順に挿入する (前方の座標が変わらない)
    blocks = [{"kind": "heading", "style": "TITLE", "text": title, "items": []}] + blocks
    reqs = []
    for seg in reversed(build_segments(blocks)):
        reqs += segment_requests(seg, 1, width)
    batch_update(doc_id, reqs)
    print("[OK] 本文を書き込みました", file=sys.stderr)

    url = created.get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit"
    print(json.dumps({"documentId": doc_id, "title": title, "url": url,
                      "folder": fname, "folderUrl": folder_url(fid)}, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    doc_opts = argparse.ArgumentParser(add_help=False)
    doc_opts.add_argument("input", nargs="?", help="input/gws-md2gdoc/ に置いた Markdown (.md)。省略するとそこの 1 つを使う")
    doc_opts.add_argument("--name", help="ファイル名の yyyymmdd_ より後 (既定: 入力の最初の見出し)")
    doc_opts.add_argument("--folder", help="保存先のフォルダの URL か ID (既定: マイドライブ/gws-skills/gws-md2gdoc)")
    sub.add_parser("plan", parents=[doc_opts], help="作る内容を表示する (読み取りのみ)")
    sub.add_parser("create", parents=[doc_opts], help="ドキュメントを作って書き込む")
    args = ap.parse_args()

    try:
        return {"plan": cmd_plan, "create": cmd_create}[args.cmd](args)
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
