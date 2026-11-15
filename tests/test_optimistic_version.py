"""3단계: 버전 CAS, 구 DB 마이그레이션, 2단계 P2 재현."""
import ast
from copy import deepcopy
import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, event, func, inspect, update
from sqlalchemy.orm import sessionmaker

from app import case_write_service as cases, intake_write_service as docs
from app import db as database, main as m, models
from scripts.write_guard import bulk_write_violations


@pytest.fixture
def store(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "versions.db"))
    models.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        db.add(models.CaseApplication(case_id="case", org_id="org_demo", company_name="Original",
                                      profile_ext={"city": "Seoul"}))
        db.flush()
        db.add(models.DocumentAsset(document_id="doc", case_id="case", filename="version.txt",
                                     fields={"address": "Original", "_verify": {"verdict": "warn"}}))
        db.commit()
    yield engine, sessions
    engine.dispose()


def row(db, kind):
    return db.get(models.CaseApplication, "case") if kind == "case" else db.get(models.DocumentAsset, "doc")


def snapshot(record, kind):
    return cases.case_snapshot(record) if kind == "case" else docs.document_snapshot(record)


def write(db, record, kind, *, base=None, value="Mine"):
    if kind == "case":
        changes = {"company_name": value} if base is not None else lambda latest: {"company_name": value}
        return cases.write_case(db, record, base=base, changes=changes, source="version_test")
    changes = {"address": value} if base is not None else lambda latest: {**latest, "address": value}
    return docs.write_document_fields(db, record, base_fields=base, new_fields=changes, source="version_test")


@pytest.mark.parametrize("kind", ["case", "doc"])
def test_increment_atomic_data_and_version_without_orm_duplicate_update(store, kind):
    engine, sessions = store
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, sql, *args: statements.append(sql))
    with sessions() as db:
        record = row(db, kind)
        assert record.row_version is None
        original = snapshot(record, kind)
        assert deepcopy(original).row_version == 0
        write(db, record, kind, base=original)
        assert record.row_version == 1
        write(db, record, kind, value="Second")
        assert record.row_version == 2
        db.flush()
        assert record.row_version == 2
        db.commit()
    with sessions() as db:
        record = row(db, kind)
        assert record.row_version == 2
        assert (record.company_name if kind == "case" else record.fields["address"]) == "Second"
    updates = [sql for sql in statements if sql.startswith("UPDATE") and "row_version" in sql]
    assert len(updates) == 2
    assert all("row_version" in sql.split(" WHERE ")[0] and "row_version" in sql.split(" WHERE ")[1]
               for sql in updates)


@pytest.mark.parametrize("kind", ["case", "doc"])
def test_same_version_uses_fast_path_without_three_way_merge(store, monkeypatch, caplog, kind):
    _, sessions = store
    def reject(*args, **kwargs):
        pytest.fail("unchanged version entered the three-way merger")
    monkeypatch.setattr(cases, "_merge", reject)
    monkeypatch.setattr(cases, "merge_profile", reject)
    monkeypatch.setattr(docs, "_merge_value", reject)
    monkeypatch.setattr(docs, "_merge_autofill", reject)
    with sessions() as db, caplog.at_level(logging.INFO, logger="glhac.write_version"):
        record = row(db, kind)
        base = snapshot(record, kind)
        write(db, record, kind, base=base)
        assert record.row_version == 1
        assert "optimistic_merge" not in caplog.text


@pytest.mark.parametrize("versioned", [False, True])
def test_fast_and_merge_paths_both_reject_invalid_profile_without_writing(store, versioned):
    _, sessions = store
    with sessions() as db:
        case = row(db, "case")
        base = cases.case_snapshot(case)
        if not versioned:
            base = dict(base)
        with pytest.raises(ValueError, match="dict or None"):
            cases.write_case(db, case, base=base, changes={"profile_ext": "invalid"}, source="invalid")
        assert case.row_version is None
        assert case.profile_ext == {"city": "Seoul"}


