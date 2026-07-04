"""AI 스크리닝 평가 하네스(보강안 §7.3·§12.3).

골든셋(tests/eval/*.json)을 스크리닝 엔진에 통과시켜 지표를 산출한다.
- danger recall: 위험(하람/mushbooh/무인증 동물성) 케이스 중 비-PASS 플래그 비율 — 목표 ≥0.98
- safe precision: 플래그된 것 중 실제 위험 비율(FP=safe가 플래그) — 목표 ≥0.90
- label detect recall: 라벨 텍스트에서 필수 성분 uid 탐지율 — 목표 ≥0.90
실행: <venv>/bin/python scripts/ai_eval.py   (서버 불필요, DB는 임시 시드)
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GLHAC_DB_URL", "sqlite:///./glhac_eval.db")
os.environ.setdefault("GLHAC_DEV", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_engine():
    from app.db import Base, engine, SessionLocal
    from app.ontology_seed import seed
    from app import screening
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed(db)
        screening.load_ontology(db)
    finally:
        db.close()
    return screening


def run_ingredient_eval(screening, verbose=True):
    g = json.load(open(os.path.join(ROOT, "tests/eval/ingredients_golden.json"), encoding="utf-8"))
    tp = fn = fp = tn = 0
    cleared_ok = cleared_bad = 0
    misses, false_pos = [], []
    for c in g["cases"]:
        r = screening.screen_merged(
            c["name"], c.get("e_number"), c.get("source"), c.get("cert_no"),
            bool(c.get("evidence_provided")), c.get("source_known", True), c.get("note", ""))
        flagged = r["result"] not in ("PASS", "CLEARED")
        exp = c["expect"]
        if exp == "danger":
            if flagged:
                tp += 1
            else:
                fn += 1
                misses.append((c["name"], r["result"], c["why"]))
        elif exp == "safe":
            if flagged:
                fp += 1
                false_pos.append((c["name"], r["result"], c["why"]))
            else:
                tn += 1
        elif exp == "cleared":
            if r["result"] == "CLEARED":
                cleared_ok += 1
            else:
                cleared_bad += 1
                misses.append((c["name"], r["result"], "CLEARED 기대"))
        # conservative_ok: 어느 쪽이든 허용(지표 제외)
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    if verbose:
        print("── 원재료 스크리닝 평가 ──")
        print(f"  danger {tp+fn}건: 플래그 {tp} · 누락 {fn} → recall={recall:.3f} (목표 ≥0.98)")
        print(f"  safe   {tn+fp}건: 통과 {tn} · 오탐 {fp} → precision={precision:.3f} (목표 ≥0.90)")
        print(f"  cleared 검증: {cleared_ok} ok / {cleared_bad} bad")
        for n, res, why in misses:
            print(f"    ✗ 누락: {n!r} → {res} ({why})")
        for n, res, why in false_pos:
            print(f"    ✗ 오탐: {n!r} → {res} ({why})")
    return {"recall": recall, "precision": precision,
            "misses": misses, "false_pos": false_pos, "cleared_bad": cleared_bad}


def run_label_eval(screening, verbose=True):
    g = json.load(open(os.path.join(ROOT, "tests/eval/labels_golden.json"), encoding="utf-8"))
    need = hit = 0
    misses = []
    for c in g["cases"]:
        found = {uid for uid, _ in screening.scan_text(c["text"])}
        for uid in c["must_detect"]:
            need += 1
            if any(u in found for u in uid.split("|")):   # '|' = 대체 uid 허용(동일물질 별칭)
                hit += 1
            else:
                misses.append((uid, c["text"][:50]))
    recall = hit / need if need else 1.0
    if verbose:
        print("── 라벨 텍스트 탐지 평가 ──")
        print(f"  필수 {need}건 중 탐지 {hit} → recall={recall:.3f} (목표 ≥0.90)")
        for uid, t in misses:
            print(f"    ✗ 미탐지: {uid} in {t!r}")
    return {"recall": recall, "misses": misses}


if __name__ == "__main__":
    sc = load_engine()
    ing = run_ingredient_eval(sc)
    lab = run_label_eval(sc)
    ok = ing["recall"] >= 0.98 and ing["precision"] >= 0.90 and lab["recall"] >= 0.90 \
        and ing["cleared_bad"] == 0
    print("\n결과:", "PASS ✅" if ok else "FAIL ⛔")
    sys.exit(0 if ok else 1)
