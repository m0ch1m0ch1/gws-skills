#!/usr/bin/env python3
"""YAML (設定値など) を Google スプレッドシートのパラメータシートにする

gws-yaml2gsheet スキルが plan → create の順で使う。

使い方:
  yaml2gsheet.py plan [<入力のフォルダ>] [--name <タイトル>] [--folder <フォルダ>]
      gws・認証・入力・保存先を確かめてから、作るスプレッドシートの名前・保存先・シートを表示する (読み取りのみ)
  yaml2gsheet.py create [<入力のフォルダ>] [--name <タイトル>] [--folder <フォルダ>]
      plan と同じ確認をしてから、保存先に yyyymmdd_<タイトル>_パラメータシート の名前でスプレッドシートを作り、
      先頭に目次のシートを置き、入力のフォルダの YAML 1 つを 1 つのシートにして書き込む。
      最後の行に {"spreadsheetId", "title", "url", "folder", "folderUrl", "sheets"}
  タイトル: --name があればそれ。無ければ入力のフォルダの名前
  保存先: --folder で指定したフォルダ (URL か ID)。無ければマイドライブの gws-skills/gws-yaml2gsheet/ (無ければ作る)
終了コード (plan・create で共通。この順に確かめる):
  10=gws が無い 20=未認証/認証切れ
  30=入力が決まらない (フォルダが無い・複数ある・指定したものが無い・YAML が無い・シート名が重なる)
  35=入力を読めない (YAML の誤り・扱えない書き方・中身が無い)
  40=保存先を使えない (読めない・フォルダでない・作る権限が無い) 2=引数の誤り 1=そのほかの API の失敗

入力: input/gws-yaml2gsheet/ の下のフォルダ。その直下の YAML (.yaml / .yml) を名前の順にシートにする。
  省略するとそこにある 1 つを使う。フォルダの名前だけを渡すとそこから探す。

シート: No / 項目 / 値 / 備考 の表。項目は階層ごとの細い列で、キーは "キー:" と書いてその階層の列に置く
  (右の空いた列にはみ出して見える)。子を持つキーは太字の行にする。リストの要素は [1] [2] … (要素がマッピングなら親の行)。
  値は YAML に書いたとおりの文字列 (引用符は取る)。
  備考は、キーの直前の行のコメントと、行末のコメント。最初のキーより前のコメント (ファイルの説明・注記) は使わない。
  ドキュメント (--- で区切ったもの) ごとに見出しの行 (kind / metadata.name。kind が無ければ、複数のときだけ
  「ドキュメント N」) を入れ、ドキュメントの間を空ける。

YAML は、パラメータでよく使う範囲だけを読む (標準ライブラリだけで動かすため):
  ブロックのマッピング・リスト、引用符の文字列、| と > の複数行の文字列、[...] と {...}、コメント、---。
  アンカー (&)・エイリアス (*)・タグ (!)・? のキーは [NG] にする。

Google Workspace の操作はすべて gws CLI で行う。認証情報はこのスクリプトでは扱わない (gws が管理する)。
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

SKILL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REPO_ROOT = os.path.normpath(os.path.join(SKILL_DIR, "..", "..", ".."))
INPUT_DIR = os.path.join(REPO_ROOT, "input", os.path.basename(SKILL_DIR))  # 入力を置く (Git 管理対象外)
INPUT_REL = os.path.relpath(INPUT_DIR, REPO_ROOT) + "/"
INPUT_EXTS = (".yaml", ".yml")
OUTPUT_FOLDERS = ["gws-skills", os.path.basename(SKILL_DIR)]  # 既定の保存先 (マイドライブからのパス)
DEFAULT_FOLDER = "マイドライブ/" + "/".join(OUTPUT_FOLDERS)
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
FILE_SUFFIX = "パラメータシート"  # ファイル名は yyyymmdd_<タイトル>_パラメータシート
MAX_BODY = 500_000  # バイト / 1 回の batchUpdate の JSON の大きさ。コマンドの引数で渡すので、macOS の引数と環境変数の合計の上限 (1 MiB) の半分にする。小さくすると書き込みの回数の上限 (1 分あたり) に当たる


def rgb(hex_):
    return {k: int(hex_[i:i + 2], 16) / 255 for k, i in (("red", 0), ("green", 2), ("blue", 4))}


# 色はグレーを基本にし、親の行 (青みのある灰色) とパラメータの値 (薄い黄色) だけ色を付けて見分けやすくする。
# 罫線は淡くし、文字と地の色で構造を見せる
TEXT_COLOR = rgb("1F2329")  # 値が URL に見えても、リンクの色・下線にしない
HEADER_BG = rgb("F1F3F5")  # 表の見出しの行
HEADER_TEXT = rgb("3C4043")
HEADER_LINE = rgb("80868B")  # 表の見出しの下の線
DOC_BG = rgb("2F3437")  # リソース (ドキュメント) の見出しの行
DOC_TEXT = rgb("FFFFFF")  # kind
DOC_SUBTEXT = rgb("B8C0C8")  # 名前
PARENT_BG = rgb("DFE6EE")  # 子を持つ項目 (パラメータの無い行) の地。1 階層目
PARENT_BG_SUB = rgb("EDF1F6")  # 2 階層目より下
PARENT_TEXT = rgb("24364B")  # 子を持つ項目の文字
VALUE_BG = rgb("FFF8DC")  # パラメータの値のセルの地
NOTE_TEXT = rgb("5F6368")  # 備考は値より控えめにする
LINE = rgb("D5DAE0")
INDEX_COLOR = rgb("9AA0A6")  # No は目立たせない
PAD = 6  # px / セルの左右の余白
DOC_PAD = 10  # px / リソースの見出しの左の余白
ROW_HEIGHT = 26  # px / 1 行に収まる行の高さ
HEADER_HEIGHT = 30  # px / 表の見出しの行の高さ
DOC_HEIGHT = 32  # px / リソースの見出しの行の高さ
TITLE_SIZE = 16  # 目次のタイトルの文字の大きさ
TITLE_HEIGHT = 40  # px / 目次のタイトルの行の高さ
LINK_COLOR = rgb("1A5FB4")  # 目次のリンク
SPACE_HEIGHT = 20  # px / リソースの間の空きの行の高さ
VALUE_FONT = "Roboto Mono"  # 値は等幅にする (0 と O、空白を見分けやすい)


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


def batch_update(sheet_id, requests):
    # リクエストは先頭から順に適用されるので、分割しても結果は変わらない
    chunk, size = [], 0
    for r in requests:
        n = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
        if chunk and size + n > MAX_BODY:
            gws("sheets", "spreadsheets", "batchUpdate", params={"spreadsheetId": sheet_id}, body={"requests": chunk})
            chunk, size = [], 0
        chunk.append(r)
        size += n
    if chunk:
        gws("sheets", "spreadsheets", "batchUpdate", params={"spreadsheetId": sheet_id}, body={"requests": chunk})


# ------------------------------------------------------------------------ YAML

class YamlError(Exception):
    """(行番号, 内容)"""


def indent_of(line):
    return len(line) - len(line.lstrip(" "))


def is_wide(ch):
    """全角の文字 (日本語など) か。"""
    return ord(ch) > 0x2E7F


def strip_comment(line):
    """行を (本体, コメント) に分ける。引用符の中の # はコメントにしない。コメントが無ければ None。"""
    quote, i = None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 1
            elif ch == quote:
                if quote == "'" and line[i + 1:i + 2] == "'":
                    i += 1
                else:
                    quote = None
        elif ch in "'\"" and (i == 0 or line[i - 1] in " \t[{,"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip(), line[i + 1:].strip()
        i += 1
    return line.rstrip(), None


ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
           "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0"}


