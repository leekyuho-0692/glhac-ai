#!/usr/bin/env python3
"""[C1] 참조무결성 사전점검 — case_id 자식 테이블의 고아행 스캔.
   FK 부착(EPIC C) 전 정리용. 기본 dry-run. --fix로 진짜 고아행 삭제(백업 후).
   workflow_event/audit_log의 센티넬 case_id(user_id·billing-defaults:·fatwa-committee:)는 정상으로 분류."""
import sys, sqlite3, os

DB = os.environ.get("GLHAC_DB_PATH", "/Users/dany/IdeaProjects/llmdev/glhac-ai-v3/glhac_v3.db")
FIX = "--fix" in sys.argv

# case_id → case_application 참조 자식 테이블(부모/센티넬 제외 대상은 아래서 특수처리)
CHILD = ["generated_document","notification","change_impact","product","product_material","material",
         "pendamping_assignment","audit_finding","lph_assignment","external_identity","halal_certificate",
         "fatwa_decision","invoice","hpas_evaluation","document_asset","sjph_evidence","onsite_checklist",
         "auditor_pool","discussion","ai_extraction","audit_plan","fatwa_vote","contract","consultation",
         "corrective_action","integration_event","signature","payment","payment_match_candidate","refund",
         "material_measurement"]
SENTINEL = ["workflow_event","audit_log"]  # case_id에 센티넬 허용 → 별도 분류

def main():
    db = sqlite3.connect(DB); cur = db.cursor()
    def tables(): return {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    T = tables()
    cases = {r[0] for r in cur.execute("SELECT case_id FROM case_application")}
    users = {r[0] for r in cur.execute("SELECT id FROM users") } if "users" in T else set()
    print(f"[C1] 무결성 스캔 · DB={os.path.basename(DB)} · 케이스 {len(cases):,} · {'FIX' if FIX else 'DRY-RUN'}")
    total_orphan = 0
    for t in CHILD:
        if t not in T: continue
        rows = cur.execute(f"SELECT case_id, COUNT(*) FROM {t} WHERE case_id NOT IN (SELECT case_id FROM case_application) GROUP BY case_id").fetchall()
        n = sum(c for _,c in rows)
        if n:
            total_orphan += n
            print(f"  ⚠ {t}: 고아행 {n} (참조없는 case_id {len(rows)}종)")
            if FIX:
                cur.execute(f"DELETE FROM {t} WHERE case_id NOT IN (SELECT case_id FROM case_application)")
                print(f"     → 삭제 {cur.rowcount}")
    # 센티넬 테이블: 진짜 고아 vs 센티넬 구분
    for t in SENTINEL:
        if t not in T: continue
        rows = cur.execute(f"SELECT DISTINCT case_id FROM {t} WHERE case_id NOT IN (SELECT case_id FROM case_application)").fetchall()
        sent=[]; orph=[]
        for (cid,) in rows:
            s=str(cid)
            if s in users or s.startswith("billing-defaults:") or s.startswith("fatwa-committee:") or ":" in s:
                sent.append(cid)
            else: orph.append(cid)
        print(f"  · {t}: 비케이스 case_id {len(rows)}종 = 센티넬 {len(sent)} / 진짜고아 {len(orph)}")
        if orph: print(f"     ⚠ 진짜 고아 예시: {orph[:3]}")
    if FIX: db.commit(); print("커밋 완료")
    print(f"── 요약: 순수 자식 고아행 {total_orphan}건 {'삭제됨' if FIX else '(dry-run)'}")

if __name__=="__main__": main()
