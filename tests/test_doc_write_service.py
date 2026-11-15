"""단일 문서 저장 경계: 쓰기/정책 누락, 최신 결정, 트랜잭션·경합 회귀."""
import ast
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import intake, main as m, models
from app import intake_write_service as service


ROOT = Path(__file__).resolve().parents[1]
INITIALIZERS = {
    ("app/main.py", "add_material_evidence"),
    ("app/main.py", "_apply_intake_autofill"),
    ("app/main.py", "parse_file_ep"),
    ("app/main.py", "upload_case_document"),
}


def _fields_target(node, aliases):
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Subscript):
        return _fields_target(node.value, aliases)
    if isinstance(node, ast.BoolOp):
        return any(_fields_target(value, aliases) for value in node.values)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return (node.func.attr in ("get", "setdefault") and
                _fields_target(node.func.value, aliases))
    return isinstance(node, ast.Attribute) and node.attr == "fields"


def _direct_writes(tree):
    class Writes(ast.NodeVisitor):
        def __init__(self):
            self.aliases, self.lines = set(), set()

        def visit_FunctionDef(self, node):
            outer, self.aliases = self.aliases, set()
            self.generic_visit(node)
            self.aliases = outer

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node):
            self.generic_visit(node)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if _fields_target(node.value, self.aliases):
                        self.aliases.add(target.id)
                    else:
                        self.aliases.discard(target.id)

        def visit_AnnAssign(self, node):
            self.generic_visit(node)
            if isinstance(node.target, ast.Name):
                if _fields_target(node.value, self.aliases):
                    self.aliases.add(node.target.id)
                else:
                    self.aliases.discard(node.target.id)

        def visit_AugAssign(self, node):
            if _fields_target(node.target, self.aliases):
                self.lines.add(node.lineno)
            self.generic_visit(node)

        def visit_Attribute(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)) and _fields_target(node, self.aliases):
                self.lines.add(node.lineno)
            self.generic_visit(node)

        visit_Subscript = visit_Attribute

        def visit_Call(self, node):
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name in ("flag_modified", "setattr") and len(node.args) > 1:
                if isinstance(node.args[1], ast.Constant) and node.args[1].value == "fields":
                    self.lines.add(node.lineno)
            if isinstance(node.func, ast.Attribute):
                if (_fields_target(node.func.value, self.aliases) and name in (
                        "update", "pop", "popitem", "clear", "setdefault", "__setitem__",
                        "__delitem__", "append", "extend", "insert", "remove", "sort", "reverse")):
                    self.lines.add(node.lineno)
            self.generic_visit(node)

    visitor = Writes()
    visitor.visit(tree)
    from scripts.write_guard import bulk_write_violations
    return sorted(visitor.lines | set(bulk_write_violations(tree, "DocumentAsset", {"fields"})))


def _python_sources(root):
    return (path for directory in ("app", "scripts") for path in (root / directory).rglob("*.py"))


def _source_audit(root):
    violations, constructors = [], set()
    for path in _python_sources(root):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if rel != "app/intake_write_service.py":
            violations += [(rel, line) for line in _direct_writes(tree)]
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", getattr(node.func, "id", None)) == "DocumentAsset"
                    and any(kw.arg == "fields" for kw in node.keywords)):
                parent = parents.get(node)
                while parent and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parent = parents.get(parent)
                constructors.add((rel, parent.name if parent else None))
    return violations, constructors


def test_all_document_writes_go_through_service():
    violations, constructors = _source_audit(ROOT)
    assert violations == []
    assert constructors == INITIALIZERS


@pytest.mark.parametrize("statement", [
    "doc.fields = {}", "doc.fields['x'] = 1", "doc.fields['x']['y'] = 1",
    "doc.fields |= {}", "flag_modified(doc, 'fields')", "setattr(doc, 'fields', {})",
    "doc.fields.update(x=1)",
    "db.query(DocumentAsset).filter_by(document_id='id').update({'fields': {}})",
    "query.update({DocumentAsset.fields: {}})",
    "update(DocumentAsset).values(fields={})",
    "update(DocumentAsset).values({'fields': {}})",
    "f = doc.fields; f['x'] = 1", "f = doc.fields or {}; other = f; other.update(x=1)",
    "f: dict = doc.fields; f |= {'x': 1}", "f = doc.fields; del f['x']",
    "doc.fields.get('_autofill', {}).update(applied={})",
    "f = doc.fields.get('_autofill', {}); f.get('conflicts', []).append({})",
])
def test_static_guard_detects_direct_writes(statement):
    assert _direct_writes(ast.parse(statement))


@pytest.mark.parametrize("statement", [
    "f = dict(doc.fields); f['x'] = 1", "f = deepcopy(doc.fields); f.update(x=1)",
    "f = doc.fields; f = {}; f.update(x=1)",
    "def read(doc):\n    f = doc.fields\ndef other():\n    f = {}; f['x'] = 1",
])
def test_static_guard_allows_detached_copies_and_local_rebinding(statement):
    assert not _direct_writes(ast.parse(statement))


def test_static_guard_includes_scripts(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "backfill.py").write_text(
        "db.query(DocumentAsset).update({'fields': {}})\n", encoding="utf-8")
    assert _source_audit(tmp_path)[0] == [("scripts/backfill.py", 1)]


def _reserved_keys(tree):
    keys = set()
    for node in ast.walk(tree):
        candidates = []
        if isinstance(node, ast.Dict):
            candidates = node.keys
        elif isinstance(node, ast.Subscript):
            candidates = [node.slice]
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in ("get", "pop", "setdefault") and node.args):
            candidates = node.args[:1]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"):
            keys.update(kw.arg for kw in node.keywords if kw.arg and kw.arg.startswith("_"))
        for value in candidates:
            if (isinstance(value, ast.Constant) and isinstance(value.value, str)
                    and value.value.startswith("_") and not value.value.startswith("__")):
                keys.add(value.value)
    return keys


