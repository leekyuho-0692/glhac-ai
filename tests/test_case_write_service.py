"""2단계: 케이스 쓰기 경계, 실제 WAL 경합, 잠금 순서/재시도, 늦은 검증."""
import ast
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import db as database, intake, main as m, models
from app import case_write_service as service
from app.intake_write_service import aggregate_document_fields, write_document_fields

ROOT = Path(__file__).resolve().parents[1]
USER = {"uid": "admin1", "role": "admin", "org_id": "org_demo"}


@pytest.fixture
def case_doc():
    with m.SessionLocal() as db:
        case = service.new_case(db, source="test", org_id="stage2-" + models.uid(), company_name="Stage two",
                                profile_ext={"city": "Seoul", "product_aliases": {"p": ["Old alias"]}})
        db.add(case)
        db.flush()
        doc = models.DocumentAsset(case_id=case.case_id, filename="case.txt", doc_type="other",
            fields={"address": "Old", "_rejected_identifiers": {"nib": "bad"}}, text_full="Old document",
            content_b64=base64.b64encode(b"Old document").decode())
        db.add(doc)
        db.commit()
        return case.case_id, doc.document_id


@pytest.mark.parametrize("key", ["sector", "sectors", "halal_org", "pending_handled", "product_aliases",
                                 "email_consent", "pic_name", "cp_phone", "city", "zip", "unknown_manual"])
@pytest.mark.parametrize("scenario", ["mine", "latest", "both", "delete", "null"])
def test_decision_and_mixed_key_policy(key, scenario):
    base, latest, mine = {key: "base"}, {key: "base"}, {key: "mine"}
    if scenario in ("latest", "both", "delete", "null"):
        latest[key] = "latest"
    if scenario == "latest":
        mine = deepcopy(base)
    elif scenario == "delete":
        mine = {}
    elif scenario == "null":
        latest[key], mine[key] = None, "mine"
    original = deepcopy((base, latest, mine))
    assert service.merge_profile(base, latest, mine) == (mine if scenario == "mine" else latest)
    assert (base, latest, mine) == original


def test_machine_receipt_is_replaced_atomically_and_disjoint_decisions_merge():
    assert service.merge_profile({"_registration_extraction": {"id": "old"}},
                                 {"_registration_extraction": {"id": "latest", "old_only": True}},
                                 {"_registration_extraction": {"id": "mine"}}) == {
        "_registration_extraction": {"id": "mine"}}
    base = {"product_aliases": {"p": ["Old"], "q": ["Q"]}, "halal_org": {"members": []}}
    latest = {"product_aliases": {"p": ["Human"], "q": ["Q"]}, "halal_org": {"members": ["Kim"]}}
    mine = {"product_aliases": {"p": ["Machine"], "q": ["Q", "New"]},
            "halal_org": {"members": [], "penyelia_quals": {"Kim": {"sk": True}}}}
    assert service.merge_profile(base, latest, mine) == {
        "product_aliases": {"p": ["Human"], "q": ["Q", "New"]},
        "halal_org": {"members": ["Kim"], "penyelia_quals": {"Kim": {"sk": True}}}}
    assert service.merge_profile({"halal_org": {"members": []}}, {},
                                 {"halal_org": {"members": ["Late"]}}) == {}
    assert service.merge_profile({"city": "A"}, {"city": "A"}, {}) == {}
    assert service.merge_profile({"city": "A"}, {"city": "A"}, {"city": None}) == {"city": None}


