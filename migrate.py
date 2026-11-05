"""
版の移行レポート: 同じ資料の2つの版を、メッセージ・コードの「項目」単位で比べ、追加・削除・変更された項目を一覧にする。

  文書どうし   report(eng, doc_a, doc_b)            （doc_a が古い版、doc_b が新しい版）
  製品の版どうし product_report(eng, "z/OS 3.1", "z/OS 3.2")
                 同じ資料番号の系列（families）で、ラベルが A の文書と B の文書の組を全部比べる

- 項目は検索画面のページ表示と同じ判定（entry_id_at + 直後に Explanation などの節）で切り出す。
  メッセージ集・コード集以外（解説書など）は項目が無いので、その資料の「Summary of changes」の記載だけを示す
- 本文の比較は柱・ページ番号・著作権表示を除いた行で行う（compare.norm_page）。
  版の番号だけが変わった行（「z/OS 3.1」→「z/OS 3.2」など）しか違わない項目は「変更」に数えない
"""
import re
from collections import Counter

import compare
import hsearch as H
from triage import explanation_near

VERSION_TOKEN_RE = re.compile(r"\b(V\d+R\d+|\d+\.\d+(\.\d+)?|Version \d+ Release \d+)\b")
# Summary of changes の中の、変更の中身ではない行（見出し・リリース名・「None.」・柱）
SOC_NOISE_RE = re.compile(r"^(New|Changed|Deleted|Removed|Updated|•?\s*None\.?|The following content is \w+\.?|"
                          r"\w+ \d{4} (release|refresh|update)|©.*|[xvil]+|\d+|Summary of changes.*)$", re.I)
ENTRY_END_RE = re.compile(r"^(Chapter \d+\.\s|Appendix [A-Z]\.|Part \d+\.\s|Notices$|Index$|Glossary$|"
                          r"Accessibility$|Trademarks$)")
PAREN_HEAD_RE = r"^{id}\s*\(.*\)$"   # 「IEC161I (return code 040)」のように項目が細かく分かれている見出し
HEAD_RE = re.compile(r"^(\d+\s+\S.{0,70}\D|\D.{0,70}\S\s+\d+)$")   # 行頭か行末だけに数字（ページ番号）がある短い行


def _mask(s):
    return re.sub(r"\d+", "#", s)


