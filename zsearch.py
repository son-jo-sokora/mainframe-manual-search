#!/usr/bin/env python3
"""
extract.py で作ったコーパスを検索し、資料番号・版・ページ付きで結果を出す。

  python3 zsearch.py <コーパス> find <パターン> [-i] [-F] [-C 行数] [--product z/OS] [--ver 3.2] [--form SA38] [--title 文字列] [--max 件数]
  python3 zsearch.py <コーパス> page <text/...txt> <ページ番号> [<終了ページ>]
  python3 zsearch.py <コーパス> docs [--product Db2] [--ver 13] [--title 文字列]

--product は製品名の部分一致（大文字小文字を無視）、--ver は製品の版の完全一致。

ripgrep (rg) があれば高速に検索し、無ければ Python で走査する。
"""
import argparse
import bisect
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def load(corpus: Path):
    return json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))


def select(manifest, ver=None, form=None, title=None, product=None):
    out = []
    for key, m in manifest.items():
        if ver and m.get("version") != ver:
            continue
        if product and product.lower() not in (m.get("product") or "").lower():
            continue
        if form and not (m.get("form") or "").startswith(form):
            continue
        if title and title.lower() not in (m.get("title") or "").lower():
            continue
        out.append(key)
    return sorted(out)


def page_of(m, line_no):
    return bisect.bisect_right(m["page_starts"], line_no)


def label(m):
    prod = f'{m.get("product") or "製品不明"} {m.get("version") or ""}'.strip()
    if "redbook" in Path(m.get("source", "")).parent.name.lower():
        prod = f"Redbook（{prod}）"   # 製品のマニュアルと区別する
    return f'{prod} / {m.get("form") or "番号不明"} / {m.get("title") or ""}'


def basis(m):
    """版をどう判断したか（根拠の文つき）"""
    if {"version", "product"} & set(m.get("overridden") or []):
        return "版: overrides.tsv で手で指定"
    a = m.get("auto", {})
    how = {"edition": "版の宣言文から推定", "cover": "表紙から推定"}.get(a.get("version_from"), "推定できず")
    ev = a.get("version_evidence")
    return f"版: {how}" + (f"「{ev}」" if ev else "") + f"  元ファイル: {Path(m.get('source', '')).name}"


def hits_rg(corpus, keys, pat, icase, fixed):
    cmd = ["rg", "-n", "--no-heading", "--with-filename", "--no-messages"]
    if icase:
        cmd.append("-i")
    if fixed:
        cmd.append("-F")
    cmd += ["-e", pat, "--"]
    # コーパス内で相対パスを渡す（Windows の "C:\..." のコロンで出力の区切りが崩れないように）。
    # Windows のコマンドライン長の上限（約32000字）を超えないよう、ファイルを分けて渡す
    batch, size = [], 0
    for k in list(keys) + [None]:
        if k is not None and size + len(k) < 20000:
            batch.append(k)
            size += len(k) + 1
            continue
        if batch:
            proc = subprocess.run(cmd + batch, capture_output=True, cwd=corpus)
            if proc.returncode not in (0, 1):   # 1 は「一致なし」。それ以外はエラー
                sys.exit("rg の実行に失敗しました: " + proc.stderr.decode("utf-8", "replace").strip()[:300])
            for raw in proc.stdout.decode("utf-8", "replace").splitlines():
                path, ln, text = raw.split(":", 2)
                yield path.replace("\\", "/"), int(ln)   # manifest のキーは / 区切り
        batch, size = ([k], len(k) + 1) if k is not None else ([], 0)


def hits_py(corpus, keys, pat, icase, fixed):
    rx = re.compile(re.escape(pat) if fixed else pat, re.I if icase else 0)
    for k in keys:
        with open(corpus / k, encoding="utf-8", errors="replace") as fh:
            for ln, line in enumerate(fh, 1):
                if rx.search(line):
                    yield k, ln


def read_lines(path, start, end):
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for ln, line in enumerate(fh, 1):
            if ln > end:
                break
            if ln >= start:
                out.append((ln, line.rstrip("\n")))
    return out


# 識別子の「定義」らしい行の判定（hsearch.py の definition_score と同じ考え方の簡略版。
# zsearch.py はコーパスに単体でコピーされて動くので、hsearch.py を import しない）
DEF_SECTION_RE = re.compile(r"^(Explanation|System action|Severity|User response|Operator response|"
                            r"System programmer response|Programmer response|Problem determination|"
                            r"Administrator response|Response|Module|Source|Routing code|Descriptor code)\b")
ID_LINE_RE = re.compile(r"^\$?[A-Z]{2,5}\d{2,5}[A-Z]{0,2}\b")
CODE_MANUAL_RE = re.compile(r"\b(Messages|Codes)\b")