@pytest.mark.parametrize("by_id", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
def test_stale_case_identity_and_profile_merge_under_lock(case_doc, by_id, autoflush):
    cid, _ = case_doc
    with m.SessionLocal(autoflush=autoflush) as old, m.SessionLocal() as other:
        case = old.get(models.CaseApplication, cid)
        base = service.case_snapshot(case)
        service.write_case(other, cid, base=None, changes=lambda latest: {
            "company_name": "Latest human", "profile_ext": {**latest["profile_ext"],
                "city": "Busan", "product_aliases": {"p": ["Merged by human"]}}}, source="human")
        other.commit()
        case.profile_ext = {"stale": "untrusted"}
        case.draft_state = "saved"
        service.write_case(old, cid if by_id else case, base=base, changes={
            "company_name": "Late machine", "profile_ext": {**base["profile_ext"], "country": "Korea", "city": "Jeju"}},
            source="late")
        assert case.company_name == "Latest human"
        assert case.profile_ext == {"city": "Busan", "country": "Korea", "product_aliases": {"p": ["Merged by human"]}}
        assert case.draft_state == "saved"
        old.commit()


def test_case_write_no_flush_reentry_and_rollback(case_doc):
    cid, _ = case_doc
    with m.SessionLocal(autoflush=True) as db:
        case = db.get(models.CaseApplication, cid)
        original = service.case_snapshot(case)
        def reject_flush(*args):
            pytest.fail("write service flushed partial commands")
        event.listen(db, "before_flush", reject_flush)
        service.write_case(db, case, base=None, changes=lambda latest: {
            "profile_ext": {**latest["profile_ext"], "city": "Busan"}}, source="first")
        service.write_case(db, case, base=None, changes=lambda latest: {
            "profile_ext": {**latest["profile_ext"], "country": "Korea"}}, source="second")
        assert case.profile_ext["city"] == "Busan"
        event.remove(db, "before_flush", reject_flush)
        db.rollback()
        service.write_case(db, case, base=None, changes=lambda latest: {
            "profile_ext": {**latest["profile_ext"], "sector": "food"}}, source="after_rollback")
        assert case.profile_ext == {**original["profile_ext"], "sector": "food"}
        with pytest.raises(ValueError, match="snapshot"):
            service.write_case(db, case, base=None, changes={}, source="bad")


@pytest.mark.parametrize("route", ["profile", "sector", "halal", "dismiss"])
def test_previously_unlocked_routes_keep_latest_profile(case_doc, route):
    cid, did = case_doc
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        if route == "dismiss":
            doc = db.get(models.DocumentAsset, did)
            doc.fields = {"_scope_pending": [{"kind": "product_names", "value": "Dismiss me"}]}
            db.commit()
            _ = case.profile_ext
        with m.SessionLocal() as other:
            service.write_case(other, cid, base=None, changes=lambda latest: {
                "profile_ext": {**latest["profile_ext"], "product_aliases": {"p": ["Latest merged name"]},
                    "pending_handled": ["earlier"], "halal_org": {"penyelia_quals": {"Lee": {"sk": True}}}}}, source="other")
            other.commit()
        if route == "profile":
            m.update_profile(cid, m.schemas.CaseProfileReq(profile_ext={"city": "Busan"}), user=USER, db=db)
        elif route == "sector":
            m.set_case_sector(cid, {"sector": next(iter(intake.SECTORS))}, user=USER, db=db)
        elif route == "halal":
            m.put_halal_org(cid, {"members": [{"name": "Kim"}]}, user=USER, db=db)
        else:
            m.apply_pending_extractions(cid, {"dismiss": ["Dismiss me"], "reason": "duplicate"}, user=USER, db=db)
        db.expire_all()
        assert case.profile_ext["product_aliases"] == {"p": ["Latest merged name"]}
        assert "earlier" in case.profile_ext["pending_handled"]
        assert case.profile_ext["halal_org"]["penyelia_quals"]["Lee"]["sk"] is True
        if route == "dismiss":
            assert m._norm_material("Dismiss me") in case.profile_ext["pending_handled"]


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_case_writers_wait_until_transaction_finishes(tmp_path, monkeypatch, finish):
    engine = create_engine("sqlite:///" + str(tmp_path / "case-race.db"),
                           connect_args={"check_same_thread": False, "timeout": 5})
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    models.CaseApplication.__table__.create(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        case = service.new_case(db, source="setup", org_id="race", profile_ext={"count": 0})
        db.add(case)
        db.commit()
        cid = case.case_id
    staged, release, attempted, entered = (threading.Event() for _ in range(4))
    lock = database.lock_cases
    def observed(db, ids):
        if db.info.get("writer") == "B" and ids:
            attempted.set()
        return lock(db, ids)
    monkeypatch.setattr(database, "lock_cases", observed)
    def writer(name):
        with sessions() as db:
            db.info["writer"] = name
            def increment(latest):
                if name == "B":
                    entered.set()
                return {"profile_ext": {"count": latest["profile_ext"]["count"] + 1}}
            service.write_case(db, cid, base=None, changes=increment, source="race")
            if name == "A":
                staged.set()
                assert release.wait(5)
                getattr(db, finish)()
            else:
                db.commit()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(writer, "A")
            assert staged.wait(5)
            second = pool.submit(writer, "B")
            try:
                assert attempted.wait(5)
                assert not entered.wait(.2), "second writer entered before the first transaction ended"
            finally:
                release.set()
            first.result(timeout=5)
            second.result(timeout=5)
        with sessions() as db:
            assert db.get(models.CaseApplication, cid).profile_ext["count"] == (2 if finish == "commit" else 1)
    finally:
        release.set()
        engine.dispose()


def _record_case_locks(monkeypatch, *, postgres=False):
    records = []
    owner = threading.current_thread()
    raw = database.lock_chain_tail
    def record(db, key):
        # 이전 테스트의 비동기 검증은 현재 명령의 잠금 순서에 섞지 않는다.
        if key.startswith("wf:") and threading.current_thread() is owner:
            records.append((db.get_transaction(), key[3:]))
        if postgres:
            conn = db.connection()
            if not conn.connection.driver_connection.in_transaction:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            return
        return raw(db, key)
    monkeypatch.setattr(database, "lock_chain_tail", record)
    if postgres:
        monkeypatch.setattr(m.engine.dialect, "name", "postgresql")
        monkeypatch.setattr(m, "lock_chain_tail", record)
        monkeypatch.setattr(m.sm, "lock_chain_tail", record)
    return records


def _assert_ordered(records):
    transactions = {tx for tx, _ in records}
    for tx in transactions:
        ids = [cid for transaction, cid in records if transaction is tx]
        assert ids == sorted(set(ids)), ids


def test_case_lock_recorder_isolates_background_workers_without_filtering_case_ids(monkeypatch):
    records = _record_case_locks(monkeypatch, postgres=True)
    def background():
        with m.SessionLocal() as db:
            database.lock_chain_tail(db, "wf:background-case")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(background).result(timeout=5)
    with m.SessionLocal() as db:
        database.lock_chain_tail(db, "wf:target-case")
        database.lock_chain_tail(db, "wf:unexpected-case")
    # 현재 명령이 잘못 잠근 케이스도 빠짐없이 남겨 기존 잠금 순서 검사를 유지한다.
    assert [cid for _, cid in records] == ["target-case", "unexpected-case"]


def test_lock_helper_rejects_unsorted_extension_before_sql(case_doc, monkeypatch):
    cid, _ = case_doc
    with m.SessionLocal() as db:
        db.add(models.CaseApplication(case_id="000-stage2", org_id="ordering"))
        db.commit()
        records = _record_case_locks(monkeypatch)
        monkeypatch.setattr(m.engine.dialect, "name", "postgresql")
        recorded_lock = database.lock_chain_tail
        def emulate(session, key):
            with monkeypatch.context() as patch:
                patch.setattr(m.engine.dialect, "name", "sqlite")
                return recorded_lock(session, key)
        monkeypatch.setattr(database, "lock_chain_tail", emulate)
        database.lock_cases(db, [cid])
        with pytest.raises(HTTPException) as error:
            database.lock_cases(db, ["000-stage2"])
        assert error.value.status_code == 409
        assert [key for _, key in records] == [cid]


def _shared_cases():
    suffix = models.uid()
    org = "shared-" + suffix
    with m.SessionLocal() as db:
        fac = models.Facility(org_id=org, name="Stage two", address="Seoul")
        db.add(fac)
        db.flush()
        a, b = [models.CaseApplication(case_id=letter + suffix, org_id=org, company_name="Stage two",
                    factory_address="Seoul", facility_ids=[fac.facility_id]) for letter in ("a", "b")]
        db.add_all([a, b])
        db.flush()
        doc = models.DocumentAsset(case_id=b.case_id, filename="factory.txt", doc_type="factory_registration",
            text_full="공장등록증명서 Stage two Seoul 전화 0212345678", fields={"company_name": "Stage two",
                "factory_address": "Seoul", "factory_phone": "0212345678"})
        db.add(doc)
        db.commit()
        return a.case_id, b.case_id, fac.facility_id, doc.document_id


@pytest.mark.parametrize("entry", ["profile", "intake", "owned", "reprocess", "verify", "classify"])
def test_shared_facility_entry_locks_full_set_from_first_acquisition(monkeypatch, entry):
    a, b, fid, did = _shared_cases()
    monkeypatch.setattr(intake, "refine_doctype_reason", lambda *args: ("factory_registration", "rule"))
    monkeypatch.setattr(intake, "autofill_value_in_text", lambda *args: True)
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *args: None)
    records = _record_case_locks(monkeypatch, postgres=True)
    with m.SessionLocal() as db:
        case, doc = db.get(models.CaseApplication, b), db.get(models.DocumentAsset, did)
        if entry == "profile":
            m.autofill_profile(b, user=USER, db=db)
        elif entry == "intake":
            parsed = {"filename": doc.filename, "doc_type": doc.doc_type, "text_full": doc.text_full,
                      "fields": deepcopy(doc.fields)}
            m._apply_intake_autofill(db, case, {"classified": [parsed], "extracted": {}}, {did: deepcopy(doc.fields)})
            db.commit()
        elif entry == "owned":
            m._apply_owned_autofill(db, case, doc)
            db.commit()
        elif entry == "reprocess":
            doc.content_b64 = base64.b64encode(doc.text_full.encode()).decode()
            db.commit()
            text, fields = doc.text_full, deepcopy(doc.fields)
            monkeypatch.setattr(intake, "parse_file", lambda *a, **k: text)
            monkeypatch.setattr(intake, "classify", lambda *a, **k: {"doc_type": "factory_registration", "fields": fields})
            monkeypatch.setattr(intake, "parse_typed_text", lambda *a, **k: {"doc_type": "factory_registration", "fields": fields})
            m._reprocess_doc(db, doc, case)
        elif entry == "classify":
            m.classify_factories(b, user=USER, db=db)
        else:
            fac = db.get(models.Facility, fid)
            fac.profile_ext = {"phone": "0212345678"}
            doc.fields = {**doc.fields, "_autofill": {"changes": {"facility_profile.phone": {
                "scope": "facility_profile", "field": "phone", "facility_id": fid,
                "before": None, "present": False, "value": "0212345678", "status": "applied"}}}}
            db.commit()
            records.clear()
            monkeypatch.setattr(intake, "verify_extraction", lambda *a: {"verdict": "fail", "field_issues": []})
            m._verify_doc(db, doc, case)
        profile = db.get(models.Facility, fid).profile_ext or {}
        assert profile.get("phone") == (None if entry in ("verify", "classify") else "0212345678")
    assert [cid for _, cid in records][:2] == [a, b]
    _assert_ordered(records)


@pytest.mark.parametrize("selected", ["Busan", "OLD"])
def test_candidate_regeneration_expands_set_by_rollback_then_sorted_retry(monkeypatch, selected):
    a, b, fid, did = _shared_cases()
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        doc.fields = {"_autofill": {"conflicts": [{"scope": "case", "field": "factory_reg_no",
            "current": None, "proposed": "OLD", "status": "pending"}]}}
        db.commit()
    records, changed = [], False
    def regenerate(db, key):
        nonlocal changed
        if key == "wf:" + b and not changed:
            changed = True
            with m.SessionLocal() as other:
                doc = other.get(models.DocumentAsset, did)
                doc.fields = {"_autofill": {"conflicts": [{"scope": "facility", "field": "address",
                    "facility_id": fid, "current": "Seoul", "proposed": "Busan", "status": "pending"}]}}
                other.commit()
        # 이전 테스트의 검증 워커가 전역 패치에 진입해도 이 케이스들의 잠금만 기록한다.
        if key.startswith("wf:") and key[3:] in (a, b):
            records.append((db.get_transaction(), key[3:]))
        conn = db.connection()
        if not conn.connection.driver_connection.in_transaction:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    monkeypatch.setattr(m.engine.dialect, "name", "postgresql")
    monkeypatch.setattr(database, "lock_chain_tail", regenerate)
    monkeypatch.setattr(m, "lock_chain_tail", regenerate)
    monkeypatch.setattr(m.sm, "lock_chain_tail", regenerate)
    with m.SessionLocal() as db:
        def apply():
            return m.apply_pending_extractions(b, {"autofill_choices": [{"conflict_id": did + ":0", "value": selected}]},
                user={**USER, "role": "consultant", "org_id": db.get(models.CaseApplication, b).org_id}, db=db)
        if selected == "OLD":
            with pytest.raises(HTTPException) as error:
                apply()
            assert error.value.status_code == 422
            db.rollback()
        else:
            assert apply()["autofill_confirmed"][0]["scope"] == "facility"
        assert db.get(models.Facility, fid).address == ("Seoul" if selected == "OLD" else "Busan")
    assert [cid for _, cid in records] == [b, a, b]
    assert records[0][0] is not records[1][0]
    _assert_ordered(records)


def test_changing_lock_set_stops_at_three_attempts(case_doc, monkeypatch):
    cid, _ = case_doc
    attempts, resolutions, rollbacks = [], [], []
    def resolve():
        resolutions.append(True)
        return {cid, *["new-" + str(n) for n in range(len(resolutions))]}
    with m.SessionLocal() as db:
        original = database.lock_cases
        def lock(db, ids):
            attempts.append(set(ids))
            return original(db, ids)
        monkeypatch.setattr(database, "lock_cases", lock)
        event.listen(db, "after_rollback", lambda *args: rollbacks.append(True))
        with pytest.raises(HTTPException) as error:
            database.lock_case_set(db, resolve)
        assert error.value.status_code == 409
        assert len(attempts) == len(rollbacks) == 3
        assert not db.in_transaction()


@pytest.mark.parametrize("change", ["value", "delete", "rejected", "type", "text", "filename"])
def test_late_verification_is_discarded_if_machine_input_changed(case_doc, monkeypatch, change):
    cid, did = case_doc
    def verify(*args):
        with m.SessionLocal() as other:
            doc = other.get(models.DocumentAsset, did)
            fields = deepcopy(doc.fields)
            if change == "value":
                fields["address"] = "New"
            elif change == "delete":
                fields.pop("address")
            elif change == "rejected":
                fields["_rejected_identifiers"] = {"nib": "new rejection"}
            elif change == "type":
                doc.doc_type = "nib_business_license"
            elif change == "text":
                doc.text_full = "New text"
            else:
                doc.filename = "new.txt"
            fields["_verify"] = {"verdict": "ok", "summary": "latest generation"}
            doc.fields = fields
            other.commit()
        return {"verdict": "fail", "field_issues": [], "summary": "stale failure", "_ai_budget": {"late": True}}
    monkeypatch.setattr(intake, "verify_extraction", verify)
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        m._verify_doc(db, doc, db.get(models.CaseApplication, cid))
        db.expire_all()
        assert doc.fields["_verify"] == {"verdict": "ok", "summary": "latest generation"}
        assert "_ai_budget" not in doc.fields


def test_verification_still_applies_after_only_human_metadata_change(case_doc, monkeypatch):
    cid, did = case_doc
    def verify(*args):
        with m.SessionLocal() as other:
            doc = other.get(models.DocumentAsset, did)
            doc.fields = {**doc.fields, "_counts_as_evidence": False}
            other.commit()
        return {"verdict": "ok", "field_issues": [], "summary": "same inputs"}
    monkeypatch.setattr(intake, "verify_extraction", verify)
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        m._verify_doc(db, doc, db.get(models.CaseApplication, cid))
        assert doc.fields["_verify"]["summary"] == "same inputs"
        assert doc.fields["_counts_as_evidence"] is False


def test_zip_checklist_uses_merged_empty_document(case_doc, monkeypatch):
    cid, did = case_doc
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *a: None)
    with m.SessionLocal() as db:
        case, doc = db.get(models.CaseApplication, cid), db.get(models.DocumentAsset, did)
        doc.doc_type, doc.fields = "product_list", {"product_names": ["Old"]}
        doc.file_hash = m._sha256_b64(doc.content_b64)
        db.commit()
        base = {did: deepcopy(doc.fields)}
        classified = [{"filename": doc.filename, "doc_type": doc.doc_type, "fields": deepcopy(doc.fields),
                       "text_full": doc.text_full, "content_b64": doc.content_b64}]
        result = {"classified": classified, "extracted": {}, "checklist": [], "missing": [], "complete": True}
        with m.SessionLocal() as other:
            current = other.get(models.DocumentAsset, did)
            current.fields = {"_empty": True}
            other.commit()
        m._apply_intake_autofill(db, case, result, base)
        assert classified[0]["fields"]["_empty"] is True
        row = next(row for row in result["checklist"] if row["doc_type"] == "product_list")
        assert row["satisfied"] is False
        assert row["doc_type_ko"] in result["missing"]
        assert result["complete"] is False


