"""인도네시아 실무 기록물 사진(Catatan…) OCR 텍스트 → 표 행으로 복원.

왜 필요한가: SJPH 매뉴얼 부록 6·7·10·12·13은 구매·검사·보관·생산·유통 기록을 적는 표다.
그 값(수량·날짜·담당자)은 플랫폼 DB에 없고 업체가 낸 사진 안에만 있다. 지어내면 위조이고,
비워 두면 매뉴얼이 반쪽이다. 그래서 사진에서 읽어 온다.

OCR은 표를 왼→오른쪽으로 곧게 읽어 주지 않는다(열이 섞여 나온다). 그래서 열 위치를
가정하지 않고, 각 행에 반드시 하나씩 있는 것(행번호·날짜)을 닻으로 삼아 조각을 가른 뒤
그 안에서 수량·판정·이름을 집어낸다. 확신이 없으면 그 칸은 비운다 — 틀린 값보다 빈칸이 낫다.
"""
import re

_DATE = r"\d{1,2}/\d{1,2}/\d{4}"
_QTY_UNIT = (r"KG|GRAM|GR|LITER|LTR|PCS|PACK|BOTOL|BUNGKUS|SACHET|RENCENG|BUAH|GALON|"
             r"KALENG|DUS|BOX|IKAT|LEMBAR")
_QTY = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(%s)\b" % _QTY_UNIT, re.I)
_PLAIN_QTY = re.compile(r"\b(\d{1,5})\b")
_CONFORM = re.compile(r"\b(SESUAI|TIDAK\s+SESUAI|CONFORMING|NON[- ]?CONFORMING)\b", re.I)
# 서명부·머리말 — 데이터가 아니다
_TAIL = re.compile(r"(Ditetapkan\s+di|Pemilik\s+Usaha|Penyelia\s+Halal)", re.I)


def rows_from_ocr(lines, row_tol=0.6):
    """OCR 조각(좌표 포함) → 표의 행 목록. 각 행은 x 오름차순 셀 텍스트.

    같은 행인지는 세로 중심이 글자 높이의 row_tol 안에 있는지로 본다. 열 경계를 미리
    가정하지 않는다 — 템플릿마다 열 폭이 다르고, 병합 셀도 있기 때문이다."""
    items = [dict(l) for l in (lines or []) if l.get("box") and (l.get("text") or "").strip()]
    if not items:
        return []
    for it in items:
        x0, y0, x1, y1 = it["box"]
        it["cy"], it["h"] = (y0 + y1) / 2.0, max(1.0, y1 - y0)
    items.sort(key=lambda x: x["cy"])
    rows, cur = [], [items[0]]
    for it in items[1:]:
        ref = sum(c["cy"] for c in cur) / len(cur)
        tol = row_tol * max(c["h"] for c in cur)
        if abs(it["cy"] - ref) <= tol:
            cur.append(it)
        else:
            rows.append(cur)
            cur = [it]
    rows.append(cur)
    out = []
    for r in rows:
        r.sort(key=lambda x: x["box"][0])
        out.append([{"text": c["text"].strip(), "x": c["box"][0]} for c in r])
    return out


def cells(row):
    return [c["text"] for c in row]


def _body(text):
    """서명부 앞까지만 본다."""
    t = re.sub(r"\s+", " ", text or "").strip()
    m = _TAIL.search(t)
    return t[:m.start()] if m else t


def _pic(t):
    """모든 행에 반복되는 담당자 이름 — 가장 많이 나온 사람 이름꼴을 담당자로 본다."""
    names = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b", t)
    if not names:
        return ""
    best, n = "", 0
    for x in set(names):
        c = names.count(x)
        if c > n and len(x) > 5:
            best, n = x, c
    return best if n >= 2 else ""


def _chunks(t):
    """날짜를 닻으로 행 조각을 만든다. 각 조각 = 한 행(대개)."""
    pos = [m.start() for m in re.finditer(_DATE, t)]
    if not pos:
        return []
    out = []
    for i, p in enumerate(pos):
        start = pos[i - 1] + 10 if i else 0
        end = pos[i + 1] if i + 1 < len(pos) else len(t)
        out.append((re.search(_DATE, t[p:p + 12]).group(0), t[start:end]))
    return out


def _clean_name(s, drop_words=()):
    s = re.sub(r"\b\d{1,2}/\d{1,2}/\d{4}\b", " ", s)
    s = _QTY.sub(" ", s)
    for w in drop_words:
        s = re.sub(re.escape(w), " ", s, flags=re.I)
    s = _CONFORM.sub(" ", s)
    s = re.sub(r"\b(PT\.?|CV\.?)\s+[A-Za-z][\w.]*(?:\s+[A-Z][\w.]*){0,4}", " ", s)
    s = re.sub(r"^\s*\d{1,2}\s+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–|")
    return s