@pytest.mark.parametrize("kind", ["case", "doc"])
def test_different_version_merges_latest_human_decision_and_logs(store, caplog, kind):
    _, sessions = store
    with sessions() as db, sessions() as other:
        record = row(db, kind)
        base = snapshot(record, kind)
        if kind == "case":
            cases.write_case(other, "case", base=None, changes=lambda latest: {
                "profile_ext": {"city": "Human"}}, source="human")
        else:
            docs.write_document_fields(other, "doc", base_fields=None, new_fields=lambda latest: {
                **latest, "_verify": {"verdict": "ok"}}, source="human", decision_keys=("_verify",))
        other.commit()
        with caplog.at_level(logging.INFO, logger="glhac.write_version"):
            if kind == "case":
                cases.write_case(db, record, base=base, changes={"profile_ext": {
                    "city": "Machine", "country": "Korea"}}, source="late")
                assert record.profile_ext == {"city": "Human", "country": "Korea"}
            else:
                docs.write_document_fields(db, record, base_fields=base,
                    new_fields={"address": "Machine"}, source="late")
                assert record.fields == {"address": "Machine", "_verify": {"verdict": "ok"}}
            assert "optimistic_merge" in caplog.text
            assert "base_version=0 latest_version=1" in caplog.text
        assert record.row_version == 2
        assert base.row_version == 0
        db.commit()


def inject_bypass(engine, kind, limit):
    """SQLite writer 잠금 안에서 CAS 직전 우회 SQL을 주입해 실제 rowcount=0을 만든다."""
    model = models.CaseApplication if kind == "case" else models.DocumentAsset
    calls = []
    active = False
    def before(conn, cursor, sql, parameters, context, executemany):
        nonlocal active
        if active or not sql.startswith("UPDATE " + model.__tablename__) or "row_version" not in sql:
            return
        calls.append(sql)
        if len(calls) <= limit:
            active = True
            try:
                conn.execute(update(model.__table__).values(row_version=func.coalesce(model.row_version, 0) + 1))
            finally:
                active = False
    event.listen(engine, "before_cursor_execute", before)
    return calls


@pytest.mark.parametrize("kind", ["case", "doc"])
def test_bypass_version_update_reloads_merges_and_retries_actual_cas(store, caplog, kind):
    engine, sessions = store
    calls = inject_bypass(engine, kind, 1)
    with sessions() as db, caplog.at_level(logging.INFO, logger="glhac.write_version"):
        record = row(db, kind)
        write(db, record, kind, base=snapshot(record, kind))
        assert len(calls) == 2
        assert record.row_version == 2
        assert "optimistic_retry" in caplog.text and "optimistic_merge" in caplog.text
        db.commit()
    with sessions() as db:
        record = row(db, kind)
        assert record.row_version == 2
        assert (record.company_name if kind == "case" else record.fields["address"]) == "Mine"


@pytest.mark.parametrize("kind", ["case", "doc"])
def test_third_conflict_aborts_entire_transaction_with_409(store, kind):
    engine, sessions = store
    calls = inject_bypass(engine, kind, 99)
    with sessions() as db:
        record = row(db, kind)
        db.execute(update(models.CaseApplication).values(draft_state="uncommitted"))
        with pytest.raises(HTTPException) as exc:
            write(db, record, kind, base=snapshot(record, kind))
        assert exc.value.status_code == 409
        assert exc.value.detail == {"code": "CONCURRENT_CHANGE_RETRY"}
        assert len(calls) == 3
        assert not db.in_transaction()
    with sessions() as db:
        assert row(db, kind).row_version is None
        assert row(db, "case").draft_state is None


@pytest.mark.parametrize("kind", ["case", "doc"])
@pytest.mark.parametrize("flush", [False, True])
def test_savepoint_and_root_rollback_restore_versions_and_data(store, kind, flush):
    _, sessions = store
    with sessions() as db:
        record = row(db, kind)
        write(db, record, kind, value="Outer")
        nested = db.begin_nested()
        write(db, record, kind, value="Inner")
        assert record.row_version == 2
        if flush:
            db.flush()
        nested.rollback()
        assert record.row_version == 1
        assert (record.company_name if kind == "case" else record.fields["address"]) == "Outer"
        write(db, record, kind, value="After savepoint")
        assert record.row_version == 2
        db.rollback()
        assert record.row_version is None
        assert (record.company_name if kind == "case" else record.fields["address"]) == "Original"