def unquote(s):
    """引用符で囲んだ文字列の中身。囲みが閉じていないか、後に文字があれば ValueError。"""
    end = quoted_end(s, 0)
    if end < 0:
        raise ValueError("引用符が閉じていません (複数行にまたがる引用符の文字列は扱えません)")
    if end != len(s):
        raise ValueError("引用符の後に文字があります")
    inner = s[1:-1]
    if s[0] == "'":
        return inner.replace("''", "'")

    def esc(m):
        c = m.group(1)
        if c[0] in "xuU" and len(c) > 1:
            return chr(int(c[1:], 16))
        if c in ESCAPES:
            return ESCAPES[c]
        raise ValueError(f"扱えないエスケープ \\{c} があります")
    return re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", esc, inner)


def quoted_end(s, i):
    """s[i] から始まる引用符の文字列の終わり (閉じの引用符の次)。閉じていなければ -1。"""
    q, j = s[i], i + 1
    while j < len(s):
        if q == '"' and s[j] == "\\":
            j += 2
            continue
        if s[j] == q:
            if q == "'" and s[j + 1:j + 2] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return -1


def split_key(body):
    """"キー: 値" の行なら (キー, 値の部分) を返す。そうでなければ None。"""
    if body[0] in "'\"":
        end = quoted_end(body, 0)
        if end < 0:
            return None
        m = re.match(r"\s*:(\s|$)", body[end:])
        return (unquote(body[:end]), body[end + m.end():].strip()) if m else None
    if is_seq(body) or body[0] in "[{?&*!|>%@`":
        return None
    m = re.search(r":(\s|$)", body)
    return (body[:m.start()].strip(), body[m.end():].strip()) if m else None


def is_seq(body):
    return body == "-" or body.startswith("- ")


def scalar(text):
    return ("scalar", text)


