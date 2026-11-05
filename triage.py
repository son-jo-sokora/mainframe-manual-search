"""
障害調査: ジョブログ・SYSLOG・ダンプの抜粋から、メッセージID・アベンドコード・SQLCODE・理由コードを拾い、
それぞれの定義のページと、理由コード・戻りコードの表の該当行を探す。

  拾うもの（ログの1行ごと）
    メッセージID   IEF450I, $HASP373, DFHAC2206 など（コーパスに定義があるものだけ残す）
    アベンド       ABEND=S0C4, SYSTEM COMPLETION CODE=0C4, USER=4038, S806, U4038, 013-18（= S013 と理由コード 18）
    SQLCODE        SQLCODE = -904 → -904
    Db2 の理由コード 00C90081（Db2 Codes に項目がある）
    理由コード     同じ行の REASON=, RSN=, RC=, RETURN CODE など。メッセージ直後の「IEC161I 040(…」の 040 も
  理由コードの行の探し方
    1. 「IEC161I (return code 040)」のように、コードごとに項目が分かれていればその見出し
    2. 定義の項目の中の表（コードだけの行）。pdftotext では表の列がずれ、コードの行が先に並んで説明が後に続くことが
       多いので、説明は並び順から推定する（画面と出力に「推定」と示す）
    3. 項目の中に無ければ、項目が参照するメッセージ（013 → IEC141I など）の中を探す
"""
import re

import hsearch as H

MSG_RE = re.compile(r"(?<![A-Za-z0-9$#@])(\$?[A-Z]{2,5}\d{2,5}[A-Z]{0,2})(?![A-Za-z0-9$#@])")
ABEND_RES = [
    (re.compile(r"\bABEND\s*(?:CODE)?[=:\s]\s*([SU])?([0-9A-F]{3,4})\b"), None),
    (re.compile(r"\b(?:SYSTEM|SYS)\s+(?:COMPLETION|ABEND)\s+CODE\s*[=:\s]\s*([0-9A-F]{3})\b"), "S"),
    (re.compile(r"\bUSER\s+(?:COMPLETION|ABEND)\s+CODE\s*[=:\s]\s*(\d{4})\b"), "U"),
    (re.compile(r"\bCOMPLETION CODE\b.*?\bSYSTEM=([0-9A-F]{3})\b"), "S"),
    (re.compile(r"\bCOMPLETION CODE\b.*?\bUSER=(\d{4})\b"), "U"),
]
ABEND_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9$#@])(S[0-9A-F]{3}|U\d{4})(?![A-Za-z0-9$#@])")
ABEND_RC_RE = re.compile(r"(?<![A-Za-z0-9$#@\-])([0-9A-F]{3})-([0-9A-F]{2,8})(?![A-Za-z0-9$#@])")
SQLCODE_RE = re.compile(r"\bSQLCODE\s*[=:]?\s*([-+]?\d{1,5})\b", re.I)
DB2_RSN_RE = re.compile(r"(?<![A-Za-z0-9])(00[C-F][0-9A-F]{5})(?![A-Za-z0-9])")
RSN_RE = re.compile(r"(?i:\b(?:REASON(?:\s+CODE)?|RSN(?:CODE)?|RC|RETURN\s+CODE|RETCODE))\s*[=:]?\s*"
                    r"(?:X')?([0-9A-F]{1,8})\b")
MSG_RC_RE = r"(?<![A-Z0-9$#@]){id}\s+([0-9A-F]{{2,3}})[(\-,]"     # 「IEC161I 040(8,…」の 040
SHORT_CODE_RE = re.compile(r"^\W*(\S+)[\s,]+(?:X')?([0-9A-F]{1,8})'?\s*$")   # 「S806 04」のような短い問い
HEXLINE_RE = re.compile(r"^(?:X')?([0-9A-F]{1,8})'?$")
INLINE_ROW_RE = re.compile(r"^(?:X')?([0-9A-F]{1,8})'?\s+(\S.*)$")
REF_MSG_RE = re.compile(r"\bmessages?\s+([A-Z]{3,5}\d{3,5}[A-Z])\b")
CODE_HEAD_RE = r"^{id}\s*\((?:return|reason)\s+code\s+([0-9A-F]{{1,8}})\)"
KIND_JA = {"msg": "メッセージ", "abend": "アベンド", "sqlcode": "SQLCODE", "code": "理由コード"}
MAX_FINDINGS = 40