def test_migrate_adds_nullable_columns_to_existing_sqlite_without_backfill(store, monkeypatch):
    engine, sessions = store
    with engine.begin() as conn:
        for table in ("case_application", "document_asset"):
            conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN row_version")
    monkeypatch.setattr(m, "engine", engine)
    m._migrate()
    m._migrate()
    for table in ("case_application", "document_asset"):
        column = next(c for c in inspect(engine).get_columns(table) if c["name"] == "row_version")
        assert column["nullable"] is True and column["default"] is None
    with sessions() as db:
        for kind in ("case", "doc"):
            record = row(db, kind)
            assert record.row_version is None
            assert snapshot(record, kind).row_version == 0
            write(db, record, kind, base=snapshot(record, kind))
            assert record.row_version == 1
        db.commit()


def test_alembic_revision_handles_preexisting_and_missing_columns(store):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("version_revision",
        Path(__file__).resolve().parents[1] / "alembic/versions/0008_optimistic_version.py")
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine, _ = store
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()
            revision.downgrade()
            for table in ("case_application", "document_asset"):
                assert "row_version" not in {c["name"] for c in inspect(conn).get_columns(table)}
            revision.upgrade()
            revision.upgrade()
            for table in ("case_application", "document_asset"):
                assert "row_version" in {c["name"] for c in inspect(conn).get_columns(table)}


@pytest.mark.parametrize("operation", ["query_update", "orm_update", "core_update", "query_delete", "orm_delete"])
def test_bulk_dml_counts_as_started_write_and_expansion_aborts(store, operation):
    _, sessions = store
    with sessions() as db:
        database.lock_cases(db, ["case"])
        query = db.query(models.DocumentAsset).filter_by(document_id="doc")
        if operation == "query_update":
            query.update({"review_status": "approved"}, synchronize_session=False)
        elif operation == "orm_update":
            db.execute(update(models.DocumentAsset).values(review_status="approved"))
        elif operation == "core_update":
            db.execute(update(models.DocumentAsset.__table__).values(review_status="approved"))
        elif operation == "query_delete":
            query.delete(synchronize_session=False)
        else:
            db.execute(delete(models.DocumentAsset))
        assert not (db.new or db.dirty or db.deleted)
        assert db.info["transaction_write_started"]
        with pytest.raises(HTTPException) as exc:
            database.lock_case_set(db, lambda: {"case", "new-case"})
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "CONCURRENT_CHANGE_RETRY"
        assert "transaction_write_started" not in db.info
        assert row(db, "doc").review_status == "pending"


@pytest.mark.parametrize("model,key", [("CaseApplication", "nib"), ("DocumentAsset", "fields")])
@pytest.mark.parametrize("mutation", [
    "values[KEY] = 'x'", "values.update(KEY='x')", "alias = values; alias[KEY] = 'x'",
    "alias = values; alias.update({KEY: 'x'})", "values |= {KEY: 'x'}",
    "values.setdefault(KEY, 'x')", "values.__setitem__(KEY, 'x')",
])
def test_static_guard_tracks_mutated_dicts_at_model_bulk_write(model, key, mutation):
    mutation = mutation.replace("KEY=", key + "=").replace("KEY", repr(key))
    source = f"query = db.query({model})\nvalues = {{}}\n{mutation}\nquery.update(values)"
    assert bulk_write_violations(ast.parse(source), model, {key}) == [len(source.splitlines())]


