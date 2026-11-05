"""
同じ資料の別の版どうしで、あるページに対応する箇所を探して差分を取る。hsearch.py から使う。
標準ライブラリだけで動く。

考え方:
- 他の版の中で、そのページと語の重なりが大きいページを全文検索で数件選ぶ
- 候補ページ（前後1ページも含めた範囲）と行単位で突き合わせ、最もよく一致した範囲を対応箇所とする
- 一致率が低ければ「対応箇所なし」（その版には無い、または大きく書き換わった）
- 柱・ページ番号の行は版ごとに必ず変わるので比較から外す。どの行が柱かは、文書ごとに
  「ページの端にあり、数字が PDF のページ番号と同じ歩調で増える行」として学習する（doc_profile）。
  こうすると「Descriptor code: 4」や表の数値だけの行のような本文は外さずに済む
- 「Explanation」のようにほぼ全ページに出る定型の行は、一致率の計算からは外す（差分には出す）
"""
import difflib
import re
from collections import Counter

ARABIC = r"\d{1,5}"
ROMAN = r"[ivxlc]{1,7}"
# ページ番号の付いた行: 「812」「812 z/OS: z/OS JES2 Commands」「Chapter 2. System codes 195」
PNO_LINE_RES = [re.compile(rf"^(?P<n>{ARABIC}|{ROMAN})$"),
                re.compile(rf"^(?P<n>{ARABIC}|{ROMAN})\s+(?P<r>.+)$"),
                re.compile(rf"^(?P<r>.+?)\s+(?P<n>{ARABIC}|{ROMAN})$")]
COPYRIGHT_RE = re.compile(r"^©\s*Copyright\b")
EDGE_TOP, EDGE_BOTTOM = 2, 3      # 柱を探すページ先頭・末尾の行数
STOP = {"this", "that", "with", "from", "have", "will", "when", "which", "there", "their", "into",
        "then", "than", "also", "must", "only", "were", "been", "does", "each", "other", "more"}
MIN_MATCH = 0.3   # これ未満の一致率なら「対応箇所なし」


def clean(ln):
    return " ".join(ln.replace("®", "").replace("™", "").split())   # 版によって商標記号の有無が違う


def page_lines(text):
    return [s for s in (clean(ln) for ln in text.split("\n")) if s]


def edge_index(lines):
    """ページ先頭・末尾の、柱になりうる行の位置"""
    n = len(lines)
    return set(range(min(EDGE_TOP, n))) | set(range(max(0, n - EDGE_BOTTOM), n))


def pno_parts(ln):
    """(番号, 番号以外の部分) または None"""
    for rx in PNO_LINE_RES:
        m = rx.match(ln)
        if m:
            return m.group("n"), (m.groupdict().get("r") or "")
    return None


def doc_profile(pages):
    """
    pages: [(PDFのページ番号, 本文), ...]（文書全体）
    戻り値: {"offset": PDFページ番号 - 印刷ページ番号（不明なら None）,
             "footers": 柱の文字部分の集合, "common": 多くのページに出る定型の行の集合}
    """
    offsets, seen_on = Counter(), Counter()
    edges = []
    for pno, text in pages:
        lines = page_lines(text)
        for ln in set(lines):
            seen_on[ln] += 1
        for i in edge_index(lines):
            parts = pno_parts(lines[i])
            if parts and parts[0].isdigit():
                offsets[pno - int(parts[0])] += 1
                edges.append((pno, int(parts[0]), parts[1]))
    n_pages = max(len(pages), 1)
    offset = None
    if offsets:
        off, cnt = offsets.most_common(1)[0]
        if cnt >= 3 and cnt >= 0.1 * n_pages:
            offset = off
    footers = Counter(r for p, n, r in edges if offset is not None and p - n == offset and r)
    common = {ln for ln, c in seen_on.items() if c >= max(5, 0.05 * n_pages)}
    return {"offset": offset, "footers": {r for r, c in footers.items() if c >= 2}, "common": common}


def norm_page(pno, text, prof):
    """比較用の行（柱・ページ番号・著作権表示を除く）"""
    lines = page_lines(text)
    drop = set()
    off = prof.get("offset") if prof else None
    for i in edge_index(lines):
        parts = pno_parts(lines[i])
        if not parts:
            continue
        n, rest = parts
        if n.isdigit():
            if off is not None and pno - int(n) == off:
                drop.add(i)          # 印刷ページ番号と一致する柱
        elif rest in prof.get("footers", ()) or (not rest and off is not None and pno <= off):
            drop.add(i)              # 前付けのローマ数字の柱
    return [ln for i, ln in enumerate(lines) if i not in drop and not COPYRIGHT_RE.match(ln)]