@pytest.mark.parametrize("changed", [False, True])
def test_prelock_zip_lookup_is_reused_only_for_the_same_merged_address(case_doc, monkeypatch, changed):
    cid, did = case_doc
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", True)
    monkeypatch.setattr(intake, "_lookup_zip", lambda address: "12345" if "Seoul" in address else "67890")
    with m.SessionLocal() as db:
        doc, case = db.get(models.DocumentAsset, did), db.get(models.CaseApplication, cid)
        doc.doc_type = "nib_business_license"
        doc.fields = {"address": "Seoul", "factory_address": "Busan"}
        doc.file_hash = m._sha256_b64(doc.content_b64)
        db.commit()
        base = {did: deepcopy(doc.fields)}
        row = {"filename": doc.filename, "doc_type": doc.doc_type, "fields": deepcopy(doc.fields),
               "text_full": doc.text_full, "content_b64": doc.content_b64}
        result = {"classified": [row], "extracted": intake.aggregate_fields([row])}
        assert result["extracted"]["zip"] == "12345"
        assert result["extracted"]["factory_zip"] == "67890"
        if changed:
            with m.SessionLocal() as other:
                current = other.get(models.DocumentAsset, did)
                current.fields = {"address": "New office", "factory_address": "New factory"}
                other.commit()
        monkeypatch.setattr(intake, "_lookup_zip", lambda *a: pytest.fail("lookup repeated under lock"))
        m._apply_intake_autofill(db, case, result, base)
        assert result["extracted"]["zip"] == (None if changed else "12345")
        assert result["extracted"]["factory_zip"] == (None if changed else "67890")