@pytest.mark.parametrize("source", [
    "values = {'email': 'a@example.com'}; db.query(User).update(values)",
    "values = {'fields': {}}; update(User).values(values)",
    "local = {}; alias = local; alias.update({'company_name': 'X'})",
    "local = {}; alias = local; alias.update(fields={})",
    "query = db.query(User); alias = query; alias.update(nib='123')",
    "values = {}; alias = values; values = {}; alias['nib'] = '123'; db.query(CaseApplication).update(values)",
    "query = db.query(DocumentAsset); query.update({'company_name': 'X'})",
    "query = db.query(CaseApplication); query.update({'fields': {}})",
])
def test_static_guard_ignores_other_models_and_plain_dict_aliases(source):
    from test_case_write_service import _case_write_violations
    from test_doc_write_service import _direct_writes
    tree = ast.parse(source)
    assert _case_write_violations(tree) == []
    assert _direct_writes(tree) == []


def test_grandfather_batch_orders_cases_before_postgres_locks(store, monkeypatch):
    from scripts import formal_flow_grandfather
    engine, sessions = store
    with sessions() as db:
        db.add_all([models.CaseApplication(case_id="z-case", org_id="org_demo", status="audit_in_progress"),
                    models.CaseApplication(case_id="a-case", org_id="org_demo", status="audit_in_progress")])
        db.commit()
    locks = []
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(engine.dialect, "name", "postgresql")
    monkeypatch.setattr(database, "lock_chain_tail", lambda db, key: locks.append(key))
    monkeypatch.setattr(sys, "argv", ["formal_flow_grandfather.py", "--apply"])
    formal_flow_grandfather.main()
    assert locks == ["wf:a-case", "wf:z-case"]
    with sessions() as db:
        assert db.query(models.WorkflowEvent).count() == 4


def test_versions_are_not_api_or_json_keys(store):
    _, sessions = store
    with sessions() as db:
        case, doc = row(db, "case"), row(db, "doc")
        write(db, case, "case")
        write(db, doc, "doc")
        assert "row_version" not in m._case_dict(case)
        visible = m.list_documents("case", lang="en", user={"uid": "admin1", "role": "admin", "org_id": "org_demo"}, db=db)
        assert "row_version" not in json.dumps(visible)
        assert "row_version" not in json.dumps(cases.case_snapshot(case))
        assert "row_version" not in json.dumps(docs.document_snapshot(doc))
        assert docs.document_snapshots(db, "case")["doc"].row_version == 1


@pytest.fixture
def identity_store(store):
    engine, sessions = store
    class IdentitySession(sessions.class_):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # 앞선 웹훅 테스트의 main 재로드로 누적된 옛 콜백은 이 세션에서만 제외한다.
            # 전역 리스너/다른 워커는 그대로 두고 현재 신원 가드는 실제로 실행한다.
            self.dispatch.do_orm_execute = [
                callback for callback in self.dispatch.do_orm_execute
                if callback is m._recheck_bulk_case_writes or
                (callback.__module__, callback.__name__) != (m.__name__, "_recheck_bulk_case_writes")]
    return engine, sessionmaker(bind=engine, class_=IdentitySession, autoflush=False)


def _record_case_identity_writes(engine, monkeypatch):
    updates, identities = [], []
    def record_sql(conn, cursor, sql, *args):
        if sql.startswith("UPDATE case_application") and "row_version" in sql:
            updates.append(sql)
    event.listen(engine, "before_cursor_execute", record_sql)
    original = m._assert_unique_case_identity
    def check(db, case):
        if db.get_bind() is engine and case.case_id == "case":
            identities.append((case.company_name, case.nib))
        return original(db, case)
    monkeypatch.setattr(m, "_assert_unique_case_identity", check)
    return updates, identities