def test_reserved_field_keys_have_a_policy():
    # app/scripts의 예약 JSON 키를 검사한다. 다른 저장소/임시 결과에 속한 키만 제외한다.
    non_document = {
        "_chain",  # 감사로그 해시체인
        "_verify_ids",  # 인테이크 응답의 검증 큐 전달 목록
        "_registration_extraction",  # 케이스 프로필의 가입 근거
        "_doc_texts",  # 증빙 검증기의 임시 입력
        "_cas", "_names",  # scripts/build_kma1360.py의 카탈로그 생성용 중간 자료
    }
    referenced = set()
    for path in _python_sources(ROOT):
        referenced |= _reserved_keys(ast.parse(path.read_text(encoding="utf-8")))
    assert referenced - non_document - service.FIELD_POLICIES.keys() == set()
    assert "_new_decision" in _reserved_keys(ast.parse("fields['_new_decision'] = True"))
    assert "_new_decision" in _reserved_keys(ast.parse("fields.update(_new_decision=True)"))
    assert "_new_decision" not in service.FIELD_POLICIES
    assert {"_autofill." + key for key in ("applied", "changes", "conflicts", "product_pending")} <= service.FIELD_POLICIES.keys()


@pytest.fixture
def document(monkeypatch):
    monkeypatch.setattr(m, "verify_enqueue", lambda *args: False)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", False)
    with m.SessionLocal() as db:
        case = models.CaseApplication(org_id="org_demo", company_name="Document writer")
        db.add(case)
        db.flush()
        doc = models.DocumentAsset(case_id=case.case_id, filename="writer.txt", doc_type="other",
                                   fields={"company_name": "Old", "obsolete": "old"}, confidence=.1,
                                   content_b64=base64.b64encode(b"Readable text").decode(), text_full="Readable text")
        db.add(doc)
        db.commit()
        return case.case_id, doc.document_id


DECISIONS = {
    "_autofill": {
        "applied": {"company_name": "Human"},
        "changes": {"company_name": {"scope": "case", "field": "company_name", "status": "confirmed",
                                       "value": "Human", "confirmed_by": "reviewer"}},
        "conflicts": [{"scope": "case", "field": "address", "status": "confirmed", "selected": "Seoul"}],
        "product_pending": [{"kind": "product_names", "value": "Pending"}],
    },
    "_verify": {"verdict": "warn", "summary": "Latest review"},
    "_scope_pending": [{"kind": "material_names", "value": "Water"}],
    "_counts_as_evidence": False,
    "_upload_id": "latest-upload",
}


ABSENT = object()
MERGE_CASES = [
    pytest.param("base", "base", "mine", "mine", "mine", id="only-mine-changed"),
    pytest.param("base", "latest", "base", "latest", "latest", id="only-latest-changed"),
    pytest.param("base", "latest", "mine", "latest", "mine", id="both-changed"),
    pytest.param("base", "base", ABSENT, ABSENT, ABSENT, id="mine-deleted"),
    pytest.param("base", ABSENT, "base", ABSENT, ABSENT, id="latest-deleted"),
    pytest.param("base", ABSENT, "mine", ABSENT, "mine", id="delete-vs-change"),
    pytest.param("base", "latest", ABSENT, "latest", ABSENT, id="change-vs-delete"),
    pytest.param(ABSENT, "latest", "mine", "latest", "mine", id="both-added"),
    pytest.param(ABSENT, ABSENT, None, None, None, id="null-is-not-absence"),
    pytest.param(None, None, ABSENT, ABSENT, ABSENT, id="delete-null"),
]


@pytest.mark.parametrize("key,decision", [
    ("address", False), ("_sources", False), ("_field_meta", False),
    ("_verify", True), ("_scope_pending", True), ("_counts_as_evidence", True), ("_upload_id", True),
])
@pytest.mark.parametrize("before,latest,mine,decision_expected,machine_expected", MERGE_CASES)
def test_three_way_key_policy(key, decision, before, latest, mine, decision_expected, machine_expected):
    def fields(value):
        return {} if value is ABSENT else {key: value}

    result = service.normalize_document_fields(fields(mine), base_fields=fields(before),
        current_fields=fields(latest), decision_keys=("_verify",))
    assert result == fields(decision_expected if decision else machine_expected)


@pytest.mark.parametrize("bucket", ["changes", "applied"])
@pytest.mark.parametrize("before,latest,mine,expected,unused", MERGE_CASES)
def test_three_way_autofill_merges_each_field(bucket, before, latest, mine, expected, unused):
    def fields(value):
        entries = {"untouched": {"status": "confirmed", "value": "Keep"}}
        if value is not ABSENT:
            entries["address"] = None if value is None else {"status": "applied", "value": value}
        return {"_autofill": {bucket: entries}}

    result = service.normalize_document_fields(fields(mine), base_fields=fields(before),
                                                current_fields=fields(latest))
    assert result == fields(expected)


def test_autofill_disjoint_edits_deletions_and_confirmation_do_not_replace_whole_map():
    base = {"_autofill": {
        "applied": {"address": "A", "company_name": "Old", "obsolete": "Gone"},
        "changes": {"address": {"status": "applied", "value": "A"},
                    "company_name": {"status": "applied", "value": "Old"},
                    "obsolete": {"status": "applied", "value": "Gone"}},
        "product_pending": [{"value": "Old product"}]}}
    latest, mine = deepcopy(base), deepcopy(base)
    latest["_autofill"]["changes"]["address"]["status"] = "confirmed"
    latest["_autofill"]["product_pending"] = [{"value": "New product"}]
    for bucket in ("changes", "applied"):
        mine["_autofill"][bucket].pop("obsolete")
    mine["_autofill"]["applied"]["company_name"] = "New"
    mine["_autofill"]["changes"]["company_name"]["value"] = "New"
    originals = deepcopy((base, latest, mine))
    result = service.normalize_document_fields(mine, base_fields=base, current_fields=latest)
    assert result == {"_autofill": {
        "applied": {"address": "A", "company_name": "New"},
        "changes": {"address": {"status": "confirmed", "value": "A"},
                    "company_name": {"status": "applied", "value": "New"}},
        "product_pending": [{"value": "New product"}]}}
    result["_autofill"]["changes"]["address"]["value"] = "Mutated result"
    assert (base, latest, mine) == originals