# Org/Facility는 이번 단계 대상이 아니다. 수신 객체와 함수까지 고정하고,
# 공용 obj는 Case 분기를 빠져나온 else/시설 scope 안에서만 예외로 허용한다.
_NON_CASE_RECEIVERS = {
    "_set_tenant": {"o"}, "update_profile": {"org_row"}, "_apply_owned_autofill": {"fac"},
    "_set_factory_fields": {"f"}, "classify_factories": {"f"},
    "_send_notice": {"invoice"},  # 청구서 납부기한은 케이스 식별 컬럼이 아니다.
}

_CASE_WRITE_ALLOWLIST = {
    "app/case_write_service.py": "케이스 프로필·식별 컬럼의 유일한 쓰기 서비스",
    "scripts/encrypt_backfill.py": "값 보존 암호화 백필 유지보수: enc/dec 왕복 검증과 원시값 비교 후 갱신",
}


def _case_write_violations(tree, *, main=False):
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    def context(node):
        chain = []
        while node in parents:
            node = parents[node]
            chain.append(node)
        function = next((n.name for n in chain if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        return function, chain
    def non_case(node, receiver):
        if not main or not isinstance(receiver, ast.Name):
            return False
        function, chain = context(node)
        if receiver.id in _NON_CASE_RECEIVERS.get(function, set()):
            return True
        if receiver.id == "obj":
            for parent in chain:
                if not isinstance(parent, ast.If):
                    continue
                test = ast.unparse(parent.test)
                if function == "_revert_failed_autofill" and test == "isinstance(obj, models.CaseApplication)":
                    return any(node is child for stmt in parent.orelse for child in ast.walk(stmt))
                if function == "_confirm_autofill" and test == "scope == 'facility_profile'":
                    return True
        return False
    class Writes(ast.NodeVisitor):
        def __init__(self):
            self.aliases, self.lines = set(), set()
        def target(self, node):
            if isinstance(node, ast.Name):
                return node.id in self.aliases
            if isinstance(node, ast.Attribute):
                return node.attr == "profile_ext" and not non_case(node, node.value)
            if isinstance(node, ast.Subscript):
                return self.target(node.value)
            if isinstance(node, ast.BoolOp):
                return any(self.target(value) for value in node.values)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                return node.func.attr in ("get", "setdefault") and self.target(node.func.value)
            return False
        def visit_FunctionDef(self, node):
            outer, self.aliases = self.aliases, set()
            self.generic_visit(node)
            self.aliases = outer
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_Assign(self, node):
            self.generic_visit(node)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if self.target(node.value):
                        self.aliases.add(target.id)
                    else:
                        self.aliases.discard(target.id)
        def visit_AnnAssign(self, node):
            self.generic_visit(node)
            if isinstance(node.target, ast.Name) and self.target(node.value):
                self.aliases.add(node.target.id)
        def visit_AugAssign(self, node):
            if self.target(node.target):
                self.lines.add(node.lineno)
            self.generic_visit(node)
        def visit_Attribute(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                if self.target(node):
                    self.lines.add(node.lineno)
                elif (isinstance(node, ast.Attribute) and node.attr in service.CASE_COLUMNS
                      and not non_case(node, node.value)):
                    self.lines.add(node.lineno)
            self.generic_visit(node)
        visit_Subscript = visit_Attribute
        def visit_Call(self, node):
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name == "CaseApplication" and any(kw.arg in service.CASE_COLUMNS | {"profile_ext", None}
                                                  for kw in node.keywords):
                self.lines.add(node.lineno)
            if name in ("setattr", "flag_modified") and len(node.args) > 1:
                key = node.args[1]
                if (isinstance(key, ast.Constant) and key.value in service.CASE_COLUMNS | {"profile_ext"}
                        and not non_case(node, node.args[0])):
                    self.lines.add(node.lineno)
                if isinstance(node.args[0], ast.Name) and node.args[0].id in ("case", "c", "new_c", "src"):
                    if (isinstance(key, ast.Constant) and key.value in service.CASE_COLUMNS or
                        isinstance(key, ast.Name) and key.id != "_col"):
                        self.lines.add(node.lineno)
            if isinstance(node.func, ast.Attribute):
                if self.target(node.func.value) and name in (
                        "update", "pop", "popitem", "clear", "setdefault", "__setitem__", "__delitem__",
                        "append", "extend", "insert", "remove", "sort", "reverse"):
                    self.lines.add(node.lineno)
            self.generic_visit(node)
    visitor = Writes()
    visitor.visit(tree)
    from scripts.write_guard import bulk_write_violations
    return sorted(visitor.lines | set(bulk_write_violations(
        tree, "CaseApplication", service.CASE_COLUMNS | {"profile_ext"})))


def _audit_case_sources(root):
    violations = []
    for directory in ("app", "scripts"):
        for path in (root / directory).rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel in _CASE_WRITE_ALLOWLIST:
                continue
            tree = ast.parse(path.read_text())
            violations.extend((rel, line) for line in _case_write_violations(tree, main=rel == "app/main.py"))
    return violations


def test_all_case_profile_and_identity_writes_use_service():
    assert _audit_case_sources(ROOT) == []


@pytest.mark.parametrize("statement", [
    "case.profile_ext = {}", "obj.profile_ext['x'] = 1", "c.profile_ext['a']['b'] = 1",
    "obj.profile_ext |= {}", "setattr(obj, 'profile_ext', {})", "flag_modified(obj, 'profile_ext')",
    "c.profile_ext.update(x=1)", "f = c.profile_ext or {}; alias = f; alias.pop('x')",
    "f: dict = c.profile_ext; f |= {'city': 'Seoul'}", "del c.profile_ext['x']",
    "c.profile_ext.get('halal_org', {}).update(members=[])",
    "models.CaseApplication(org_id='x', profile_ext={})", "CaseApplication(company_name='x')",
    "c.company_name = 'x'", "setattr(c, 'nib', 'x')", "setattr(c, key, value)",
    "db.query(CaseApplication).update({'profile_ext': {}})",
    "update(CaseApplication).values(profile_ext={})", "query.update({CaseApplication.profile_ext: {}})",
])
def test_static_case_guard_catches_direct_writes(statement):
    assert _case_write_violations(ast.parse(statement))


def test_static_case_guard_includes_scripts_and_allows_detached_copies(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "repair.py").write_text("case.profile_ext = {}\n")
    assert _audit_case_sources(tmp_path) == [("scripts/repair.py", 1)]
    assert not _case_write_violations(ast.parse("p = deepcopy(c.profile_ext); p['city'] = 'Seoul'"))


def test_case_workflow_locks_cannot_bypass_lock_cases():
    violations = []
    for directory in ("app", "scripts"):
        for path in (ROOT / directory).rglob("*.py"):
            if path == ROOT / "app/db.py":
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", getattr(node.func, "attr", None))
                if name == "lock_chain_tail" and len(node.args) > 1:
                    if any(isinstance(part, ast.Constant) and isinstance(part.value, str) and part.value.startswith("wf:")
                           for part in ast.walk(node.args[1])):
                        violations.append((str(path.relative_to(ROOT)), node.lineno))
    assert violations == []


@pytest.mark.parametrize("route", ["profile", "reprocess", "intake"])
def test_external_analysis_cannot_overwrite_a_new_human_identity(case_doc, monkeypatch, route):
    cid, did = case_doc
    nib = "1" + str(int(cid, 16))[-12:]
    text = f"사업자등록증 Stage two NIB {nib} Address Machine address"
    extracted = {"company_name": "Stage two", "nib": nib, "address": "Machine address"}
    parsed = {"doc_type": "nib_business_license", "confidence": .99, "fields": extracted,
              "text_full": text, "filename": "case.txt", "content_b64": base64.b64encode(text.encode()).decode()}
    monkeypatch.setattr(intake, "refine_doctype_reason", lambda *a: ("nib_business_license", "rule"))
    monkeypatch.setattr(intake, "autofill_value_in_text", lambda *a: True)
    monkeypatch.setattr(intake, "classify", lambda *a: deepcopy(parsed))
    monkeypatch.setattr(intake, "parse_typed_text", lambda *a: deepcopy(parsed))
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *a: None)
    def concurrent_human(*args, **kwargs):
        with m.SessionLocal() as other:
            service.write_case(other, cid, base=None, changes=lambda latest: {
                "address": "Human address", "profile_ext": {**latest["profile_ext"], "city": "Human city"}}, source="human")
            other.commit()
        return text
    monkeypatch.setattr(intake, "parse_file", concurrent_human)
    with m.SessionLocal() as db:
        case, doc = db.get(models.CaseApplication, cid), db.get(models.DocumentAsset, did)
        service.write_case(db, case, base=None, changes=lambda latest: {"nib": nib}, source="setup")
        doc.fields, doc.doc_type = {}, "nib_business_license"
        doc.file_hash = m._sha256_b64(parsed["content_b64"])
        db.commit()
        before = service.case_snapshot(case)
        if route == "profile":
            m.autofill_profile(cid, user=USER, db=db)
        elif route == "reprocess":
            m._reprocess_doc(db, doc, case)
        else:
            concurrent_human()
            m._apply_intake_autofill(db, case, {"classified": [deepcopy(parsed)], "extracted": {}}, {did: {}}, before)
            db.commit()
        db.expire_all()
        assert case.address == "Human address"
        assert case.profile_ext["city"] == "Human city"
        conflicts = doc.fields["_autofill"]["conflicts"]
        assert any(item["field"] == "address" and item["status"] == "pending" for item in conflicts)


def test_savepoint_rollback_reacquires_only_released_case_locks(monkeypatch):
    a, b, _, _ = _shared_cases()
    records = _record_case_locks(monkeypatch, postgres=True)
    with m.SessionLocal() as db:
        database.lock_cases(db, [a])
        nested = db.begin_nested()
        database.lock_cases(db, [b])
        nested.rollback()
        assert db.info["case_locks"][1] == {a}
        database.lock_cases(db, [a, b])
        assert [cid for _, cid in records] == [a, b, b]
        db.rollback()
        assert "case_locks" not in db.info
        assert "case_lock_owners" not in db.info


def test_expanding_set_rolls_back_partial_work_instead_of_replaying_it(case_doc):
    cid, _ = case_doc
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        calls = []
        def resolve():
            calls.append(True)
            if len(calls) == 2:
                case.draft_state = "must roll back"
                return {cid, "additional-case"}
            return {cid}
        with pytest.raises(HTTPException) as error:
            database.lock_case_set(db, resolve)
        assert error.value.status_code == 409
        assert not db.in_transaction()
        assert db.get(models.CaseApplication, cid).draft_state is None


@pytest.mark.parametrize("expansion_at", [3, 4])
def test_upload_flush_then_lock_set_expansion_aborts_atomically(case_doc, monkeypatch, expansion_at):
    cid, existing_doc = case_doc
    nib = "1234567890123"
    parsed = {"doc_type": "nib_business_license", "fields": {"nib": nib, "company_name": "Stage two"},
              "text": "사업자등록증 Stage two NIB " + nib, "confidence": .99}
    parses, queued, resolutions = [], [], []
    def parse(*args):
        parses.append(True)
        return deepcopy(parsed)
    monkeypatch.setattr(intake, "parse_typed", parse)
    monkeypatch.setattr(intake, "refine_doctype_reason", lambda *a: ("nib_business_license", "rule"))
    monkeypatch.setattr(m, "autoparse_enqueue", lambda did, **kw: queued.append(("parse", did)))
    monkeypatch.setattr(m, "verify_enqueue", lambda did: queued.append(("verify", did)))
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *a: None)
    _record_case_locks(monkeypatch, postgres=True)
    def resolve(db, org_id):
        resolutions.append(True)
        if len(resolutions) >= 3:
            assert db.info["transaction_write_started"]
            assert not (db.new or db.dirty or db.deleted)  # 기존 판정이 놓친 flush 후 상태
        return {cid, "000-new-case"} if len(resolutions) >= expansion_at else {cid}
    monkeypatch.setattr(m, "_autofill_org_case_ids", resolve)
    with m.SessionLocal() as db:
        with pytest.raises(HTTPException) as error:
            m.parse_file_ep(cid, m.schemas.ParseFileReq(filename="new.txt", doc_type="nib_business_license",
                file_b64=base64.b64encode(parsed["text"].encode()).decode()), user=USER, db=db)
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "CONCURRENT_CHANGE_RETRY"
        assert not db.in_transaction()
        assert "transaction_write_started" not in db.info
    with m.SessionLocal() as db:
        assert [d.document_id for d in db.query(models.DocumentAsset).filter_by(case_id=cid)] == [existing_doc]
        assert db.query(models.WorkflowEvent).filter_by(case_id=cid).count() == 0
        assert db.query(models.AiExtraction).filter_by(case_id=cid).count() == 0
    assert len(parses) == 1
    assert queued == []


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_flush_history_lasts_until_root_transaction_ends(case_doc, finish):
    cid, _ = case_doc
    with m.SessionLocal() as db:
        service.write_case(db, cid, base=None, changes=lambda latest: {"address": "Written"}, source="test")
        db.flush()
        assert not (db.new or db.dirty or db.deleted)
        assert db.info["transaction_write_started"]
        savepoint = db.begin_nested()
        savepoint.rollback()
        assert db.info["transaction_write_started"]
        getattr(db, finish)()
        assert "transaction_write_started" not in db.info
        database.lock_case_set(db, lambda: {cid})
        database.lock_case_set(db, lambda: {cid, "000-next-transaction"})
        assert "transaction_write_started" not in db.info


@pytest.mark.parametrize("entry", ["profile", "owned"])
def test_autofill_reloads_deleted_document_by_saved_id_after_retry(case_doc, monkeypatch, entry):
    cid, did = case_doc
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        doc.doc_type, doc.fields = "nib_business_license", {"address": "Office A"}
        db.commit()
    records = _record_case_locks(monkeypatch, postgres=True)
    original = database.lock_chain_tail
    deleted = False
    def lock(db, key):
        nonlocal deleted
        if key == "wf:" + cid and not deleted:
            with m.SessionLocal() as other:
                other.delete(other.get(models.DocumentAsset, did))
                other.commit()
            deleted = True
        return original(db, key)
    monkeypatch.setattr(database, "lock_chain_tail", lock)
    monkeypatch.setattr(m, "_autofill_org_case_ids",
                        lambda db, org: {cid, "000-added-case"} if deleted else {cid})
    with m.SessionLocal() as db:
        case, doc = db.get(models.CaseApplication, cid), db.get(models.DocumentAsset, did)
        if entry == "profile":
            assert m.autofill_profile(cid, user=USER, db=db)["applied"] == []
        else:
            assert m._apply_owned_autofill(db, case, doc) == {"skipped": "document_missing"}
        assert db.get(models.DocumentAsset, did, populate_existing=True) is None
    assert deleted
    assert len({tx for tx, _ in records}) == 2


@pytest.mark.parametrize("entry", ["profile", "intake"])
def test_two_registration_documents_advance_same_request_base(case_doc, monkeypatch, entry):
    cid, did = case_doc
    nib = "1" + str(int(cid, 16))[-12:]
    monkeypatch.setattr(intake, "refine_doctype_reason", lambda *a: ("nib_business_license", "rule"))
    monkeypatch.setattr(intake, "autofill_value_in_text", lambda *a: True)
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *a: None)
    rows = [{"filename": "office-" + letter + ".txt", "doc_type": "nib_business_license",
             "fields": {"company_name": "Stage two", "nib": nib, "address": "Office " + letter},
             "text_full": "사업자등록증 Stage two NIB " + nib + " Office " + letter} for letter in ("A", "B")]
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        service.write_case(db, case, base=None, changes=lambda latest: {"nib": nib}, source="setup")
        db.delete(db.get(models.DocumentAsset, did))
        if entry == "profile":
            db.add_all(models.DocumentAsset(case_id=cid, **row) for row in rows)
        db.commit()
        base = service.case_snapshot(case)
        if entry == "profile":
            m.autofill_profile(cid, user=USER, db=db)
        else:
            m._apply_intake_autofill(db, case, {"classified": deepcopy(rows), "extracted": {}}, {}, base)
            db.commit()
        assert base["address"] is None
        db.expire_all()
        assert case.address == "Office B"
        docs = db.query(models.DocumentAsset).filter_by(case_id=cid).order_by(models.DocumentAsset.filename).all()
        assert [d.fields["_autofill"]["applied"]["address"] for d in docs] == ["Office A", "Office B"]
        assert all(not d.fields["_autofill"]["conflicts"] for d in docs)