class Parser:
    """インデントで入れ子を読む。ノードは ("map", [(キー, ノード, 備考)]) / ("seq", [(ノード, 備考)]) / ("scalar", 文字列)。"""

    def __init__(self, text):
        self.lines = text.splitlines()
        self.i = 0
        self.pending = []  # 内容のある行より前のコメント行 (空の行で捨てる)
        self.content = False  # このドキュメントの内容のある行を読んだか
        self.boundary = False  # 次のドキュメントの --- の手前で止まっているか

    def err(self, msg, line=None):
        raise YamlError(line or min(self.i + 1, len(self.lines)), msg)

    def peek(self):
        """次の内容のある行まで進めて、(字下げ, 本体, 行末のコメント) を返す。無いか、次のドキュメントの --- なら None。"""
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if re.match(r" *\t", raw):
                self.err("字下げにタブがあります (空白で字下げしてください)")
            body, comment = strip_comment(raw)
            if not body.strip():
                # ドキュメントの最初の内容より前のコメントは、ファイルの説明や注記なので使わない
                if self.content and comment is None:
                    self.pending = []
                elif self.content and comment:
                    self.pending.append(comment)
                self.i += 1
                continue
            if indent_of(raw) == 0 and body in ("---", "...") or (not self.content and body.startswith("%")):
                if body == "...":
                    rest = [ln for ln in self.lines[self.i + 1:] if strip_comment(ln)[0].strip()]
                    if rest:
                        self.err("... の後にも内容があります")
                    self.i = len(self.lines)
                    return None
                if body == "---" and self.content:
                    self.boundary = True
                    return None
                self.i += 1
                continue
            if body.startswith("--- "):
                self.err("--- と同じ行の値は扱えません")
            if body.strip() == "?" or body.lstrip().startswith("? "):
                self.err("? のキーは扱えません")
            self.content = True
            return indent_of(raw), body.strip(), comment
        return None

    def next_document(self):
        """--- の手前で止まっていれば、次のドキュメントへ進めて True を返す。"""
        if not self.boundary:
            return False
        self.i += 1
        self.pending, self.content, self.boundary = [], False, False
        return True

    def take_note(self, comment):
        notes = self.pending + ([comment] if comment else [])
        self.pending = []
        return "\n".join(notes)

    def node(self, min_indent):
        """min_indent 以上の字下げで始まるノード。無ければ None。"""
        p = self.peek()
        if p is None or p[0] < min_indent:
            return None
        indent, body, _ = p
        if is_seq(body):
            return self.seq(indent)
        if split_key(body):
            return self.map(indent)
        self.pending = []
        self.i += 1
        return self.value(body, indent - 1)

    def map(self, indent):
        entries, seen = [], set()
        while True:
            p = self.peek()
            if p is None or p[0] < indent:
                break
            ind, body, comment = p
            if ind > indent:
                self.err("字下げがそろっていません")
            kv = split_key(body)
            if not kv:
                if is_seq(body):
                    break  # キーと同じ字下げのリストは、そのキーの値 (value で読む)。ここに来るのは誤り
                self.err("「キー: 値」の形ではありません")
            key, rest = kv
            if key in seen:
                self.err(f"キー {key} が重なっています")
            seen.add(key)
            note = self.take_note(comment)
            self.i += 1
            entries.append((key, self.value(rest, indent, in_map=True), note))
        return ("map", entries)

    def seq(self, indent):
        items = []
        while True:
            p = self.peek()
            if p is None or p[0] < indent:
                break
            ind, body, comment = p
            if ind > indent:
                self.err("字下げがそろっていません")
            if not is_seq(body):
                break
            raw = self.lines[self.i]
            if body == "-":
                note = self.take_note(comment)
                self.i += 1
                items.append((self.node(indent + 1) or scalar(""), note))
                continue
            # "- " を空白に置き換えて、その位置から始まるノードとして読む (- name: x の入れ子もこれで読める)
            m = re.match(r" *- +", raw)
            self.lines[self.i] = " " * m.end() + raw[m.end():]
            ind2, body2, comment2 = self.peek()
            if is_seq(body2) or split_key(body2):
                items.append((self.node(ind2), ""))  # 直前のコメントは中の最初のキーの備考にする
            else:
                note = self.take_note(comment2)
                self.i += 1
                items.append((self.value(body2, indent), note))
        return ("seq", items)

    def value(self, rest, indent, in_map=False):
        """キーや - の後の値。rest はその行の残り、indent は親の字下げ (続きの行はこれより深い)。"""
        line = self.i  # 値が始まる行 (1 始まり)
        if rest == "":
            p = self.peek()
            if in_map and p and p[0] == indent and is_seq(p[1]):
                return self.seq(indent)
            return self.node(indent + 1) or scalar("")
        if rest[0] in "&*!":
            self.err("アンカー (&)・エイリアス (*)・タグ (!) は扱えません", line)
        if rest[0] in "|>":
            return self.block(rest, indent, line)
        if rest[0] in "[{":
            return self.flow(rest, indent, line)
        if rest[0] in "'\"":
            try:
                return scalar(unquote(rest))
            except ValueError as e:
                self.err(str(e), line)
        parts = [rest]  # 素の文字列は、より深い字下げの行に続けて書ける
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            body, _ = strip_comment(raw)
            if not body.strip() or indent_of(raw) <= indent:
                break
            if split_key(body.strip()) or is_seq(body.strip()):
                self.err("値の後の行の字下げが深すぎます", self.i + 1)
            parts.append(body.strip())
            self.i += 1
        return scalar(join_lines(parts))

    def block(self, header, indent, line):
        m = re.fullmatch(r"([|>])([+-]?)([1-9]?)([+-]?)", header)
        if not m:
            self.err(f"{header[0]} の後に扱えない文字があります", line)
        width = int(m.group(3)) if m.group(3) else None
        lines, block_indent = [], (indent + width) if width else None
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip():
                if indent_of(raw) <= indent or (block_indent is not None and indent_of(raw) < block_indent):
                    break
                block_indent = block_indent or indent_of(raw)
                lines.append(raw[block_indent:])
            else:
                lines.append("")
            self.i += 1
        while lines and not lines[-1]:  # 末尾の改行はセルに要らないので、chomping (+ / -) によらず取る
            lines.pop()
        if m.group(1) == "|":
            return scalar("\n".join(lines))
        out, prev = "", None  # > は行をつなぐ。空の行は改行にし、深く字下げした行の前後は改行のまま
        for ln in lines:
            if prev is None:
                out = ln
            elif not ln:
                out += "\n"
            elif not prev:
                out += ln
            elif ln[0] == " " or prev[0] == " ":
                out += "\n" + ln
            else:
                out += " " + ln
            prev = ln
        return scalar(out)

    def flow(self, rest, indent, line):
        text = rest
        while not flow_closed(text):
            if self.i >= len(self.lines) or (self.lines[self.i].strip() and indent_of(self.lines[self.i]) <= indent):
                self.err("[ か { が閉じていません", line)
            text += " " + strip_comment(self.lines[self.i])[0].strip()
            self.i += 1
        try:
            node, pos = parse_flow(text, 0)
        except ValueError as e:
            self.err(str(e), line)
        if text[pos:].strip():
            self.err("] か } の後に文字があります", line)
        return node