@pytest.mark.parametrize("latest", [{}, {"_verify": {"verdict": "ok", "summary": "During analysis"}}])
def test_reanalysis_clears_base_verification_without_erasing_concurrent_decision(latest):
    base = {"address": "A", "_verify": {"verdict": "warn", "summary": "Old extraction"}}
    current = {"address": "A", **latest}
    mine = service.reanalysis_document_fields(base, {"address": "B"})
    result = service.normalize_document_fields(mine, base_fields=base, current_fields=current,
                                               analysis={}, text="Address B", clear_verification=True)
    assert result == {"address": "B", **latest}


def test_precomputed_write_requires_explicit_snapshot(document):
    _, document_id = document
    with m.SessionLocal() as db:
        with pytest.raises(TypeError, match="base_fields"):
            service.write_document_fields(db, document_id, new_fields={}, source="missing_snapshot")
        with pytest.raises(ValueError, match="snapshot"):
            service.write_document_fields(db, document_id, base_fields=None, new_fields={}, source="missing_snapshot")
    with pytest.raises(ValueError, match="snapshot"):
        service.normalize_document_fields({}, current_fields={})


def test_new_applied_value_replaces_a_conflicting_confirmation_in_both_autofill_maps():
    base = {"address": "Original", "_autofill": {
        "applied": {"address": "Original"},
        "changes": {"address": {"scope": "case", "field": "address", "value": "Original",
                                "status": "applied"}}}}
    latest, mine = deepcopy(base), deepcopy(base)
    latest["_autofill"]["applied"]["address"] = "A"
    latest["_autofill"]["changes"]["address"].update(value="A", status="confirmed", confirmed_by="reviewer")
    mine["address"] = "B"
    mine["_autofill"]["applied"]["address"] = "B"
    mine["_autofill"]["changes"]["address"].update(value="B", before="A")
    result = service.normalize_document_fields(mine, base_fields=base, current_fields=latest)
    assert result == mine
    assert result["_autofill"]["changes"]["address"]["status"] == "applied"
    assert "confirmed_by" not in result["_autofill"]["changes"]["address"]


@pytest.mark.parametrize("route", ["reprocess", "verify", "intake"])
def test_external_work_preserves_decisions_committed_after_base_snapshot(document, monkeypatch, route):
    case_id, document_id = document
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, document_id)
        case = db.get(models.CaseApplication, case_id)
        doc.fields = {**doc.fields, "_verify": {"verdict": "fail", "summary": "Before analysis"}}
        doc.file_hash = m._sha256_b64(doc.content_b64)
        db.commit()
        snapshot = deepcopy(doc.fields)
        parsed = {"filename": doc.filename, "doc_type": "other", "confidence": .9,
                  "fields": {"company_name": "Parsed"}, "text_full": "Parsed text",
                  "content_b64": doc.content_b64}

        def during_external_work(*args, **kwargs):
            with m.SessionLocal() as other:
                current = other.get(models.DocumentAsset, document_id)
                current.fields = {**current.fields, **deepcopy(DECISIONS)}
                current.review_status = "approved"
                other.commit()
            return {"verdict": "ok"} if route == "verify" else "Parsed text"

        monkeypatch.setattr(intake, "parse_file", during_external_work)
        monkeypatch.setattr(intake, "classify", lambda *args: deepcopy(parsed))
        monkeypatch.setattr(intake, "verify_extraction", during_external_work)
        monkeypatch.setattr(m, "_notify_package_gaps", lambda *args: None)
        monkeypatch.setattr(m, "_apply_agg_to_case", lambda *args, **kwargs: {})
        if route == "reprocess":
            m._reprocess_doc(db, doc, case, apply=False)
        elif route == "verify":
            m._verify_doc(db, doc, case)
        else:
            during_external_work()
            m._apply_intake_autofill(db, case, {"classified": [parsed], "extracted": {}},
                                   base_fields_by_id={document_id: snapshot})
            db.commit()
        db.expire_all()
        assert {key: doc.fields[key] for key in DECISIONS} == DECISIONS
        assert doc.review_status == "approved"
        assert snapshot["_verify"]["summary"] == "Before analysis"


@pytest.mark.parametrize("by_id", [False, True], ids=["stale-object", "document-id"])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("changed", [False, True], ids=["same-values", "new-values"])
def test_two_sessions_preserve_decisions_committed_after_read(document, by_id, autoflush, changed):
    case_id, document_id = document
    with m.SessionLocal(autoflush=autoflush) as a, m.SessionLocal() as b:
        old = a.get(models.DocumentAsset, document_id)
        stale = deepcopy(old.fields)
        current = b.get(models.DocumentAsset, document_id)
        service.write_document_fields(b, current, base_fields=deepcopy(current.fields),
                                      new_fields={**current.fields, **DECISIONS},
                                      source="human_decision", decision_keys=tuple(DECISIONS))
        b.commit()
        assert "_autofill" not in old.fields  # A의 identity map은 B의 COMMIT을 모른다.
        old.confidence = .96
        old.fields = {**stale, "company_name": "Stale mutation"}
        new = {**stale, "company_name": "New" if changed else stale["company_name"],
               "_sources": {"company_name": "llm"}}
        service.write_document_fields(a, document_id if by_id else old, base_fields=stale, new_fields=new,
                                      source="update_metadata")
        a.commit()
    with m.SessionLocal() as db:
        result = db.get(models.DocumentAsset, document_id)
        assert result.fields == {**new, **DECISIONS}
        assert result.confidence == .96


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_competing_writes_wait_until_lock_holder_finishes(tmp_path, monkeypatch, finish):
    """재조회뿐 아니라 서비스 반환 뒤 COMMIT/ROLLBACK까지 실제 DB 잠금이 유지돼야 한다."""
    engine = create_engine("sqlite:///" + str(tmp_path / "writers.db"),
                           connect_args={"check_same_thread": False, "timeout": 5})
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    models.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        case = models.CaseApplication(org_id="race", company_name="Race")
        db.add(case)
        db.flush()
        doc = models.DocumentAsset(case_id=case.case_id, filename="race.txt", fields={"count": 0})
        db.add(doc)
        db.commit()
        document_id = doc.document_id

    staged, release, attempted, entered = (threading.Event() for _ in range(4))
    lock = service.database.lock_chain_tail

    def observed_lock(db, key):
        if db.info["writer"] == "B":
            attempted.set()
        return lock(db, key)

    monkeypatch.setattr(service.database, "lock_chain_tail", observed_lock)

    def write(name):
        with sessions() as db:
            db.info["writer"] = name

            def increment(current):
                if name == "B":
                    entered.set()
                return {**current, "count": current["count"] + 1}

            service.write_document_fields(db, document_id, base_fields=None, new_fields=increment, source="race")
            if name == "A":
                staged.set()
                assert db.connection().connection.driver_connection.in_transaction, "쓰기 잠금이 없음"
                assert release.wait(5), "잠금 보유 세션의 종료 신호가 없음"
                getattr(db, finish)()
            else:
                db.commit()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(write, "A"), None
            try:
                assert staged.wait(5), "A가 서비스 쓰기를 완료하지 못함"
                b = pool.submit(write, "B")
                assert attempted.wait(5), "B가 잠금을 요청하지 못함"
                assert not entered.wait(.3), "A의 잠금 보유 중 B가 쓰기에 진입함"
            finally:
                release.set()
                a.result(timeout=10)
                if b is not None:
                    b.result(timeout=10)
        assert entered.is_set()
        with sessions() as db:
            assert db.get(models.DocumentAsset, document_id).fields["count"] == (2 if finish == "commit" else 1)
    finally:
        engine.dispose()