def test_base_advances_only_own_changes_and_savepoint_rollback_restores_it(case_doc):
    cid, _ = case_doc
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, cid)
        base = service.case_snapshot(case)
        with m.SessionLocal() as other:
            service.write_case(other, cid, base=None, changes=lambda latest: {"profile_ext": {
                **latest["profile_ext"], "city": "Human city"}}, source="human")
            other.commit()
        service.write_case(db, case, base=base, changes={"address": "Office A", "profile_ext": {
            **base["profile_ext"], "city": "Machine city", "country": "A"}}, source="first")
        advanced = service.case_write_base(db, cid, base)
        assert advanced["address"] == "Office A"
        assert advanced["profile_ext"]["city"] == "Seoul"
        assert advanced["profile_ext"]["country"] == "A"
        savepoint = db.begin_nested()
        service.write_case(db, case, base=base, changes={"address": "Office B", "profile_ext": {
            **base["profile_ext"], "city": "Late city", "country": "B"}}, source="second")
        assert case.address == "Office B"
        assert case.profile_ext["city"] == "Human city"
        assert case.profile_ext["country"] == "B"
        db.flush()
        savepoint.rollback()
        assert service.case_write_base(db, cid, base) == advanced
        db.rollback()
        assert service.case_write_base(db, cid, base) == base


