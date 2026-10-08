"""GLHAC_FORMAL_FLOW 전환 시 진행 중 케이스 면제(grandfather) 스크립트.

`GLHAC_FORMAL_FLOW=1` 을 켤 때 이미 진행 중인 케이스는 새 절차(인증 가능 판정→정식 신청→접수 확인)를
면제한다 — 면제 이벤트를 넣어 게이트를 통과시킨다. 멱등·기본 dry-run.

실행:
    python scripts/formal_flow_grandfather.py [--apply] [--db sqlite:///...]
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()

    if args.db:
        os.environ["GLHAC_DB_URL"] = args.db

    from app.db import SessionLocal
    from app.db import engine as _engine
    from app import models, nav8
    models.Base.metadata.create_all(bind=_engine)
    from app import state_machine as sm

    db = SessionLocal()
    try:
        initial_statuses = {
            "onboarding",
            "application_draft",
            "ai_pre_assessment_ready",
            "ai_pre_assessment_running",
            "pathway_determination",
            "supplementation_required",
            "supplementation_submitted",
        }

        cases = db.query(models.CaseApplication).all()

        targets = []
        for c in cases:
            if nav8.formal_state(db, c.case_id) is not None:
                continue
            has_contract = (
                db.query(models.Contract)
                .filter(models.Contract.case_id == c.case_id)
                .first()
                is not None
            )
            if (c.status not in initial_statuses) or has_contract:
                targets.append((c, has_contract))

        print(f"대상 케이스 {len(targets)}건")
        for c, has_contract in targets:
            company = getattr(c, "company_name", None) or getattr(c, "company", None) or "(회사명 없음)"
            print(f"  - {company} | status={c.status} | contract={'있음' if has_contract else '없음'}")

        if not args.apply:
            print("변경 없음 — --apply 로 적용")
            return

        for c, _has_contract in targets:
            if nav8.eligibility_state(db, c.case_id) is None:
                sm.record_event(
                    db,
                    c,
                    c.status,
                    c.status,
                    "eligibility.verdict",
                    "system",
                    None,
                    {
                        "verdict": "eligible",
                        "reason": "",
                        "note": "grandfathered: 흐름 전환 전 진행 중 케이스",
                        "grandfathered": True,
                    },
                )
            sm.record_event(
                db,
                c,
                c.status,
                c.status,
                "formal_application.accepted",
                "system",
                None,
                {
                    "grandfathered": True,
                    "note": "흐름 전환(GLHAC_FORMAL_FLOW) 전 진행 중 케이스 — 접수 확인 면제",
                },
            )

        db.commit()
        print(f"{len(targets)}건 처리 완료")
    finally:
        db.close()


if __name__ == "__main__":
    main()