@pytest.mark.parametrize("new,analysis,text,expected", [
    ({"value": 1}, None, None, {"_llm_error": "OLD_LLM", "_ocr_error": "OLD_OCR"}),
    ({"value": 1}, {"ocr_error": "NEW"}, "Readable", {"_llm_error": "OLD_LLM", "_ocr_error": "NEW"}),
    ({"value": 1}, {"fields": {"_llm_error": "NEW"}}, "Readable", {"_llm_error": "NEW", "_ocr_error": "OLD_OCR"}),
    ({"_empty": True}, {}, "Readable", {"_llm_error": "OLD_LLM", "_ocr_error": "OLD_OCR"}),
    ({"_analysis": {"partial": True}}, {}, "Readable", {"_llm_error": "OLD_LLM", "_ocr_error": "OLD_OCR"}),
    ({"value": 1}, {}, " ", {"_llm_error": "OLD_LLM", "_ocr_error": "OLD_OCR"}),
    ({"value": 1}, {}, "Readable", {}),
])
def test_error_preservation_and_successful_reanalysis(document, new, analysis, text, expected):
    _, document_id = document
    with m.SessionLocal() as db:
        service.write_document_fields(db, document_id, base_fields=deepcopy(db.get(models.DocumentAsset, document_id).fields),
            new_fields={"_llm_error": "OLD_LLM", "_ocr_error": "OLD_OCR"}, source="failed_analysis")
        db.commit()
        fields = service.write_document_fields(db, document_id,
                                               base_fields=deepcopy(db.get(models.DocumentAsset, document_id).fields), new_fields=new,
                                               source="analysis", analysis=analysis, text=text)
        assert {key: fields[key] for key in ("_llm_error", "_ocr_error") if key in fields} == expected
        db.commit()


def test_parser_error_and_unknown_reserved_key_keep_legacy_behavior():
    text = intake._ParsedText("Readable", {"_ocr_error": "PAGE_TIMEOUT"})
    fields = service.normalize_document_fields({"_future_key": "new"},
        base_fields={"_future_key": "old", "_old_unknown": True},
        current_fields={"_future_key": "old", "_old_unknown": True}, analysis={}, text=text)
    assert fields == {"_future_key": "new", "_ocr_error": "PAGE_TIMEOUT"}


def test_deleted_decisions_are_not_resurrected_and_initial_evidence_is_allowed():
    stale = {key: value for key, value in DECISIONS.items() if key != "_counts_as_evidence"}
    assert service.normalize_document_fields(stale, base_fields=stale, current_fields={}) == {}
    assert service.normalize_document_fields({}, base_fields={}, current_fields={},
        decision_keys=("_autofill.product_pending",)) == {}
    assert service.normalize_document_fields({"_counts_as_evidence": False}, base_fields={}, current_fields={}) == {
        "_counts_as_evidence": False}
    assert service.normalize_document_fields({"_counts_as_evidence": False},
        base_fields={}, current_fields={"_counts_as_evidence": True}) == {"_counts_as_evidence": True}
    assert service.normalize_document_fields({"_counts_as_evidence": False},
        base_fields={"_counts_as_evidence": True},
        current_fields={"_counts_as_evidence": True},
        decision_keys=("_counts_as_evidence",)) == {"_counts_as_evidence": False}


@pytest.mark.parametrize("verification", [
    {"llm_error": "TIMEOUT"},
    {"verdict": "unknown", "llm_error": "TIMEOUT"},
    {"verdict": "warn", "llm_error": "TIMEOUT"},
    {"verdict": "fail", "summary": "Previous extraction"},
    {"verdict": "ok"},
    {"llm_error": "TIMEOUT", "field_issues": [{"field": "nib"}]},
])
def test_successful_reanalysis_clears_every_previous_verification(verification):
    current = {"_verify": verification}
    assert service.normalize_document_fields(current, base_fields=current, current_fields=current,
                                             analysis={}, text="text") == current
    result = service.normalize_document_fields(current, base_fields=current, current_fields=current, analysis={}, text="text",
                                               clear_verification=True)
    assert "_verify" not in result
    failed = service.normalize_document_fields(current, base_fields=current, current_fields=current,
        analysis={"llm_error": "TIMEOUT"}, text="text", clear_verification=True)
    assert failed["_verify"] == verification


@pytest.mark.parametrize("incoming", [
    {}, {"address": "B"}, {"address": "A", "new_value": "B"},
    {"address": "A", "_rejected_identifiers": {"nib": "invalid"}},
])
def test_changed_or_deleted_extraction_invalidates_verification(incoming):
    current = {"address": "A", "_verify": {"verdict": "ok"}}
    assert "_verify" not in service.normalize_document_fields(incoming, base_fields=current, current_fields=current)


def test_decision_commands_apply_intent_when_latest_equals_base():
    incoming = {"_autofill": {"changes": {"company_name": {"scope": "case", "field": "company_name",
                                                           "status": "applied", "value": "Human"}},
                              "product_pending": []}, "_verify": {"verdict": "ok"}}
    result = service.normalize_document_fields(incoming, base_fields=DECISIONS, current_fields=DECISIONS,
        decision_keys=("_autofill.changes", "_autofill.product_pending"))
    assert result == incoming
    with pytest.raises(ValueError, match="decision key"):
        service.normalize_document_fields({}, decision_keys=("_typo",))