def flow_closed(text):
    depth, i = 0, 0
    while i < len(text):
        ch = text[i]
        if ch in "'\"":
            end = quoted_end(text, i)
            if end < 0:
                return False
            i = end
            continue
        depth += (ch in "[{") - (ch in "]}")
        i += 1
    return depth <= 0


def parse_flow(s, i):
    """[...] / {...} / 値 を読み、(ノード, 次の位置) を返す。"""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        raise ValueError("[ か { の中が途中で終わっています")
    if s[i] in "[{":
        close, items, i = "]" if s[i] == "[" else "}", [], i + 1
        while True:
            while i < len(s) and s[i] == " ":
                i += 1
            if i < len(s) and s[i] == close:
                return (("seq", items) if close == "]" else ("map", items)), i + 1
            if close == "]":
                node, i = parse_flow(s, i)
                items.append((node, ""))
            else:
                key, i = flow_scalar(s, i, ",}:")
                while i < len(s) and s[i] == " ":
                    i += 1
                node = scalar("")
                if i < len(s) and s[i] == ":":
                    node, i = parse_flow(s, i + 1)
                if any(k == key[1] for k, _, _ in items):
                    raise ValueError(f"キー {key[1]} が重なっています")
                items.append((key[1], node, ""))
            while i < len(s) and s[i] == " ":
                i += 1
            if i < len(s) and s[i] == ",":
                i += 1
            elif not (i < len(s) and s[i] == close):
                raise ValueError(f"{close} が足りないか、区切りの , がありません")
    return flow_scalar(s, i, ",]}")


def flow_scalar(s, i, stops):
    """引用符か素の値を読む。素の値は stops の文字で終わる (: は後ろが空白・区切り・終わりのときだけ。http://… は切らない)。"""
    if s[i] in "&*!":
        raise ValueError("アンカー (&)・エイリアス (*)・タグ (!) は扱えません")
    if s[i] in "'\"":
        end = quoted_end(s, i)
        if end < 0:
            raise ValueError("引用符が閉じていません")
        return scalar(unquote(s[i:end])), end
    j = i
    while j < len(s) and s[j] not in stops.replace(":", ""):
        if ":" in stops and s[j] == ":" and s[j + 1:j + 2] in ("", " ", ",", "}"):
            break
        j += 1
    return scalar(s[i:j].strip()), j


def join_lines(lines):
    """複数行の素の文字列をつなぐ。全角の文字どうしの間には空白を入れない。"""
    out = ""
    for ln in lines:
        if out and not (is_wide(out[-1]) and is_wide(ln[0])):
            out += " "
        out += ln
    return out


def parse_yaml(text):
    """ドキュメント (--- で区切ったもの) のノードの列を返す。中身の無いドキュメントは除く。"""
    p, docs = Parser(text), []
    try:
        while True:
            node = p.node(0)
            if p.peek() is not None:
                p.err("この行を読めません (字下げを確かめてください)")
            if node is not None:
                docs.append(node)
            if not p.next_document():
                return docs
    except ValueError as e:  # 引用符で囲んだキーの誤り (split_key の unquote から)
        raise YamlError(min(p.i + 1, len(p.lines)), str(e))


# ----------------------------------------------------------------------- sheet

def flatten(node):
    """ノードを行の列 [(階層, 項目, 値, 備考)] にする。項目は YAML と同じ書き方にする。

    キーは "キー:"。子を持つキーは、値を None にした親の行にし、子は 1 つ下の階層に置く。
    リストの要素は "[1]" "[2]" … とし、リストだとわかるようにする (要素がマッピングなら親の行にする)。
    """
    rows = []

    def add(level, label, child, note):
        kind, value = child
        if kind == "scalar":
            rows.append((level, label, value, note))
        elif not value:
            rows.append((level, label, "{}" if kind == "map" else "[]", note))
        else:
            rows.append((level, label, None, note))
            walk(child, level + 1)

    def walk(n, level):
        kind, children = n
        if kind == "map":
            for key, child, note in children:
                add(level, f"{key}:", child, note)
            return
        for i, (child, note) in enumerate(children, 1):
            add(level, f"[{i}]", child, note)

    walk(node, 0)
    return rows


def doc_label(node):
    """ドキュメントの見出し。kind があれば "kind / metadata.name" (Kubernetes のマニフェストなど)、無ければ None。"""
    if node[0] == "map":
        top = {key: child for key, child, _ in node[1]}
        kind, meta = top.get("kind", ("", "")), top.get("metadata", ("", []))
        name = next((c[1] for key, c, _ in meta[1] if key == "name" and c[0] == "scalar"), "") \
            if meta[0] == "map" else ""
        if kind[0] == "scalar" and kind[1]:
            return f"{kind[1]} / {name}" if name else kind[1]
    return None