def is_definition(lines, i, pat, code_manual):
    """lines[i] が識別子 pat の定義（説明の見出し）らしいか。lines は空白を除いた行のリスト"""
    s = lines[i]
    alone = s == pat
    if not (alone or s.startswith((pat + " ", pat + "("))):
        return False
    around = [x for x in lines[max(0, i - 3):i] + lines[i + 1:i + 4] if x]
    if sum(1 for x in around if ID_LINE_RE.match(x)) >= 2:
        return False   # 識別子が並んだ一覧の中
    before = [x for x in lines[max(0, i - 40):i] if x][-4:]
    prev = before[-1] if before else ""
    if DEF_SECTION_RE.match(prev) or prev.endswith(":") or any(x.startswith("Related ") for x in before):
        return False   # 別のメッセージの節の中・出力例・関連情報の一覧
    if any(DEF_SECTION_RE.match(x) for x in lines[i + 1:i + 31]):
        return True
    nxt = next((x for x in lines[i + 1:i + 6] if x), "")
    return alone and code_manual and len(nxt) > 30 and nxt[:1].isupper()


def cmd_find(corpus, manifest, a):
    keys = select(manifest, a.ver, a.form, a.title, a.product)
    if not keys:
        sys.exit("条件に合う文書がありません")
    gen = hits_rg if shutil.which("rg") else hits_py
    shown, per_doc = 0, {}
    for k, ln in gen(corpus, keys, a.pattern, a.i, a.F):
        if ln in manifest[k]["page_starts"]:
            continue  # ページ区切り行自体は除外
        per_doc.setdefault(k, []).append(ln)

    # 固定文字列（メッセージID など）の検索では、定義らしい箇所を [定義] と示して先に出す。
    # 文書を丸ごと読むので、ヒットした文書が多すぎるときはしない
    defs = {}
    if a.F and not a.i and len(per_doc) <= 100:
        for k, lns in per_doc.items():
            text = (corpus / k).read_text(encoding="utf-8", errors="replace").split("\n")
            lines = [t.strip() for t in text]
            cm = bool(CODE_MANUAL_RE.search(manifest[k].get("title") or ""))
            defs[k] = {ln for ln in lns if is_definition(lines, ln - 1, a.pattern, cm)}
    for k in sorted(per_doc, key=lambda k: (not defs.get(k), -len(per_doc[k]))):
        m = manifest[k]
        d = defs.get(k, set())
        print(f"\n## {k}  [{label(m)}]  ヒット {len(per_doc[k])} 行" + (f"（うち定義 {len(d)}）" if d else ""))
        print(f"   {basis(m)}")
        for ln in sorted(per_doc[k], key=lambda x: (x not in d, x)):
            if shown >= a.max:
                print(f"\n…上限 {a.max} 件で打ち切り（--max で変更）")
                return
            print(f"--- p.{page_of(m, ln)} (行 {ln})" + (" [定義]" if ln in d else ""))
            for n, t in read_lines(corpus / k, max(1, ln - a.C), ln + a.C):
                print(("> " if n == ln else "  ") + t)
            shown += 1


def cmd_page(corpus, manifest, a):
    m = manifest.get(a.doc) or sys.exit(f"manifest に無い文書: {a.doc}")
    ps = m["page_starts"]
    last = a.end or a.start
    if not (1 <= a.start <= last <= len(ps)):
        sys.exit(f"ページは 1〜{len(ps)}")
    end_line = ps[last] - 1 if last < len(ps) else 10**12
    print(f"# {a.doc}  [{label(m)}]  p.{a.start}" + (f"-{last}" if last != a.start else ""))
    print(f"# {basis(m)}")
    for _, t in read_lines(corpus / a.doc, ps[a.start - 1], end_line):
        print(t)


def cmd_docs(corpus, manifest, a):
    for k in select(manifest, a.ver, None, a.title, a.product):
        m = manifest[k]
        print(f'{k}\t{m.get("product") or "-"}\t{m.get("version") or "-"}\t{m.get("form") or "-"}'
              f'\t{m["pages"]}p\t{m.get("title") or ""}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("pattern")
    f.add_argument("-i", action="store_true", help="大文字小文字を無視")
    f.add_argument("-F", action="store_true", help="正規表現ではなく固定文字列")
    f.add_argument("-C", type=int, default=3, help="前後に表示する行数")
    f.add_argument("--product", help="製品名の部分一致（例: z/OS, Db2）")
    f.add_argument("--ver", help="製品の版（例: 3.2）")
    f.add_argument("--form", help="資料番号の前方一致（例: SA38）")
    f.add_argument("--title")
    f.add_argument("--max", type=int, default=40)
    p = sub.add_parser("page")
    p.add_argument("doc")
    p.add_argument("start", type=int)
    p.add_argument("end", type=int, nargs="?")
    d = sub.add_parser("docs")
    d.add_argument("--product")
    d.add_argument("--ver")
    d.add_argument("--title")
    a = ap.parse_args()

    corpus = Path(a.corpus).resolve()
    manifest = load(corpus)
    {"find": cmd_find, "page": cmd_page, "docs": cmd_docs}[a.cmd](corpus, manifest, a)


if __name__ == "__main__":
    main()