def test_machine_update_preserves_confirmations_committed_after_snapshot():
    first = {"scope": "case", "field": "address", "status": "confirmed", "selected": "First"}
    second = {**first, "selected": "Second"}
    pending = {**first, "status": "pending", "proposed": "Third"}
    current = {"_autofill": {"conflicts": [first, pending, second]}}
    assert service.normalize_document_fields({}, base_fields={}, current_fields=current) == current
    updated = service.normalize_document_fields({"_autofill": {"conflicts": [pending]}},
        base_fields={"_autofill": {"conflicts": [pending]}}, current_fields=current,
        decision_keys=("_autofill.conflicts",))
    assert updated["_autofill"]["conflicts"] == [first, pending, second]


@pytest.mark.parametrize("new_address", ["Address A", "Address B"])
def test_reanalysis_replaces_only_changed_autofill_and_reverts_new_value(document, monkeypatch, new_address):
    case_id, document_id = document
    nib = "9120001234567"
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, case_id)
        doc = db.get(models.DocumentAsset, document_id)
        case.nib = nib
        doc.doc_type = "nib_business_license"
        doc.fields = {"company_name": case.company_name, "nib": nib, "address": "Address A"}
        doc.text_full = (f"NOMOR INDUK BERUSAHA (NIB)\nNama Perusahaan: {case.company_name}\n"
                         f"NIB: {nib}\nAlamat: Address A\n")
        db.commit()
        m._apply_owned_autofill(db, case, doc)
        m._mark_autofill_confirmed(db, doc, {"address"}, "reviewer", case)
        db.commit()
        confirmed = deepcopy(doc.fields["_autofill"]["changes"]["address"])
        assert confirmed["status"] == "confirmed"

        fields = {"company_name": case.company_name, "nib": nib, "address": new_address}
        text = doc.text_full.replace("Address A", new_address)
        snapshot = deepcopy(doc.fields)
        service.write_document_fields(db, doc, base_fields=snapshot,
                                      new_fields=service.reanalysis_document_fields(snapshot, fields), source="reanalysis",
                                      analysis={"fields": fields}, text=text, clear_verification=True)
        doc.text_full = text
        db.commit()
        m._apply_owned_autofill(db, case, doc)
        db.commit()
        meta = doc.fields["_autofill"]
        assert case.address == new_address
        assert meta["applied"]["address"] == new_address
        if new_address == "Address A":
            assert meta["changes"]["address"] == confirmed
        else:
            assert meta["changes"]["address"]["status"] == "applied"
            assert meta["changes"]["address"]["value"] == "Address B"
            assert meta["changes"]["address"]["before"] == "Address A"
            assert "confirmed_by" not in meta["changes"]["address"]
            assert "selected" not in meta["changes"]["address"]

        monkeypatch.setattr(intake, "verify_extraction", lambda *args: {
            "verdict": "fail", "field_issues": [{"field": "address", "issue": "verification failed"}]})
        m._verify_doc(db, doc, case)
        db.expire_all()
        assert case.address == "Address A"
        assert doc.fields["_verify"]["verdict"] == "fail"
        assert doc.fields["_autofill"]["changes"]["address"]["status"] == (
            "confirmed" if new_address == "Address A" else "reverted")
        if new_address == "Address B":
            assert any(item["proposed"] == "Address B" and item["reason"] == "verify_failed"
                       for item in doc.fields["_autofill"]["conflicts"])
        case.nib = None
        db.commit()


def test_autofill_confirmation_is_bound_to_each_selected_value():
    current = {"_autofill": {
        "applied": {"address": "A", "company_name": "Company"},
        "changes": {
            "address": {"scope": "case", "field": "address", "value": "A", "selected": "Chosen",
                        "status": "confirmed", "confirmed_by": "reviewer"},
            "company_name": {"scope": "case", "field": "company_name", "value": "Company",
                             "status": "confirmed", "confirmed_by": "reviewer"}}}}
    base = deepcopy(current)
    base["_autofill"]["changes"]["address"].update(status="applied", selected="A")
    for address in ("Chosen", "B"):
        incoming = deepcopy(current)
        incoming["_autofill"]["applied"]["address"] = address
        incoming["_autofill"]["changes"]["address"] = {
            "scope": "case", "field": "address", "value": address, "before": "Chosen", "status": "applied"}
        result = service.normalize_document_fields(incoming, base_fields=base, current_fields=current,
            decision_keys=("_autofill.changes", "_autofill.applied"))["_autofill"]
        assert result["changes"]["company_name"] == current["_autofill"]["changes"]["company_name"]
        assert result["changes"]["address"] == (
            current if address == "Chosen" else incoming)["_autofill"]["changes"]["address"]
        assert result["applied"]["address"] == address


def test_certificate_reanalysis_removes_old_mismatch_and_restores_evidence(document, monkeypatch):
    from app import cert_verify

    case_id, document_id = document
    fields = {"cert_no": "ID12345678901234567", "issuer": "BPJPH", "expiry_date": "2099-12-31"}
    text = "HALAL CERTIFICATE\nCertificate No: ID12345678901234567\nIssuer: BPJPH\nValid until: 2099-12-31"
    parsed = {"doc_type": "halal_certificate", "confidence": .98, "fields": fields}
    monkeypatch.setattr(intake, "parse_file", lambda *args, **kwargs: text)
    monkeypatch.setattr(intake, "classify", lambda *args: deepcopy(parsed))
    monkeypatch.setattr(intake, "parse_typed_text", lambda *args: deepcopy(parsed))
    monkeypatch.setattr(intake.ai_local, "provider", lambda: "openai")
    monkeypatch.setattr(intake.ai_local, "llm_json", lambda *args, **kwargs: {
        "verdict": "ok", "doc_type_ok": True, "field_issues": []})
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *args: None)
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, case_id)
        doc = db.get(models.DocumentAsset, document_id)
        doc.doc_type, doc.review_status = "nib_business_license", "approved"
        doc.fields = {"company_name": "Wrong owner", "_verify": {"verdict": "warn", "field_issues": [
            {"field": "company_name", "code": "company_name_mismatch", "issue": "different company"}]}}
        db.commit()
        assert "verification" in cert_verify.certificate_evidence_issues({"fields": doc.fields})
        m._reprocess_doc(db, doc, case, apply=False)
        assert "company_name" not in doc.fields and "_verify" not in doc.fields
        assert doc.review_status == "approved"
        m._verify_doc(db, doc, case)
        db.expire_all()
        assert doc.fields["_verify"]["verdict"] == "ok"
        assert doc.fields["_verify"]["field_issues"] == []
        # 기존 승인을 유지한 채 새 검증 결과로 증빙에 포함된다.
        evidence = {"doc_type": doc.doc_type, "filename": doc.filename, "text_full": doc.text_full,
                    "fields": doc.fields, "review_status": doc.review_status}
        assert cert_verify.certificate_evidence_issues(evidence) == []
        assert cert_verify.verify(fields["cert_no"], [evidence])["verdict"] == "VERIFIED"


