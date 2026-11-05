#!/usr/bin/env python3
"""
IBM マニュアルの PDF 群をテキスト化し、検索用コーパスを作る（z/OS、Db2 など製品は問わない）。

  python3 extract.py <PDFのディレクトリ> <出力ディレクトリ> [-j 並列数] [--layout]

PDF のディレクトリが複数あるときは、同じ出力ディレクトリを指定して1つずつ実行する。
文書は text/<PDFディレクトリ名>/<相対パス>.txt に置かれる。

出力:
  <出力>/text/<PDFディレクトリ名>/<相対パス>.txt   ページ区切り行「=== PAGE n ===」入りのテキスト
  <出力>/manifest.json   文書ごとの製品名・版・資料番号・タイトル・ページ開始行
  <出力>/failed.tsv      抽出に失敗した、または文字がほぼ無い（スキャンの疑い）PDF
  <出力>/overrides.tsv   推定を手で直すためのファイル（任意。書式は下の OVERRIDE_HELP）

方針は docs/INGEST.md を参照。
既に抽出済みで、元 PDF の大きさ・更新日時が前回と同じで、抽出規則の版（EXTRACTOR_VERSION）が
同じものはスキップする。別の場所にある同じ名前の PDF フォルダを追加しようとすると、上書きせずに止まる。
必要なもの: pdftotext（poppler）
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# 抽出・メタデータ推定の規則を変えたら上げる。版が違う文書は次回の実行で抽出し直される
EXTRACTOR_VERSION = 7

PAGE_MARK = "=== PAGE {} ==="
META_FIELDS = ("product", "version", "form", "title")

OVERRIDE_HELP = """\
# 推定した製品名・版・資料番号・タイトルを手で直す。タブ区切り。空欄の項目は推定のまま。
# 1列目はPDFのファイル名（例: dsnm_v13.pdf）か manifest のキー（例: text/db2/dsnm_v13.txt）
# file\tproduct\tversion\tform\ttitle
"""

# IBM の資料番号（例: SA38-0668-70, G550-0432-07, GC27-3667-70）
FORM_RE = re.compile(r"\b([A-Z]{1,2}\d{2,3}-\d{4}-\d{1,2})\b")
FORM_LINE_RE = re.compile(r"^[A-Z]{1,2} ?\d{2,3}-\d{4}")

# 製品名を構成する語: 先頭は大文字か z/（z/OS など）。2語目以降は to/for/of/from も可
# （and は入れない。「ICSF FMID HCR77E0 and IBM z/OS 3.1」のように2製品が並ぶため）
_W1 = r"(?:[A-Z]|z/)[\w/+.-]*"
_WN = r"(?:(?:[A-Z]|z/)[\w/+.-]*|to|for|of|from)"
NAME = rf"{_W1}(?:\s+{_WN}){{0,6}}?"

# 「This edition applies to …」の宣言文から製品名と版を取る（上から順に試す）
EDITION_RES = [
    # Version 1 Release 2 of IBM Encryption Facility for z/OS (5655-P97)
    re.compile(r"[Vv]ersion\s+(?P<v>\d+)[,.]?\s*[Rr]elease\s+(?P<r>\d+)(?:[,.]?\s*[Mm]odification\s+\d+)?"
               r"\s+of\s+(?:the\s+)?(?P<name>.+?)(?=\s*\(|,|;|\s+and\s|\s+for\s+z/OS|\.\s|$)"),
    # IBM Print Services Facility Version 4 Release 8 Modification 0 for z/OS
    re.compile(rf"(?P<name>{NAME})\s+[Vv]ersion\s+(?P<v>\d+)[,.]?\s*[Rr]elease\s+(?P<r>\d+)"),
    # IBM CICS Transaction Server for z/OS, Version 6 (product number 5655-BTA)（Release の無い形）
    re.compile(rf"(?P<name>{NAME}),?\s+[Vv]ersion\s+(?P<v>\d+)(?:\.(?P<r>\d+))?(?![\d.]|[,.]?\s*[Rr]elease)"),
    # IBM z/OS 3.2 (5655-ZOS) / Db2 13 for z/OS (product number 5698-DB2)
    re.compile(rf"(?P<name>{NAME})\s+(?P<v>\d+)(?:\.(?P<r>\d+))?(?:\.\d+)*\s+(?:for\s+z/OS\s+)?\("),
]
# 表紙の版だけの行（3.2 / Version 4.8 / V04.07.00 / Version 1.Release 2）
COVER_VER_RE = re.compile(r"^(?:Version\s+|V)?(?P<v>\d+)(?:\.(?P<r>\d+))?(?:\.\d+)*$"
                          r"|^Version\s+(?P<v2>\d+)\.?\s*Release\s+(?P<r2>\d+)$")
# 表紙・書名中の表記（上から順に試す）
COVER_RES = [
    EDITION_RES[1],                                                       # … Version 4 Release 7.0 …
    re.compile(rf"(?P<name>{NAME})\s+V(?P<v>\d+)R(?P<r>\d+)\b"),          # IBM BatchPipes OS/390 V2R1
    re.compile(rf"^(?P<name>{NAME})\s+V?(?P<v>\d+)\.(?P<r>\d+)(?:\.\d+)*\b", re.M),  # z/OS 3.2 Licensed …
    re.compile(rf"^(?P<name>{NAME})\s+(?P<v>\d+)\s+for\s+z/OS\b", re.M),              # Db2 13 for z/OS …
]
# 注: ファイル名の _v3r2 などは「同梱先の z/OS の版」で、製品の版ではないので使わない
# （例: aokfa00_v2r3.pdf と aokfa00_v3r2.pdf は同じ資料 G325-2634-40）
# 書名を取るとき、ここで読むのをやめる行
TITLE_STOP_RE = re.compile(r"^(IBM|Note|Date:|Latest Update|First Edition|Program Number|Document Date)\b")


def norm_version(v, r=None):
    return f"{int(v)}.{int(r)}" if r is not None else str(int(v))


def norm_product(name):
    """製品名の表記をそろえる。製品名らしくなければ None"""
    s = "".join(ch for ch in name if ch.isprintable() or ch.isspace())  # ゼロ幅文字などを除く
    s = re.sub(r"\s+", " ", s).strip(" ,.:;")
    s = re.sub(r"^(?:the\s+)?(?:IBM\s+)*", "", s)
    s = re.sub(r"^Program Directory for\s+(?:IBM\s+)?", "", s, flags=re.I)
    s = re.sub(r"\s*\([^)]*\)$", "", s)                     # 末尾の (SCRT) など
    s = re.sub(r"\s+V?\d+(?:\.\d+)*$", "", s)               # 名前に付いた版（… for z/OS 6.2）
    s = re.sub(r"\s+for\s+(?:IBM\s+)?z/OS$", "", s)         # Db2 for z/OS → Db2（表記ゆれをそろえる）
    if s.startswith("z/OS "):                               # z/OS Communications Server など z/OS の構成要素
        s = "z/OS"
    s = s.strip()
    # 表紙の行を丸ごと拾ったもの（書名・目次など）は製品名として扱わない
    if not s or len(s) > 50 or len(s.split()) > 6 or ":" in s or '"' in s:
        return None
    return s


def edition_statement(head):
    """「This edition applies to …」の文。(applies to 以降の最大500字, 根拠として見せる1文)"""
    flat = re.sub(r"\s+", " ", head)
    m = re.search(r"(?:This\s+)?(?:edition|book|publication)\s+applies\s+to\s+(.{0,500})", flat, re.I)
    if not m:
        return "", ""
    shown = re.split(r"(?<=[a-z0-9)])\.\s", m.group(0), maxsplit=1)[0]
    return m.group(1), shown[:240]


def cover_lines(pages):
    """表紙（2行以上ある最初のページ）の空でない行"""
    for p in pages[:3]:
        lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
        if len(lines) >= 2:
            return lines
    return []


def guess_product_version(head, cover):
    """(製品名, 版, 出どころ, 根拠の文) を返す。根拠は検索画面で「なぜこの版か」を見せるのに使う"""
    st, shown = edition_statement(head)
    if re.match(r"\s*(?:the\s+)?following\b", st, re.I) or re.search(r"\bthe following\b", st[:80], re.I):
        st = ""   # 「the following licensed programs: …」のように複数の製品を挙げる宣言文からは決めない
    for rx in EDITION_RES:
        m = rx.search(st)
        if m and "following" not in m.group("name"):
            prod = norm_product(m.group("name"))
            if prod:
                return prod, norm_version(m.group("v"), m.group("r")), "edition", shown
    # 表紙: 製品名の行（1〜4行）のすぐ後に版だけの行がある
    for i in range(1, min(5, len(cover))):
        m = COVER_VER_RE.match(cover[i])
        if m:
            v, r = (m.group("v"), m.group("r")) if m.group("v") else (m.group("v2"), m.group("r2"))
            prod = norm_product(" ".join(cover[:i]))
            if prod and not COVER_VER_RE.match(cover[0]):
                return prod, norm_version(v, r), "cover", " / ".join(cover[:i + 1])[:240]
            break
    joined, top = " ".join(cover[:6]), "\n".join(cover[:3])
    for rx in COVER_RES:
        m = rx.search(top if rx.flags & re.M else joined)
        if m and norm_product(m.group("name")):
            return (norm_product(m.group("name")), norm_version(m.group("v"), m.groupdict().get("r")), "cover",
                    re.sub(r"\s+", " ", m.group(0))[:240])
    return None, None, None, None


ITSO_RE = re.compile(r"^International Technical Support Organization$")
MONTH_RE = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
                      r"\s+\d{4}$")


def redbook_title(pages):
    """Redbook の扉（「International Technical Support Organization」の次の行から日付の前まで）の書名"""
    for p in pages[:8]:
        lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
        for i, ln in enumerate(lines):
            if ITSO_RE.match(ln):
                out = []
                for x in lines[i + 1:i + 6]:
                    if MONTH_RE.match(x) or FORM_LINE_RE.match(x):
                        break
                    out.append(x)
                if out:
                    return " ".join(out)[:200]
    return ""


def guess_title(cover):
    """表紙の先頭から、版だけの行を除いて最大5行をつなげる"""
    out = []
    if cover and cover[0] == "Front cover":   # Redbook の表紙の見出し
        cover = cover[1:]
    for ln in cover:
        if TITLE_STOP_RE.match(ln) or FORM_LINE_RE.match(ln) or len(out) >= 5:
            break
        if COVER_VER_RE.match(ln):
            continue
        out.append(ln)
    return " ".join(out)[:200]


def guess_meta(pages, filename):
    head = "\n".join(pages[:4]).replace("®", "").replace("™", "")
    cover = cover_lines([p.replace("®", "").replace("™", "") for p in pages])
    product, version, source, evidence = guess_product_version(head, cover)
    form = FORM_RE.search(head) or FORM_RE.search(filename)
    return {
        "product": product,
        "version": version,
        "version_from": source,       # edition（版の宣言文）/ cover（表紙）/ None
        "version_evidence": evidence,  # 推定に使った PDF 中の文（検索画面に出す）
        "form": form.group(1) if form else None,
        "title": redbook_title(pages) or guess_title(cover),
    }


def extract_one(pdf: Path, out_txt: Path, layout: bool):
    # -eol unix: Windows 版 pdftotext は既定で CRLF を出すため、改行を LF にそろえる
    args = ["pdftotext", "-enc", "UTF-8", "-eol", "unix"]
    if layout:
        args.append("-layout")
    args += [str(pdf), "-"]
    try:
        res = subprocess.run(args, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    if res.returncode != 0:
        return {"error": res.stderr.decode("utf-8", "replace").strip()[:300]}

    raw = res.stdout.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    pages = raw.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()

    page_starts, lines_out, line_no = [], [], 0
    for i, p in enumerate(pages, 1):
        lines_out.append(PAGE_MARK.format(i))
        line_no += 1
        page_starts.append(line_no)  # 1始まりの行番号（マーカー行）
        body = p.rstrip("\n").split("\n")
        lines_out.extend(body)
        line_no += len(body)

    text = "\n".join(lines_out) + "\n"
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    # バイト列で書く（Windows のテキストモードで \n が \r\n に変わり、行番号がずれるのを防ぐ）
    out_txt.write_bytes(text.encode("utf-8"))

    chars = sum(len(p.strip()) for p in pages)
    auto = guess_meta(pages, pdf.name)
    return {
        "pages": len(pages),
        "chars": chars,
        "page_starts": page_starts,
        "suspect_scan": len(pages) > 0 and chars / len(pages) < 100,
        "auto": auto,
        **{k: auto[k] for k in META_FIELDS},
    }


def read_text_any(path: Path):
    """メモ帳（BOM 付き UTF-8）や Excel（Shift-JIS）で保存されたファイルも読む"""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        print(f"注意: {path.name} を UTF-8 として読めないので Shift-JIS として読みます", file=sys.stderr)
        return raw.decode("cp932", "replace")


def load_overrides(path: Path):
    if not path.exists():
        path.write_text(OVERRIDE_HELP, encoding="utf-8")
        return {}
    out = {}
    for ln in read_text_any(path).splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        cols = (ln.split("\t") + [""] * 5)[:5]
        key = cols[0].strip().replace("\\", "/")   # Windows の区切りで書かれたキーも manifest に合わせる
        out[key] = {k: c.strip() for k, c in zip(META_FIELDS, cols[1:]) if c.strip()}
    return out


def source_of(m):
    """元 PDF のパス（EXTRACTOR_VERSION 1 の項目は source_pdf という名前だった）"""
    return m.get("source") or m.get("source_pdf") or ""


def apply_overrides(manifest, overrides):
    """推定値（auto）に戻してから補正を当てる。補正を消せば推定値に戻る"""
    used = set()
    for key, m in manifest.items():
        if "auto" not in m:   # 古い形式の項目（そのディレクトリを抽出し直すまで残る）
            continue
        name = Path(m["source"]).name
        ov = overrides.get(key) or overrides.get(name)
        for k in META_FIELDS:
            m[k] = m["auto"].get(k)
        m.pop("overridden", None)
        if ov:
            m.update(ov)
            m["overridden"] = sorted(ov)
            used.add(key if key in overrides else name)
    return [k for k in overrides if k not in used]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--layout", action="store_true",
                    help="段組・表の位置を保つ（表が多いマニュアル向け。行が長くなる）")
    a = ap.parse_args()

    src, dst = Path(a.src).resolve(), Path(a.dst).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    text_root = dst / "text" / src.name
    manifest_path, failed_path = dst / "manifest.json", dst / "failed.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    failed = json.loads(failed_path.read_text(encoding="utf-8")) if failed_path.exists() else {}

    pdfs = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf")
    wanted = {}
    for pdf in pdfs:
        out_txt = text_root / pdf.relative_to(src).with_suffix(".txt")
        wanted[out_txt.relative_to(dst).as_posix()] = (pdf, out_txt)

    # このディレクトリ由来で、元 PDF が消えた・置き場所が変わった文書を除く
    # （source_pdf は EXTRACTOR_VERSION 1 の項目名。古い置き場所の文書もここで消える）
    for key in [k for k, m in manifest.items()
                if Path(source_of(m)).is_relative_to(src) and k not in wanted]:
        (dst / key).unlink(missing_ok=True)
        del manifest[key]
    failed = {k: v for k, v in failed.items() if not Path(v["source"]).is_relative_to(src) or k in wanted}

    # 別の場所にある同じ名前のフォルダの文書と置き場所がぶつかるなら、上書きせずに止める
    clash = [k for k in wanted if k in manifest and not Path(source_of(manifest[k])).is_relative_to(src)]
    if clash:
        other = Path(source_of(manifest[clash[0]])).parent
        sys.exit(f"フォルダ名 '{src.name}' が、既にコーパスにある別のフォルダ（{other} など）と同じで、"
                 f"{len(clash)} 件の置き場所がぶつかります。PDF のフォルダ名を変えてから実行してください。")

    todo = []
    for key, (pdf, out_txt) in wanted.items():
        m = manifest.get(key)
        st = pdf.stat()
        # PDF の大きさと更新日時が前回と同じときだけスキップする（古い日付のファイルで差し替えても検出できる）
        if (m and m.get("extractor") == EXTRACTOR_VERSION and out_txt.exists()
                and m.get("source") == str(pdf)
                and m.get("pdf_size") == st.st_size and m.get("pdf_mtime") == st.st_mtime):
            continue
        todo.append((pdf, out_txt, key))

    print(f"PDF {len(pdfs)} 件中、処理対象 {len(todo)} 件", file=sys.stderr)
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(extract_one, p, o, a.layout): (p, k) for p, o, k in todo}
        for n, f in enumerate(as_completed(futs), 1):
            pdf, key = futs[f]
            r = f.result()
            if "error" in r:
                failed[key] = {"source": str(pdf), "error": r["error"]}
                manifest.pop(key, None)
            else:
                failed.pop(key, None)
                st = pdf.stat()
                manifest[key] = {**r, "source": str(pdf), "extractor": EXTRACTOR_VERSION,
                                 "pdf_size": st.st_size, "pdf_mtime": st.st_mtime}
            if n % 50 == 0 or n == len(todo):
                print(f"  {n}/{len(todo)}", file=sys.stderr)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    unused = apply_overrides(manifest, load_overrides(dst / "overrides.tsv"))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    failed_path.write_text(json.dumps(failed, ensure_ascii=False), encoding="utf-8")

    # failed.tsv は全ディレクトリ分を毎回書き出す（抽出失敗 + スキャンの疑い）
    rows = [(v["source"], v["error"]) for v in failed.values()]
    rows += [(source_of(v), "文字が少ない（スキャンPDFの疑い。OCRが必要）")
             for v in manifest.values() if v.get("suspect_scan")]
    with open(dst / "failed.tsv", "w", encoding="utf-8", newline="\n") as fh:
        for p, e in sorted(rows):
            fh.write(f"{p}\t{e}\n")

    # 単純検索のツールをコーパスに置く
    here = Path(__file__).resolve().parent
    shutil.copy2(here / "zsearch.py", dst / "zsearch.py")

    no_prod = sum(1 for v in manifest.values() if not v.get("product"))
    no_ver = sum(1 for v in manifest.values() if not v.get("version"))
    no_form = sum(1 for v in manifest.values() if not v.get("form"))
    print(f"完了: {len(manifest)} 文書 / 要確認 {len(rows)} 件 (failed.tsv)", file=sys.stderr)
    print(f"推定できず: 製品名 {no_prod} 件、版 {no_ver} 件、資料番号 {no_form} 件"
          f"（overrides.tsv で補正できる）", file=sys.stderr)
    if unused:
        print(f"注意: overrides.tsv の次の行に一致する文書がありません: {', '.join(unused)}", file=sys.stderr)


if __name__ == "__main__":
    main()