def entries(eng, doc_id):
    """{項目のキー: (最初のページ, [本文の行])}（文書ごとに1回だけ作る）"""
    cache = eng.__dict__.setdefault("_entries", {})
    if doc_id in cache:
        return cache[doc_id]
    prof = eng.profile(doc_id)
    lines, freq, npages = [], Counter(), 0
    for pno, text in eng.pages(doc_id, 1, 10 ** 9):
        lines += [(pno, s) for s in compare.norm_page(pno, text, prof)]
        freq.update({_mask(s) for s in compare.page_lines(text) if HEAD_RE.match(s)})
        npages += 1
    texts = [s for _, s in lines]
    # 柱の取り残し（「252 IBM MQ Reference」のように、ページ番号だけが違う同じ行が多くのページに出るもの。
    # norm_page は印刷ページ番号のずれが一定の柱しか除かないので、ずれが変わる所の柱が残る）
    eng.__dict__.setdefault("_heads", {})[doc_id] = {k for k, n in freq.items() if n >= max(20, npages // 5)}
    # 行頭の語の集合（「追加・削除」の確かめ用。項目として切り出せなくても、行頭に識別子があれば資料にはある）
    eng.__dict__.setdefault("_firsts", {})[doc_id] = {s.split(" ", 1)[0] for s in texts}
    code_manual = bool(H.CODE_MANUAL_RE.search(eng.docs[doc_id]["title"] or ""))
    starts = []
    for n, s in enumerate(texts):
        if not (H.ID_LINE_RE.match(s) or (code_manual and H.CODE_LINE_RE.match(s))):
            continue   # 速さのため、識別子で始まらない行は判定しない
        v = H.entry_id_at(texts, n, code_manual)
        if v and explanation_near(texts, n):
            starts.append((n, v))
    out = {}
    for i, (n, v) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(texts)
        # 章・付録・特記事項の見出しで項目を終える（最後の項目が付録まで続かないように）
        end = next((m for m in range(n + 1, end) if ENTRY_END_RE.match(texts[m])), end)
        key = texts[n] if re.match(PAREN_HEAD_RE.format(id=re.escape(v)), texts[n]) else v
        if key in out:
            # 同じ識別子の項目が複数ある（ICH408I のように本文の型ごとに項目が分かれているなど）。
            # 番号を振ると版によって番号がずれて「追加・削除」に見えるので、1つの項目にまとめる
            out[key] = (out[key][0], out[key][1] + texts[n:end])
        else:
            out[key] = (lines[n][0], texts[n:end])
    cache[doc_id] = out
    return out


PAGE_REF_RE = re.compile(r"\b(page|Table|Figure)\s+\d+\b", re.I)


def _canon(lines, heads=()):
    """比較用の行: 柱（「124 書名」など本文の途中に残ったもの）を除き、ページ番号・図表番号を伏せる"""
    return [PAGE_REF_RE.sub(r"\1 #", s) for s in lines
            if not H.SNIPPET_NOISE_RE.match(s) and not (HEAD_RE.match(s) and _mask(s) in heads)]


def _strip_versions(lines):
    return [VERSION_TOKEN_RE.sub("#", s) for s in lines]


def doc_info(eng, doc_id):
    d = eng.docs[doc_id]
    return {"doc_id": doc_id, "label": H.doc_label(d), "form": d["form"], "title": H.clean_title(d["title"] or ""),
            "path": d["path"]}


def report(eng, doc_a, doc_b, max_hunks=3):
    ea, eb = entries(eng, doc_a), entries(eng, doc_b)
    fa, fb = eng._firsts[doc_a], eng._firsts[doc_b]
    # 片方でしか項目として切り出せなかったものは、もう片方の本文の行頭に識別子があれば「判定できず」に回す
    # （2段組みの崩れ方が版で違い、項目の見出しを見落とすことがあるため）
    added = [{"key": k, "page": eb[k][0]} for k in eb if k not in ea and k.split(" ", 1)[0] not in fa]
    removed = [{"key": k, "page": ea[k][0]} for k in ea if k not in eb and k.split(" ", 1)[0] not in fb]
    unsure = sum(1 for k in eb if k not in ea and k.split(" ", 1)[0] in fa) + \
        sum(1 for k in ea if k not in eb and k.split(" ", 1)[0] in fb)
    changed, same = [], 0
    for k in ea:
        if k not in eb:
            continue
        a, b = _canon(ea[k][1], eng._heads[doc_a]), _canon(eb[k][1], eng._heads[doc_b])
        # 行の並びだけが違うもの（2段組みの読み取り順の違いで「Explanation:」の位置がずれるなど）も同じとみなす
        if a == b or sorted(_strip_versions(a)) == sorted(_strip_versions(b)):
            same += 1
            continue
        hs = compare.hunks(a, b)
        n = sum(len(h["a"]) + len(h["b"]) for h in hs)
        changed.append({"key": k, "page_a": ea[k][0], "page_b": eb[k][0], "lines": n, "hunks": hs[:max_hunks]})
    changed.sort(key=lambda c: -c["lines"])
    soc = [{"page": p, "release": r, "kind": kd, "line": ln} for p, r, kd, ln in eng.soc(doc_b) if not SOC_NOISE_RE.match(ln)]
    return {"a": doc_info(eng, doc_a), "b": doc_info(eng, doc_b), "entries_a": len(ea), "entries_b": len(eb),
            "added": added, "removed": removed, "changed": changed, "same": same, "unsure": unsure, "soc": soc}


def _edition(eng, doc_id):
    """同じラベルの版が複数あるとき（PSF 4.8 の -06 と -07 など）新しい方を選ぶための並びのキー"""
    form = eng.docs[doc_id]["form"] or ""
    m = re.search(r"-(\d+)$", form)
    return int(m.group(1)) if m else -1


def product_report(eng, label_a, label_b):
    """ラベル A の文書と B の文書が両方ある資料を全部比べる。A と B が同じラベルなら、同じ版の中の旧い刷と新しい刷を比べる"""
    out = []
    for base, ids in sorted(eng.families.items()):
        da = sorted((i for i in ids if H.doc_label(eng.docs[i]) == label_a), key=lambda i: _edition(eng, i))
        db = sorted((i for i in ids if H.doc_label(eng.docs[i]) == label_b), key=lambda i: _edition(eng, i))
        note = ""
        if da and db:
            a, b = da[-1], db[-1]
            if a == b:
                if len(da) < 2:
                    continue
                a = da[-2]
        elif len(da) >= 2 and label_a != label_b:
            # 内容が変わっていない資料は、新しいリリースの版でも「z/OS 3.1 に適用」と書かれていることがある。
            # 資料番号の末尾（-60 → -70）で古い刷と新しい刷を比べる
            a, b = da[0], da[-1]
            if _edition(eng, a) == _edition(eng, b):
                continue
            note = f"新しい刷（{eng.docs[b]['form']}）も {label_a} と表記している資料"
        else:
            continue
        r = report(eng, a, b, max_hunks=1)
        r["base"], r["note"] = base, note
        out.append(r)
    return {"label_a": label_a, "label_b": label_b, "docs": out}


def text_report(r, indent=""):
    a, b = r["a"], r["b"]
    out = [f"{indent}{b['title'] or a['title']}",
           f"{indent}  比較: {a['label']} ({a['form'] or '資料番号なし'}) → {b['label']} ({b['form'] or '資料番号なし'})   項目 {r['entries_a']} → {r['entries_b']}"]
    if r.get("note"):
        out.append(f"{indent}  （{r['note']}）")
    if not r["entries_a"] and not r["entries_b"]:
        out.append(f"{indent}  （メッセージ・コードの項目が無い資料。Summary of changes の記載だけを示す）")
    else:
        out.append(f"{indent}  追加 {len(r['added'])} ・ 削除 {len(r['removed'])} ・ 変更 {len(r['changed'])} ・ 同じ {r['same']}"
                   + (f" ・ 判定できず {r['unsure']}（片方の版で項目の見出しを読み取れなかった）" if r.get("unsure") else ""))
        if r["added"]:
            out.append(f"{indent}  追加: " + ", ".join(f"{x['key']} (p.{x['page']})" for x in r["added"][:60])
                       + (" …" if len(r["added"]) > 60 else ""))
        if r["removed"]:
            out.append(f"{indent}  削除: " + ", ".join(f"{x['key']} (旧 p.{x['page']})" for x in r["removed"][:60])
                       + (" …" if len(r["removed"]) > 60 else ""))
        for c in r["changed"][:40]:
            out.append(f"{indent}  変更: {c['key']}  旧 p.{c['page_a']} → 新 p.{c['page_b']}（違う行 {c['lines']}）")
            for h in c["hunks"][:1]:
                for ln in h["a"][:2]:
                    out.append(f"{indent}      - {ln[:150]}")
                for ln in h["b"][:2]:
                    out.append(f"{indent}      + {ln[:150]}")
        if len(r["changed"]) > 40:
            out.append(f"{indent}  （変更はほか {len(r['changed']) - 40} 項目）")
    soc = [s for s in r["soc"] if s["kind"]]
    if soc:
        out.append(f"{indent}  新しい版の Summary of changes（種類が分かる行）:")
        for s in soc[:25]:
            out.append(f"{indent}    【{s['kind']}】{s['line'][:140]}  p.{s['page']}")
    return "\n".join(out) + "\n"


def text_product_report(eng, label_a, label_b):
    r = product_report(eng, label_a, label_b)
    if not r["docs"]:
        return (f"{label_a} と {label_b} の両方の版がある資料はコーパスにありません"
                "（両方の版の資料を取り込むと比べられます）\n")
    head = f"{label_a} → {label_b}: 比べた資料 {len(r['docs'])} 冊\n"
    return head + "\n".join(text_report(d) for d in r["docs"])