@pytest.mark.parametrize("route", ["reprocess", "intake", "profile_autofill"])
@pytest.mark.parametrize("changed_type", [False, True])
def test_successful_analysis_clears_old_verify_and_preserves_review(document, monkeypatch, route, changed_type):
    case_id, document_id = document
    fields = {"company_name": "Document writer"}
    text = "Company name: Document writer"
    parsed = {"doc_type": "nib_business_license" if route == "profile_autofill" and not changed_type else "other",
              "confidence": .95, "fields": fields, "text_full": text,
              "filename": "writer.txt", "content_b64": base64.b64encode(text.encode()).decode()}
    monkeypatch.setattr(intake, "parse_file", lambda *args, **kwargs: text)
    monkeypatch.setattr(intake, "classify", lambda *args, **kwargs: deepcopy(parsed))
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *args: None)
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, case_id)
        doc = db.get(models.DocumentAsset, document_id)
        doc.fields = {**fields, "_verify": {"verdict": "warn"}, "_counts_as_evidence": False}
        doc.doc_type = "nib_business_license" if changed_type or route == "profile_autofill" else "other"
        doc.review_status, doc.text_full = "approved", text
        doc.file_hash = m._sha256_b64(parsed["content_b64"])
        db.commit()
        if route == "reprocess":
            m._reprocess_doc(db, doc, case, apply=False)
        elif route == "intake":
            m._apply_intake_autofill(db, case, {"classified": [deepcopy(parsed)], "extracted": {}},
                                   base_fields_by_id={document_id: deepcopy(doc.fields or {})})
            db.commit()
        else:
            m.autofill_profile(case_id, force=True, user={"uid": "admin1", "role": "admin"}, db=db)
        db.expire_all()
        assert "_verify" not in doc.fields
        assert doc.review_status == "approved"
        assert doc.fields["_counts_as_evidence"] is False  # 원본 문서는 그대로다.


@pytest.mark.parametrize("changed", ["none", "field", "text", "next_text", "doc_type", "failed_analysis"])
def test_review_survives_and_only_base_verification_is_invalidated(document, changed):
    _, document_id = document
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, document_id)
        doc.review_status = "approved"
        doc.fields = {"address": "A", "_verify": {"verdict": "ok"}, "_counts_as_evidence": False}
        db.commit()
        base = deepcopy(doc.fields)
        incoming = {**base, "address": "B" if changed == "field" else "A"}
        if changed == "doc_type":
            doc.doc_type = "halal_certificate"
        if changed == "text":
            doc.text_full = "New text"
        service.write_document_fields(db, doc, base_fields=base, new_fields=incoming, source="update",
                                      next_text="New text" if changed == "next_text" else None,
                                      analysis={"llm_error": "TIMEOUT"} if changed == "failed_analysis" else None,
                                      text="Unsaved parser text", clear_verification=True)
        db.commit()
        unchanged = changed in ("none", "failed_analysis")
        assert doc.review_status == "approved"
        assert ("_verify" in doc.fields) == unchanged
        assert doc.fields["_counts_as_evidence"] is False  # 원본에 대한 증빙 제외 지정


def test_lock_refresh_order_no_implicit_flush_and_local_columns_survive(document, monkeypatch):
    case_id, document_id = document
    with m.SessionLocal(autoflush=True) as db:
        case = db.get(models.CaseApplication, case_id)
        doc = db.get(models.DocumentAsset, document_id)
        case.profile_ext = {"city": "Seoul"}
        doc.confidence, doc.review_status, doc.text_full = .95, "approved", "New text"
        calls = []
        lock, get = service.database.lock_chain_tail, db.get
        def locked(session, key):
            calls.append("lock")
            return lock(session, key)
        def fetched(model, key, **kwargs):
            if kwargs.get("populate_existing"):
                assert kwargs["with_for_update"] is True
                calls.append("refresh")
            return get(model, key, **kwargs)
        def flushed(*args):
            pytest.fail("서비스 내부 flush는 호출자의 중간 편집을 저장합니다")
        monkeypatch.setattr(service.database, "lock_chain_tail", locked)
        monkeypatch.setattr(db, "get", fetched)
        event.listen(db, "before_flush", flushed)
        service.write_document_fields(db, doc, base_fields=deepcopy(doc.fields), new_fields={"value": "new"}, source="analysis")
        assert calls == ["lock", "refresh"]
        event.remove(db, "before_flush", flushed)
        db.commit()
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, document_id)
        assert (doc.confidence, doc.review_status, doc.text_full) == (.95, "approved", "New text")
        assert db.get(models.CaseApplication, case_id).profile_ext == {"city": "Seoul"}


def test_uncommitted_commands_survive_reentry_and_rollback_does_not_leak(document):
    _, document_id = document
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, document_id)
        service.write_document_fields(db, doc, base_fields=deepcopy(doc.fields),
                                      new_fields={"company_name": "New", "_verify": {"verdict": "fail"}},
                                      source="verify", decision_keys=("_verify",))
        service.write_document_fields(db, doc, base_fields=deepcopy(doc.fields),
                                      new_fields={**doc.fields, "_autofill": deepcopy(DECISIONS["_autofill"])},
                                      source="confirm", decision_keys=("_autofill",))
        assert doc.fields["_verify"] == {"verdict": "fail"}
        assert doc.fields["_autofill"] == DECISIONS["_autofill"]
        db.rollback()
        service.write_document_fields(db, doc, base_fields=deepcopy(doc.fields),
                                      new_fields={"company_name": "After rollback"}, source="analysis")
        assert "_verify" not in doc.fields and "_autofill" not in doc.fields
        db.commit()
        service.write_document_fields(db, doc, base_fields=deepcopy(doc.fields),
                                      new_fields={"company_name": "Uncommitted"}, source="analysis")
        db.rollback()
        assert doc.fields == {"company_name": "After rollback"}