@pytest.mark.parametrize("change", ["company_name", "sources", "autofill", "budget"])
def test_verification_compares_all_inputs_and_ignores_sources(case_doc, monkeypatch, change):
    cid, did = case_doc
    def verify(doc_type, filename, text, fields, company):
        assert company == "Stage two"
        with m.SessionLocal() as other:
            if change == "company_name":
                service.write_case(other, cid, base=None, changes=lambda latest: {
                    "company_name": "Different applicant"}, source="human")
            else:
                current = other.get(models.DocumentAsset, did)
                extra = {"sources": {"_sources": {"address": "manual"}},
                         "autofill": {"_autofill": {"changes": {"address": {
                             "field": "address", "value": "Different", "status": "applied", "scope": "case"}}}},
                         "budget": {"_ai_budget": {"used": 5}}}[change]
                current.fields = {**current.fields, **extra}
            other.commit()
        return {"verdict": "ok", "summary": "old applicant result", "_ai_budget": {"stale": True}}
    monkeypatch.setattr(intake, "verify_extraction", verify)
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, did)
        m._verify_doc(db, doc, db.get(models.CaseApplication, cid))
        db.expire_all()
        if change == "sources":
            assert doc.fields["_verify"]["verdict"] == "ok"
            assert doc.fields["_sources"] == {"address": "manual"}
        else:
            assert "_verify" not in doc.fields
            assert not (doc.fields.get("_ai_budget") or {}).get("stale")


