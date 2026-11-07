#!/usr/bin/env python3
"""会議の名前から、その打ち合わせの過去の経緯 (関連する予定と資料) を集める

gws-meeting-history スキルが find → collect (→ doc) → clean の順で使う。Google Workspace は読むだけで、何も作らない。
collect と doc は、顧客の情報を含む大きな結果を .tmp/gws-meeting-history/ のファイルに書き、標準出力にはその場所と大きさだけを出す
(会話の外に結果の写しを残さないため)。結果を示したら clean で消す。

使い方:
  meeting_history.py find <会議名>
      主カレンダー (今日の 90 日前から 60 日後まで) から会議名に当たる打ち合わせを決めて、次回 (明日以降の最初の同じ名前の回)、
      今日までの最後の回 (説明・資料)、それより前の回と、キーワードの候補を表示する
  meeting_history.py collect <会議名> --keyword <語> [--keyword <語> ...] [--days N] [--doc-budget N] [--max-chars N]
      今日の N 日前 (既定 90) から今日までの「関連する予定」(同じ名前・タイトルにキーワードを含む・同じ資料を使う) と、
      明日から 60 日後までの関連する予定を一覧にする (繰り返しの予定は 1 行にまとめる)。
      中身は、次回と、資料のある直近 2 回の分だけを読む: 予定の説明 (合わせて 2000 字まで) と、
      資料 (ドキュメント・スライド) の本文 (会議のメモ → ほかのドキュメント → スライドの順。
      合わせて --doc-budget 字 (既定 10000)・1 つ --max-chars 字 (既定 4000)・1 つ 7 秒まで)。それより前の資料は読まない。
      最後に、ドライブで名前にキーワードを含むファイルを候補として書く (本文は読まない)。
      キーワードは大文字・小文字と全角・半角を区別しない。「A+B」と書くと、A と B の両方を含むものにする
      書き先: .tmp/gws-meeting-history/collect.txt (毎回上書きする)
  meeting_history.py doc <ファイルの ID か URL> [--max-chars N]
      ドキュメント・スライドの本文をテキストで書く。書き先: .tmp/gws-meeting-history/doc-<ID>.txt
  meeting_history.py clean
      .tmp/gws-meeting-history/ の中のファイルをすべて消す (collect・doc の結果を示し終えたら、必ず実行する)
打ち合わせ (参加者のいる予定) だけを使う。フォーカスタイム・勤務場所・不在・参加者のいない予定は使わない。
自分の関わり: 主催 / 参加 (承諾) / 仮承諾 / 未回答 / 辞退 / 招待なし (自分が参加者にいない)
予定の説明のうち、会議に入るための情報は書かない: ドキュメント・スライド・スプレッドシート・ドライブ以外の URL と、
  電話番号らしい数字 (区切りを含めて 9 桁以上) はその部分を除き、ID・パスワード・アクセスコード・参加コード・暗証番号・PIN の行は除く。
主催者は表示名だけを書く (メールアドレスは書かない)。結果のディレクトリは 0700、ファイルは 0600 で作る。
終了コード: 10=gws が無い 20=未認証/認証切れ 30=打ち合わせが決まらない (見つからない・名前が複数の打ち合わせに当てはまる)
  40=ファイルを読めない (無い・権限が無い・テキストにできない種類) 2=引数の誤り 1=そのほかの API の失敗

Google Workspace の操作はすべて gws CLI で行い、決まった読むだけの操作 (READ_CALLS) しか実行しない。認証情報はこのスクリプトでは扱わない (gws が管理する)。
"""
import argparse
import concurrent.futures
import contextlib
import datetime
import html
import io
import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata

SKILL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
REPO_ROOT = os.path.normpath(os.path.join(SKILL_DIR, "..", "..", ".."))
WORK_DIR = os.path.join(REPO_ROOT, ".tmp", os.path.basename(SKILL_DIR))  # 結果を書く (Git 管理対象外。clean で消す)
WORK_REL = os.path.relpath(WORK_DIR, REPO_ROOT) + "/"
READ_CALLS = {  # gws で使ってよい (サービス, 資源, 操作) の組 (読むだけ)
    ("drive", "about", "get"), ("drive", "files", "get"), ("drive", "files", "list"),
    ("calendar", "events", "list"), ("docs", "documents", "get"), ("slides", "presentations", "get"),
}
GWS_ERR_LINES = 3  # gws が失敗したときに出す stderr の行数
# collect で取得する量。目的は要約なので、中身は直近の分だけを読む (深掘りは doc や --days で別に行う)
RECENT = 2  # 説明と資料の本文を読む、直近の関連する予定の数 (それより前は一覧だけ)
READ_WORKERS = 8  # 資料の本文を同時に読む数
DOC_TIMEOUT = 7  # 秒 / collect で資料 1 つの本文を読む時間の上限。ふつうの資料は 2〜4 秒、大きなスライドは同時に読むと 6〜7 秒ほど。
#                  記録がたまり続けた大きなドキュメント (十数秒以上かかる) は打ち切り、要約では読まない
DOC_CMD_TIMEOUT = 90  # 秒 / doc で本文を読む時間の上限 (頼まれて読むので長めにする)
DOC_MIN = 500  # 字 / 本文の上限の残りがこれより少なければ、途中で切れる資料は載せない (見出しと数行だけになるため)
DESC_BUDGET = 2000  # 字 / 予定の説明の合計の上限
REPEAT_MIN = 3  # 同じ名前の予定がこの回数以上あれば、一覧では 1 行にまとめる
# 予定の一覧はカレンダーを読んだ結果を並べるだけで時間がかからないので、多めに出す
SINGLES_MAX = 30  # 今日までの一覧に出す、繰り返しでない予定の数 (新しいもの)
UPCOMING_MAX = 30  # これからの一覧に出す予定の数 (同じ名前の予定は 1 行)
LIST_MAX = 5  # find の「それより前の回」と collect の「関連が薄いかもしれない予定」の行数の上限
NOTE_RE = re.compile(r"gemini|メモ|議事|notes|minutes|memo|agenda|アジェンダ|transcript|文字起こし|recap|(?<![a-z])mom(?![a-z])", re.I)  # 会議のメモらしい資料の名前 (先に読む)
FIND_PAST_DAYS = 90  # find で対象を探す範囲 (今日の前)
FUTURE_DAYS = 60  # 今日より後に探す範囲 (find・collect で共通)
DESC_MAX = 1500  # 字 / 予定の説明の表示の上限
DRIVE_MAX = 5  # ドライブの候補の表示の上限
SKIP_TYPES = {"workingLocation", "outOfOffice", "focusTime"}  # 打ち合わせでない予定
WEEKDAYS = "月火水木金土日"