def test_reprocess_completes_external_analysis_before_lock(document, monkeypatch):
    case_id, document_id = document
    calls = []
    with m.SessionLocal() as db:
        def unlocked(label):
            assert not db.connection().connection.driver_connection.in_transaction
            calls.append(label)
        def parse(*args, **kwargs):
            unlocked("ocr")
            return "Readable text"
        def classify(*args, **kwargs):
            unlocked("llm")
            return {"doc_type": "other", "confidence": .98, "fields": {"address": "Seoul"}}
        def aggregate(*args, **kwargs):
            unlocked("address_lookup")
            return {"products": [], "materials": [], "pending": []}
        monkeypatch.setattr(intake, "parse_file", parse)
        monkeypatch.setattr(intake, "classify", classify)
        monkeypatch.setattr(intake, "aggregate_fields", aggregate)
        monkeypatch.setattr(m, "_apply_agg_to_case", lambda *a, **k: {})
        monkeypatch.setattr(m, "_autofile_evidence", lambda *a, **k: None)
        monkeypatch.setattr(m, "_notify_package_gaps", lambda *a, **k: None)
        result = m._reprocess_doc(db, db.get(models.DocumentAsset, document_id),
                                 db.get(models.CaseApplication, case_id))
        assert calls == ["ocr", "llm", "address_lookup"]
        assert result["confidence"] == .98


def test_frozen_scope_only_aggregates_lists_without_address_lookup(document, monkeypatch):
    case_id, document_id = document
    monkeypatch.setattr(intake, "_lookup_zip", lambda *a: pytest.fail("동결 보류 중 주소 조회"))
    with m.SessionLocal() as db:
        case = db.get(models.CaseApplication, case_id)
        case.scope_frozen = True
        doc = db.get(models.DocumentAsset, document_id)
        doc.doc_type = "nib_business_license"
        service.write_document_fields(db, doc, base_fields=deepcopy(doc.fields),
            new_fields={"address": "Seoul", "product_names": ["Sample"]}, source="analysis")
        db.commit()
        result = m._apply_agg_to_case(db, case, {"products": [], "materials": [], "pending": []}, documents=[doc])
        assert result["scope_frozen"] is True
        assert doc.fields["address"] == "Seoul"


def test_late_failed_verification_does_not_revert_a_committed_ok(document, monkeypatch):
    case_id, document_id = document
    with m.SessionLocal() as db:
        case, doc = db.get(models.CaseApplication, case_id), db.get(models.DocumentAsset, document_id)
        case.address = "B"
        doc.fields = {"address": "B", "_autofill": {
            "applied": {"address": "B"}, "changes": {"address": {
                "scope": "case", "field": "address", "before": "A", "value": "B", "status": "applied"}}}}
        db.commit()

        def late_fail(*args):
            with m.SessionLocal() as other:
                current = other.get(models.DocumentAsset, document_id)
                base = deepcopy(current.fields)
                service.write_document_fields(other, current, base_fields=base,
                    new_fields={**base, "_verify": {"verdict": "ok", "field_issues": []}},
                    source="concurrent_verify", decision_keys=("_verify",))
                other.commit()
            return {"verdict": "fail", "field_issues": []}

        monkeypatch.setattr(intake, "verify_extraction", late_fail)
        m._verify_doc(db, doc, case)
        db.expire_all()
        assert doc.fields["_verify"]["verdict"] == "ok"
        assert case.address == doc.fields["address"] == "B"
        assert doc.fields["_autofill"]["changes"]["address"]["status"] == "applied"
        assert not doc.fields["_autofill"].get("conflicts")


@pytest.mark.parametrize("latest", [{}, {"product_names": ["New product"]}], ids=["deleted", "renamed"])
def test_reprocess_registers_products_from_the_merge_after_concurrent_reprocess(document, monkeypatch, latest):
    case_id, document_id = document
    text = "Product list\nOld product\nNew product"
    original = {"product_names": ["Old product"]}
    parsed = {"doc_type": "product_list", "confidence": .95, "fields": original}
    monkeypatch.setattr(intake, "classify", lambda *args: deepcopy(parsed))
    monkeypatch.setattr(intake, "parse_typed_text", lambda *args: deepcopy(parsed))
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *args: None)
    with m.SessionLocal() as db:
        doc, case = db.get(models.DocumentAsset, document_id), db.get(models.CaseApplication, case_id)
        doc.doc_type, doc.fields, doc.text_full = "product_list", deepcopy(original), text
        db.commit()

        def parse_after_other_reprocess(*args, **kwargs):
            with monkeypatch.context() as nested, m.SessionLocal() as other:
                newer = {**parsed, "fields": deepcopy(latest)}
                nested.setattr(intake, "parse_file", lambda *a, **k: text)
                nested.setattr(intake, "classify", lambda *a: deepcopy(newer))
                nested.setattr(intake, "parse_typed_text", lambda *a: deepcopy(newer))
                m._reprocess_doc(other, other.get(models.DocumentAsset, document_id),
                                 other.get(models.CaseApplication, case_id), apply=False)
            return text

        monkeypatch.setattr(intake, "parse_file", parse_after_other_reprocess)
        result = m._reprocess_doc(db, doc, case)
        db.expire_all()
        expected = latest.get("product_names", [])
        assert doc.fields.get("product_names", []) == expected
        assert [p.name for p in db.query(models.Product).filter_by(case_id=case_id)] == expected
        assert result["applied"]["products"] == len(expected)