def hexval(s):
    try:
        return int(s, 16)
    except (TypeError, ValueError):
        return None


# ---- ログから拾う ----
def parse_log(text):
    """[{kind, id, codes, lines(1始まりの行番号), sample}] を出てきた順に"""
    found = {}

    def add(kind, ident, n, line, code=None):
        f = found.setdefault(ident, {"kind": kind, "id": ident, "codes": [], "lines": [], "sample": line.strip()[:200]})
        if n not in f["lines"]:
            f["lines"].append(n)
        if code and code not in f["codes"]:
            f["codes"].append(code)
        return f

    for n, line in enumerate(text.lstrip("﻿").splitlines(), 1):
        if not line.strip():
            continue
        u = line.upper()
        here = []   # この行で拾ったもの（理由コードを付ける先）
        abends = []
        for r, sign in ABEND_RES:
            for m in r.finditer(u):
                if sign is None:
                    s, c = m.group(1), m.group(2)
                    s = s or ("U" if len(c) == 4 else "S")
                else:
                    s, c = sign, m.group(1)
                if (s == "S" and len(c) == 3 and c != "000") or (s == "U" and len(c) == 4 and c.strip("0")):
                    abends.append(s + c)
        for m in ABEND_TOKEN_RE.finditer(u):
            t = m.group(1)
            if any(ch.isdigit() for ch in t) and t not in ("S000",) and t.strip("U0"):
                abends.append(t)
        rc_pairs = [(m.group(1), m.group(2)) for m in ABEND_RC_RE.finditer(u)]
        for a, rc in rc_pairs:
            if a[0].isdigit():   # 「013-18」
                here.append(add("abend", "S" + a, n, line, rc.lstrip("0") or "0"))
        for a in dict.fromkeys(abends):
            here.append(add("abend", a, n, line))
        for m in SQLCODE_RE.finditer(line):
            v = m.group(1)
            here.append(add("sqlcode", v if v.startswith(("-", "+")) else v, n, line))
        msgs = []
        for m in MSG_RE.finditer(u):
            t = m.group(1)
            if t in abends or ABEND_TOKEN_RE.fullmatch(t):
                continue
            msgs.append(t)
            f = add("msg", t, n, line)
            here.append(f)
            mc = re.search(MSG_RC_RE.format(id=re.escape(t)), u)
            if mc and not any(a == mc.group(1) for a, _ in rc_pairs):   # 「IEC141I 013-18」の 013 はアベンド
                code = mc.group(1)
                f["codes"] = list(dict.fromkeys(f["codes"] + [code]))
        # 「IEC141I 013-18,…」の 18 はメッセージ側の戻りコードでもある
        for a, rc in rc_pairs:
            for t in msgs:
                if re.search(rf"{re.escape(t)}\s+{a}-{rc}", u):
                    found[t]["codes"] = list(dict.fromkeys(found[t]["codes"] + [rc]))
        for m in DB2_RSN_RE.finditer(u):
            here.append(add("code", m.group(1), n, line))
        rsn = [m.group(1) for m in RSN_RE.finditer(u)
               if not DB2_RSN_RE.fullmatch(m.group(1))]
        if not rsn and len(line.strip()) <= 40:
            m = SHORT_CODE_RE.match(u)
            if m and m.group(1) in [f["id"] for f in here]:
                rsn = [m.group(2)]
        if rsn and here:
            # 理由コードは、その行のアベンドがあればそれに、無ければ行の最初のメッセージに付ける
            tgt = next((f for f in here if f["kind"] == "abend"), here[0])
            for c in rsn:
                if c not in tgt["codes"]:
                    tgt["codes"].append(c)
    return list(found.values())


# ---- 定義を探す ----
def _norm(ln):
    return " ".join(ln.split())


def explanation_near(lines, n):
    nxt = [x for x in lines[n + 1:n + 8] if x][:4]
    return any(H.DEF_SECTION_RE.match(x) for x in nxt)


CHAPTER_RE = re.compile(r"^Chapter \d+\.\s+(.+?)\s+\d+$", re.M)