@pytest.mark.parametrize("statement", [
    "application.nib = '123'", "alias = application; alias.company_name = 'x'",
    "application.address += 'x'", "del application.phone", "setattr(application, 'nib', '123')",
    "query.update({'company_name': 'x'})", "alias = query; alias.update({CaseApplication.nib: '123'})",
    "update(CaseApplication).values(nib='123')", "update(models.CaseApplication).values({'address': 'x'})",
    "CaseApplication(**values)", "models.CaseApplication(org_id='x', **values)",
])
def test_static_case_guard_catches_identity_aliases_bulk_and_dynamic_initializers(statement):
    assert _case_write_violations(ast.parse("query = db.query(CaseApplication)\n" + statement))


def test_encryption_backfill_has_explicit_value_preserving_maintenance_exception(tmp_path):
    reason = _CASE_WRITE_ALLOWLIST["scripts/encrypt_backfill.py"]
    assert "값 보존 암호화 백필" in reason
    (tmp_path / "scripts").mkdir()
    for name in ("encrypt_backfill.py", "ordinary_backfill.py"):
        (tmp_path / "scripts" / name).write_text("application.nib = 'new'\n")
    assert _audit_case_sources(tmp_path) == [("scripts/ordinary_backfill.py", 1)]


def test_sqlite_autofill_lock_select_count_is_constant_for_1000_cases(monkeypatch, record_property):
    org_id = "large-" + models.uid()
    with m.SessionLocal() as db:
        db.add_all(models.CaseApplication(case_id=f"{org_id}-{n:04}", org_id=org_id) for n in range(1000))
        db.commit()
    statements, locks = [], []
    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    original = database.lock_chain_tail
    def lock(db, key):
        locks.append(key)
        return original(db, key)
    monkeypatch.setattr(database, "lock_chain_tail", lock)
    monkeypatch.setattr(m, "_autofill_org_case_ids", lambda *a: pytest.fail("SQLite traversed organization cases"))
    event.listen(m.engine, "before_cursor_execute", count)
    try:
        with m.SessionLocal() as db:
            m._lock_autofill_cases(db, org_id + "-0999")
            m._lock_autofill_cases(db, org_id + "-0000")
            m._lock_autofill_org_cases(db, org_id)
            assert len(locks) == 1
            assert db.connection().connection.driver_connection.in_transaction
            selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
            record_property("sqlite_autofill_select_count", len(selects))
            assert len(selects) <= 10, len(selects)
            assert not any("FOR UPDATE" in sql.upper() for sql in statements)
            assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == 1
            db.rollback()
            m._lock_autofill_cases(db, org_id + "-0999")
            assert len(locks) == 2
    finally:
        event.remove(m.engine, "before_cursor_execute", count)


@pytest.mark.parametrize("parent_prefix,child_prefix", [("z-parent", "a-child"), ("a-parent", "z-child")])
@pytest.mark.parametrize("entry", ["close", "revoke", "approve_revoke", "revoke_other_cert", "revoke_closed"])
def test_lineage_is_locked_before_first_mutation_and_reused(monkeypatch, parent_prefix, child_prefix, entry):
    suffix = "-" + models.uid()
    parent, child, grandchild, sibling = [prefix + suffix for prefix in
        (parent_prefix, child_prefix, "0-grandchild", "m-sibling")]
    ids = {parent, child, grandchild, sibling}
    parent_status = "cancelled" if entry == "revoke_closed" else (
        "application_draft" if entry == "close" else "certificate_issued")
    with m.SessionLocal() as db:
        db.add_all(models.CaseApplication(case_id=cid, org_id="lineage" + suffix,
            status=parent_status if cid == parent else "onboarding") for cid in ids)
        db.flush()
        for cid, pid in ((child, parent), (grandchild, child), (sibling, parent)):
            m.sm.record_event(db, db.get(models.CaseApplication, cid), "onboarding", "onboarding",
                              "case.renew", payload={"parent_case_id": pid})
        if entry != "close":
            db.add(models.HalalCertificate(case_id=parent, certificate_no="cert" + suffix, status="active"))
            if entry == "revoke_other_cert":
                db.add(models.HalalCertificate(case_id=parent, certificate_no="other" + suffix, status="suspended"))
        approval = models.ApprovalRequest(case_id=parent, action_type="certificate.revoke",
            requested_by="operator1", requester_role="operator", payload={"reason": "test"})
        db.add(approval)
        db.commit()
        approval_id = approval.id
    monkeypatch.setattr(m, "_notify", lambda *a, **kw: None)
    records = _record_case_locks(monkeypatch, postgres=True)
    mutations, resolutions = [], []
    children = m._renewal_children
    def resolve(db, cid):
        resolutions.append(cid)
        return children(db, cid)
    monkeypatch.setattr(m, "_renewal_children", resolve)
    def before_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("UPDATE ", "INSERT ", "DELETE ")):
            assert [cid for _, cid in records] == sorted(ids)
            mutations.append(statement)
    event.listen(m.engine, "before_cursor_execute", before_write)
    try:
        with m.SessionLocal() as db:
            case = db.get(models.CaseApplication, parent)
            if entry == "close":
                m._close_case(db, case, USER, "cancelled", "test")
                db.commit()
            elif entry == "approve_revoke":
                result = m.approve_request(approval_id, m.schemas.ApprovalDecisionReq(), user=USER, db=db)
                assert result["status"] == "approved"
            else:
                m._do_revoke_certificate(db, case, USER, "test")
            expected_parent = ("cancelled" if entry in ("close", "revoke_closed") else
                               "certificate_issued" if entry == "revoke_other_cert" else "revoked")
            db.expire_all()
            assert db.get(models.CaseApplication, parent).status == expected_parent
            assert all(db.get(models.CaseApplication, cid).status == "cancelled" for cid in ids - {parent})
            if entry != "close":
                assert db.query(models.HalalCertificate).filter_by(case_id=parent, status="withdrawn").count() == 1
            assert "case_close_lineage" not in db.info
        assert mutations
        assert [cid for _, cid in records] == sorted(ids)
        _assert_ordered(records)
        # 집합 확정 두 번 + 각 종결의 자식 순회만 한다. 재귀마다 집합을 재확보하지 않는다.
        assert len(resolutions) <= 3 * len(ids) + 1
    finally:
        event.remove(m.engine, "before_cursor_execute", before_write)