def load_sheet(path):
    """入力を読んで {"name", "path", "blocks"} にする。読めなければ Fail (35)。

    blocks はドキュメントごとの {"label", "rows", "top"}。label は見出しの行の文字
    (kind があれば "kind / metadata.name"、無ければ複数のときだけ「ドキュメント N」、1 つなら None で見出しを入れない)。
    """
    rel = os.path.relpath(path, REPO_ROOT)
    hint = "内容をユーザーに伝え、YAML を直してもらってください"
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
    except UnicodeDecodeError:
        raise Fail(35, f"{rel} が UTF-8 ではありません", "UTF-8 で保存し直してもらってください")
    try:
        docs = parse_yaml(text)
    except YamlError as e:
        line, msg = e.args
        raise Fail(35, f"{rel}:{line}: {msg}", hint)
    if not docs:
        raise Fail(35, f"{rel} に中身がありません", hint)
    blocks = []
    for k, node in enumerate(docs, 1):
        where = f"{rel} の {k} つ目のドキュメント" if len(docs) > 1 else f"{rel} "
        if node[0] == "scalar":
            raise Fail(35, f"{where}のいちばん外側がマッピング (キー: 値) かリスト (- 値) ではありません", hint)
        if not node[1]:
            raise Fail(35, f"{where}に中身がありません", hint)
        blocks.append({"label": doc_label(node) or (f"ドキュメント {k}" if len(docs) > 1 else None),
                       "rows": flatten(node),
                       "top": [key for key, _, _ in node[1]] if node[0] == "map" else [f"[1]〜[{len(node[1])}]"]})
    return {"name": os.path.splitext(os.path.basename(path))[0], "path": path, "blocks": blocks}


def width_of(text):
    return max((sum(2 if is_wide(ch) else 1 for ch in ln) for ln in text.split("\n")), default=0)


LEVEL_WIDTH = 22  # px / 階層の列の幅 (最後の階層の列を除く)。キーは右の空いた列にはみ出して見える
LINE_STYLE = {"style": "SOLID", "colorStyle": {"rgbColor": LINE}}
BOX = {"top": LINE_STYLE, "bottom": LINE_STYLE, "left": LINE_STYLE, "right": LINE_STYLE}


def fmt(bold=False, color=None, bg=None, font=None, align=None, text=False, borders=BOX, wrap="WRAP", pad=PAD,
        size=None):
    """セルの書式。文字は上下中央にそろえ、左右に余白を取る。"""
    f = {"verticalAlignment": "MIDDLE", "wrapStrategy": wrap,
         "padding": {"left": pad, "right": PAD, "top": 2, "bottom": 2},
         "textFormat": {"bold": bold, "underline": False,
                        "foregroundColorStyle": {"rgbColor": color or TEXT_COLOR}}}
    if borders:
        f["borders"] = borders
    if bg:
        f["backgroundColorStyle"] = {"rgbColor": bg}
    if font:
        f["textFormat"]["fontFamily"] = font
    if size:
        f["textFormat"]["fontSize"] = size
    if align:
        f["horizontalAlignment"] = align
    if text:  # 値は書いたとおりに見せる (0755 や 1e3 を数に変えない)
        f["numberFormat"] = {"type": "TEXT"}
    return f


# 表の見出しは淡い地にし、下の線だけを濃くして表の始まりを示す
HEADER_FMT = fmt(bold=True, color=HEADER_TEXT, bg=HEADER_BG,
                 borders={**BOX, "bottom": {**LINE_STYLE, "colorStyle": {"rgbColor": HEADER_LINE}}})


def cell(value, f, runs=None, formula=False):
    c = {"userEnteredFormat": f}
    if value != "":  # 空のセルには値を入れない (左のセルの文字がはみ出せるように)
        kind = "formulaValue" if formula else "numberValue" if isinstance(value, int) else "stringValue"
        c["userEnteredValue"] = {kind: value}
    if runs:
        c["textFormatRuns"] = runs
    return c


def write_requests(sid, data, merges, heights, widths):
    """セルの書き込み・セルの結合・行の高さ・列幅のリクエスト列。"""
    def update(r0, r1):
        return {"updateCells": {"start": {"sheetId": sid, "rowIndex": r0, "columnIndex": 0}, "rows": data[r0:r1],
                                "fields": "userEnteredValue,userEnteredFormat,textFormatRuns"}}

    # updateCells 1 つが MAX_BODY を超えないように、行をまとめる
    reqs, start, size = [], 0, 0
    for r, row in enumerate(data):
        n = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        if r > start and size + n > MAX_BODY:
            reqs.append(update(start, r))
            start, size = r, 0
        size += n
    reqs.append(update(start, len(data)))
    reqs += [{"mergeCells": {"range": g, "mergeType": "MERGE_ALL"}} for g in merges]
    for dimension, sizes in (("ROWS", heights), ("COLUMNS", dict(enumerate(widths)))):
        reqs += [{"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": dimension, "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": px}, "fields": "pixelSize"}} for i, px in sizes.items()]
    return reqs


def depth_of(sheet):
    """項目の階層の数 (階層の列の数)。"""
    return max(level for b in sheet["blocks"] for level, _, _, _ in b["rows"]) + 1