def test_failed_autofill_restores_complete_identity_and_profile_in_one_cas(identity_store, monkeypatch):
    engine, sessions = identity_store
    nib = "9120001234567"
    original_profile = {"country": None, "manual_note": "Keep me"}
    with sessions() as db:
        case, doc = row(db, "case"), row(db, "doc")
        case.company_name, case.profile_ext = "My Company", deepcopy(original_profile)
        db.add(models.Org(org_id=case.org_id, name="PT Verified", profile_ext={"nib": nib}))
        db.add(models.CaseApplication(case_id="verified", org_id=case.org_id,
                                      company_name="PT Verified", nib=nib))
        doc.doc_type = "nib_business_license"
        doc.text_full = ("NOMOR INDUK BERUSAHA (NIB)\nNama Perusahaan: PT Verified\nNIB: " + nib +
                         "\nAlamat: Jl. Sudirman No. 24, Jakarta\nCity: Jakarta\nCountry: Indonesia")
        doc.fields = {"company_name": "PT Verified", "nib": nib,
                      "address": "Jl. Sudirman No. 24, Jakarta", "city": "Jakarta", "country": "Indonesia"}
        db.commit()
    updates, identities = _record_case_identity_writes(engine, monkeypatch)
    monkeypatch.setattr(m.intake, "verify_extraction", lambda *args: {
        "verdict": "fail", "field_issues": [], "summary": "Controlled verification failure"})
    with sessions() as db:
        case, doc = row(db, "case"), row(db, "doc")
        applied = m._apply_owned_autofill(db, case, doc)
        db.commit()
        assert (case.company_name, case.nib) == ("PT Verified", nib)
        assert applied["profile.city"] == "Jakarta"
        assert applied["profile.country"] == "Indonesia"
        m._verify_doc(db, doc, case)
    with sessions() as db:
        case, doc = row(db, "case"), row(db, "doc")
        assert (case.company_name, case.nib, case.address) == ("My Company", None, None)
        assert case.profile_ext == original_profile
        assert case.row_version == 2
        assert doc.fields["_verify"]["verdict"] == "fail"
        assert doc.fields["_verify"]["summary"] == "Controlled verification failure"
        meta = doc.fields["_autofill"]
        assert {"company_name", "nib", "profile.city", "profile.country"} <= meta["changes"].keys()
        assert all(change["status"] == "reverted" for change in meta["changes"].values())
        assert len(meta["conflicts"]) == len(meta["changes"])
        assert all(conflict["reason"] == "verify_failed" and conflict["status"] == "pending"
                   for conflict in meta["conflicts"])
        verified = db.get(models.CaseApplication, "verified")
        assert (verified.company_name, verified.nib) == ("PT Verified", nib)
    assert len(updates) == 2
    assert identities == [("PT Verified", nib), ("My Company", None)]


@pytest.mark.parametrize("reverse", [False, True])
def test_confirm_autofill_checks_complete_identity_and_profiles_once(identity_store, monkeypatch, reverse):
    engine, sessions = identity_store
    old_nib, new_nib = "8120001234567", "9120001234567"
    conflicts = [
        {"scope": "case", "field": "company_name", "current": "Original", "proposed": "PT Verified"},
        {"scope": "case", "field": "nib", "current": old_nib, "proposed": new_nib},
        {"scope": "profile", "field": "city", "current": "Seoul", "proposed": "Jakarta"},
        {"scope": "profile", "field": "country", "current": None, "proposed": "Indonesia"},
    ]
    with sessions() as db:
        case, doc = row(db, "case"), row(db, "doc")
        case.nib = old_nib
        db.add_all([models.CaseApplication(case_id="original", org_id=case.org_id,
                                           company_name="Original", nib=old_nib),
                    models.CaseApplication(case_id="verified", org_id=case.org_id,
                                           company_name="PT Verified", nib=new_nib)])
        doc.doc_type = "nib_business_license"
        doc.fields = {"_autofill": {"conflicts": [{**item, "status": "pending"} for item in conflicts]}}
        db.commit()
    updates, identities = _record_case_identity_writes(engine, monkeypatch)
    choices = [{"conflict_id": "doc:" + str(i), "value": item["proposed"]}
               for i, item in enumerate(conflicts)]
    if reverse:
        choices.reverse()
    with sessions() as db:
        changed = m._confirm_autofill(db, row(db, "case"), choices,
                                      {"uid": "applicant", "role": "applicant"})
        assert len(changed) == len(choices)
        db.commit()
    with sessions() as db:
        case, doc = row(db, "case"), row(db, "doc")
        assert (case.company_name, case.nib) == ("PT Verified", new_nib)
        assert case.profile_ext == {"city": "Jakarta", "country": "Indonesia"}
        assert case.row_version == 1
        assert [item["status"] for item in doc.fields["_autofill"]["conflicts"]] == ["confirmed"] * 4
    assert len(updates) == 1
    assert identities == [("PT Verified", new_nib)]