@pytest.mark.parametrize("storage", ["shared_memory", "wal"])
def test_savepoint_commit_then_two_case_writers_keep_both_increments(tmp_path, monkeypatch, storage):
    url = ("sqlite:///file:case-savepoint-" + models.uid() + "?mode=memory&cache=shared&uri=true"
           if storage == "shared_memory" else "sqlite:///" + str(tmp_path / "savepoint.db"))
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 5})
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    models.CaseApplication.__table__.create(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        case = service.new_case(db, source="setup", org_id="savepoint", profile_ext={"count": 0})
        db.add(case)
        db.commit()
        cid = case.case_id
    staged, attempted, entered, release, committed = (threading.Event() for _ in range(5))
    original = database.lock_cases
    def observed(db, ids):
        if db.info.get("writer") == "B" and ids:
            attempted.set()
        return original(db, ids)
    monkeypatch.setattr(database, "lock_cases", observed)
    def increment(latest):
        return {"profile_ext": {"count": latest["profile_ext"]["count"] + 1}}
    def first_writer():
        with sessions() as db:
            savepoint = db.begin_nested()
            db.get(models.CaseApplication, cid)
            database.lock_cases(db, [cid])
            assert "sqlite_case_writer" not in db.info
            savepoint.commit()
            assert not db.connection().connection.driver_connection.in_transaction
            service.write_case(db, cid, base=None, changes=increment, source="A")
            assert db.info["sqlite_case_writer"][0] is db.get_transaction()
            staged.set()
            assert release.wait(5)
            db.commit()
            committed.set()
    def second_writer():
        with sessions() as db:
            db.info["writer"] = "B"
            def change(latest):
                entered.set()
                return increment(latest)
            try:
                service.write_case(db, cid, base=None, changes=change, source="B")
            except HTTPException as exc:
                # 공유 메모리 SQLITE_LOCKED는 busy_timeout 없이 반환하므로
                # 서비스의 409 계약대로 첫 커밋을 기다린 뒤 같은 명령을 재시도한다.
                assert storage == "shared_memory" and exc.status_code == 409
                assert committed.wait(5)
                service.write_case(db, cid, base=None, changes=change, source="B-retry")
            db.commit()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(first_writer)
            try:
                assert staged.wait(5)
                second = pool.submit(second_writer)
                assert attempted.wait(5)
                assert not entered.wait(.2)
            finally:
                release.set()
            first.result(timeout=5)
            second.result(timeout=5)
        with sessions() as db:
            assert db.get(models.CaseApplication, cid).profile_ext["count"] == 2
    finally:
        release.set()
        engine.dispose()


def test_sqlite_read_transaction_is_upgraded_and_cache_checks_actual_connection(case_doc):
    cid, _ = case_doc
    with m.SessionLocal() as db:
        conn = db.connection()
        conn.exec_driver_sql("BEGIN")
        db.get(models.CaseApplication, cid)
        database.lock_cases(db, [cid])
        assert db.info["sqlite_case_writer"] == (db.get_transaction(), conn.get_transaction())
        with m.engine.connect() as other:
            other.exec_driver_sql("PRAGMA busy_timeout=0")
            from sqlalchemy.exc import OperationalError
            with pytest.raises(OperationalError, match="locked"):
                other.exec_driver_sql("BEGIN IMMEDIATE")
            other.exec_driver_sql("PRAGMA busy_timeout=5000")
        # SessionTransaction 객체만 남아 있어도 실제 트랜잭션 종료를 감지한다.
        conn.exec_driver_sql("COMMIT")
        assert not conn.connection.driver_connection.in_transaction
        database.lock_cases(db, [cid])
        assert conn.connection.driver_connection.in_transaction
        savepoint = db.begin_nested()
        database.lock_cases(db, [cid])
        savepoint.rollback()
        assert db.info["sqlite_case_writer"] == (db.get_transaction(), conn.get_transaction())


@pytest.mark.parametrize("statement", [
    "values = {'nib': '123'}; query.update(values)",
    "values = {'company_name': 'X'}; update(CaseApplication).values(**values)",
    "values: dict = {'address': 'X'}; alias = values; query.update(alias)",
    "values = dict(phone='123'); query.update(values)",
    "values = {'email': 'x'}; query.update({**values})",
    "query = db.query(CaseApplication); query.update(values)",
    "query = db.query(models.CaseApplication).filter_by(case_id=cid); query.update(get_values())",
    "stmt = update(CaseApplication); stmt.values(**values)",
    "db.bulk_update_mappings(CaseApplication, [{'case_id': cid, 'nib': '123'}])",
    "db.bulk_insert_mappings(models.CaseApplication, values)",
    "db.bulk_update_mappings(mapper=CaseApplication, mappings=values)",
    "model = models.CaseApplication; db.bulk_insert_mappings(model, values)",
    "db.bulk_update_mappings(CaseApplication, [{'case_id': cid, 'status': 'onboarding'}])",
])
def test_static_case_guard_catches_variable_mappings_and_unknown_case_bulk_writes(statement):
    assert _case_write_violations(ast.parse("query = db.query(CaseApplication)\n" + statement))


def test_static_bulk_guard_preserves_plain_dicts_other_models_and_backfill_exception(tmp_path):
    for statement in (
        "values = {'company_name': 'X'}; local = {}; local.update(values)",
        "db.bulk_update_mappings(User, values)",
        "values = {'status': 'cancelled'}; db.query(CaseApplication).update(values)",
    ):
        assert not _case_write_violations(ast.parse(statement))
    (tmp_path / "scripts").mkdir()
    for name in ("encrypt_backfill.py", "ordinary_backfill.py"):
        (tmp_path / "scripts" / name).write_text("db.bulk_insert_mappings(CaseApplication, values)\n")
    assert _audit_case_sources(tmp_path) == [("scripts/ordinary_backfill.py", 1)]


def test_sqlite_facility_choice_uses_one_writer_and_constant_selects(monkeypatch, record_property):
    original = m._autofill_org_case_ids
    calls = []
    def resolve(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(m, "_autofill_org_case_ids", resolve)
    select_counts = []
    for size in (0, 1000):
        _, cid, fid, did = _shared_cases()
        with m.SessionLocal() as db:
            org = db.get(models.CaseApplication, cid).org_id
            db.add_all(models.CaseApplication(org_id=org) for _ in range(size))
            doc = db.get(models.DocumentAsset, did)
            doc.fields = {"_autofill": {"conflicts": [{"scope": "facility", "field": "address",
                "facility_id": fid, "current": "Seoul", "proposed": "Busan", "status": "pending"}]}}
            db.commit()
        calls.clear()
        statements = []
        def count(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(m.engine, "before_cursor_execute", count)
        try:
            with m.SessionLocal() as db:
                result = m.apply_pending_extractions(cid, {"autofill_choices": [
                    {"conflict_id": did + ":0", "value": "Busan"}]},
                    user={**USER, "role": "consultant", "org_id": org}, db=db)
                assert result["autofill_confirmed"][0]["scope"] == "facility"
                assert db.get(models.Facility, fid).address == "Busan"
            assert len(calls) <= 1
            assert sum(sql == "BEGIN IMMEDIATE" for sql in statements) == 1
            select_counts.append(sum(sql.lstrip().upper().startswith("SELECT") for sql in statements))
        finally:
            event.remove(m.engine, "before_cursor_execute", count)
    record_property("sqlite_facility_choice_select_counts", select_counts)
    assert select_counts[0] == select_counts[1]