DOC_MIME = "application/vnd.google-apps.document"
SLIDES_MIME = "application/vnd.google-apps.presentation"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
KINDS = {DOC_MIME: "ドキュメント", SLIDES_MIME: "スライド", SHEET_MIME: "スプレッドシート"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # 資料の本文から除くメールアドレス
LINK_RE = re.compile(r"https://docs\.google\.com/(document|presentation|spreadsheets)/d/([\w-]{20,})")
LINK_MIMES = {"document": DOC_MIME, "presentation": SLIDES_MIME, "spreadsheets": SHEET_MIME}
# 予定の説明から除く、会議に入るための情報
# URL: ドキュメント・スライド・スプレッドシート・ドライブ以外の URL は、その部分を除く (同じ行の議題は残す)
URL_RE = re.compile(r"\b(?:https?|tel|sip):[A-Za-z0-9\-._~:/?#\[\]@!$&*+,;=%]+", re.I)
KEEP_URL_RE = re.compile(r"https://(?:docs\.google\.com/(?:document|presentation|spreadsheets)/|drive\.google\.com/)", re.I)
# 行: ID・パスワード・アクセスコード・参加コード・暗証番号・PIN の行は除く
SECRET_LINE_RE = re.compile(r"(?<![a-z])(?:id|pin|pwd|passcode|password|access\s*code)(?![a-z])"
                            r"|パスワード|パスコード|アクセスコード|参加コード|暗証番号", re.I)
# 電話番号らしい部分: 数字が区切り (空白・-・.・括弧) を含めて 9 桁以上続くもの (「工数+30h」のような短い数は残す)
PHONE_RE = re.compile(r"\+?\(?\d(?:[\s().-]{0,2}\d){8,}")
DATE_RE = re.compile(r"(?:19|20)\d\d[-.]\d{1,2}[-.]\d{1,2}(?!\d)")  # 電話番号として除かない日付 (2026-11-07 10:00 など)

# キーワードの候補から外す語 (打ち合わせの種類・確度を表すだけの語)
STOPWORDS = {
    "ext", "external", "int", "internal", "with", "mtg", "meeting", "sync", "call", "fb", "re", "fw", "tbd",
    "fixed", "tentative", "daily", "weekly", "monthly", "regular", "online",
    "kickoff", "review", "1on1", "standup", "retro", "status", "update", "follow", "up", "for",
    "定例", "定例会", "打ち合わせ", "打合せ", "打合", "お打ち合わせ", "ミーティング", "会議", "確定", "確", "仮",
    "相談", "ご相談", "共有", "件", "様", "向け", "社内", "社外", "内部", "外部", "週次", "月次", "勉強会",
    "キックオフ", "レビュー", "報告", "報告会", "進捗", "確認",
}
NEAR_MIN = 0.5  # 見つからないときに示す似た名前の、似ている度合いの下限 (入力の語の重みのうち、含まれる割合)
NEAR_MAX = 3  # 見つからないときに示す似た名前の数の上限


class GwsError(Exception):
    pass


class GwsTimeout(GwsError):
    """時間内に終わらなかった (大きすぎる資料など)。"""


class Fail(Exception):
    """[NG] で終える。(終了コード, 内容, 案内)"""


def gws(*args, params=None, pages=False, timeout=None):
    if args not in READ_CALLS:  # Google Workspace は読むだけにする
        raise GwsError(f"読むだけでない gws の操作は実行しません: gws {' '.join(args)}")
    cmd = ["gws", *args]
    if params is not None:
        cmd += ["--params", json.dumps(params, ensure_ascii=False)]
    if pages:
        cmd += ["--page-all", "--page-limit", "50"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GwsTimeout(f"{timeout} 秒で終わらなかったので打ち切りました (大きすぎる)")
    if res.returncode != 0:
        # stdout (結果の一部のことがある) は出さず、stderr の先頭だけを出す
        err = "\n".join(res.stderr.strip().splitlines()[:GWS_ERR_LINES])
        raise GwsError(f"gws {' '.join(args)} が失敗しました (終了コード {res.returncode}):\n{err}")
    if pages:  # 1 ページが 1 行の JSON (splitlines は文字列の中の U+2028 などでも分けるので使わない)
        return [json.loads(line) for line in res.stdout.split("\n") if line.startswith("{")]
    return json.loads(res.stdout) if res.stdout.strip() else {}


def require_auth():
    """gws があり認証済みかを確かめて、(メールアドレス, 表示名) を返す。"""
    if not shutil.which("gws"):
        raise Fail(10, "gws がありません", "Google Workspace CLI (gws) をインストールしてもらってください")
    try:
        user = gws("drive", "about", "get", params={"fields": "user(emailAddress,displayName)"}).get("user", {})
    except GwsError as e:
        raise Fail(20, f"gws が認証されていません\n{e}",
                   "ユーザーに `! gws auth login` の実行を依頼し、済んだらやり直してください")
    return user.get("emailAddress", "?"), user.get("displayName", "")


# ------------------------------------------------------------------- 文字列

def norm(s):
    """比べるための形 (全角・半角と大文字・小文字をそろえ、空白をまとめる)。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip().lower()


def squash(s):
    """記号と空白を除いた形 (名前の書き方の揺れを吸収する)。"""
    return re.sub(r"[\W_]+", "", norm(s))


def strip_join_info(s):
    """会議に入るための URL と電話番号らしい部分を除く。残す URL (資料) の中の数字 (gid など) はそのままにする。"""
    def no_phone(t):
        return PHONE_RE.sub(lambda m: m.group() if DATE_RE.match(m.group()) else "", t)

    out, pos = [], 0
    for m in URL_RE.finditer(s):
        out += [no_phone(s[pos:m.start()]), m.group() if KEEP_URL_RE.match(m.group()) else ""]
        pos = m.end()
    return "".join(out) + no_phone(s[pos:])


def plain(desc):
    """予定の説明 (HTML のことがある) を、会議に入るための情報を除いたテキストにする。
    リンクは表示される文字だけにする (ドキュメント・スライドのリンクは、資料として ID を別に書く)。"""
    s = re.sub(r"<br\s*/?>|</p>|</li>|</tr>|</div>|</h\d>", "\n", desc or "", flags=re.I)
    s = re.sub(r"<li[^>]*>", "・", s, flags=re.I)
    s = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
               lambda m: m.group(2) if re.sub(r"<[^>]+>", "", m.group(2)).strip() else m.group(1), s, flags=re.I | re.S)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    lines = [ln.rstrip() for ln in strip_join_info(s).splitlines() if not SECRET_LINE_RE.search(ln)]
    s = re.sub(r"・(\s*・)+", "・", re.sub(r"\n\s*\n", "\n", "\n".join(lines))).strip()  # 入れ子のリストと段落の空行を詰める
    return s if len(s) <= DESC_MAX else s[:DESC_MAX] + f"\n…(説明の残り {len(s) - DESC_MAX} 字は省略)"


def dates_text(ds):
    """日付の並び。4 つ以上なら「最初〜最後 の N 回」にまとめる。"""
    return ", ".join(ds) if len(ds) <= 3 else f"{ds[0]}〜{ds[-1]} の {len(ds)} 回"


def tally(events):
    """自分の関わりの内訳 (例: 参加 (承諾) 40・辞退 15)。"""
    counts = {}
    for e in events:
        counts[e.involvement()] = counts.get(e.involvement(), 0) + 1
    return "・".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def print_desc(desc):
    """予定の説明を字下げして表示する。"""
    print("  説明:\n" + "\n".join("    " + ln if ln else "" for ln in desc.splitlines()))


def script(c):
    """文字の種類 (漢字・ひらがな・カタカナ・そのほか (ラテン文字と数字など))。"""
    if c.isascii():
        return "L"
    n = unicodedata.name(c, "")
    if n.startswith("CJK") or c == "々":
        return "CJK"
    return next((k for k in ("HIRAGANA", "KATAKANA") if n.startswith(k)), "L")  # 「ー」はカタカナ


def title_words(s, generic=False):
    """名前の語 (表記はそのまま。大文字・小文字を区別せず重複を除く)。Unicode の英数字の並びで分け、
    さらに文字の種類が変わるところで分ける。2 字未満・数字だけ・ひらがなだけの語は除く。
    generic が偽なら、種類・確度を表すだけの語 (STOPWORDS) も除く。"""
    words = {}
    for run in re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", s or "")):
        parts = [run] if run.lower() in STOPWORDS else ["".join(g) for _, g in itertools.groupby(run, key=script)]
        for w in parts:
            if (len(w) < 2 or w.isdigit() or all(script(c) == "HIRAGANA" for c in w)
                    or (not generic and w.lower() in STOPWORDS)):
                continue
            words.setdefault(w.lower(), w)
    return list(words.values())


# ------------------------------------------------------------------- 予定

def parse_dt(v):
    if "dateTime" in v:
        return datetime.datetime.fromisoformat(v["dateTime"].replace("Z", "+00:00")).astimezone()  # マシンの時間帯にそろえる
    d = datetime.date.fromisoformat(v["date"])
    return datetime.datetime(d.year, d.month, d.day).astimezone()


class Event:
    def __init__(self, e):
        self.title = (e.get("summary") or "(タイトルなし)").strip()
        self.key = squash(self.title)  # 同じ打ち合わせかを比べる形
        self.start = parse_dt(e["start"])
        self.end = parse_dt(e["end"]) if e.get("end") else self.start
        self.all_day = "date" in e["start"]
        self.desc = plain(e.get("description", ""))
        organizer = e.get("organizer") or {}
        self.organizer, self.organizer_self = organizer.get("displayName", ""), organizer.get("self", False)  # 表示名だけ
        self.attendees = e.get("attendees", [])
        self.files = {}  # fileId -> (タイトル, mimeType)。添付と説明のリンク
        for a in e.get("attachments", []):
            if a.get("fileId"):
                self.files[a["fileId"]] = (a.get("title", ""), a.get("mimeType", ""))
        for kind, fid in LINK_RE.findall(e.get("description", "")):
            self.files.setdefault(fid, ("", LINK_MIMES[kind]))

    def involvement(self):
        me = next((a for a in self.attendees if a.get("self")), None)
        if self.organizer_self or (me and me.get("organizer")):
            return "主催"
        if me is None:
            return "招待なし"
        return {"accepted": "参加 (承諾)", "tentative": "仮承諾", "needsAction": "未回答",
                "declined": "辞退"}.get(me.get("responseStatus"), "未回答")

    def when(self):
        d = self.start.strftime("%Y-%m-%d") + f" ({WEEKDAYS[self.start.weekday()]})"
        return d + " 終日" if self.all_day else d + f" {self.start:%H:%M}-{self.end:%H:%M}"

    def header(self, today):
        d = self.start.date()
        tag = "今日" if d == today else "済み" if d < today else "次回"
        people = f" / 参加者 {len(self.attendees)} 人" if self.attendees else ""
        org = f" / 主催: {self.organizer}" if self.organizer and self.involvement() != "主催" else ""
        return f"{self.when()} [{tag}] {self.title}\n  自分: {self.involvement()}{org}{people}"


def list_events(time_min, time_max):
    pages = gws("calendar", "events", "list", pages=True, params={
        "calendarId": "primary", "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
        "singleEvents": True, "orderBy": "startTime", "maxResults": 2500,
        "fields": "nextPageToken,items(summary,description,start,end,status,eventType,"
                  "organizer(displayName,self),attendees(self,organizer,responseStatus),attachments(fileId,title,mimeType))"})
    events = []
    for p in pages:
        for e in p.get("items", []):
            if (e.get("status") == "cancelled" or e.get("eventType") in SKIP_TYPES or not e.get("start")
                    or not e.get("attendees")):  # 参加者のいない予定 (自分の作業) は打ち合わせでない
                continue
            events.append(Event(e))
    return events


def name_words(s, generic=False):
    """名前を比べるための語 (小文字。分け方は title_words と同じ)。"""
    return {w.lower() for w in title_words(s, generic)}


def near_titles(name, titles):
    """名前に似た予定のタイトル (多くても NEAR_MAX 件)。
    語ごとの重みは、予定のタイトルに出てくる数が少ない語ほど高くする (顧客名や案件名のような固有の語が効き、よく出る語は効きにくい)。
    入力の語の重みのうち NEAR_MIN 以上が含まれるタイトルだけを返す。
    一般的な語 (STOPWORDS) は判定に使わず、同じ点のタイトルを並べ替えるときにだけ使う。
    英数字の語はタイトルの語と丸ごと合うものだけを数え (「ai」が「daily」に合わないように)、
    日本語の語はタイトルに含まれていれば数える (区切りの無い複合語に合うように)。"""
    words, generic = name_words(name), name_words(name, generic=True) - name_words(name)
    parsed = {t: (norm(t), name_words(t, generic=True)) for t in titles}

    def has(w, t):
        n, ws = parsed[t]
        return w in ws if w.isascii() else w in n

    weight = {w: math.log((len(titles) + 1) / (sum(has(w, t) for t in titles) + 1)) for w in words}
    total = sum(weight.values())
    if total <= 0:
        return []
    scored = [(sum(weight[w] for w in words if has(w, t)) / total, sum(has(w, t) for w in generic), t) for t in titles]
    return [t for s, _, t in sorted(scored, key=lambda x: (-x[0], -x[1], x[2])) if s >= NEAR_MIN][:NEAR_MAX]


def pick_meeting(events, name, today_end):
    """名前から打ち合わせを決める。(今日までの同じ名前の回, 明日以降の最初の回 (無ければ None))"""
    q, qs = norm(name), squash(name)
    if not qs:
        raise Fail(2, "会議名が空です", "会議の名前を渡してください")
    hits = [e for e in events if q in norm(e.title)] or [e for e in events if qs in e.key]
    if not hits:
        near = "\n".join(f"  - {t}" for t in near_titles(name, {e.title for e in events}))
        raise Fail(30, f"「{name}」という名前の予定が見つかりません (今日の {FIND_PAST_DAYS} 日前から {FUTURE_DAYS} 日後まで)"
                   + (f"\n似た名前:\n{near}" if near else ""),
                   "この内容を伝え、ユーザーに会議名をもう一度入力してもらってください (こちらで選ばない)" if near else
                   "見つからないことを伝え、予定のタイトルをカレンダーからそのままコピーして入力し直してもらってください")
    groups = {}
    for e in hits:
        groups.setdefault(e.key, []).append(e)
    if len(groups) > 1:
        exact = [k for k, v in groups.items() if norm(v[0].title) == q]
        if len(exact) != 1:
            lines = "\n".join(f"  - {v[0].title} ({len(v)} 回、最後は {v[-1].start:%Y-%m-%d})" for v in groups.values())
            raise Fail(30, f"「{name}」が複数の打ち合わせに当てはまります:\n{lines}",
                       "この内容を伝え、ユーザーに会議名をもう一度入力してもらってください (こちらで選ばない)")
        same = groups[exact[0]]
    else:
        same = next(iter(groups.values()))
    past = [e for e in same if e.start < today_end]
    return past, next((e for e in same if e.start >= today_end), None)


def day_bounds():
    """(今, 今日の日付, 明日の 0 時)。今日の予定は「今日まで」に入れる。"""
    now = datetime.datetime.now().astimezone()
    today = now.date()
    return now, today, datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time()).astimezone()


def auth_and_events(time_min, time_max):
    """認証の確認とカレンダーの読み込みを同時に行う。((メールアドレス, 表示名), 予定)"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        auth = pool.submit(require_auth)
        events = pool.submit(list_events, time_min, time_max)
        return auth.result(), events.result()  # 認証の [NG] を先に出す


# ------------------------------------------------------------------- 資料

def element_text(el):
    """段落の要素の文字。スマートチップ (人・日付・リンク) は表示される文字にする。"""
    if "textRun" in el:
        return el["textRun"].get("content", "")
    if "person" in el:
        p = el["person"].get("personProperties", {})
        return p.get("name") or p.get("email", "")
    if "dateElement" in el:
        return el["dateElement"].get("dateElementProperties", {}).get("displayText", "")
    if "richLink" in el:
        return el["richLink"].get("richLinkProperties", {}).get("title", "")
    return ""


def row_text(cells):
    """表の 1 行を「 | 」でつないだ行。右の空のセルは除く (日程表などの空のマスで字数を使わないため)。空の行は ""。"""
    cells = [" ".join(c.split()) for c in cells]
    while cells and not cells[-1]:
        cells.pop()
    return " | ".join(cells) + "\n" if cells else ""


def doc_text(d):
    """ドキュメントの本文 (最初のタブ) のテキスト。表は 1 行を「 | 」でつなぐ。"""
    def walk(content):
        out = []
        for c in content:
            if "paragraph" in c:
                p = c["paragraph"]
                t = "".join(element_text(el) for el in p.get("elements", []))
                if p.get("bullet") and t.strip():
                    t = "  " * p["bullet"].get("nestingLevel", 0) + "・" + t
                out.append(t)
            elif "table" in c:
                for row in c["table"].get("tableRows", []):
                    out.append(row_text(walk(cell.get("content", [])) for cell in row.get("tableCells", [])))
            elif "tableOfContents" in c:
                out.append(walk(c["tableOfContents"].get("content", [])))
        return "".join(out)

    return walk(d.get("body", {}).get("content", []))


def slides_text(p):
    """スライドのテキスト (図形と表)。"""
    out = []

    def text(t):
        return "".join(x.get("textRun", {}).get("content", "") for x in (t or {}).get("textElements", []))

    def shape_text(el):
        if "shape" in el:
            t = text(el["shape"].get("text"))
            if t.strip():
                out.append(t.rstrip() + "\n")
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                out.append(row_text(text(c.get("text")) for c in row.get("tableCells", [])))
        elif "elementGroup" in el:
            for ch in el["elementGroup"].get("children", []):
                shape_text(ch)

    for i, s in enumerate(p.get("slides", []), 1):
        out.append(f"--- スライド {i} ---\n")
        n = len(out)
        for el in s.get("pageElements", []):
            shape_text(el)
        if len(out) == n:  # 文字の無いスライド (図だけなど) は見出しも書かない
            out.pop()
    return "".join(out)


def read_file(fid, max_chars, mime=None, timeout=None):
    """(名前, 種類, 更新日時, 本文)。本文が読めない種類なら None。
    mime がわかっていれば (予定の添付・リンクから)、ファイルの情報は読まずに本文だけを読む (gws の呼び出しを 1 回にする)。
    timeout 秒で本文を読み終わらなければ打ち切る (記録がたまり続けた大きなドキュメントなど)。"""
    meta = {"mimeType": mime}
    if mime is None:
        try:
            meta = gws("drive", "files", "get", params={"fileId": fid, "supportsAllDrives": True,
                                                        "fields": "name,mimeType,modifiedTime"})
        except GwsError as e:
            raise Fail(40, f"ファイル {fid} を読めません (無いか、権限がありません)\n{e}", "ID か URL を確かめてください")
    mime = meta.get("mimeType", "")
    name = meta.get("name", "")
    try:
        if mime == DOC_MIME:
            d = gws("docs", "documents", "get", params={"documentId": fid}, timeout=timeout)
            name, text = name or d.get("title", ""), doc_text(d)
        elif mime == SLIDES_MIME:
            p = gws("slides", "presentations", "get", params={"presentationId": fid}, timeout=timeout)
            name, text = name or p.get("title", ""), slides_text(p)
        else:
            text = None
    except GwsError as e:
        reason = "大きすぎて時間内に読めない" if isinstance(e, GwsTimeout) else "権限が無いか、読めない"
        raise Fail(40, f"本文を読めません ({reason}): {name or fid}\n{e}", "権限を確かめてください")
    if text is not None:
        text = EMAIL_RE.sub("<メール>", re.sub(r"\n{3,}", "\n\n", text)).strip()  # 体制表などのメールアドレスは出さない
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…(残り {len(text) - max_chars} 字は省略。`doc {fid} --max-chars <字数>` で続きを読めます)"
    return name, KINDS.get(mime, mime), (meta.get("modifiedTime") or "")[:10], text


def file_id(s):
    m = LINK_RE.search(s) or re.search(r"/d/([\w-]{20,})", s)
    if m:
        return m.groups()[-1]
    if re.fullmatch(r"[\w-]{20,}", s):
        return s
    raise Fail(2, f"ファイルの ID か URL ではありません: {s}", "ドキュメントかスライドの URL を渡してください")


# ------------------------------------------------------------------- 照合

def parse_keywords(kws):
    groups = []
    for k in kws:
        terms = [norm(t) for t in k.split("+") if norm(t)]
        if not terms:
            raise Fail(2, f"キーワードが空です: {k!r}", "--keyword に案件を表す語を渡してください")
        groups.append((k, terms))
    return groups


def matched(groups, text):
    t = norm(text)
    return [k for k, terms in groups if all(x in t for x in terms)]


def drive_candidates(groups, since, known):
    # name contains は語の先頭で合わせるので、そろえた形 (小文字・半角) と元の表記の両方で問い合わせる
    term_sets = {tuple(terms) for _, terms in groups}
    term_sets |= {tuple(t.strip() for t in k.split("+") if t.strip()) for k, _ in groups}
    clauses = ["(" + " and ".join("name contains '" + t.replace("\\", "\\\\").replace("'", "\\'") + "'" for t in terms)
               + ")" for terms in sorted(term_sets)]
    q = (f"({' or '.join(clauses)}) and modifiedTime > '{since:%Y-%m-%dT00:00:00}' and trashed = false and "
         f"(mimeType = '{DOC_MIME}' or mimeType = '{SLIDES_MIME}' or mimeType = '{SHEET_MIME}')")
    res = gws("drive", "files", "list", params={
        "q": q, "pageSize": 100, "orderBy": "modifiedTime desc", "corpora": "allDrives",
        "includeItemsFromAllDrives": True, "supportsAllDrives": True,
        "fields": "files(id,name,mimeType,modifiedTime)"})
    return [f for f in res.get("files", []) if f["id"] not in known][:DRIVE_MAX]


# ------------------------------------------------------------------- コマンド

def show_event(e, today):
    print(e.header(today))
    for fid, (title, mime) in e.files.items():
        print(f"  資料: {title or '(リンク)'} ({KINDS.get(mime, mime)}, ID {fid})")
    if e.desc:
        print_desc(e.desc)


def cmd_find(args):
    now, today, today_end = day_bounds()
    (email, name), events = auth_and_events(now - datetime.timedelta(days=FIND_PAST_DAYS),
                                            today_end + datetime.timedelta(days=FUTURE_DAYS))
    past, nxt = pick_meeting(events, args.name, today_end)
    title = (nxt or past[-1]).title
    print(f"[OK] gws: {email} ({name})")
    print(f"\n== 打ち合わせ: {title} ==")
    if nxt:
        print("\n== 次回 ==")
        show_event(nxt, today)
    else:
        print(f"次回: 決まっていません ({FUTURE_DAYS} 日後まで)")
    if past:
        print("\n== 今日までの最後の回 ==")
        show_event(past[-1], today)
    if len(past) > 1:
        before = past[:-1]
        print(f"\n== それより前の回 ({len(before)} 回。自分: {tally(before)}) ==")
        if len(before) > LIST_MAX:
            print(f"- (古い {len(before) - LIST_MAX} 回は省略。{before[0].start:%Y-%m-%d} から)")
        for e in before[-LIST_MAX:]:
            print(f"- {e.when()} 自分: {e.involvement()}")
    kws = title_words(title)
    print("\n== キーワードの候補 (タイトルから) ==")
    print("  " + (", ".join(kws) if kws else "(なし)"))
    print("\n案内: 案件を表す語を選んで、"
          "collect <会議名> --keyword <語> [--keyword <語> ...] を実行してください (「A+B」で両方を含むもの)")
    return 0


def cmd_collect(args):
    groups = parse_keywords(args.keyword)
    now, today, today_end = day_bounds()
    # カレンダーは 1 回だけ読み、そこから打ち合わせ (と次回) を決めて、今日までの args.days 日を使う
    since = now - datetime.timedelta(days=args.days)
    (email, name), window = auth_and_events(min(since, now - datetime.timedelta(days=FIND_PAST_DAYS)),
                                            today_end + datetime.timedelta(days=FUTURE_DAYS))
    past, nxt = pick_meeting(window, args.name, today_end)
    title = (nxt or past[-1]).title
    key = (nxt or past[-1]).key
    events = [e for e in window if since <= e.start < today_end]
    seed = {fid for e in events if e.key == key for fid in e.files} | (set(nxt.files) if nxt else set())

    related, desc_only = [], []
    for e in events:
        why = ["同じ名前"] if e.key == key else []
        why += matched(groups, e.title)
        if e.key != key and set(e.files) & seed:
            why.append("同じ名前の打ち合わせと同じ資料")
        if why:
            related.append((e, why))
        elif hit := matched(groups, e.desc):
            desc_only.append((e, hit))
    # 関連する予定の資料を使うほかの予定も関連とする (1 回だけ広げる)
    shared = {fid for e, _ in related for fid in e.files}
    weak = []
    for e, hit in desc_only:
        if set(e.files) & shared:
            related.append((e, hit + ["関連する予定と同じ資料"]))
        else:
            weak.append((e, hit))
    related.sort(key=lambda x: x[0].start)

    print(f"[OK] gws: {email} ({name})")
    print(f"打ち合わせ: {title}")
    print(f"次回: {nxt.when()} (自分: {nxt.involvement()})" if nxt else f"次回: 決まっていません ({FUTURE_DAYS} 日後まで)")
    print(f"範囲: {since:%Y-%m-%d} 〜 {today:%Y-%m-%d} (今日まで) / キーワード: {', '.join(k for k, _ in groups)}")

    # 予定は全部を並べる (カレンダーは 1 回で読んであるので、件数が多くても時間は変わらない)。
    # 同じ名前で REPEAT_MIN 回以上ある予定 (定例・デイリーなど) は 1 行にまとめる
    used = {}
    series = {}
    for e, why in related:
        series.setdefault(e.key, []).append((e, why))
        for fid in e.files:
            used.setdefault(fid, []).append(f"{e.start:%m/%d}")
    repeated, singles = [], []
    for items in series.values():
        evs = [e for e, _ in items]
        why = ", ".join(dict.fromkeys(w for _, ws in items for w in ws))
        if len(evs) >= REPEAT_MIN:
            repeated.append((evs[0].start, f"- {evs[-1].title} … {len(evs)} 回 ({evs[0].start:%Y-%m-%d}〜{evs[-1].start:%Y-%m-%d}、"
                                           f"最後は {evs[-1].when()}) (自分: {tally(evs)} / 一致: {why})"))
        else:
            singles += [(e.start, f"- {e.when()} {e.title} (自分: {e.involvement()} / 一致: {', '.join(ws)}"
                                  + (f" / 資料 {len(e.files)}" if e.files else "") + ")") for e, ws in items]
    print(f"\n== 関連する予定 (今日まで、{len(related)} 件) ==")
    if repeated:
        print(f"繰り返しの予定 (同じ名前で {REPEAT_MIN} 回以上):")
        for _, line in sorted(repeated):
            print(line)
    if singles:
        singles.sort()
        print(f"そのほかの予定 (古い順" + (f"。新しい {SINGLES_MAX} 件だけ。古い {len(singles) - SINGLES_MAX} 件は省略"
                                         if len(singles) > SINGLES_MAX else "") + "):")
        for _, line in singles[-SINGLES_MAX:]:
            print(line)

    if weak:
        print(f"\n== 関連が薄いかもしれない予定 (説明にだけキーワードがある、{len(weak)} 件) ==")
        for e, hit in weak[-LIST_MAX:]:
            print(f"- {e.when()} {e.title} (自分: {e.involvement()} / 一致: {', '.join(hit)})")

    # これからの関連する予定 (明日から FUTURE_DAYS 日後まで)。選び方は今日までと同じで、繰り返しの予定は 1 行にまとめる
    files = seed | shared
    coming = {}
    for e in window:
        if e.start < today_end:
            continue
        if e.key == key or matched(groups, e.title) or set(e.files) & files:
            coming.setdefault(e.key, []).append(e)
    lines = []
    for evs in coming.values():
        first = evs[0]
        more = f"、以降 {len(evs) - 1} 回 ({evs[-1].start:%Y-%m-%d} まで)" if len(evs) > 1 else ""
        lines.append((first.start, f"- {first.when()} {first.title}{more} (自分: {first.involvement()})"))
    print(f"\n== これからの関連する予定 (明日から {FUTURE_DAYS} 日後まで。同じ名前の予定は最初の回だけ) ==")
    for _, line in sorted(lines)[:UPCOMING_MAX]:
        print(line)
    if len(lines) > UPCOMING_MAX:
        print(f"- ほか {len(lines) - UPCOMING_MAX} 件")
    if not lines:
        print("(なし)")

    # 次回と直近の回の資料は、下の「資料」に名前・ID・使った予定と一緒に書くので、ここでは数だけ書く
    if nxt:
        print("\n== 次回の打ち合わせ ==")
        print(nxt.header(today) + (f"\n  資料: {len(nxt.files)} 件" if nxt.files else ""))
        if nxt.desc:
            print_desc(nxt.desc)
        for fid in nxt.files:
            used.setdefault(fid, []).append(f"次回 {nxt.start:%m/%d}")

    # 中身 (説明・資料の本文) は、直近の RECENT 回の予定の分だけを読む。それより前は一覧だけ
    # 直近の回は、本文を読める資料 (ドキュメント・スライド) が付いている予定から選ぶ (無ければ単に新しい予定)
    has_text = [e for e, _ in related if any(m in (DOC_MIME, SLIDES_MIME) for _, m in e.files.values())]
    recent = (has_text or [e for e, _ in related])[-RECENT:][::-1]

    # 説明は直近の予定から、同じ文は 1 回だけ、合わせて DESC_BUDGET 字まで
    print(f"\n== 資料のある直近 {len(recent)} 回の予定と説明 (新しい順。同じ説明は 1 回だけ。合わせて {DESC_BUDGET} 字まで) ==")
    left, seen = DESC_BUDGET, {nxt.desc} if nxt else set()
    for e in recent:
        print("\n" + e.header(today) + (f"\n  資料: {len(e.files)} 件" if e.files else ""))
        if not e.desc:
            continue
        if e.desc in seen:
            print("  説明: (次回か、より新しい予定と同じ)")
        elif left <= 0:
            print("  説明: (上限で省略)")
        else:
            seen.add(e.desc)
            desc = e.desc if len(e.desc) <= left else e.desc[:left] + "\n…(上限で省略)"
            left -= len(desc)
            print_desc(desc)

    # 資料は次回の分 → 直近の予定の分の順に読み、本文は合わせて args.doc_budget 字まで
    sources = ([nxt] if nxt else []) + recent
    order = list(dict.fromkeys(fid for e in sources for fid in e.files))
    older_files = {fid for e, _ in related for fid in e.files} - set(order)
    titles, mimes = {}, {}
    for e in sources:
        for fid, (t, mime) in e.files.items():
            if t:
                titles.setdefault(fid, t)
            mimes.setdefault(fid, mime)
    label = {fid: f"{titles.get(fid, '(リンク)')} (ID {fid}, 予定: {dates_text(used[fid])})" for fid in order}
    # 会議のメモ (Gemini のメモ・議事メモ) → ほかのドキュメント → スライドの順に読む (要約に効くものから)
    rank = {fid: (0 if NOTE_RE.search(titles.get(fid, "")) else 1 if mimes[fid] == DOC_MIME else 2) for fid in order}
    readable = sorted((fid for fid in order if mimes[fid] in (DOC_MIME, SLIDES_MIME)), key=lambda f: rank[f])
    skipped = [f"{label[fid]} … {KINDS.get(mimes[fid], mimes[fid] or '種類不明')} は本文を読めない種類"
               for fid in order if fid not in readable]

    def read(fid):
        try:
            return read_file(fid, args.max_chars, mimes[fid], timeout=DOC_TIMEOUT)
        except Fail as f:
            return f

    print(f"\n== 資料 (次回と直近 {len(recent)} 回の分。本文は合わせて {args.doc_budget} 字・1 つ {DOC_TIMEOUT} 秒まで) ==")
    left, i = args.doc_budget, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=READ_WORKERS + 1) as pool:
        drive = pool.submit(drive_candidates, groups, since, set(order))  # ドライブの検索は、資料を読む間に済ませる
        # READ_WORKERS 個ずつ同時に読み、上限に近づいたら次の組は読まない
        while i < len(readable) and left >= DOC_MIN:
            batch = readable[i:i + READ_WORKERS]
            i += len(batch)
            for fid, res in zip(batch, pool.map(read, batch)):
                if isinstance(res, Fail):
                    skipped.append(f"{label[fid]} … {res.args[1].splitlines()[0]}")
                    continue
                fname, kind, _, text = res
                text = text or "(本文なし)"
                if len(text) > left:
                    if left < DOC_MIN:
                        skipped.append(f"{label[fid]} … 本文の上限を超えたので省いた")
                        continue
                    text = text[:left] + f"\n…(本文の上限で省略。`doc {fid}` で続きを読めます)"
                left -= len(text)
                print(f"\n### {fname or titles.get(fid, '')} ({kind}, ID {fid}, 予定: {dates_text(used[fid])})")
                print(text)
        try:
            cands = drive.result()
        except GwsError as e:
            cands = f"(ドライブを検索できませんでした: {str(e).splitlines()[0]})"
    skipped += [f"{label[fid]} … 本文の上限を超えたので読んでいない" for fid in readable[i:]]
    if not order:
        print("(関連する予定と次回に資料はありません)")
    if skipped:
        print(f"\n読まなかった資料 ({len(skipped)} 件。頼まれたら doc <ID> で読む):")
        for s in skipped:
            print(f"- {s}")
    if older_files:
        print(f"\n(それより前の予定の資料 {len(older_files)} 件は読んでいない。古い経緯の中身を頼まれたら、"
              f"ドライブの候補などから doc <ID> で読む)")

    print("\n== ドライブの候補 (名前にキーワードがある。本文は読んでいない) ==")
    if isinstance(cands, str):
        print(cands)
    else:
        for f in cands:
            print(f"- {f['name']} ({KINDS.get(f['mimeType'], f['mimeType'])}, 更新 {f['modifiedTime'][:10]}, ID {f['id']})")
        if not cands:
            print("(なし)")
    print("\n案内: 関連しない予定を除いて要約してください。資料の本文は、ユーザーに頼まれたときだけ doc <ID> で読んでください")
    return 0


def cmd_doc(args):
    fid = file_id(args.file)
    require_auth()
    fname, kind, modified, text = read_file(fid, args.max_chars, timeout=DOC_CMD_TIMEOUT)
    if text is None:
        raise Fail(40, f"{fname} は {kind} なので本文を読めません", "ドキュメントかスライドを渡してください")
    print(f"[OK] {fname} ({kind}, 更新 {modified}, ID {fid})\n")
    print(text or "(本文なし)")
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


def to_file(func, args, name):
    """func の出力を WORK_DIR/name に書き、場所と大きさだけを出す。失敗したら何も書かない。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = func(args)
    text = buf.getvalue()
    # 自分だけが読めるようにする (ディレクトリ 0700・ファイル 0600。すでにあるときも直す。リンクはたどらない)
    os.makedirs(WORK_DIR, mode=0o700, exist_ok=True)
    os.chmod(WORK_DIR, 0o700)
    fd = os.open(os.path.join(WORK_DIR, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with open(fd, "w", encoding="utf-8") as f:
        f.write(text)
    print(text.splitlines()[0] if text else "[OK]")
    print(f"[OK] 結果を {WORK_REL}{name} に書きました ({len(text)} 字・{text.count(chr(10))} 行)")
    print("案内: Read で読んでください。結果を示し終えたら clean で消してください")
    return code


def positive(s):
    if not s.isdigit() or int(s) < 1:
        raise argparse.ArgumentTypeError(f"1 以上の整数にしてください: {s}")
    return int(s)


def main():
    ap = argparse.ArgumentParser(description="会議の名前から、その打ち合わせの過去の経緯 (関連する予定と資料) を集める (読み取りのみ)",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("find", help="打ち合わせと次回を決め、キーワードの候補を表示する")
    p.add_argument("name", help="会議名 (予定のタイトル。一部でもよい)")
    p = sub.add_parser("collect", help="今日までの関連する予定と資料を集める")
    p.add_argument("name", help="会議名 (find と同じ)")
    p.add_argument("--keyword", action="append", required=True, help="案件を表す語 (繰り返せる。「A+B」で両方を含むもの)")
    p.add_argument("--days", type=positive, default=90, help="今日の何日前から探すか (既定 90)")
    p.add_argument("--doc-budget", type=positive, default=10000, help="資料の本文の字数の合計の上限 (既定 10000)")
    p.add_argument("--max-chars", type=positive, default=4000, help="資料 1 つの本文の字数の上限 (既定 4000)")
    p = sub.add_parser("doc", help="ドキュメント・スライドの本文を表示する")
    p.add_argument("file", help="ファイルの ID か URL")
    p.add_argument("--max-chars", type=positive, default=30000, help="本文の字数の上限 (既定 30000)")
    sub.add_parser("clean", help=f"{WORK_REL} の結果を消す")
    args = ap.parse_args()
    try:
        if args.cmd == "collect":
            return to_file(cmd_collect, args, "collect.txt")
        if args.cmd == "doc":
            return to_file(cmd_doc, args, f"doc-{file_id(args.file)}.txt")
        return {"find": cmd_find, "clean": cmd_clean}[args.cmd](args)
    except Fail as f:
        code, msg, hint = f.args
        print(f"[NG] {msg}")
        print(f"案内: {hint}")
        return code
    except GwsError as e:
        print(f"[NG] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
