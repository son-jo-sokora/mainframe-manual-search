#!/usr/bin/env python3
"""
z/OS コーパスのハイブリッド検索（全文検索 + 意味検索）。単体で動く。

  python3 hsearch.py <コーパス> serve [--port 8765]     # ブラウザで http://127.0.0.1:8765
  python3 hsearch.py <コーパス> q "検索文" [--product z/OS] [--ver 3.2] [--k 10] [--mode auto|fts|vec]

- メッセージID・アベンドコード等の識別子を含む問いは、それを含む箇所を最優先する
- 日本語の問いでも、多言語埋め込みにより英語の本文から意味の近い箇所を探す
- vectors.* が無ければ全文検索だけで動く
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

import compare

RRF_K = 60
# 識別子: IEF450I, IGD17101I, ICH408I, $HASP373, IEASYSxx, S0C4, U4038, 013-18
ID_RES = [
    re.compile(r"\$?\b[A-Z]{2,5}\d{2,5}[A-Z]{0,2}\b"),
    re.compile(r"\b[A-Z]{3,8}xx\b"),
    re.compile(r"\b[SU][0-9A-F]{3}\b"),
    re.compile(r"\b[0-9A-F]{3}-[0-9A-F]{2}\b"),
    re.compile(r"(?<![\w.\-+])[-+]\d{2,5}\b"),   # SQLCODE（-904、+100）
]
WORD_RE = re.compile(r"[A-Za-z0-9$#@]+")
PAGE_SPLIT_RE = re.compile(r"^=== PAGE (\d+) ===\n?", re.M)
STAMP_FILES = ("index.db", "vectors.json", "manifest.json")


def fts_quote(s):
    return '"' + s.replace('"', '""') + '"'


def identifiers(q):
    found = []
    for r in ID_RES:
        for m in r.finditer(q):
            if m.group(0) not in found:
                found.append(m.group(0))
    return found


def is_redbook(d):
    """Redbook のフォルダ（名前に redbook を含む PDF フォルダ）から取り込んだ文書か"""
    parts = (d.get("path") or "").split("/")
    return len(parts) > 1 and "redbook" in parts[1].lower()


def doc_label(d):
    """
    製品名と版の表示用ラベル（例: z/OS 3.2, Print Services Facility 4.8）。
    Redbook は製品のマニュアルと区別して「Redbook」とする（対象の製品・版は版の根拠の文に出る）
    """
    if is_redbook(d):
        return "Redbook"
    return f"{d.get('product') or '製品不明'} {d.get('version') or ''}".strip()


def family_key(d):
    """
    同じ資料の別の版をまとめるキー（無ければ None）。
    資料番号があれば、末尾の刷の番号を除いた部分（GC27-1234-00 と GC27-1234-90）。
    資料番号が無ければ（MQ・CICS など）、製品名と、版の数字を除いたファイル名
    （mq93.administer と mq94.administer、izsb100_v3r1 と izsb100_v3r2）
    """
    if d["form"]:
        return d["form"].rsplit("-", 1)[0]
    if not d.get("product") or not d.get("version") or is_redbook(d):
        return None
    stem = Path(d["path"]).stem
    m = re.search(r"v(\d+)r(\d+)", stem, re.I)
    if m:
        stem = stem[:m.start()] + "#" + stem[m.end():]
    elif re.sub(r"\D", "", d["version"]):
        stem = stem.replace(re.sub(r"\D", "", d["version"]), "#", 1)
    return f"{d['product']}|{stem}"


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def doc_filter(product=None, ver=None, label=None):
    """文書の絞り込み条件。product は部分一致（大文字小文字を無視）、ver と label は完全一致"""
    if not (product or ver or label):
        return None
    p = (product or "").lower()
    return lambda d: ((not p or p in (d.get("product") or "").lower())
                      and (not ver or d.get("version") == ver)
                      and (not label or doc_label(d) == label))


def id_variants(ident):
    """本文での書かれ方の候補。S0C4 は System Codes では「0C4」と書かれる"""
    return [ident, ident[1:]] if re.fullmatch(r"[SU][0-9A-F]{3}", ident) else [ident]


# 定義の直後に来る見出し（メッセージ・コードの説明の型）
DEF_SECTION_RE = re.compile(r"^(Explanation|System action|Severity|User response|Operator response|"
                            r"System programmer response|Programmer response|Problem determination|"
                            r"Administrator response|Response|Module|Source|Routing code|Descriptor code)\b")
ID_LINE_RE = re.compile(r"^\$?[A-Z]{2,5}\d{2,5}[A-Z]{0,2}\b")   # 行頭が識別子
TOC_LINE_RE = re.compile(r"\.{6,}\s*[\divxlc]+$")
DEF_TIERS = (5, 2)   # 定義らしさの点がこれ以上なら「定義」「見出し」


CODE_MANUAL_RE = re.compile(r"\b(Messages|Codes)\b")   # メッセージ集・コード集の書名


def definition_score(text, ids, code_manual=False):
    """
    そのチャンク（と続き）が識別子の「定義」らしいか。
    定義: 識別子だけの行、または行頭の識別子 + メッセージ本文があり、少し後に Explanation などの見出しが続く。
      コード集（System Codes など）は理由コードの一覧が長く、見出しが150行後になることもあるので、
      code_manual のときは「識別子だけの行 + 直後に説明の文」も定義とみなす。
    言及: 文中に出てくるだけ、または他の識別子と並んだ一覧（関連情報・変更点の章・目次）の中。
    """
    lines = [ln.strip() for ln in text.split("\n")]
    if sum(1 for ln in lines if TOC_LINE_RE.search(ln)) >= 3:
        return 0   # 目次のページ
    variants = [v for i in ids for v in id_variants(i)]
    return max((line_score(lines, n, v, code_manual) for n in range(len(lines)) for v in variants), default=0)


def line_score(lines, n, v, code_manual=False):
    """lines[n] が識別子 v の定義（項目の見出し）らしい度合い（definition_score の1行分。lines は strip 済み）"""
    ln = lines[n]
    if ln == v:
        s = 3
    elif ln.startswith((v + " ", v + "(")):
        s = 2
    else:
        return 0
    around = [x for x in lines[max(0, n - 3):n] + lines[n + 1:n + 4] if x]
    if sum(1 for x in around if ID_LINE_RE.match(x)) >= 2:
        return 0   # 識別子が並んだ一覧の中
    nxt = next((x for x in lines[n + 1:] if x), "")
    if any(DEF_SECTION_RE.match(x) for x in lines[n + 1:n + 31]):
        s += 3
    elif len(nxt) > 30 and nxt[:1].isupper():
        s += 3 if (code_manual and s == 3) else 1   # 直後が説明の文
    before = [x for x in lines[max(0, n - 40):n] if x][-4:]
    prev = before[-1] if before else ""
    # 識別子だけの行のすぐ後に Explanation が来るなら、直前が「Descriptor code」などの節の見出しでも項目の始まり
    # （2段組みで、前の項目の節の見出しと値が離れて抽出されることがある）
    head_like = ln == v and "Explanation" in lines[n + 1:n + 8]
    if ((DEF_SECTION_RE.match(prev) and not head_like)      # 別のメッセージの Explanation などの節の中
            or prev.endswith(":")                           # 「… the bind job:」に続く出力例
            or any(x.startswith("Related ") for x in before)    # 関連情報の一覧
            or (ln != v and len(prev) > 40 and prev[-1:].isalpha())):   # 文の途中の折り返し（「… SQLCODE」/「-904 is issued.」）
        s = min(s, 2)
    if ln != v and not code_manual:
        # 「IEF450I IR21J … ABEND=S000」のような行は、コマンド集などではコンソール出力の例のことが多い。
        # 「識別子 + 本文」の形を定義とみなすのはメッセージ集・コード集の中だけにする
        s = min(s, DEF_TIERS[0] - 1)
    return s


# ---- ページを「項目」に分ける（画面で、どこからどこまでが1つのメッセージ・コードの説明かを示すため） ----
# 節の見出しの行（行全体が見出しのもの。「Response SYSTEM 1 …」のような本文の行は含めない）
SECTION_HEAD_RE = re.compile(r"^(Explanation|System action|Severity|User response|Operator response|"
                             r"System programmer response|Programmer response|Problem determination|"
                             r"Administrator response|Response|Module|Source|Routing code|Descriptor code|"
                             r"Related (information|reference|concepts|tasks))$")
CODE_LINE_RE = re.compile(r"^[0-9A-F]{3}$")   # System Codes の「0C4」など


def entry_id_at(lines, n, code_manual=False):
    """lines[n] が項目（メッセージ・コードの説明）の始まりなら、その識別子"""
    s = lines[n]
    m = ID_LINE_RE.match(s) or (code_manual and CODE_LINE_RE.match(s))
    if not m:
        return None
    v = m.group(0)
    return v if line_score(lines, n, v, code_manual) >= DEF_TIERS[0] else None


def hit_terms(q):
    """問いのうち、本文で強調する語（識別子とその書き方の候補、英数字の語）"""
    ids = identifiers(q)
    return [v for i in ids for v in id_variants(i)] + [w for w in WORD_RE.findall(q) if len(w) > 1]


def page_blocks(text, terms, code_manual=False, lookahead=""):
    """
    ページを表示用のブロックに分ける。
      entry: 1つの項目（識別子の見出し + 説明）。title に識別子
      cont : ページ先頭の、前のページから続く項目の残り
      para : 項目が見つからないページ（解説書など）の段落
    各ブロックに hit（問いの語を含むか）を付ける。lookahead は次のページの先頭（ページ末尾の項目の判定用）
    """
    lines = []
    for ln in text.split("\n"):
        s = " ".join(ln.split())
        if SNIPPET_NOISE_RE.match(s):
            continue   # 柱・著作権表示
        if s or (lines and lines[-1]):
            lines.append(s)
    while lines and not lines[-1]:
        lines.pop()
    ext = lines + [" ".join(x.split()) for x in lookahead.split("\n")[:40]]
    starts = [(n, v) for n in range(len(lines)) for v in [entry_id_at(ext, n, code_manual)] if v]
    blocks = []
    if starts:
        if starts[0][0] > 0:
            blocks.append({"kind": "cont", "title": None, "lines": lines[:starts[0][0]]})
        ends = [n for n, _ in starts[1:]] + [len(lines)]
        for (n, v), e in zip(starts, ends):
            blocks.append({"kind": "entry", "title": v, "lines": lines[n:e]})
    else:
        cur = []
        for ln in lines + [""]:
            if ln:
                cur.append(ln)
            elif cur:
                blocks.append({"kind": "para", "title": None, "lines": cur})
                cur = []
    low_terms = list(dict.fromkeys(t.lower() for t in terms if len(t) > 1))
    out = []
    for b in blocks:
        while b["lines"] and not b["lines"][0]:
            b["lines"].pop(0)
        while b["lines"] and not b["lines"][-1]:
            b["lines"].pop()
        if b["lines"]:
            body = "\n".join(b["lines"]).lower()
            out.append({"kind": b["kind"], "title": b["title"], "text": "\n".join(b["lines"]),
                        "n": sum(1 for t in low_terms if t in body)})
    # 問いの語を最も多く（種類で）含むブロックを「該当」にする（1語含むだけのブロックを全部強調しないように）
    best = max((b["n"] for b in out), default=0)
    for b in out:
        b["hit"] = best > 0 and b.pop("n") == best
    return out


def crumb(text, terms, code_manual=False):
    """チャンクの中で問いの語が出てくる位置が「どの項目のどの節か」（例: DSNT408I › Explanation）"""
    lines = [" ".join(ln.split()) for ln in text.split("\n")]
    low_terms = [t.lower() for t in terms if len(t) > 1]
    hit = next((n for n, ln in enumerate(lines) if any(t in ln.lower() for t in low_terms)), None)
    if hit is None:
        return ""
    entry = section = ""
    for n in range(hit, -1, -1):
        if not section and n < hit and SECTION_HEAD_RE.match(lines[n]):
            section = lines[n]
        v = entry_id_at(lines, n, code_manual)
        if v:
            entry = v
            break
    if entry and not section and n == hit:
        return f"{entry}（項目の見出し）"
    if entry:
        return f"{entry} › {section or 'メッセージ本文'}"
    return f"› {section}" if section else ""


INDEX_LINE_RE = re.compile(r"\s\d{1,5}(?:,\s*\d{1,5})*$")   # 索引の行（「語 123, 456」）
PROSE_WORD_RE = re.compile(r"^[A-Za-z][a-z]{2,}[.,;:]?$")    # 普通の英単語


def is_junk(text):
    """
    意味検索・語の一致の上位を占めがちだが、答えにならないチャンク:
    目次（点線とページ番号）、索引（語 + ページ番号の並び）、16進ダンプ・証明書・ロケール表など英文の少ないもの
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return True
    if sum(1 for ln in lines if TOC_LINE_RE.search(ln)) >= 3:
        return True
    if len(lines) >= 8 and sum(1 for ln in lines if INDEX_LINE_RE.search(ln)) >= 0.5 * len(lines):
        return True
    tokens = text.split()
    if sum(1 for t in tokens if ENCODED_RE.match(t)) >= 5:
        return True   # 証明書・base64 などの符号化されたデータ
    return len(tokens) >= 30 and sum(1 for t in tokens if PROSE_WORD_RE.match(t)) < 0.3 * len(tokens)