def chapter(eng, doc_id, page):
    """ページの属する章の名前（柱の「Chapter 2. System completion codes 21」から。柱は片側のページにしか無い）"""
    for p in (page, page + 1, page - 1):
        m = CHAPTER_RE.search(eng.page_text(doc_id, max(1, p))[0])
        if m:
            return m.group(1)
    return ""


def definitions(eng, term, keep=None, limit=3):
    """term の定義のページ（定義らしいものを、文書ごとに1つ）。[{doc_id, page, ...}]"""
    cache = eng.__dict__.setdefault("_defcache", {})
    key = (term, id(keep))
    if key in cache:
        return cache[key]
    clause = H.fts_quote(term.lstrip("+-")) if term[:1] in "+-" else H.id_clause(term)
    hits = eng.fts(clause, 600)
    out = []
    if hits:
        ranked, tiers = eng.by_definition(hits, [term])
        top = [c for c in ranked if tiers[c] == 0][:60]
        rows = {}
        if top:
            for cid, doc_id, page, text in eng.con().execute(
                    f"SELECT id, doc_id, page, text FROM chunks WHERE id IN ({','.join('?' * len(top))})", top):
                rows[cid] = (doc_id, page, text)
        variants = H.id_variants(term)
        cands = []
        for order, cid in enumerate(top):
            doc_id, page, text = rows[cid]
            d = eng.docs[doc_id]
            if (keep and not keep(d)) or H.is_redbook(d):   # Redbook は解説書なので定義の出典にしない
                continue
            lines = [ln.strip() for ln in text.split("\n")]
            near = any(explanation_near(lines, n) for n, ln in enumerate(lines)
                       if any(ln == v or ln.startswith(v + " ") for v in variants))
            title = d["title"] or ""
            # システム・アベンドは z/OS MVS System Codes を優先する（他の資料にも同じ3桁の表がある）。
            # 同じ本の「Wait state codes」の章にも 0C4 などの項目があるので、その章のページは後ろに回す
            syscodes = term[:1] == "S" and "System Codes" in title
            wait = syscodes and "system completion codes" not in chapter(eng, doc_id, page).lower()
            # SQLCODE と Db2 の理由コードは Codes の本を優先する（Messages の本にも同じ形の行がある）
            codes_book = (term[:1] in "+-0" and bool(re.search(r"\bCodes\b", title))
                          and (d["product"] or "").lower().startswith("db2"))
            cands.append(((0 if syscodes or codes_book else 1, 1 if wait else 0,
                           0 if eng.is_newest(doc_id) else 1, 0 if near else 1, order),
                          doc_id, page))
        seen = set()
        for _, doc_id, page in sorted(cands):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            d = eng.docs[doc_id]
            out.append({"doc_id": doc_id, "page": page, "label": H.doc_label(d), "form": d["form"],
                        "title": H.clean_title(d["title"] or ""), "path": d["path"]})
            if len(out) >= limit:
                break
    cache[key] = out
    return out


def references(eng, term, keep=None, limit=60):
    """term に触れている（定義ではない）ページ。説明の中で参照しているメッセージ・解説書のページなど"""
    clause = H.fts_quote(term.lstrip("+-")) if term[:1] in "+-" else H.id_clause(term)
    hits = eng.fts(clause, 1500)
    if not hits:
        return []
    ranked, tiers = eng.by_definition(hits, [term])
    ment = [c for c in ranked if tiers[c] > 0]
    rows = {}
    for i in range(0, len(ment), 500):
        part = ment[i:i + 500]
        for cid, doc_id, page, text in eng.con().execute(
                f"SELECT id, doc_id, page, text FROM chunks WHERE id IN ({','.join('?' * len(part))})", part):
            rows[cid] = (doc_id, page, text)
    variants = H.id_variants(term)
    out, seen = [], set()
    for cid in ment:
        doc_id, page, text = rows[cid]
        d = eng.docs[doc_id]
        if (keep and not keep(d)) or (doc_id, page) in seen or H.is_junk(text):
            continue
        seen.add((doc_id, page))
        code_manual = bool(H.CODE_MANUAL_RE.search(d["title"] or ""))
        out.append({"doc_id": doc_id, "page": page, "label": H.doc_label(d), "form": d["form"],
                    "title": H.clean_title(d["title"] or ""), "path": d["path"],
                    "crumb": H.crumb(text, variants, code_manual)})
        if len(out) >= limit:
            break
    return out