def parse_purchase(text):
    """CATATAN PEMBELIAN BAHAN — 재료명·수량·구매일·담당자."""
    t = _body(text)
    pic = _pic(t)
    rows = []
    for i, (dt, seg) in enumerate(_chunks(t), 1):
        q = _QTY.search(seg)
        rows.append({"no": str(i), "name": _clean_name(seg, (pic,)),
                     "qty": ("%s %s" % (q.group(1), q.group(2).upper())) if q else "",
                     "date": dt, "pic": pic})
    return [r for r in rows if r["name"] or r["qty"]]


def parse_inspection(text):
    """FORM PEMERIKSAAN BAHAN — 입고일·재료명·공급사·적합여부·담당자."""
    t = _body(text)
    pic = _pic(t)
    rows = []
    for i, (dt, seg) in enumerate(_chunks(t), 1):
        sup = re.search(r"\b((?:PT|CV)\.?\s+[A-Za-z][\w.]*(?:\s+[A-Z][\w.]*){0,4})", seg)
        if not sup:
            sup = re.search(r"\b(UNILEVER[A-Z ]*)\b", seg)
        conf = _CONFORM.search(seg)
        rows.append({"no": str(i), "date": dt, "name": _clean_name(seg, (pic,)),
                     "supplier": (sup.group(1).strip() if sup else ""),
                     "conform": (conf.group(1).upper() if conf else ""), "pic": pic})
    return [r for r in rows if r["name"] or r["supplier"]]