def layout(sheet):
    """シートの行の列。各行は ("header",) / ("space",) / ("doc", 見出し) / ("item", No か None, 階層, 項目, 値, 備考)。

    ドキュメントの間は空きの行で区切る。No はパラメータ (値のある行) にだけ通しで振る。
    """
    rows, n = [("header",)], 0
    for k, b in enumerate(sheet["blocks"]):
        if b["label"] is not None:
            if k:
                rows.append(("space",))
            rows.append(("doc", b["label"]))
        for level, label, value, note in b["rows"]:
            if value is not None:
                n += 1
            rows.append(("item", n if value is not None else None, level, label, value, note))
    return rows


def sheet_requests(sheet, sid):
    """書き込みと書式のリクエスト列 (シートの追加・名前の変更は含まない)。

    列は No / 項目 (階層ごとの細い列) / 値 / 備考。キーは階層の列に書き、右の空いた列にはみ出させる。
    項目の列の間には縦の線を引かず、1 つの列が字下げされているように見せる。
    """
    depth = depth_of(sheet)
    val_col, ncols = depth + 1, depth + 3
    rows = layout(sheet)
    items = [row for row in rows if row[0] == "item"]

    # 列幅: 最後の階層の列は、いちばん長いキーが入る幅にする (それより左の階層の列の幅を引く)。
    # 値と備考は、いちばん長い文字列に合わせる (極端に狭く・広くならないように丸める)
    item_w = max(width_of(label) * 8 + 2 * PAD - (depth - 1 - level) * LEVEL_WIDTH
                 for _, _, level, label, _, _ in items)
    val_w = max(width_of(value or "") * 8 + 2 * PAD for *_, value, _ in items)
    note_w = max(width_of(note) * 7 + 2 * PAD for *_, note in items)
    widths = [44] + [LEVEL_WIDTH] * (depth - 1) + [min(max(item_w, 100), 360), min(max(val_w, 180), 440),
                                                   min(max(note_w, 200), 360)]

    line = LINE_STYLE

    def grid(r0, r1, c0, c1):
        return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}

    def one_line(value, note):
        """値と備考が 1 行に収まるか (収まる行だけ高さを決める。収まらない行は折り返して高くなる)。"""
        return "\n" not in value + note and width_of(value) * 8 + 2 * PAD <= widths[val_col] \
            and width_of(note) * 7 + 2 * PAD <= widths[val_col + 1]

    data, heights, merges = [], {}, []
    for r, row in enumerate(rows):
        if row[0] == "header":
            f = HEADER_FMT
            vals = [cell("No", {**f, "horizontalAlignment": "CENTER"}), cell("項目", f)] \
                + [cell("", f) for _ in range(depth - 1)] + [cell("値", f), cell("備考", f)]
            if depth > 1:  # 「項目」を階層の列にまたがらせる (列が 1 つなら結合しない)
                merges.append(grid(r, r + 1, 1, val_col))
            heights[r] = HEADER_HEIGHT
        elif row[0] == "space":
            vals = [cell("", fmt(borders=None)) for _ in range(ncols)]
            heights[r] = SPACE_HEIGHT
        elif row[0] == "doc":
            # リソースの見出しは、行をつなげて濃い地にする。kind は太字の白、名前は細い淡い色にする
            kind, sep, name = row[1].partition(" / ")
            f = fmt(bold=True, color=DOC_TEXT, bg=DOC_BG, borders=None, pad=DOC_PAD)
            runs = [{"startIndex": 0, "format": {"bold": True}}]
            if sep:
                runs.append({"startIndex": len(kind.encode("utf-16-le")) // 2,
                             "format": {"bold": False, "foregroundColorStyle": {"rgbColor": DOC_SUBTEXT}}})
            vals = [cell(row[1], f, runs)] + [cell("", f) for _ in range(ncols - 1)]
            heights[r] = DOC_HEIGHT
            merges.append(grid(r, r + 1, 0, ncols))
        else:
            _, no, level, label, value, note = row
            # 親の行は行全体に地を付け (1 階層目は濃く、それより下は淡く)、パラメータの行は白地で値のセルだけ黄色にする
            parent = value is None
            bg = (PARENT_BG if level == 0 else PARENT_BG_SUB) if parent else None
            vals = [cell(no if no is not None else "", fmt(align="CENTER", color=INDEX_COLOR, bg=bg))]
            for c in range(depth):
                sides = {"top": line, "bottom": line, **({"left": line} if c == 0 else {}),
                         **({"right": line} if c == depth - 1 else {})}
                vals.append(cell(label if c == level else "",
                                 fmt(bold=parent, color=PARENT_TEXT if parent else None, bg=bg, borders=sides,
                                     wrap="OVERFLOW_CELL")))
            vals += [cell("" if parent else value, fmt(font=VALUE_FONT, text=True, bg=bg or VALUE_BG)),
                     cell(note, fmt(bg=bg, color=NOTE_TEXT))]
            if one_line(value or "", note):
                heights[r] = ROW_HEIGHT
        data.append({"values": vals})
    return write_requests(sid, data, merges, heights, widths)


def sheet_properties(sheet, sid, index):
    return {"sheetId": sid, "title": sheet["name"], "index": index,
            "gridProperties": {"rowCount": len(layout(sheet)), "columnCount": depth_of(sheet) + 3,
                               "frozenRowCount": 1, "hideGridlines": True}}