def norm_pages(pages, prof):
    out = []
    for pno, text in pages:
        out += norm_page(pno, text, prof)
    return out


def key_words(text, limit=40):
    """候補ページを探すための語（長めの語を出現順に）"""
    seen, out = set(), []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9$#@_-]{3,}", text):
        lw = w.lower()
        if lw in STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def align(a, b_window, common=frozenset()):
    """
    b_window の中で a に対応する範囲を切り出す。
    一致率は、定型の行（common）を除いた a の行のうち、b と一致した行の割合
    """
    # 位置合わせは定型でない行だけで行う。定型の行も含めると、隣のページの同じ定型の並びに
    # 引っ張られて、肝心の行（メッセージIDなど）が一致しなくなる
    ai = [i for i, ln in enumerate(a) if ln not in common] or list(range(len(a)))
    bj = [j for j, ln in enumerate(b_window) if ln not in common] or list(range(len(b_window)))
    sm = difflib.SequenceMatcher(None, [a[i] for i in ai], [b_window[j] for j in bj], autojunk=False)
    blocks = [x for x in sm.get_matching_blocks() if x.size]
    if not blocks:
        return 0.0, []
    ratio = sum(x.size for x in blocks) / len(ai)
    first, last = blocks[0], blocks[-1]
    a0, b0 = ai[first.a], bj[first.b]
    a1, b1 = ai[last.a + last.size - 1], bj[last.b + last.size - 1]
    lo = max(0, b0 - a0)
    hi = min(len(b_window), b1 + (len(a) - a1))
    return ratio, b_window[lo:hi]


def hunks(a, b, context=1):
    """行単位の差分。各要素: {"before": 前の文脈, "a": この版だけの行, "b": 他の版だけの行}"""
    out = []
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        out.append({"before": a[max(0, i1 - context):i1], "a": a[i1:i2], "b": b[j1:j2]})
    return out


SOC_START = re.compile(r"^Summary of changes$", re.I)
SOC_END = re.compile(r"^(Chapter 1\b|Part 1\b)")
SOC_RELEASE = re.compile(r"^(Summary of changes for|Changes made in|Changes made for)\b", re.I)
SOC_KIND = {"new": "新規", "changed": "変更", "deleted": "削除", "removed": "削除",
            "no longer": "削除", "updated": "変更", "added": "新規"}


def summary_of_changes(pages, max_pages=20):
    """
    pages: [(ページ番号, 本文), ...]（文書の先頭から）
    「Summary of changes」の章を読み、行ごとに (ページ, どのリリースの変更か, 種類, 行) を返す
    """
    out, inside, release, kind, used = [], False, "", "", 0
    for pno, text in pages:
        for raw in text.split("\n"):
            ln = " ".join(raw.split())
            if not ln:
                continue
            if not inside:
                inside = bool(SOC_START.match(ln))
                continue
            if SOC_END.match(ln):
                return out
            if release.count("(") > release.count(")") and not kind and len(release) < 160:
                release += " " + ln   # 2行に折り返した見出しをつなぐ
                continue
            if SOC_RELEASE.match(ln):
                release, kind = ln, ""
                continue
            low = ln.lower()
            if len(ln) < 60:
                for k, v in SOC_KIND.items():
                    if low == k or low.startswith("the following") and k in low:
                        kind = v
                        break
            out.append((pno, release, kind, ln))
        if inside:
            used += 1
            if used >= max_pages:
                break
    return out


def find_counterpart(page_a, prof_a, candidates, window, prof_b):
    """
    page_a: (PDFページ番号, 本文)。candidates: 他の版で候補になったページ番号のリスト
    window(page) -> そのページと前後1ページの [(ページ番号, 本文), ...]
    戻り値: {"status": same|diff|notfound, "page": 対応ページ, "match": 一致率, "hunks": [...]}
    """
    a = norm_page(page_a[0], page_a[1], prof_a)
    if not a:
        return {"status": "notfound", "page": None, "match": 0.0, "hunks": []}
    best = None
    for p in candidates:
        ratio, b = align(a, norm_pages(window(p), prof_b), prof_a["common"])
        if best is None or ratio > best[0]:
            best = (ratio, p, b)
    if best is None or best[0] < MIN_MATCH:
        return {"status": "notfound", "page": best[1] if best else None,
                "match": round(best[0], 2) if best else 0.0, "hunks": []}
    ratio, p, b = best
    hs = hunks(a, b)
    return {"status": "diff" if hs else "same", "page": p, "match": round(ratio, 2), "hunks": hs}