def parse_storage(text):
    """CATATAN PENYIMPANAN — 재료명·제조사·입고일·출고일·수량."""
    t = _body(text)
    pic = _pic(t)
    rows, chunks = [], _chunks(t)
    # 보관 기록은 한 행에 날짜가 둘(입고·출고)이라 두 조각이 한 행이다.
    for i in range(0, len(chunks) - 1, 2):
        d_in, seg = chunks[i]
        d_out, seg2 = chunks[i + 1]
        sup = re.search(r"\b((?:PT|CV)\.?\s+[A-Za-z][\w.]*(?:\s+[A-Z][\w.]*){0,5})", seg + " " + seg2)
        nums = re.findall(r"\b(\d{1,4})\b", seg2)
        rows.append({"no": str(i // 2 + 1), "name": _clean_name(seg, (pic,)),
                     "supplier": (sup.group(1).strip() if sup else ""),
                     "in": d_in, "out": d_out,
                     "qty": (nums[0] if nums else ""), "pic": pic})
    return [r for r in rows if r["name"]]


def _parse_simple(text, tail_label):
    """생산·유통 기록 — '번호 날짜 제품명 수량 [비고]' 로 곧게 읽히는 표."""
    t = _body(text)
    rows = []
    for m in re.finditer(r"(\d{1,2})\s+(%s)\s+(.+?)\s+(\d{1,5})\b(\s+[A-Z][A-Z ]{2,})?" % _DATE, t):
        name = re.sub(r"\s+", " ", m.group(3)).strip(" -–|")
        if not name or len(name) > 60:
            continue
        rows.append({"no": m.group(1), "date": m.group(2), "name": name,
                     "qty": m.group(4), tail_label: (m.group(5) or "").strip()})
    # 제품명이 수량 뒤로 밀린 행 보정 — 'No Date 50 NAME'
    if not rows:
        for m in re.finditer(r"(\d{1,2})\s+(%s)\s+(\d{1,5})\s+([A-Z][A-Za-z ]{2,40})" % _DATE, t):
            rows.append({"no": m.group(1), "date": m.group(2),
                         "name": m.group(4).strip(), "qty": m.group(3), tail_label: ""})
    return rows


def parse_production(text):
    """CATATAN HASIL PRODUKSI — 생산일·제품명·수량·비고."""
    return _parse_simple(text, "note")


def parse_distribution(text):
    """CATATAN DISTRIBUSI/PENJUALAN — 출고일·제품명·수량·행선지."""
    return _parse_simple(text, "dest")


PARSERS = {"purchase_log": parse_purchase, "receiving_log": parse_inspection,
           "usage_log": parse_storage, "production_log": parse_production,
           "distribution_log": parse_distribution}


def parse(evidence_key, text):
    fn = PARSERS.get(evidence_key)
    if not fn or not (text or "").strip():
        return []
    try:
        return fn(text)
    except Exception:  # noqa: BLE001
        return []


# ── 좌표 기반 파서 ──────────────────────────────────────────────────────
# 평문 파서는 OCR이 열을 섞어 내보내면 재료명과 담당자가 엉킨다(실측). 좌표가 있으면
# 행을 기하로 복원할 수 있으므로 이쪽이 정본이고, 좌표가 없을 때만 평문 파서로 떨어진다.

def _row_text(row):
    return " ".join(c["text"] for c in row).strip()


def _is_data_row(row):
    """맨 왼쪽 셀이 행번호인 줄만 데이터로 본다 — 머리글·이어붙은 줄을 걸러낸다."""
    return bool(row) and re.fullmatch(r"\d{1,2}", row[0]["text"].strip() or "")


def _merge_wrapped(rows):
    """데이터 행 다음의 번호 없는 줄(줄바꿈된 셀)을 그 행에 이어 붙인다."""
    out = []
    for row in rows:
        if _is_data_row(row) or not out:
            out.append(list(row))
        else:
            out[-1].extend(row)
    return [r for r in out if _is_data_row(r)]


def _pick(row, pat):
    for c in row:
        m = re.search(pat, c["text"], re.I)
        if m:
            return m.group(0).strip()
    return ""


def _names(row, exclude=()):
    """수량·날짜·판정·사람이름을 뺀 나머지를 이름 칸으로 본다."""
    bad = re.compile(r"^\d{1,3}$|^%s$|^\d+\s*(%s)$|^(SESUAI|TIDAK)$" % (_DATE, _QTY_UNIT), re.I)
    keep = [c["text"] for c in row[1:]
            if c["text"] and not bad.match(c["text"].strip())
            and c["text"].strip() not in exclude]
    return " ".join(keep).strip()


def _pic_from_rows(rows):
    """모든 행에 반복되는 담당자 — 이름이 여러 셀로 쪼개져 나오므로 토큰 단위로 본다.

    'Malvin David Huliselan' 이 행마다 [Malvin][David] / [Huliselan] 처럼 갈라져
    담당자 칸에서 일부만 잡히고 나머지가 재료명에 섞여 들어갔다(실측). 절반 이상의
    행에 나오는 사람이름 토큰을 담당자 토큰으로 보고 이름 칸에서 전부 뺀다."""
    if not rows:
        return "", set()
    cnt = {}
    for r in rows:
        seen = set()
        for c in r[1:]:
            for tok in re.findall(r"\b[A-Z][a-z]{2,}\b", c["text"]):
                seen.add(tok)
        for tok in seen:
            cnt[tok] = cnt.get(tok, 0) + 1
    half = max(2, (len(rows) + 1) // 2)
    toks = {t for t, n in cnt.items() if n >= half}
    # 순서를 지켜 사람 이름으로 복원(가장 오른쪽 셀에 나온 순)
    order = []
    for r in rows:
        for c in r[1:]:
            for tok in re.findall(r"\b[A-Z][a-z]{2,}\b", c["text"]):
                if tok in toks and tok not in order:
                    order.append(tok)
    return " ".join(order), toks


def parse_rows(evidence_key, ocr_lines):
    """좌표가 있는 OCR 결과 → 기록 행. 좌표가 없으면 빈 목록(호출부가 평문으로 폴백)."""
    grid = _merge_wrapped(rows_from_ocr(ocr_lines))
    if not grid:
        return []
    pic, pic_toks = _pic_from_rows(grid)
    ex = set(pic.split()) | pic_toks
    out = []
    for row in grid:
        no = row[0]["text"].strip()
        date = _pick(row, _DATE)
        qty_u = _pick(row, r"\d+(?:[.,]\d+)?\s*(?:%s)" % _QTY_UNIT)
        sup = _pick(row, r"(?:PT|CV)\.?\s+[A-Za-z][\w.]*(?:\s+[A-Z][\w.]*){0,5}") \
            or _pick(row, r"UNILEVER[A-Z ]*")
        conf = _pick(row, r"SESUAI|TIDAK\s+SESUAI")
        nums = [c["text"] for c in row[1:] if re.fullmatch(r"\d{1,4}", c["text"].strip())]
        name = _names(row, ex | {sup, conf, qty_u, date})
        if sup:
            name = name.replace(sup, " ")
        for tok in pic_toks:
            name = re.sub(r"\b%s\b" % re.escape(tok), " ", name)
        name = re.sub(r"\s+", " ", name).strip(" -–|")
        if evidence_key == "purchase_log":
            out.append({"no": no, "name": name, "qty": qty_u, "date": date, "pic": pic})
        elif evidence_key == "receiving_log":
            out.append({"no": no, "date": date, "name": name, "supplier": sup,
                        "conform": conf.upper(), "pic": pic})
        elif evidence_key == "usage_log":
            ds = re.findall(_DATE, _row_text(row))
            out.append({"no": no, "name": name, "supplier": sup,
                        "in": (ds[0] if ds else ""), "out": (ds[1] if len(ds) > 1 else ""),
                        "qty": (nums[0] if nums else ""), "pic": pic})
        elif evidence_key in ("production_log", "distribution_log"):
            tail = "note" if evidence_key == "production_log" else "dest"
            # 비고·행선지는 제품명 조각이 아니라 실제 값일 때만 넣는다.
            extra = _pick(row, r"\bCUSTOMER\b|\bEXPIRED\b|\bEKSPOR\b|\bLOKAL\b")
            out.append({"no": no, "date": date, "name": name,
                        "qty": (nums[-1] if nums else qty_u),
                        tail: (extra if extra and extra not in name else "")})
    return [r for r in out if (r.get("name") or "").strip()]