TOC_NAME = "目次"
TOC_HEAD = 3  # 目次の表の見出しの行 (タイトル・概要・空きの行の後)


def toc_rows(sheets):
    """目次の表の行 [(シート, リソース, パラメータ数, 移る先の行)]。リソースの見出しが無いシートは 1 行にまとめる。"""
    out = []
    for s in sheets:
        heads = [r for r, row in enumerate(layout(s)) if row[0] == "doc"]
        for k, b in enumerate(s["blocks"]):
            params = sum(1 for row in b["rows"] if row[2] is not None)
            if b["label"] is None:
                top = ", ".join(b["top"][:4]) + (" …" if len(b["top"]) > 4 else "")
                out.append((s, top, params, 0))
            else:
                out.append((s, b["label"], params, heads[k]))
    return out


def toc_requests(title, sheets, sids, sid):
    """目次のシートのリクエスト列。sids は各シートの sheetId。

    タイトルと概要の下に、シート / リソース / パラメータ数 の表を置く。リソースは、そのシートのその見出しへのリンクにする。
    """
    rows = toc_rows(sheets)
    total = sum(params for *_, params, _ in rows)
    summary = (f"作成日 {datetime.date.today():%Y-%m-%d}　・　シート {len(sheets)}　・　"
               f"リソース {len(rows)}　・　パラメータ {total}")
    plain = fmt(borders=None)
    # タイトルと概要は、右の空いたセルにはみ出させる
    data = [{"values": [cell(title, fmt(bold=True, borders=None, size=TITLE_SIZE, wrap="OVERFLOW_CELL"))]
             + [cell("", plain)] * 3},
            {"values": [cell(summary, fmt(color=NOTE_TEXT, borders=None, wrap="OVERFLOW_CELL"))]
             + [cell("", plain)] * 3},
            {"values": [cell("", plain)] * 4},
            {"values": [cell("No", {**HEADER_FMT, "horizontalAlignment": "CENTER"}), cell("シート", HEADER_FMT),
                        cell("リソース", HEADER_FMT),
                        cell("パラメータ数", {**HEADER_FMT, "horizontalAlignment": "RIGHT"})]}]
    prev = None
    for n, (s, label, params, row) in enumerate(rows, 1):
        # シートの名前は、そのシートの最初の行にだけ書く。シートの間にだけ上の線を引いて、まとまりに見せる
        first = s is not prev
        sheet_sides = {"left": LINE_STYLE, "right": LINE_STYLE, **({"top": LINE_STYLE} if first else {}),
                       **({"bottom": LINE_STYLE} if n == len(rows) or rows[n][0] is not s else {})}
        url = f"#gid={sids[sheets.index(s)]}&range=A{row + 1}"
        link = '=HYPERLINK("{}","{}")'.format(url, label.replace('"', '""'))
        data.append({"values": [
            cell(n, fmt(align="CENTER", color=INDEX_COLOR)),
            cell(s["name"] if first else "", fmt(bold=True, borders=sheet_sides)),
            cell(link, fmt(color=LINK_COLOR), formula=True),
            cell(params, fmt(align="RIGHT")),
        ]})
        prev = s
    heights = {0: TITLE_HEIGHT, 1: ROW_HEIGHT, 2: SPACE_HEIGHT, TOC_HEAD: HEADER_HEIGHT,
               **{TOC_HEAD + 1 + i: ROW_HEIGHT for i in range(len(rows))}}
    name_w = max(width_of(s["name"]) * 8 + 2 * PAD for s in sheets)
    label_w = max(width_of(label) * 7 + 2 * PAD for _, label, _, _ in rows)
    widths = [44, min(max(name_w, 140), 300), min(max(label_w, 280), 480), 110]
    return write_requests(sid, data, [], heights, widths)


def toc_properties(sheets, sid):
    return {"sheetId": sid, "title": TOC_NAME, "index": 0,
            "gridProperties": {"rowCount": TOC_HEAD + 1 + len(toc_rows(sheets)), "columnCount": 4,
                               "frozenRowCount": TOC_HEAD + 1, "hideGridlines": True}}


# ------------------------------------------------------------------------ main