@pytest.mark.parametrize("same_company", [False, True])
@pytest.mark.parametrize("primary_email", [None, "primary@example.com"])
def test_create_case_inherits_columns_in_one_initial_write(store, same_company, primary_email):
    _, sessions = store
    profile = {"nib": "9120001234567", "responsible_person": "Owner", "address": "Office",
               "factory_reg_no": "FACTORY-1", "factory_address": "Factory", "phone": "0212345678",
               "halal_supervisor": "Supervisor", "email": primary_email,
               "company_email": "fallback@example.com", "email_consent": {"agreed": True}}
    company = "PT Verified" if same_company else "Another Company"
    with sessions() as db:
        db.add(models.Org(org_id="inherit", name="PT Verified", profile_ext=profile))
        db.commit()
        result = m.create_case(m.schemas.CaseCreate(org_id="inherit", company_name=company),
                               user={"uid": "admin1", "role": "admin", "org_id": "inherit"}, db=db)
        case = db.get(models.CaseApplication, result["case_id"])
        assert case.row_version == 1
        assert case.company_name == company
        for key in ("nib", "responsible_person", "address", "factory_reg_no", "factory_address"):
            assert getattr(case, key) == (profile[key] if same_company else None)
        assert case.phone == profile["phone"]
        assert case.halal_supervisor == profile["halal_supervisor"]
        assert case.email == (primary_email or profile["company_email"])
        assert case.notify_consent is (primary_email is None)


@pytest.mark.parametrize("model,key", [("CaseApplication", "nib"), ("CaseApplication", "profile_ext"),
                                       ("DocumentAsset", "fields")])
@pytest.mark.parametrize("copied", ["values.copy()", "dict(values)", "{**values}"])
@pytest.mark.parametrize("mutation", ["initial", "before_copy", "after_copy"])
@pytest.mark.parametrize("write", ["query.update(copied)", "stmt.values(**copied)"])
def test_static_guard_tracks_copied_protected_mappings(model, key, copied, mutation, write):
    setup = {"initial": f"values = {{{key!r}: 'x'}}\ncopied = {copied}",
             "before_copy": f"values = {{}}\nvalues[{key!r}] = 'x'\ncopied = {copied}",
             "after_copy": f"values = {{}}\ncopied = {copied}\ncopied[{key!r}] = 'x'"}[mutation]
    source = f"query = db.query({model})\nstmt = update({model})\n{setup}\n{write}"
    assert bulk_write_violations(ast.parse(source), model, {key}) == [len(source.splitlines())]


@pytest.mark.parametrize("copied", ["values.copy()", "dict(values)", "{**values}"])
@pytest.mark.parametrize("source", [
    "values = {'nib': '123'}\ndb.query(User).update(COPY)",
    "values = {'fields': {}}\nupdate(CaseApplication).values(COPY)",
    "values = {'profile_ext': {}}\ndb.query(DocumentAsset).update(COPY)",
    "values = {'email': 'a@example.com'}\nupdate(User).values(COPY)",
    "values = {'status': 'cancelled'}\ndb.query(CaseApplication).update(COPY)",
    "values = {}\ncopied = COPY\nvalues['nib'] = '123'\ndb.query(CaseApplication).update(copied)",
    "values = {}\ncopied = COPY\ncopied['fields'] = {}\nupdate(DocumentAsset).values(values)",
    "values = {}\nlocal = {}\nlocal.update(COPY)",
])
def test_static_guard_keeps_copies_independent_and_model_scoped(copied, source):
    from test_case_write_service import _case_write_violations
    from test_doc_write_service import _direct_writes
    tree = ast.parse(source.replace("COPY", copied))
    assert _case_write_violations(tree) == []
    assert _direct_writes(tree) == []