# ---- 理由コード・戻りコードの行を探す ----
def page_lines(eng, doc_id, first, last):
    """[(ページ, 行)]。行は空白をまとめ、柱・著作権表示を除く（page_blocks と同じ扱い。画面で何番目の行かを数えるため）"""
    out = []
    for p, t in eng.pages(doc_id, first, last):
        for ln in t.split("\n"):
            s = _norm(ln)
            if not H.SNIPPET_NOISE_RE.match(s):
                out.append((p, s))
    return out


def _row(lines, i, code, text, guessed, doc, via=None, eng=None):
    p, s = lines[i]
    occ = sum(1 for q, t in lines[:i] if q == p and t == s)
    d = eng.docs[doc]
    return {"code": code, "doc_id": doc, "page": p, "line": s, "occ": occ, "text": text, "guessed": guessed,
            "via": via, "label": H.doc_label(d), "form": d["form"], "title": H.clean_title(d["title"] or "")}


def _entry_range(lines, term, code_manual):
    """lines の中の term の項目の範囲 (開始, 終了)。見つからなければ None"""
    texts = [s for _, s in lines]
    variants = H.id_variants(term)
    starts = [n for n, s in enumerate(texts) for v in variants
              if (s == v or s.startswith(v + " ")) and H.line_score(texts, n, v, code_manual) >= H.DEF_TIERS[0]]
    if not starts:
        return None
    start = next((n for n in starts if explanation_near(texts, n)), starts[0])
    end = min(len(texts), start + 700)
    for n in range(start + 1, end):
        v = H.entry_id_at(texts, n, code_manual)
        if v and v not in variants and explanation_near(texts, n):
            return start, n
    return start, end


def _guess_text(texts, i, start, end):
    """表のコードの行 i に対応する説明。(説明, 推定か)"""
    def is_row(n):
        return bool(HEXLINE_RE.match(texts[n]))
    # i を含む「コードの行の並び」（間の空行は飛ばす）
    a = i
    while a - 1 >= start and (not texts[a - 1] or is_row(a - 1)):
        a -= 1
    b = i
    while b + 1 < end and (not texts[b + 1] or is_row(b + 1)):
        b += 1
    run = [n for n in range(a, b + 1) if texts[n] and is_row(n)]
    k = run.index(i)
    body = []
    for n in range(b + 1, min(end, b + 120)):
        if texts[n] and is_row(n):
            break
        body.append(texts[n])
    body = [x for x in body if x]
    if len(run) == 1:
        return "\n".join(body[:15]), False
    # 「…。」で終わる行の次が大文字で始まる所を説明の区切りとみなす
    paras, cur = [], []
    for n, x in enumerate(body):
        cur.append(x)
        nxt = body[n + 1] if n + 1 < len(body) else ""
        if x.endswith(".") and nxt[:1].isupper():
            paras.append(cur)
            cur = []
    if cur:
        paras.append(cur)
    if k < len(paras):
        return "\n".join(paras[k][:15]), True
    return "", True