@pytest.mark.parametrize("latest", [{}, {"product_names": ["New product"]}], ids=["deleted", "renamed"])
def test_intake_aggregates_the_stored_merge_instead_of_the_parsed_result(document, monkeypatch, latest):
    case_id, document_id = document
    with m.SessionLocal() as db:
        doc, case = db.get(models.DocumentAsset, document_id), db.get(models.CaseApplication, case_id)
        doc.doc_type, doc.fields = "product_list", {"product_names": ["Old product"]}
        doc.file_hash = m._sha256_b64(doc.content_b64)
        db.commit()
        snapshot = {document_id: deepcopy(doc.fields)}
        parsed = {"filename": doc.filename, "doc_type": doc.doc_type, "text_full": doc.text_full,
                  "content_b64": doc.content_b64, "fields": deepcopy(doc.fields)}
        res = {"classified": [parsed], "extracted": intake.aggregate_fields([parsed])}
        with m.SessionLocal() as other:
            current = other.get(models.DocumentAsset, document_id)
            service.write_document_fields(other, current, base_fields=deepcopy(current.fields),
                                          new_fields=latest, source="concurrent_reprocess")
            other.commit()
        applied = m._apply_intake_autofill(db, case, res, snapshot)
        db.commit()
        db.expire_all()
        expected = latest.get("product_names", [])
        assert [p.name for p in db.query(models.Product).filter_by(case_id=case_id)] == expected
        assert res["extracted"]["products"] == expected
        assert parsed["fields"].get("product_names", []) == expected
        assert applied["products"] == len(expected)


def test_intake_requires_a_snapshot_before_any_database_or_case_changes():
    with pytest.raises(TypeError, match="base_fields_by_id"):
        m._apply_intake_autofill(None, None, {})
    with pytest.raises(ValueError, match="snapshot"):
        m._apply_intake_autofill(None, None, {}, None)


def test_intake_processors_use_the_final_fields_for_repeated_documents(document):
    case_id, document_id = document
    with m.SessionLocal() as db:
        doc, case = db.get(models.DocumentAsset, document_id), db.get(models.CaseApplication, case_id)
        doc.doc_type, doc.fields = "halal_certificate", {"cert_no": "Old certificate"}
        doc.file_hash = m._sha256_b64(doc.content_b64)
        db.commit()
        snapshot = {document_id: deepcopy(doc.fields)}
        first = {"filename": doc.filename, "doc_type": doc.doc_type, "text_full": doc.text_full,
                 "content_b64": doc.content_b64, "fields": deepcopy(doc.fields)}
        second = {**first, "fields": {"cert_no": "New certificate"}}
        res = {"classified": [first, second], "extracted": intake.aggregate_fields([first, second])}
        m._apply_intake_autofill(db, case, res, snapshot)
        db.commit()
        assert [row.certificate_no for row in db.query(models.HalalCertificate).filter_by(case_id=case_id)] == [
            "New certificate"]
        assert [row["cert_no"] for row in res["extracted"]["certificates"]] == ["New certificate"]
        assert all(row["fields"]["cert_no"] == "New certificate" for row in res["classified"])


@pytest.mark.parametrize("route", ["reprocess", "intake", "profile"])
def test_merged_aggregation_does_not_repeat_address_lookup_under_lock(document, monkeypatch, route):
    case_id, document_id = document
    text = "Business registration\nAddress: Seoul\nFactory: Busan"
    fields = {"address": "Seoul", "factory_address": "Busan", "city": "Seoul", "country": "Korea"}
    parsed = {"doc_type": "nib_business_license", "confidence": .95, "fields": fields,
              "filename": "writer.txt", "text_full": text,
              "content_b64": base64.b64encode(text.encode()).decode()}
    monkeypatch.setattr(intake, "parse_file", lambda *a, **k: text)
    monkeypatch.setattr(intake, "classify", lambda *a: deepcopy(parsed))
    monkeypatch.setattr(intake, "parse_typed_text", lambda *a: deepcopy(parsed))
    monkeypatch.setattr(m, "_notify_package_gaps", lambda *a: None)
    monkeypatch.setattr(intake, "_ZIP_LOOKUP", True)
    calls = []
    with m.SessionLocal() as db:
        def lookup(address):
            assert not db.connection().connection.driver_connection.in_transaction
            calls.append(address)
            return "12345"

        monkeypatch.setattr(intake, "_lookup_zip", lookup)
        doc, case = db.get(models.DocumentAsset, document_id), db.get(models.CaseApplication, case_id)
        doc.doc_type, doc.fields = "nib_business_license", {}
        doc.file_hash = m._sha256_b64(parsed["content_b64"])
        db.commit()
        if route == "reprocess":
            m._reprocess_doc(db, doc, case)
        elif route == "intake":
            res = {"classified": [deepcopy(parsed)], "extracted": intake.aggregate_fields([parsed])}
            m._apply_intake_autofill(db, case, res, {document_id: {}})
        else:
            m.autofill_profile(case_id, user={"uid": "admin1", "role": "admin"}, db=db)
        assert calls == ["Seoul, Korea", "Busan"]
        assert doc.fields["address"] == "Seoul"
        assert "zip" not in doc.fields


def test_profile_response_is_aggregated_from_the_committed_merge(document, monkeypatch):
    case_id, document_id = document
    original = {"company_name": "Old company", "responsible_person": "Old person"}
    latest = {"company_name": "Latest company", "responsible_person": "Latest person", "address": "Latest address"}
    monkeypatch.setattr(intake, "classify", lambda *a: {
        "doc_type": "nib_business_license", "confidence": .95, "fields": deepcopy(original)})
    with m.SessionLocal() as db:
        doc = db.get(models.DocumentAsset, document_id)
        doc.doc_type, doc.fields = "nib_business_license", deepcopy(original)
        db.commit()

        def concurrent_update(*args, **kwargs):
            with m.SessionLocal() as other:
                current = other.get(models.DocumentAsset, document_id)
                service.write_document_fields(other, current, base_fields=deepcopy(current.fields),
                                              new_fields=latest, source="concurrent_reprocess")
                other.commit()
            return "Readable text"

        monkeypatch.setattr(intake, "parse_file", concurrent_update)
        result = m.autofill_profile(case_id, user={"uid": "admin1", "role": "admin"}, db=db)
        db.expire_all()
        assert {key: result["extracted"][key] for key in latest} == latest
        assert {key: doc.fields[key] for key in latest} == latest