def resolve_input(name):
    """入力のフォルダと、その中の YAML のパスの列。

    フォルダを省略したら INPUT_DIR の下のフォルダ (1 つだけのとき)、名前だけなら INPUT_DIR から探す。
    YAML はフォルダの直下のものを名前の順に使う (サブフォルダの中は使わない)。
    """
    if not name:
        found = sorted(d for d in os.listdir(INPUT_DIR) if os.path.isdir(os.path.join(INPUT_DIR, d))
                       and not d.startswith(".")) if os.path.isdir(INPUT_DIR) else []
        if not found:
            raise Fail(30, f"{INPUT_REL} に入力のフォルダがありません",
                       f"パラメータシートにする YAML をフォルダに入れて、{INPUT_REL} に置いてもらってください")
        if len(found) > 1:
            raise Fail(30, f"{INPUT_REL} に入力のフォルダが複数あります: " + ", ".join(found),
                       "どれを使うかユーザーに聞き、フォルダの名前を渡してください")
        folder = os.path.join(INPUT_DIR, found[0])
    else:
        folder = next((p for p in (name, os.path.join(INPUT_DIR, name)) if os.path.isdir(p)), None)
        if not folder:
            raise Fail(30, f"入力のフォルダ {name} がありません",
                       f"{INPUT_REL} に置いたフォルダの名前を渡してください")
    folder = os.path.normpath(folder)
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(INPUT_EXTS) and not f.startswith(".")
                   and os.path.isfile(os.path.join(folder, f)))
    if not files:
        raise Fail(30, f"{os.path.relpath(folder, REPO_ROOT)}/ に YAML (.yaml / .yml) がありません",
                   "パラメータシートにする YAML をフォルダに入れてもらってください")
    return folder, [os.path.join(folder, f) for f in files]


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

    (アカウント, 入力のフォルダ, シートの列, 保存先) を返す。保存先は --folder の (ID, 名前) か None (既定)。
    """
    account = require_auth()
    source, paths = resolve_input(args.input)
    names = [TOC_NAME] + [os.path.splitext(os.path.basename(p))[0] for p in paths]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise Fail(30, f"シート名 (拡張子を除いたファイル名と「{TOC_NAME}」) が重なります: " + ", ".join(dup),
                   "内容をユーザーに伝え、ファイル名を変えてもらってください")
    sheets = [load_sheet(p) for p in paths]
    folder = check_folder(args.folder) if args.folder else None
    return account, source, sheets, folder


def file_name(args, source):
    return f"{datetime.date.today():%Y%m%d}_{args.name or os.path.basename(source)}_{FILE_SUFFIX}"


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
    account, source, sheets, folder = prepare(args)
    if folder:
        where = f"{folder[1]} ({folder_url(folder[0])})"
    else:
        fid = output_folder(create=False)
        where = DEFAULT_FOLDER + (f" ({folder_url(fid)})" if fid else " (無いので作る)")
    print(f"アカウント: {account}")
    print(f"入力:       {os.path.relpath(source, REPO_ROOT)}/ ({', '.join(os.path.basename(s['path']) for s in sheets)})")
    print(f"ファイル名: {file_name(args, source)}")
    print(f"保存先:     {where}")
    print("シート:")
    print(f"  {TOC_NAME}: シートとリソースの一覧 (各リソースへのリンク)")
    for s in sheets:
        blocks = s["blocks"]
        params = sum(1 for b in blocks for row in b["rows"] if row[2] is not None)
        docs = f"{len(blocks)} ドキュメント、" if len(blocks) > 1 else ""
        top = blocks[0]["top"]
        keys = "" if blocks[0]["label"] is not None else \
            " (" + ", ".join(top[:8]) + (f" ほか {len(top) - 8} 個" if len(top) > 8 else "") + ")"
        print(f"  {s['name']}: {docs}パラメータ {params} 個{keys}")
        for b in blocks:
            if b["label"] is not None:
                print(f"    - {b['label']}")
    print(f"次: 内容の確認を得てから create {shlex.quote(os.path.basename(source))} (plan と同じオプション)")
    return 0


def cmd_create(args):
    _, source, sheets, folder = prepare(args)
    fid, fname = folder or (output_folder(create=True), DEFAULT_FOLDER)
    created = gws("drive", "files", "create", params={"fields": "id,name,webViewLink", "supportsAllDrives": True},
                  body={"name": file_name(args, source), "parents": [fid], "mimeType": SHEET_MIME})
    sheet_id = created["id"]
    print(f"[OK] スプレッドシートを作成: {fname}/{created.get('name')}", file=sys.stderr)

    # 最初からあるシートを目次にし、入力のシートはその後ろに足す
    first = gws("sheets", "spreadsheets", "get", params={"spreadsheetId": sheet_id,
                                                          "fields": "sheets.properties.sheetId"})
    toc_sid = first["sheets"][0]["properties"]["sheetId"]
    sids = [toc_sid + k + 1 for k in range(len(sheets))]
    reqs = [{"updateSheetProperties": {
        "properties": toc_properties(sheets, toc_sid),
        "fields": "title,index,gridProperties.rowCount,gridProperties.columnCount,"
                  "gridProperties.frozenRowCount,gridProperties.hideGridlines"}}]
    reqs += [{"addSheet": {"properties": sheet_properties(s, sid, k + 1)}} for k, (s, sid) in enumerate(zip(sheets, sids))]
    reqs += toc_requests(args.name or os.path.basename(source), sheets, sids, toc_sid)
    for s, sid in zip(sheets, sids):
        reqs += sheet_requests(s, sid)
    batch_update(sheet_id, reqs)
    print("[OK] シートを書き込みました", file=sys.stderr)

    url = created.get("webViewLink") or f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    print(json.dumps({"spreadsheetId": sheet_id, "title": created.get("name"), "url": url,
                      "folder": fname, "folderUrl": folder_url(fid),
                      "sheets": [TOC_NAME] + [s["name"] for s in sheets]}, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    opts = argparse.ArgumentParser(add_help=False)
    opts.add_argument("input", nargs="?", help="input/gws-yaml2gsheet/ に置いたフォルダ。省略するとそこの 1 つを使う")
    opts.add_argument("--name", help="ファイル名 yyyymmdd_<タイトル>_パラメータシート のタイトル (既定: 入力のフォルダの名前)")
    opts.add_argument("--folder", help="保存先のフォルダの URL か ID (既定: マイドライブ/gws-skills/gws-yaml2gsheet)")
    sub.add_parser("plan", parents=[opts], help="作る内容を表示する (読み取りのみ)")
    sub.add_parser("create", parents=[opts], help="スプレッドシートを作って書き込む")
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