ENCODED_RE = re.compile(r"^(?=.*\d)(?=.*[a-z])(?=.*[A-Z])[A-Za-z0-9+/=]{24,}$")


def mostly_non_ascii(q):
    """日本語など、ASCII 以外の文字が半分以上の問い"""
    chars = [c for c in q if not c.isspace()]
    return bool(chars) and sum(1 for c in chars if ord(c) > 127) >= 0.5 * len(chars)


def id_clause(ident):
    """1つの識別子を FTS の条件に。S0C4 は 0C4 でも、013-18 は "013 18" の並びでも当たるように"""
    alts = [fts_quote(ident)]
    if re.fullmatch(r"[SU][0-9A-F]{3}", ident):
        alts.append(fts_quote(ident[1:]))
    if "-" in ident:
        alts = [fts_quote(ident.replace("-", " "))]
    return "(" + " OR ".join(alts) + ")"


class Engine:
    def __init__(self, corpus: Path):
        self.corpus = corpus
        self.local = threading.local()
        self.manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
        con = self.con()
        cols = {r[1] for r in con.execute("PRAGMA table_info(docs)")}
        if "product" not in cols:
            sys.exit("index.db が古い形式です（製品名の列がありません）。"
                     f"python build_index.py {corpus} を実行し直してください")
        self._offsets, self._profiles, self._soc = {}, {}, {}
        self._gpu_lock = threading.Lock()
        # 読み込んだ時点の索引・ベクトル・manifest の更新日時（サーバーが古い索引のまま動いていないか確かめる用）
        self.file_stamps = {f: (corpus / f).stat().st_mtime for f in STAMP_FILES if (corpus / f).exists()}
        self.docs = {r[0]: {"path": r[1], "product": r[2], "version": r[3], "form": r[4], "title": clean_title(r[5])}
                     for r in con.execute("SELECT id, path, product, version, form, title FROM docs")}
        counts = {}
        for d in self.docs.values():
            counts[doc_label(d)] = counts.get(doc_label(d), 0) + 1
        # 絞り込み用の一覧（文書の多い順）
        self.labels = [{"label": k, "docs": n} for k, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]
        # 同じ資料の別の版: 資料番号の末尾（版の番号）を除いた部分が同じ文書をまとめる
        # 例: GC27-1234-00 と GC27-1234-90、SC14-7509-60 と SC14-7509-70。資料番号の無いものは family_key を参照
        self.families = {}
        for i, d in self.docs.items():
            k = family_key(d)
            if k:
                self.families.setdefault(k, []).append(i)
        self.vecs = self._emb = self.vmeta = self._newest = None
        vmeta = corpus / "vectors.json"
        if vmeta.exists():
            self.vmeta = meta = json.loads(vmeta.read_text())
            if meta["done"] < meta["rows"]:
                print(f"注意: ベクトル化が途中です（{meta['done']}/{meta['rows']}）。済んだ分だけ使います",
                      file=sys.stderr)
            self.vecs = np.memmap(corpus / "vectors.f16", dtype=np.float16, mode="r",
                                  shape=(meta["rows"], meta["dim"]))[: meta["done"]]
        else:
            print("vectors.* が無いので全文検索のみで動きます", file=sys.stderr)

    @property
    def emb(self):
        """埋め込みモデルは意味検索を初めて使うときに読み込む（ページ表示や版の比較だけなら読み込まない）"""
        if self._emb is None and self.vmeta:
            from embedder import Embedder
            print(f"埋め込みモデル {self.vmeta['model']} を読み込み中…", file=sys.stderr)
            self._emb = Embedder(self.vmeta["model"])
        return self._emb

    def con(self):
        if not hasattr(self.local, "con"):
            self.local.con = sqlite3.connect(self.corpus / "index.db", check_same_thread=False)
        return self.local.con

    # ---- 各検索 ----
    def fts(self, match, limit):
        try:
            rows = self.con().execute(
                "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?",
                (match, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows]

    def texts(self, chunk_ids):
        """{chunk id: (doc_id, 本文)}"""
        out, ids = {}, list(chunk_ids)
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for cid, doc_id, text in self.con().execute(
                    f"SELECT id, doc_id, text FROM chunks WHERE id IN ({','.join('?' * len(part))})", part):
                out[cid] = (doc_id, text)
        return out

    def drop_junk(self, chunk_ids):
        """目次・索引・ダンプなど答えにならないチャンクを除く（識別子の一覧には使わない。そちらは段で後ろに回す）"""
        t = self.texts(chunk_ids)
        return [c for c in chunk_ids if c in t and not is_junk(t[c][1])]

    def by_definition(self, chunk_ids, ids):
        """識別子の検索結果を、定義らしいもの → 見出しとして出るもの → 言及だけ、の順に並べ替える（同じ段では元の順）"""
        if not chunk_ids:
            return chunk_ids
        con = self.con()
        want = set(chunk_ids) | {c + 1 for c in chunk_ids}   # 定義の見出しが次のチャンクにずれることがある
        rows = {}
        want = list(want)
        for i in range(0, len(want), 500):
            part = want[i:i + 500]
            for cid, doc_id, text in con.execute(
                    f"SELECT id, doc_id, text FROM chunks WHERE id IN ({','.join('?' * len(part))})", part):
                rows[cid] = (doc_id, text)

        def tier(cid):
            doc_id, text = rows[cid]
            nxt = rows.get(cid + 1)
            if nxt and nxt[0] == doc_id:
                text += "\n" + nxt[1]
            code_manual = bool(CODE_MANUAL_RE.search(self.docs[doc_id]["title"] or ""))
            s = definition_score(text, ids, code_manual)
            return 0 if s >= DEF_TIERS[0] else 1 if s >= DEF_TIERS[1] else 2

        tiers = {c: tier(c) for c in chunk_ids}
        order = {cid: n for n, cid in enumerate(chunk_ids)}
        return sorted(chunk_ids, key=lambda c: (tiers[c], order[c])), tiers

    def def_preference(self, chunk_ids, ids):
        """
        定義の段のチャンクの中での優先度（小さいほど先）。
        システム・アベンド（S0C4、S013）は z/OS MVS System Codes の「System completion codes」の章を先にする
        （同じ本の Wait state codes の章や、他製品の資料にも同じ3桁の項目がある）。
        SQLCODE（-904）は Codes の本を先にする（Messages の本にも同じ形の行がある）
        """
        sabend = any(re.fullmatch(r"S[0-9A-F]{3}", i) for i in ids)
        sql = any(i[:1] in "+-" for i in ids)
        if not chunk_ids:
            return {}
        import triage
        out = {}
        rows = self.con().execute(f"SELECT id, doc_id, page FROM chunks WHERE id IN ({','.join('?' * len(chunk_ids))})",
                                  chunk_ids).fetchall()
        for cid, doc_id, page in rows:
            title = self.docs[doc_id]["title"] or ""
            ok = True
            if sabend:
                ok = "System Codes" in title and "system completion codes" in triage.chapter(self, doc_id, page).lower()
            elif sql:   # SQLCODE は Db2 のもの
                ok = bool(re.search(r"\bCodes\b", title)) and \
                    (self.docs[doc_id]["product"] or "").lower().startswith("db2")
            # 同じ資料の版が複数あるときは、新しい刷の定義を先にする（z/OS 3.1 と 3.2 の両方に同じ項目がある）
            out[cid] = (0 if ok else 2) + (0 if self.is_newest(doc_id) else 1)
        return out

    def edition_key(self, doc_id):
        """同じ系列の中での新しさの並びのキー。資料番号があれば刷の番号（末尾）、無ければ版"""
        d = self.docs[doc_id]
        if not d["form"]:
            return (version_tuple(d["version"]), -1)
        m = re.search(r"-(\d+)$", d["form"])
        return ((), int(m.group(1)) if m else -1)

    def is_newest(self, doc_id):
        """
        同じ資料の系列の中で最も新しい文書か。資料番号があれば刷の番号（末尾）、無ければ版で比べる。
        系列に入らない文書は True
        """
        if self._newest is None:
            self._newest = {max(ids, key=self.edition_key) for ids in self.families.values()}
        k = family_key(self.docs[doc_id])
        return not k or len(self.families.get(k, ())) < 2 or doc_id in self._newest

    def gpu_vecs(self):
        """
        ベクトル行列を GPU に載せたもの（初回だけ約0.9GB を転送し、以後は使い回す）。
        GPU が無い・埋め込みモデルが CPU で動いている・ZOS_VEC_GPU=0 のときは None（CPU で計算する）
        """
        if not hasattr(self, "_gpu"):
            with self._gpu_lock:
                if not hasattr(self, "_gpu"):
                    g = None
                    if os.environ.get("ZOS_VEC_GPU", "1") != "0" and getattr(self.emb, "device", "") == "cuda":
                        import torch
                        try:
                            g = torch.from_numpy(np.ascontiguousarray(self.vecs)).to("cuda")
                        except RuntimeError as e:   # GPU メモリ不足など
                            print(f"注意: ベクトルを GPU に載せられないので CPU で計算します（{e}）", file=sys.stderr)
                    self._gpu = g
        return self._gpu

    def vec(self, q, limit):
        if self.vecs is None or len(self.vecs) == 0:
            return []
        qv = self.emb.query(q).astype(np.float32)
        g = self.gpu_vecs()
        if g is not None:
            import torch
            s = g @ torch.from_numpy(qv).to(device=g.device, dtype=g.dtype)
            return [int(i) + 1 for i in torch.topk(s, min(limit, s.shape[0])).indices.cpu().numpy()]
        best_s, best_i = np.empty(0, np.float32), np.empty(0, np.int64)
        block = 262144
        for lo in range(0, len(self.vecs), block):
            s = np.asarray(self.vecs[lo:lo + block], dtype=np.float32) @ qv
            k = min(limit, len(s))
            idx = np.argpartition(-s, k - 1)[:k]
            best_s = np.concatenate([best_s, s[idx]])
            best_i = np.concatenate([best_i, idx + lo])
            if len(best_s) > limit:
                keep = np.argpartition(-best_s, limit - 1)[:limit]
                best_s, best_i = best_s[keep], best_i[keep]
        order = np.argsort(-best_s)
        return [int(i) + 1 for i in best_i[order]]  # 行 → chunk id

    # ---- 統合 ----
    def search(self, q, k=10, mode="auto", product=None, ver=None, label=None):
        q = q.strip()
        if not q:
            return {"results": [], "ids": []}
        ids = identifiers(q)
        words = [w for w in WORD_RE.findall(q) if len(w) > 1]
        keep = doc_filter(product, ver, label)
        pool = 1000 if keep else 150
        ranked = []  # (重み, [chunk id の順位リスト])
        tiers = {}   # 識別子を含むチャンクの段（0: 定義 1: 見出し 2: 言及）。含まないものは 3

        if mode in ("auto", "fts"):
            if ids:
                # 識別子は多くのページに出る（S0C4 は300チャンク超）ので、定義を取りこぼさないよう広めに取って並べ替える
                hits = self.fts(" AND ".join(id_clause(i) for i in ids), 2000)
                hits, tiers = self.by_definition(hits, ids)
                ranked.append((3.0, hits))
            if words:
                # 日本語の問いに混じる英字（Db2・SMF・MQ など）はたいてい製品名で、それを含むだけのページを
                # 押し上げてしまうので、意味検索より軽く扱う（識別子を含む問いは除く）
                w = 0.3 if (mostly_non_ascii(q) and not ids and mode == "auto") else 1.0
                ranked.append((w, self.drop_junk(self.fts(" OR ".join(fts_quote(w) for w in words), pool))))
        if mode in ("auto", "vec"):
            ranked.append((1.0, self.drop_junk(self.vec(q, pool))))

        score = {}
        for w, lst in ranked:
            for r, cid in enumerate(lst):
                score[cid] = score.get(cid, 0.0) + w / (RRF_K + r + 1)
        if not score:
            return {"results": [], "ids": ids}

        con = self.con()
        # 識別子を含む問いでは、定義らしさの段を優先し、同じ段の中を統合の点数で並べる
        # （語の一致の一覧が、識別子に何度も触れているだけのページを押し上げるのを防ぐ）
        pref = self.def_preference([c for c, t in tiers.items() if t == 0], ids) if tiers else {}
        cand = sorted(score, key=lambda c: (tiers.get(c, 3) if tiers else 0, pref.get(c, 0), -score[c]))
        out, seen = [], set()
        for i in range(0, len(cand), 500):
            part = cand[i:i + 500]
            rows = {r[0]: r[1:] for r in con.execute(
                f"SELECT id, doc_id, page, text FROM chunks WHERE id IN ({','.join('?' * len(part))})", part)}
            for cid in part:
                doc_id, page, text = rows[cid]
                d = self.docs[doc_id]
                if keep and not keep(d):
                    continue
                if (doc_id, page) in seen:   # 同じページは最上位のチャンクだけ
                    continue
                seen.add((doc_id, page))
                out.append({**d, "doc_id": doc_id, "page": page, "score": round(score[cid], 5),
                            "label": doc_label(d), "excerpt": excerpt(text, ids + words),
                            "snippet": snippet(text, [v for i in ids for v in id_variants(i)] + words),
                            "crumb": crumb(text, [v for i in ids for v in id_variants(i)] + words,
                                           bool(CODE_MANUAL_RE.search(d["title"] or ""))),
                            "tier": tiers.get(cid) if tiers else None})
                if len(out) >= k:
                    return {"results": out, "ids": ids}
        return {"results": out, "ids": ids}

    def version_info(self, doc_id):
        """この文書の版をどう判断したか、元ファイル、同じ資料の他の版"""
        d = self.docs[doc_id]
        m = self.manifest.get(d["path"], {})
        auto = m.get("auto", {})
        overridden = m.get("overridden") or []
        if "version" in overridden or "product" in overridden:
            basis = "overrides.tsv で手で指定"
        else:
            basis = {"edition": "版の宣言文から推定", "cover": "表紙から推定"}.get(
                auto.get("version_from"), "推定できず")
        others = []
        if family_key(d):
            for i in self.families.get(family_key(d), []):
                if i != doc_id:
                    o = self.docs[i]
                    others.append({"doc_id": i, "label": doc_label(o), "form": o["form"],
                                   "older": self.edition_key(i) <= self.edition_key(doc_id),
                                   "file": Path(self.manifest.get(o["path"], {}).get("source", o["path"])).name})
        others.sort(key=lambda o: (o["label"], o["form"] or ""))
        return {"basis": basis,
                "evidence": None if "version" in overridden else auto.get("version_evidence"),
                "file": Path(m.get("source") or d["path"]).name,
                "others": others}

    def offsets(self, doc_id):
        """各ページの区切り行のバイト位置（文書ごとに1回だけ読む。以後はそこへ直接移動して読む）"""
        if doc_id not in self._offsets:
            pos, offs = 0, []
            with open(self.corpus / self.docs[doc_id]["path"], "rb") as fh:
                for line in fh:
                    if line.startswith(b"=== PAGE "):
                        offs.append(pos)
                    pos += len(line)
            self._offsets[doc_id] = (offs, pos)
        return self._offsets[doc_id]

    def pages(self, doc_id, first, last=None):
        """first〜last ページを [(ページ番号, 本文), ...] で（範囲外は切り詰める）"""
        offs, size = self.offsets(doc_id)
        if not offs:
            return []
        first = max(1, min(first, len(offs)))
        last = max(first, min(last or first, len(offs)))
        end = offs[last] if last < len(offs) else size
        with open(self.corpus / self.docs[doc_id]["path"], "rb") as fh:
            fh.seek(offs[first - 1])
            raw = fh.read(end - offs[first - 1]).decode("utf-8", "replace")
        parts = PAGE_SPLIT_RE.split(raw)   # ["", 番号, 本文, 番号, 本文, ...]
        return [(int(parts[i]), parts[i + 1].rstrip("\n")) for i in range(1, len(parts) - 1, 2)]

    def page_text(self, doc_id, first, last=None):
        """first〜last ページの本文（範囲外は切り詰める）。(本文, 実際の first, ページ数)"""
        ps = self.pages(doc_id, first, last)
        return "\n".join(t for _, t in ps), (ps[0][0] if ps else 1), len(self.offsets(doc_id)[0])

    def profile(self, doc_id):
        """文書ごとの柱・ページ番号の並びと定型の行（compare.doc_profile。文書ごとに1回だけ作る）"""
        if doc_id not in self._profiles:
            self._profiles[doc_id] = compare.doc_profile(self.pages(doc_id, 1, 10**9))
        return self._profiles[doc_id]

    def page(self, doc_id, page, q=""):
        d = self.docs[doc_id]
        text, page, pages = self.page_text(doc_id, page)
        nxt = self.page_text(doc_id, page + 1)[0] if page < pages else ""
        blocks = page_blocks(text, hit_terms(q), bool(CODE_MANUAL_RE.search(d["title"] or "")), nxt)
        return {**d, "doc_id": doc_id, "page": page, "pages": pages, "label": doc_label(d),
                "info": self.version_info(doc_id), "text": text, "blocks": blocks}

    # ---- 版の違い ----
    def pages_in_doc(self, match, doc_id, limit=8):
        """文書 doc_id の中で FTS 条件に合うページ（関連度順・重複なし）"""
        try:
            rows = self.con().execute(
                "SELECT c.page FROM fts JOIN chunks c ON c.id = fts.rowid "
                "WHERE fts MATCH ? AND c.doc_id = ? ORDER BY bm25(fts) LIMIT ?",
                (match, doc_id, limit * 3)).fetchall()
        except sqlite3.OperationalError:
            return []
        out = []
        for (p,) in rows:
            if p not in out:
                out.append(p)
        return out[:limit]

    def compare(self, doc_id, page, q=""):
        """
        同じ資料の他の版それぞれで、このページに対応する箇所を探して差分を取る。
        あわせて、問い中の識別子が各版の Summary of changes（変更点の章）で触れられていれば返す。
        """
        page_a = self.pages(doc_id, page)[0]
        page = page_a[0]
        ids = identifiers(q or "")
        words = compare.key_words(page_a[1])
        or_words = " OR ".join(fts_quote(w) for w in words)
        info = self.version_info(doc_id)
        others = []
        for o in info["others"]:
            cands = []
            if ids and or_words:
                cands = self.pages_in_doc(" AND ".join(id_clause(i) for i in ids) + f" AND ({or_words})",
                                          o["doc_id"], 4)
            if or_words:
                cands += [p for p in self.pages_in_doc(or_words, o["doc_id"], 4) if p not in cands]
            r = compare.find_counterpart(
                page_a, self.profile(doc_id), cands,
                lambda p, i=o["doc_id"]: self.pages(i, p - 1, p + 1), self.profile(o["doc_id"]))
            others.append({**o, **r})
        return {"doc_id": doc_id, "page": page, "label": doc_label(self.docs[doc_id]),
                "others": others, "changes": self.change_mentions(doc_id, ids, info)}

    def soc(self, doc_id):
        """文書の「Summary of changes」の章を読んだ結果（文書ごとに1回だけ読む）"""
        if doc_id not in self._soc:
            self._soc[doc_id] = compare.summary_of_changes(self.pages(doc_id, 1, 60))
        return self._soc[doc_id]

    def change_mentions(self, doc_id, ids, info=None):
        """識別子が各版の「Summary of changes」で新規・変更・削除として挙がっているか"""
        if not ids:
            return []
        info = info or self.version_info(doc_id)
        fam = [doc_id] + [o["doc_id"] for o in info["others"]]
        rxs = [re.compile(r"(?<![\w$#@])" + re.escape(x) + r"(?![\w$#@])", re.I) for x in ids]
        out = []
        for i in fam:
            for page, release, kind, line in self.soc(i):
                if any(r.search(line) for r in rxs):
                    out.append({"doc_id": i, "label": doc_label(self.docs[i]), "form": self.docs[i]["form"],
                                "page": page, "release": release, "kind": kind, "line": line[:200]})
        return out


def snippet(text, terms, before=2, after=6, maxlen=700):
    """画面用の抜粋: ヒットした行の前後数行を、改行を残したまま（見出しと本文が詰まって読みにくくならないように）"""
    lines = [" ".join(ln.split()) for ln in text.split("\n")]
    lines = [ln for ln in lines if ln and not SNIPPET_NOISE_RE.match(ln)]   # ページ下部の柱・著作権表示は除く
    low = [ln.lower() for ln in lines]
    idx = next((i for i, ln in enumerate(low) if any(t.lower() in ln for t in terms if len(t) > 1)), 0)
    lo, hi = max(0, idx - before), min(len(lines), idx + after + 1)
    s = "\n".join(lines[lo:hi])
    if len(s) > maxlen:
        s = s[:maxlen].rsplit(" ", 1)[0] + " …"
    return ("…\n" if lo else "") + s + ("\n…" if hi < len(lines) and not s.endswith("…") else "")


# 抜粋から除く行: 著作権表示と、「258 z/OS: z/OS MVS System Messages…」「IBM MQ Reference 716」のような柱
SNIPPET_NOISE_RE = re.compile(r"^(©\s*Copyright\b|\d{1,5}\s+[^.]{3,80}:\s.{3,80}$|[^.:]{3,80}:\s[^.]{3,80}\s\d{1,5}$)")

TITLE_NOISE_RES = [re.compile(r"\s*\(?Last updated:?\s*\d{4}-\d{2}-\d{2}\)?", re.I),
                   re.compile(r"\s*\(\d{4}-\d{2}-\d{2} edition\)", re.I)]


def clean_title(t):
    """書名から更新日などを除く（「… Messages Last updated: 2026-10-28」→「… Messages」）"""
    for rx in TITLE_NOISE_RES:
        t = rx.sub("", t or "")
    return t.strip()


def excerpt(text, terms, width=360):
    low = text.lower()
    pos = min((p for p in (low.find(t.lower()) for t in terms) if p >= 0), default=0)
    lo = max(0, pos - width // 3)
    if lo:  # 語の途中から始めない
        sp = text.rfind(" ", 0, lo)
        lo = sp + 1 if sp >= 0 and lo - sp < 40 else lo
    s = text[lo:lo + width]
    return ("…" if lo else "") + re.sub(r"\s+", " ", s) + ("…" if lo + width < len(text) else "")


# ---------------- Web UI ----------------
HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>マニュアル検索</title><style>
:root{--bg:#f7f7f5;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e2e1dc;--acc:#2f5d9e;--hl:#ffe9a8;--del:#fbe3e1;--add:#e0f1e3;--warn:#a5531a;--hitbg:#f1f5fc;--dim:#77756f}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#161615;--fg:#e8e7e2;--mut:#9a9993;--card:#21211f;--line:#34332f;--acc:#8fb4ee;--hl:#6b5714;--del:#4a2523;--add:#1f3d27;--warn:#e9a46c;--hitbg:#1c2433;--dim:#8f8d87}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Hiragino Sans","Noto Sans JP",sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:8px;flex-wrap:wrap;z-index:2}
input,select,button{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
#q{flex:1;min-width:240px}button{cursor:pointer}select{max-width:100%;min-width:0}#ver{flex:0 1 auto}
main{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;padding:16px}
@media (max-width:900px){main{grid-template-columns:1fr}}
.r{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:10px;cursor:pointer}
.r:hover,.r.on{border-color:var(--acc)}.r.on{box-shadow:0 0 0 1px var(--acc)}
.meta{color:var(--mut);font-size:13px}.t{font-weight:600;font-size:15px;line-height:1.45}
.r .meta{margin:2px 0 6px}
.sn{white-space:pre-line;font-size:14px;line-height:1.65;overflow-wrap:anywhere}
.hd{font-weight:700;color:var(--acc)}
mark{background:var(--hl);color:inherit;border-radius:2px;padding:0 1px}#info{color:var(--mut);font-size:13px;margin-bottom:8px}
#view{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;position:sticky;top:72px;max-height:calc(100vh - 90px);overflow:auto}
#view .page{white-space:pre-wrap;font-size:15px;line-height:1.75;margin:12px 0 0;overflow-wrap:anywhere}
.nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap;position:sticky;top:-14px;margin:-14px -16px 0;padding:10px 16px;background:var(--card);border-bottom:1px solid var(--line);z-index:1}
.vb{border:1px solid var(--line);border-radius:6px;padding:6px 10px;margin:8px 0;font-size:13px}
.vb>summary{cursor:pointer;color:var(--mut)}.vb>summary b{color:var(--fg)}.vb[open]>summary{margin-bottom:6px}
.ev{font-size:12.5px;line-height:1.55;color:var(--mut);margin:4px 0}
/* 結果カードの「どこに当たったか」 */
.where{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0 0 6px;font-size:13px}
.tag{display:inline-block;border-radius:4px;padding:0 6px;font-size:12px;font-weight:700;line-height:1.7}
.tag.def{background:var(--acc);color:var(--card)}.tag.head{border:1px solid var(--acc);color:var(--acc)}
.tag.ment{border:1px solid var(--line);color:var(--mut)}.crumb{font-weight:600}
/* ページ表示のブロック（1つの項目 = 1つの枠） */
.blk{border:1px solid var(--line);border-radius:8px;margin:12px 0;padding:10px 14px;white-space:pre-wrap;
 font-size:15px;line-height:1.75;overflow-wrap:anywhere}
.blk .bh{display:flex;gap:8px;align-items:center;font-weight:700;font-size:16px;margin-bottom:4px;white-space:normal}
.blk.hit{border:2px solid var(--acc);background:var(--hitbg)}
.blk.para{border:none;border-left:3px solid transparent;border-radius:0;margin:6px 0;padding:2px 12px}
.blk.para.hit{border-left-color:var(--acc);background:var(--hitbg)}
.blk.dim{color:var(--dim)}.blk.cont .bh{font-weight:400;font-size:13px;color:var(--mut)}
button.sm{font-size:12px;padding:1px 8px;margin-right:6px}
.cmp>div{margin:4px 0}.st-diff{color:var(--warn);font-weight:600}.st-same{color:var(--mut)}
.df{font:13px/1.6 ui-monospace,Menlo,Consolas,monospace;margin:4px 0 0;white-space:pre-wrap}
.df div{padding:0 6px}.df .d{background:var(--del)}.df .a{background:var(--add)}.df .c{color:var(--mut)}
.df .sep{border-top:1px dashed var(--line);margin:3px 0}
/* ログ解析 */
#tri{margin-bottom:12px}#tri textarea{width:100%;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;color:inherit;
 background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px;resize:vertical}
#tri .bar{display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap}
.logl{font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--mut);white-space:pre-wrap;overflow-wrap:anywhere;margin:2px 0 6px}
.rb{border-left:3px solid var(--warn);padding:4px 10px;margin:6px 0 0;font-size:14px}
.rb .rh{font-weight:700}.rb .gs{color:var(--warn);font-size:12px;font-weight:700}
.rb.ng{border-left-color:var(--line);color:var(--mut)}
.rowhl{background:var(--hl);outline:2px solid var(--warn);border-radius:2px}
/* 本文中の識別子のリンク・参照箇所・版の比較 */
.idl{color:var(--acc);text-decoration:underline dotted;cursor:pointer}
.refs{margin:6px 0 2px;padding:6px 10px;border:1px dashed var(--line);border-radius:6px;font-size:13px;white-space:normal}
.refs div{margin:2px 0}
#mig{margin-bottom:12px}#mig .bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.mg h4{margin:8px 0 4px;font-size:13px}.mg .ks{display:flex;flex-wrap:wrap;gap:4px}
/* 資料一覧 */
.dl{width:100%;border-collapse:collapse;font-size:13px;background:var(--card)}
.dl th,.dl td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}
.dl th{position:sticky;top:61px;background:var(--bg);z-index:1}.dl tr.g td{font-weight:700;background:var(--hitbg)}
.dl tbody tr:not(.g){cursor:pointer}.dl tbody tr:not(.g):hover td{background:var(--hitbg)}
#docf{width:100%;margin-bottom:8px}
</style></head><body>
<header><input id="q" placeholder="例: IEC161I 040-024 / データセットのOPENでエラー / S0C4" autofocus>
<select id="ver"><option value="">すべての製品・版</option></select>
<select id="mode"><option value="auto">自動</option><option value="fts">全文のみ</option><option value="vec">意味のみ</option></select>
<button id="go">検索</button><button id="trib">ログ解析</button><button id="migb">版の比較</button><button id="docb">資料一覧</button></header>
<main><section><div id="tri" hidden><textarea id="log" rows="9" placeholder="ジョブログ・SYSLOG・ダンプの抜粋を貼り付けてください。メッセージID・アベンドコード・SQLCODE・理由コードを拾い、定義と表の該当行を並べます。&#10;1行で「S806 04」「IEC161I 040」「S0C4 REASON=11」のように引くこともできます。"></textarea>
<div class="bar"><button id="trigo">解析</button><span class="meta">製品・版の絞り込みは上の選択に従います</span></div></div>
<div id="mig" hidden><div class="bar"><select id="mga"></select><span>→</span><select id="mgb"></select><button id="mggo">比べる</button></div>
<div class="meta">同じ資料の2つの版を、メッセージ・コードの項目ごとに比べます（両方の版がコーパスにある資料だけ）</div></div>
<div id="info"></div><div id="res"></div></section>
<section id="view"><span class="meta">結果をクリックするとページ全体を表示します</span></section></main>
<script>
const $=s=>document.querySelector(s);let terms=[];
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function hl(s){let h=esc(s);for(const t of terms){if(t.length<2)continue;
 h=h.replace(new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi'),m=>'<mark>'+m+'</mark>')}return h}
// メッセージ・コードの説明の見出し（太字にして本文と区別する）
const HEAD=/^(Explanation|System action|Severity|User response|Operator response|System programmer response|Programmer response|Problem determination|Administrator response|Response|Module|Source|Routing code|Descriptor code|Related information|Related reference|Related concepts|Related tasks)$/;
// ログ解析から開いたページで強調する表の行（そのページで occ 番目に出てくる、内容が line の行）
let rowMark=null;
const fmt=s=>s.split('\n').map(l=>{let h=HEAD.test(l.trim())?`<span class="hd">${hl(l)}</span>`:hl(l);
 if(rowMark&&l.trim().replace(/\s+/g,' ')===rowMark.line&&rowMark.seen++===rowMark.occ)h=`<span class="rowhl">${h}</span>`;
 return h}).join('\n');
// 空行が続くのを1つにまとめ、行末の空白を除く
const tidy=s=>s.replace(/[ \t]+$/gm,'').replace(/\n{3,}/g,'\n\n').trim();
// 結果カード: 識別子の定義か言及か、どの項目のどの節に当たったか
const TIER=[['def','定義'],['ment','言及'],['ment','言及']];   // 段1（見出しの形の言及）も利用者には言及として示す
function where(r){const t=r.tier!=null?TIER[r.tier]:null;if(!t&&!r.crumb)return'';
 return `<div class="where">${t?`<span class="tag ${t[0]}">${t[1]}</span>`:''}${r.crumb?`<span class="crumb">${esc(r.crumb)}</span>`:''}</div>`}
// ページ表示: 項目（メッセージ・コードの説明）ごとの枠。問いに当たった項目を強調し、他は薄くする
function blocks(bs,more){if(!bs||!bs.length)return '';const any=bs.some(b=>b.hit);
 return bs.map((b,i)=>{const cls=['blk',b.kind,b.hit?'hit':(any?'dim':'')].join(' ');
  // ページ最後の項目は次のページに続くことが多いので、続きへのボタンを付ける
  const tail=more&&i===bs.length-1&&b.kind!=='para'?'\n<button class="sm next-pg">続きは次のページ →</button>':'';
  const head=b.kind==='entry'?`<div class="bh">${hl(b.title)}${b.hit?' <span class="tag def">該当</span>':''}<button class="sm" data-refs="${esc(b.title)}">参照箇所</button></div>`
   :b.kind==='cont'?`<div class="bh">（前のページから続く項目の残り）</div>`
   :(b.hit?'<div class="bh"><span class="tag def">該当</span></div>':'');
  // 識別子だけの1行目は枠の見出しに出したので除く（「IEF450I jobname …」のように本文が続く行は残す）
  const ls=b.text.split('\n'),body=b.kind==='entry'&&ls[0].trim()===b.title?ls.slice(1).join('\n'):b.text;
  return `<div class="${cls}">${head}${links(fmt(body))}${tail}</div>`}).join('')}
// 本文中のメッセージID（末尾が英字のもの、$HASPnnn）を、定義へ飛べるリンクにする
const IDL=/(?<![\w$#@&;\/.\-"=])(\$HASP\d{3,4}|[A-Z]{3,5}\d{3,5}[A-Z])(?![\w$#@])/g;
const links=h=>h.replace(IDL,m=>`<span class="idl" data-id="${m}">${m}</span>`);
const lab=r=>`${r.label} ・ ${r.form||'番号不明'} ・ p.${r.page}`;
fetch('api/labels').then(r=>r.json()).then(v=>v.forEach(x=>{const o=document.createElement('option');o.value=x.label;o.textContent=`${x.label}（${x.docs}）`;$('#ver').append(o)}));
async function search(){const q=$('#q').value.trim();if(!q)return;
 terms=(q.match(/[A-Za-z0-9$#@]+/g)||[]);$('#info').textContent='検索中…';
 const p=new URLSearchParams({q,label:$('#ver').value,mode:$('#mode').value,k:30});
 const d=await (await fetch('api/search?'+p)).json();
 $('#info').textContent=(d.ids.length?'識別子 '+d.ids.join(', ')+' を優先 ・ ':'')+d.results.length+' 件';
 $('#res').innerHTML='';d.results.forEach(r=>{const e=document.createElement('div');e.className='r';
  e.innerHTML=`<div class="t">${esc(r.title||r.path)}</div><div class="meta">${esc(lab(r))}</div>${where(r)}<div class="sn">${fmt(r.snippet||r.excerpt)}</div>`;
  e.onclick=()=>{document.querySelectorAll('.r.on').forEach(x=>x.classList.remove('on'));e.classList.add('on');show(r.doc_id,r.page)};
  $('#res').append(e)});
 if(d.results[0])$('#res .r').click()}
let showSeq=0,cur=null;const hist=[];   // hist: リンクで移動する前のページ（「戻る」用）
// opt: ログ解析から開くとき {q: 強調する識別子, mark: {page, line, occ}}
async function show(doc,page,opt){const my=++showSeq;const q=opt&&opt.q!=null?opt.q:$('#q').value;
 const d=await (await fetch(`api/page?doc_id=${doc}&page=${page}&q=${encodeURIComponent(q)}`)).json();
 if(my!==showSeq)return;
 rowMark=opt&&opt.mark&&opt.mark.page===d.page?{...opt.mark,seen:0}:null;
 const sub=(p)=>show(doc,p,opt&&{q:opt.q,mark:opt.mark});
 $('#view').innerHTML=`<div class="nav"><button ${d.page<=1?'disabled':''} id="pv">←</button>
 <span class="meta">${esc(lab(d))} / ${d.pages}</span><button ${d.page>=d.pages?'disabled':''} id="nx">→</button></div>
 <div class="t">${esc(d.title||'')}</div>${verbox(d)}<details class="vb cmp" id="cmp"><summary>他の版との比較を調べています…</summary></details>${d.blocks&&d.blocks.length?`<div class="page-blocks">${blocks(d.blocks,d.page<d.pages)}</div>`:`<div class="page">${links(fmt(tidy(d.text)))}</div>`}`;
 cur={doc,page:d.page,q};
 if(hist.length){const b=document.createElement('button');b.textContent='↩ 戻る';b.title='リンクで移動する前のページへ';
  b.onclick=()=>{const h=hist.pop();terms=idTerms(h.q||'').concat((h.q||'').match(/[A-Za-z0-9$#@]+/g)||[]);show(h.doc,h.page,{q:h.q})};$('#view .nav').prepend(b)}
 // 本文中の識別子 → 定義へ。項目の「参照箇所」→ その識別子に触れているページの一覧
 document.querySelectorAll('#view .idl').forEach(a=>a.onclick=async()=>{const id=a.dataset.id;
  const ds=await (await fetch('api/define?id='+encodeURIComponent(id))).json();
  if(!ds.length){a.title='コーパスに定義が見つかりません';a.style.textDecoration='line-through';return}
  hist.push(cur);terms=idTerms(id);show(ds[0].doc_id,ds[0].page,{q:id})});
 document.querySelectorAll('#view [data-refs]').forEach(b=>b.onclick=async()=>{const id=b.dataset.refs,blk=b.closest('.blk');
  if(blk.querySelector('.refs'))return blk.querySelector('.refs').remove();
  const box=document.createElement('div');box.className='refs';box.textContent='探しています…';b.parentElement.after(box);
  const rs=await (await fetch('api/refs?id='+encodeURIComponent(id))).json();
  box.innerHTML=rs.length?`<div class="meta">${esc(id)} に触れているページ ${rs.length} 件（定義のページを除く）</div>`+rs.map((r,i)=>
   `<div>${esc(r.label)} ・ ${esc(r.title)} ・ p.${r.page}${r.crumb?` <span class="crumb">${esc(r.crumb)}</span>`:''} <button class="sm" data-ref="${i}">開く</button></div>`).join('')
   :'<div class="meta">他に触れているページはありません</div>';
  box.querySelectorAll('[data-ref]').forEach(x=>x.onclick=()=>{const r=rs[+x.dataset.ref];hist.push(cur);terms=idTerms(id);show(r.doc_id,r.page,{q:id})})});
 rowMark=null;
 $('#pv').onclick=()=>sub(d.page-1);$('#nx').onclick=()=>sub(d.page+1);
 document.querySelectorAll('#view .next-pg').forEach(b=>b.onclick=()=>sub(d.page+1));
 // 表の該当行（ログ解析から）→ 該当の項目 → 最初の強調語 → ページの先頭、の順に探してそこまで移動する
 const v=$('#view'),m=v.querySelector('.rowhl')||v.querySelector('.blk.hit')||v.querySelector('.page mark'),nav=v.querySelector('.nav');
 v.scrollTop=m?Math.max(0,m.getBoundingClientRect().top-v.getBoundingClientRect().top+v.scrollTop-nav.offsetHeight-8):0;
 document.querySelectorAll('#view [data-label]').forEach(b=>b.onclick=()=>{$('#ver').value=b.dataset.label;search()});
 // 他の版との違いの一覧（どちらが古い版かはサーバーが判断した older を使う）
 document.querySelectorAll('#view [data-mig]').forEach(b=>b.onclick=()=>{const o=d.info.others.find(x=>x.doc_id===+b.dataset.mig);
  migrate(o.older?`a=${o.doc_id}&b=${doc}`:`a=${doc}&b=${o.doc_id}`)});
 const c=await (await fetch(`api/compare?doc_id=${doc}&page=${d.page}&q=${encodeURIComponent(q)}`)).json();
 if(my!==showSeq)return;   // 比較を待つ間に別のページが開かれた
 const box=$('#cmp');box.innerHTML=cmpbox(c,d.label);
 box.open=c.others.some(o=>o.status!=='same')||c.changes.length>0;   // 違いがあるときだけ開いておく
 if(box.open){   // ヒット箇所までスクロールすると比較の欄が隠れるので、上の行から飛べるようにする
  const b=document.createElement('button');b.className='sm';b.innerHTML='<b class="st-diff">他の版と差異あり</b>';
  b.onclick=()=>{const v=$('#view'),nav=$('#view .nav');   // 右の欄の中だけで、上の固定の行の下に来るように
   v.scrollTop+=box.getBoundingClientRect().top-v.getBoundingClientRect().top-nav.offsetHeight-6};
  $('#view .nav').append(b)}
 document.querySelectorAll('#cmp [data-open]').forEach(b=>b.onclick=()=>{const[x,y]=b.dataset.open.split(':');show(+x,+y)})}
const ST={same:'差異なし',diff:'差異あり',notfound:'対応箇所が見つからない（その版に無いか、大きく書き換わった）'};
function cmpbox(c,label){
 // たたんだときの1行: 他の版の数と違いの有無、変更点の章の記載の有無
 const sum=!c.others.length?'他の版なし':c.others.map(o=>`${esc(o.label)}: ${o.status==='diff'?`<b class="st-diff">差異あり ${o.hunks.length} 箇所</b>`:o.status==='same'?'差異なし':'<b class="st-diff">対応箇所なし</b>'}`).join(' ・ ');
 let h=`<summary><b>他の版との比較</b> ・ ${sum}${c.changes.length?` ・ <b class="st-diff">変更点の章に記載 ${c.changes.length} 件</b>`:''}</summary>`;
 if(!c.others.length)h+='<div class="meta">同じ資料の他の版はコーパスにありません</div>';
 for(const o of c.others){
  h+=`<div>${esc(o.label)} (${esc(o.form||'資料番号なし')})${o.page?` p.${o.page} 付近`:''}: <span class="st-${o.status}">${ST[o.status]}${o.status==='diff'?` ${o.hunks.length} 箇所`:''}</span>`
   +` <span class="meta">一致率 ${Math.round(o.match*100)}%</span>`
   +(o.page?` <button class="sm" data-open="${o.doc_id}:${o.page}">そのページを開く</button>`:'')+'</div>';
  if(o.hunks.length){h+=`<details open><summary class="meta">差分（赤: ${esc(label)} だけ / 緑: ${esc(o.label)} だけ）</summary><div class="df">`
   +o.hunks.map(x=>x.before.map(l=>`<div class="c">${esc(l)}</div>`).join('')+x.a.map(l=>`<div class="d">− ${esc(l)}</div>`).join('')
   +x.b.map(l=>`<div class="a">+ ${esc(l)}</div>`).join('')).join('<div class="sep"></div>')+'</div></details>'}}
 if(c.changes.length){h+='<div style="margin-top:6px"><b>変更点の章（Summary of changes）での記載</b></div>';
  for(const m of c.changes)h+=`<div>${m.kind?`<span class="st-diff">【${esc(m.kind)}】</span>`:''}${esc(m.release||'Summary of changes')}「${esc(m.line)}」 <span class="meta">${esc(m.label)} p.${m.page}</span> <button class="sm" data-open="${m.doc_id}:${m.page}">開く</button></div>`}
 return h}
function verbox(d){const i=d.info;
 // たたんだときの1行: 製品と版、その判断のしかた。開くと根拠の文・元ファイル・他の版
 let h=`<details class="vb"><summary><b>${esc(d.label)}</b> ・ ${esc(i.basis)} ・ ${esc(d.form||'資料番号不明')}</summary>`;
 if(i.evidence)h+=`<div class="ev">根拠:「${esc(i.evidence)}」</div>`;
 h+=`<div class="meta">元ファイル: ${esc(i.file)}</div>`;
 if(i.others.length)h+=`<div class="meta">同じ資料の他の版: `+i.others.map(o=>
  `${esc(o.label)} (${esc(o.form||'資料番号なし')}) <button class="sm" data-label="${esc(o.label)}">この版で検索</button><button class="sm" data-mig="${o.doc_id}">この版との違いを一覧</button>`).join(' ')+`</div>`;
 return h+'</details>'}
$('#go').onclick=search;$('#q').onkeydown=e=>{if(e.key==='Enter')search()};
// ---- ログ解析 ----
$('#trib').onclick=()=>{const t=$('#tri');t.hidden=!t.hidden;if(!t.hidden)$('#log').focus()};
$('#trigo').onclick=triage;$('#log').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))triage()};
const idTerms=id=>/^[SU][0-9A-F]{3,4}$/.test(id)?[id,id.slice(1)]:[id];   // S0C4 は本文では 0C4
async function triage(){const log=$('#log').value;if(!log.trim())return;$('#info').textContent='解析中…';
 const d=await (await fetch('api/triage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({log,label:$('#ver').value})})).json();
 if(d.error){$('#info').textContent='エラー: '+d.error;return}
 $('#info').textContent=`${d.findings.length} 件（ログに出てきた順）`+(d.missing.length?` ・ 定義が見つからなかったもの: ${d.missing.join(', ')}`:'');
 $('#res').innerHTML='';
 d.findings.forEach(f=>{const e=document.createElement('div');e.className='r';const df=f.defs[0];
  const ln=f.lines.length>1?`${f.lines[0]}行目 ほか${f.lines.length-1}回`:`${f.lines[0]}行目`;
  let h=`<div class="t">${esc(f.id)} <span class="tag ment">${esc(f.kind_ja)}</span></div>`
   +`<div class="logl">ログ ${ln}: ${esc(f.sample)}</div>`
   +`<div class="where"><span class="tag def">定義</span><span>${esc(df.label)} ・ ${esc(df.form||'番号不明')} ・ p.${df.page} ${esc(df.title)}</span></div>`;
  f.rows.forEach((r,i)=>{
   if(r.missing){h+=`<div class="rb ng">コード ${esc(r.code)}: 定義の項目の中に表の行が見つかりません（ページを開いて確認してください）</div>`;return}
   h+=`<div class="rb" data-row="${i}"><span class="rh">コード ${esc(r.code)} → p.${r.page} の行「${esc(r.line)}」</span>`
    +(r.via?` <span class="meta">${esc(r.via)} の説明の中</span>`:'')
    +(r.guessed?' <span class="gs">説明は表の組みから推定（原文で確認）</span>':'')
    +(r.text?`<div class="sn">${esc(r.text)}</div>`:'')+'</div>'});
  if(f.defs.length>1)h+='<div class="meta" style="margin-top:6px">他の定義: '+f.defs.slice(1).map((o,i)=>`<button class="sm" data-def="${i+1}">${esc(o.label)} p.${o.page}</button>`).join('')+'</div>';
  e.innerHTML=h;
  const open=(doc,page,mark,id)=>{document.querySelectorAll('.r.on').forEach(x=>x.classList.remove('on'));e.classList.add('on');
   terms=idTerms(id);show(doc,page,{q:id,mark})};
  // カードは最初の表の行（無ければ定義）を開く。表の行の枠・他の定義のボタンはそれぞれを開く
  const r0=f.rows.find(r=>!r.missing);
  e.onclick=()=>r0?open(r0.doc_id,r0.page,r0,r0.via||f.id):open(df.doc_id,df.page,null,f.id);
  e.querySelectorAll('[data-row]').forEach(b=>b.onclick=ev=>{ev.stopPropagation();const r=f.rows[+b.dataset.row];open(r.doc_id,r.page,r,r.via||f.id)});
  e.querySelectorAll('[data-def]').forEach(b=>b.onclick=ev=>{ev.stopPropagation();const o=f.defs[+b.dataset.def];open(o.doc_id,o.page,null,f.id)});
  $('#res').append(e)});
 if(d.findings[0])$('#res .r').click()}
// ---- 資料一覧（検索の対象になっている資料） ----
let docList=null;
$('#docb').onclick=async()=>{$('#info').textContent='読み込み中…';
 if(!docList)docList=await (await fetch('api/docs')).json();
 const pages=docList.reduce((s,d)=>s+(d.pages||0),0);
 $('#res').innerHTML=`<input id="docf" placeholder="書名・資料番号・ファイル名・製品で絞り込み（例: System Codes / SA38 / Db2 / Redbook）"><div id="doct"></div>`;
 const draw=()=>{const f=$('#docf').value.trim().toLowerCase();
  const rows=docList.filter(d=>!f||[d.label,d.title,d.form,d.file].some(x=>(x||'').toLowerCase().includes(f)));
  $('#info').textContent=`検索の対象: ${docList.length} 冊・${pages.toLocaleString()} ページ`+(f?` ・ 絞り込み ${rows.length} 冊`:'')+' ・ 行を押すと1ページ目を開きます';
  let h='<table class="dl"><thead><tr><th>書名</th><th>資料番号</th><th>ページ</th><th>ファイル</th></tr></thead><tbody>',g=null;
  for(const d of rows){if(d.label!==g){g=d.label;h+=`<tr class="g"><td colspan="4">${esc(g)}（${rows.filter(x=>x.label===g).length} 冊）</td></tr>`}
   h+=`<tr data-doc="${d.doc_id}"><td>${esc(d.title||'（書名不明）')}</td><td>${esc(d.form||'')}</td><td>${d.pages||''}</td><td class="meta">${esc(d.file)}</td></tr>`}
  $('#doct').innerHTML=h+'</tbody></table>';
  document.querySelectorAll('#doct [data-doc]').forEach(r=>r.onclick=()=>show(+r.dataset.doc,1,{q:''}))};
 $('#docf').oninput=draw;draw();$('#docf').focus()};
// ---- 版の比較（移行レポート） ----
$('#migb').onclick=()=>{const m=$('#mig');m.hidden=!m.hidden;
 if(!$('#mga').options.length)for(const o of $('#ver').options)if(o.value){$('#mga').append(new Option(o.textContent,o.value));$('#mgb').append(new Option(o.textContent,o.value))}};
$('#mggo').onclick=()=>migrate(`from=${encodeURIComponent($('#mga').value)}&to=${encodeURIComponent($('#mgb').value)}`);
async function migrate(qs){$('#info').textContent='比べています…（大きな資料は数十秒かかります）';
 const r=await (await fetch('api/migrate?'+qs)).json();if(r.error){$('#info').textContent='エラー: '+r.error;return}
 const docs=r.docs||[r];
 $('#info').textContent=docs.length?`比べた資料 ${docs.length} 冊`:'両方の版がある資料はコーパスにありません（両方の版の資料を取り込むと比べられます）';
 $('#res').innerHTML='';
 for(const x of docs){const e=document.createElement('div');e.className='r mg';
  const btn=(k,doc,page,cls)=>`<button class="sm" data-o="${doc}:${page}:${esc(k)}">${esc(k)}${cls?` <span class="meta">p.${page}</span>`:''}</button>`;
  let h=`<div class="t">${esc(x.b.title||x.a.title)}</div><div class="meta">${esc(x.a.label)} (${esc(x.a.form||'資料番号なし')}) → ${esc(x.b.label)} (${esc(x.b.form||'資料番号なし')}) ・ 項目 ${x.entries_a} → ${x.entries_b}</div>`
   +(x.note?`<div class="meta">（${esc(x.note)}）</div>`:'');
  if(!x.entries_a&&!x.entries_b)h+='<div class="meta">メッセージ・コードの項目が無い資料です（Summary of changes の記載だけを示します）</div>';
  else h+=`<div class="where"><span class="tag def">追加 ${x.added.length}</span><span class="tag head">削除 ${x.removed.length}</span><span class="tag ment">変更 ${x.changed.length}</span><span class="meta">同じ ${x.same}${x.unsure?` ・ 判定できず ${x.unsure}（片方の版で項目の見出しを読み取れなかった）`:''}</span></div>`;
  if(x.added.length)h+='<h4>追加（新しい版だけにある）</h4><div class="ks">'+x.added.slice(0,200).map(a=>btn(a.key,x.b.doc_id,a.page,1)).join('')+'</div>';
  if(x.removed.length)h+='<h4>削除（古い版だけにある）</h4><div class="ks">'+x.removed.slice(0,200).map(a=>btn(a.key,x.a.doc_id,a.page,1)).join('')+'</div>';
  if(x.changed.length)h+='<h4>変更（違う行の多い順）</h4>'+x.changed.slice(0,100).map(c=>`<div>${btn(c.key,x.b.doc_id,c.page_b,1)} <span class="meta">違う行 ${c.lines}</span>`
   +(c.hunks[0]?`<div class="df">${c.hunks[0].a.slice(0,2).map(l=>`<div class="d">− ${esc(l)}</div>`).join('')}${c.hunks[0].b.slice(0,2).map(l=>`<div class="a">+ ${esc(l)}</div>`).join('')}</div>`:'')+'</div>').join('');
  const soc=x.soc.filter(s=>s.kind);
  if(soc.length)h+=`<details><summary class="meta">新しい版の Summary of changes（${soc.length} 行）</summary>`+soc.map(s=>`<div>【${esc(s.kind)}】${esc(s.line)} <span class="meta">p.${s.page}</span></div>`).join('')+'</details>';
  e.innerHTML=h;e.style.cursor='default';
  e.querySelectorAll('[data-o]').forEach(b=>b.onclick=()=>{const[doc,page,...k]=b.dataset.o.split(':');const id=k.join(':').replace(/ #\d+$/,'').replace(/\s*\(.*\)$/,'');
   terms=idTerms(id);show(+doc,+page,{q:id})});
  $('#res').append(e)}}
</script></body></html>"""


def serve(engine, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype="application/json; charset=utf-8", code=200):
            b = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            p = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/":
                    self.send(HTML, "text/html; charset=utf-8")
                elif u.path == "/api/labels":
                    self.send(json.dumps(engine.labels, ensure_ascii=False))
                elif u.path == "/api/search":
                    r = engine.search(p.get("q", ""), int(p.get("k", 30)), p.get("mode", "auto"),
                                      p.get("product") or None, p.get("ver") or None,
                                      p.get("label") or None)
                    self.send(json.dumps(r, ensure_ascii=False))
                elif u.path == "/api/page":
                    self.send(json.dumps(engine.page(int(p["doc_id"]), int(p["page"]), p.get("q", "")),
                                         ensure_ascii=False))
                elif u.path == "/api/compare":
                    self.send(json.dumps(engine.compare(int(p["doc_id"]), int(p["page"]), p.get("q", "")),
                                         ensure_ascii=False))
                # CLI と同じ文字列を返す API（他のツールから呼ぶ用。モデルとベクトルを読み込み済みなので速い）
                elif u.path == "/api/text/search":
                    self.send(text_search(engine, p.get("q", ""), int(p.get("k", 10)), p.get("mode", "auto"),
                                          p.get("product") or None, p.get("ver") or None, p.get("diff") == "1"),
                              "text/plain; charset=utf-8")
                elif u.path == "/api/text/diff":
                    self.send(text_diff(engine, p.get("doc", ""), int(p.get("page", 1)), p.get("q", "")),
                              "text/plain; charset=utf-8")
                elif u.path in ("/api/define", "/api/refs"):
                    # 本文中の識別子のリンク（定義へ飛ぶ）と、その識別子を参照している箇所の一覧
                    import triage
                    keep = doc_filter(label=p.get("label") or None)
                    f = triage.definitions if u.path == "/api/define" else triage.references
                    self.send(json.dumps(f(eng=engine, term=p.get("id", ""), keep=keep), ensure_ascii=False))
                elif u.path == "/api/docs":
                    # 検索の対象になっている資料の一覧（検索画面の「資料一覧」）
                    out = []
                    for i, d in engine.docs.items():
                        m = engine.manifest.get(d["path"], {})
                        out.append({"doc_id": i, "label": doc_label(d), "product": d["product"], "version": d["version"],
                                    "form": d["form"], "title": d["title"], "pages": m.get("pages"),
                                    "file": Path(m.get("source") or d["path"]).name})
                    out.sort(key=lambda x: (x["label"], x["title"] or x["file"]))
                    self.send(json.dumps(out, ensure_ascii=False))
                elif u.path == "/api/migrate":
                    import migrate
                    if p.get("a") and p.get("b"):   # 文書どうし（doc_id）
                        r = migrate.report(engine, int(p["a"]), int(p["b"]))
                    else:                           # 製品の版どうし（ラベル）
                        r = migrate.product_report(engine, p.get("from", ""), p.get("to", ""))
                    self.send(json.dumps(r, ensure_ascii=False))
                elif u.path == "/api/text/migrate":
                    import migrate
                    self.send(migrate.text_product_report(engine, p.get("from", ""), p.get("to", "")),
                              "text/plain; charset=utf-8")
                elif u.path == "/api/health":
                    # どの索引・ベクトルを読み込んでいるか（呼び出し側が作り直しを検知して再起動するため）
                    self.send(json.dumps({"pid": os.getpid(), "corpus": str(engine.corpus),
                                          "files": engine.file_stamps}, ensure_ascii=False))
                else:
                    self.send('{"error":"not found"}', code=404)
            except Exception as e:  # noqa: 検索画面にエラーを返す
                self.send(json.dumps({"error": str(e)}, ensure_ascii=False), code=500)

        def do_POST(self):
            # ログの解析（ログは長いので本文で受け取る）。/api/triage は JSON {log, label}、
            # /api/text/triage は本文がログそのもので、CLI と同じ文字列を返す
            import triage
            u = urlparse(self.path)
            p = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 2_000_000:
                    self.send('{"error":"ログが大きすぎます（2MB まで）"}', code=413)
                    return
                body = self.rfile.read(n).decode("utf-8", "replace")
                if u.path == "/api/triage":
                    req = json.loads(body or "{}")
                    self.send(json.dumps(triage.triage(engine, req.get("log", ""), label=req.get("label") or None),
                                         ensure_ascii=False))
                elif u.path == "/api/text/triage":
                    self.send(triage.text_triage(engine, body, p.get("product") or None, p.get("ver") or None),
                              "text/plain; charset=utf-8")
                else:
                    self.send('{"error":"not found"}', code=404)
            except Exception as e:  # noqa
                self.send(json.dumps({"error": str(e)}, ensure_ascii=False), code=500)

    if engine.vmeta:   # 埋め込みモデルとベクトルを先に読み込む（待ち受けを始めた時点で速く答えられるように）
        print("埋め込みモデルとベクトルを読み込み中…", file=sys.stderr)
        engine.vec("warm up", 1)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"http://127.0.0.1:{port} で待ち受け中（Ctrl+C で終了）", file=sys.stderr)
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    q = sub.add_parser("q")
    q.add_argument("query")
    q.add_argument("--k", type=int, default=10)
    q.add_argument("--product", help="製品名の部分一致（例: z/OS, Db2, Print Services）")
    q.add_argument("--ver", help="製品の版（例: 3.2）")
    q.add_argument("--mode", default="auto", choices=["auto", "fts", "vec"])
    q.add_argument("--diff", action="store_true", help="同じ資料の他の版との差分も出す")
    df = sub.add_parser("diff", help="あるページを、同じ資料の他の版の対応箇所と比べる")
    df.add_argument("doc", help="文書のキー（例: text/zos-pdfs/ieam700_v3r2.txt）")
    df.add_argument("page", type=int)
    df.add_argument("--q", default="", help="検索文（識別子があれば対応箇所探しと変更点の章の検索に使う）")
    tr = sub.add_parser("triage", help="ログからメッセージID・アベンド・理由コードを拾い、定義と表の該当行を出す")
    tr.add_argument("file", help="ログのファイル（- で標準入力）")
    tr.add_argument("--product")
    tr.add_argument("--ver")
    mg = sub.add_parser("migrate", help="同じ資料の2つの版を項目ごとに比べる（例: \"z/OS 3.1\" \"z/OS 3.2\"）")
    mg.add_argument("label_from")
    mg.add_argument("label_to")
    a = ap.parse_args()

    eng = Engine(Path(a.corpus).resolve())
    if a.cmd == "serve":
        serve(eng, a.port)
    elif a.cmd == "migrate":
        import migrate
        print(migrate.text_product_report(eng, a.label_from, a.label_to), end="")
    elif a.cmd == "triage":
        import triage
        log = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8", errors="replace")
        print(triage.text_triage(eng, log, a.product, a.ver), end="")
    elif a.cmd == "diff":
        print(text_diff(eng, a.doc, a.page, a.q), end="")
    else:
        print(text_search(eng, a.query, a.k, a.mode, a.product, a.ver, a.diff), end="")


# ---- CLI とサーバーのテキスト API で共通の出力 ----
STATUS_JA = {"same": "差異なし", "diff": "差異あり", "notfound": "対応箇所が見つからない（その版に無いか、大きく書き換わった）"}


def text_search(eng, query, k=10, mode="auto", product=None, ver=None, diff=False):
    r = eng.search(query, k, mode, product, ver)
    out = [f"識別子 {', '.join(r['ids'])} を優先"] if r["ids"] else []
    for i, x in enumerate(r["results"], 1):
        info = eng.version_info(x["doc_id"])
        out.append(f"\n[{i}] {x['label']} / {x['form'] or '番号不明'} / p.{x['page']}  {x['title']}")
        out.append(f"    版: {info['basis']}" + (f"「{info['evidence']}」" if info["evidence"] else "")
                   + f"  元ファイル: {info['file']}")
        if info["others"]:
            out.append("    同じ資料の他の版: " + ", ".join(f"{o['label']} ({o['form'] or '資料番号なし'})" for o in info["others"]))
        out.append(f"    {x['path']}")
        out.append(f"    {x['excerpt']}")
        if diff:
            out.append(text_compare(eng.compare(x["doc_id"], x["page"], query), "    ").rstrip("\n"))
    if not r["results"]:
        out.append("該当なし")
    return "\n".join(out) + "\n"


def text_diff(eng, doc, page, q=""):
    doc_id = next((i for i, d in eng.docs.items() if d["path"] == doc.replace("\\", "/")), None)
    if doc_id is None:
        return f"索引に無い文書: {doc}\n"
    return text_compare(eng.compare(doc_id, page, q), "")


def text_compare(c, ind):
    out = [f"{ind}--- 他の版との比較（基準: {c['label']} p.{c['page']}）"]
    if not c["others"]:
        out.append(f"{ind}  同じ資料の他の版はコーパスにありません")
    for o in c["others"]:
        where = f" p.{o['page']} 付近" if o["page"] else ""
        n = f" {len(o['hunks'])} 箇所" if o["status"] == "diff" else ""
        out.append(f"{ind}  {o['label']} ({o['form'] or '資料番号なし'}){where}: {STATUS_JA[o['status']]}{n}（行の一致率 {o['match']:.0%}）")
        for h in o["hunks"]:
            out += [f"{ind}        {ln}" for ln in h["before"]]
            out += [f"{ind}      - {ln}" for ln in h["a"]]
            out += [f"{ind}      + {ln}" for ln in h["b"]]
            out.append(f"{ind}      ~")
    if c["others"]:
        out.append(f"{ind}  （- は {c['label']} だけにある行、+ は比べた版だけにある行）")
    for m in c["changes"]:
        kind = f"【{m['kind']}】" if m["kind"] else ""
        out.append(f"{ind}  変更点の章: {m['label']} ({m['form']}) p.{m['page']} {kind}"
                   f"{m['release'] or 'Summary of changes'}「{m['line']}」")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    main()