def find_row(eng, d, term, code, follow=True):
    """定義 d（definitions の1件）の中で、コード code の行。見つからなければ None"""
    doc_id, page = d["doc_id"], d["page"]
    want = hexval(code)
    if want is None:
        return None
    # 1. コードごとに項目が分かれている（IEC161I (return code 040)）
    if not term[:1] in "+-SU" or H.ID_LINE_RE.match(term):
        head = re.compile(CODE_HEAD_RE.format(id=re.escape(term)), re.I)
        for p in eng.pages_in_doc(f'{H.fts_quote(term)} AND {H.fts_quote(code.lstrip("0") or "0")}', doc_id, 20) + \
                eng.pages_in_doc(f'{H.fts_quote(term)} AND {H.fts_quote(code)}', doc_id, 20):
            lines = page_lines(eng, doc_id, p, p + 1)
            for i, (q, s) in enumerate(lines):
                m = head.match(s)
                if m and hexval(m.group(1)) == want and q == p:
                    texts = [t for _, t in lines]
                    e = next((n for n in range(i + 1, len(texts)) if texts[n] == "Explanation"), i)
                    return _row(lines, i, code, "\n".join(x for x in texts[e + 1:e + 12] if x), False, doc_id, eng=eng)
    # 2. 項目の中の表
    code_manual = bool(H.CODE_MANUAL_RE.search(eng.docs[doc_id]["title"] or ""))
    lines = page_lines(eng, doc_id, page, page + 6)
    rng = _entry_range(lines, term, code_manual)
    if rng:
        start, end = rng
        texts = [s for _, s in lines]
        for i in range(start + 1, end):
            m = HEXLINE_RE.match(texts[i])
            if m and hexval(m.group(1)) == want:
                text, guessed = _guess_text(texts, i, start, end)
                return _row(lines, i, code, text, guessed, doc_id, eng=eng)
        for i in range(start + 1, end):
            m = INLINE_ROW_RE.match(texts[i])
            if m and hexval(m.group(1)) == want and len(m.group(1)) >= 2:
                rest = [m.group(2)] + [x for x in texts[i + 1:min(end, i + 8)] if x and not HEXLINE_RE.match(x)]
                return _row(lines, i, code, "\n".join(rest[:8]), False, doc_id, eng=eng)
        # 3. 項目が参照するメッセージ（013 → IEC141I）
        if follow:
            body = " ".join(texts[start:end])
            for ref in list(dict.fromkeys(REF_MSG_RE.findall(body)))[:2]:
                for rd in definitions(eng, ref)[:1]:
                    r = find_row(eng, rd, ref, code, follow=False)
                    if r:
                        r["via"] = ref
                        return r
    return None


# ---- まとめ ----
def triage(eng, text, product=None, ver=None, label=None):
    keep = H.doc_filter(product, ver, label)
    items = parse_log(text)
    out, missing = [], []
    for f in items:
        if len(out) >= MAX_FINDINGS:
            break
        defs = definitions(eng, f["id"], keep)
        if not defs:
            if f["kind"] != "msg" or re.search(r"[A-Z]$", f["id"]):
                missing.append(f["id"])   # 末尾が英字のメッセージIDらしいもの・アベンドなど
            continue
        rows = []
        for c in f["codes"][:3]:
            # 表の行は最上位の定義の中だけで探す（別の製品の同じ番号の表に当てないように）
            r = find_row(eng, defs[0], f["id"], c)
            rows.append(r or {"code": c, "missing": True})
        out.append({**f, "kind_ja": KIND_JA[f["kind"]], "defs": defs, "rows": rows})
    return {"findings": out, "missing": missing}


def text_triage(eng, text, product=None, ver=None):
    r = triage(eng, text, product, ver)
    out = []
    for n, f in enumerate(r["findings"], 1):
        ln = f["lines"]
        where = f"{ln[0]}行目" + (f" ほか {len(ln) - 1} 回" if len(ln) > 1 else "")
        out.append(f"\n[{n}] {f['id']}（{f['kind_ja']}）  ログ {where}: {f['sample'][:120]}")
        d = f["defs"][0]
        out.append(f"    定義: {d['label']} / {d['form'] or '番号不明'} / p.{d['page']}  {d['title']}")
        out.append(f"          {d['path']}")
        for o in f["defs"][1:]:
            out.append(f"    他の定義: {o['label']} / {o['form'] or '番号不明'} / p.{o['page']}  {o['title']}")
        for row in f["rows"]:
            if row.get("missing"):
                out.append(f"    コード {row['code']}: 定義の項目の中に表の行が見つからない（page で項目全体を読むこと）")
                continue
            via = f"（{row['via']} の説明の中）" if row.get("via") else ""
            out.append(f"    コード {row['code']} → {row['label']} / p.{row['page']} の行「{row['line']}」{via}")
            if row["text"]:
                tag = "説明（表の組みから推定。原文で確認すること）" if row["guessed"] else "説明"
                body = row["text"].replace("\n", " ")
                out.append(f"      {tag}: {body[:400]}")
    if r["missing"]:
        out.append("\nコーパスに定義が見つからなかったもの: " + ", ".join(r["missing"]))
    if not r["findings"] and not r["missing"]:
        out.append("識別子・コードが見つかりませんでした")
    return "\n".join(out).lstrip("\n") + "\n"
