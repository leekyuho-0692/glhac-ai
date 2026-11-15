"""dict.get()의 중첩 반환값과 SQL 쿼리 체이닝을 구분하는 회귀 검사."""
import ast

import pytest

from scripts.write_guard import bulk_write_violations


@pytest.mark.parametrize("model,key", [("CaseApplication", "nib"), ("DocumentAsset", "fields")])
@pytest.mark.parametrize("selection", [
    "payload.get('case')", "payload.get('case', {})",
    "payload.get('case', {}).copy()", "dict(payload.get('case'))",
    "{**payload.get('case', {})}",
])
@pytest.mark.parametrize("write", ["query.update(VALUE)", "update(MODEL).values(**VALUE)"])
def test_get_nested_protected_mapping_is_detected(model, key, selection, write):
    source = (f"query = db.query({model})\n"
              f"payload = {{'case': {{{key!r}: '123'}}}}\n"
              + write.replace("MODEL", model).replace("VALUE", selection))
    assert bulk_write_violations(ast.parse(source), model, {key}) == [3]


@pytest.mark.parametrize("model,key", [("CaseApplication", "nib"), ("DocumentAsset", "fields")])
@pytest.mark.parametrize("selection", ["payload.get('case')", "payload.get('case', {})",
                                       "payload.get('case', {}).copy()"])
def test_get_does_not_inherit_protected_metadata_keys(model, key, selection):
    source = (f"query = db.query({model})\n"
              f"payload = {{'case': {{'status': 'cancelled'}}, {key!r}: 'metadata'}}\n"
              f"query.update({selection})")
    assert bulk_write_violations(ast.parse(source), model, {key}) == []


@pytest.mark.parametrize("setup,selection", [
    ("payload = load_payload()", "payload.get('case', {})"),
    ("payload = {'case': load_values()}", "payload.get('case')"),
    ("payload = {'status': 'metadata'}", "payload.get(dynamic_key, {})"),
    ("payload = {'case': {'status': 'cancelled'}}; payload.update(external)",
     "payload.get('case', {}).copy()"),
    ("payload = {'case': {'status': 'cancelled'}}; payload[dynamic_key] = external",
     "payload.get('case')"),
    ("payload = {'case': {'status': 'cancelled'}, **external}", "payload.get('case')"),
    ("payload = {}", "payload.get('missing', {'nib': '123'})"),
    ("payload = {'case': {'status': 'cancelled'}}; del payload['case']",
     "payload.get('case', {'nib': '123'})"),
    ("payload = {'case': {'status': 'cancelled'}}; payload.clear()",
     "payload.get('case', {'nib': '123'})"),
    ("payload = {'case': {'status': 'cancelled'}}; payload.pop('case')",
     "payload.get('case', {'nib': '123'})"),
])
def test_get_unknown_values_and_protected_defaults_are_conservative(setup, selection):
    source = f"query = db.query(CaseApplication)\n{setup}\nquery.update({selection})"
    assert bulk_write_violations(ast.parse(source), "CaseApplication", {"nib"}) == [3]


@pytest.mark.parametrize("setup", [
    "payload = {}; payload['case'] = {'nib': '123'}",
    "payload = {}; payload.update(case={'nib': '123'})",
    "payload = {}; payload |= {'case': {'nib': '123'}}",
    "payload = {}; payload.setdefault('case', {'nib': '123'})",
    "payload = {}; payload.__setitem__('case', {'nib': '123'})",
    "payload = {'case': {}}; alias = payload.get('case'); alias['nib'] = '123'",
    "payload = {'case': {}}; payload['case']['nib'] = '123'",
    "payload = {}; payload.setdefault('case', {}).update(nib='123')",
])
def test_get_tracks_nested_assignment_and_mutation(setup):
    source = f"query = db.query(CaseApplication)\n{setup}\nquery.update(payload.get('case'))"
    assert bulk_write_violations(ast.parse(source), "CaseApplication", {"nib"}) == [3]


@pytest.mark.parametrize("source", [
    "payload = {}; db.query(CaseApplication).update(payload.get('missing', {'status': 'cancelled'}))",
    "payload = {**external, 'case': {'status': 'cancelled'}}; "
    "db.query(CaseApplication).update(payload.get('case'))",
    "payload = {'case': {'nib': '123'}}; db.query(User).update(payload.get('case'))",
    "payload = {'case': {'nib': '123'}}; local = {}; local.update(payload.get('case'))",
    "payload = {'case': {}}; copied = payload.get('case').copy(); "
    "payload.get('case')['nib'] = '123'; db.query(CaseApplication).update(copied)",
    "from copy import deepcopy; payload = {'case': {}}; copied = deepcopy(payload); "
    "payload.get('case')['nib'] = '123'; db.query(CaseApplication).update(copied.get('case'))",
])
def test_get_preserves_safe_defaults_copies_and_model_scope(source):
    assert bulk_write_violations(ast.parse(source), "CaseApplication", {"nib"}) == []


@pytest.mark.parametrize("source", [
    "db.query(CaseApplication).filter_by(case_id='id').update({'nib': '123'})",
    "update(CaseApplication).where(CaseApplication.case_id == 'id').values(nib='123')",
])
def test_query_chaining_keeps_model_target(source):
    assert bulk_write_violations(ast.parse(source), "CaseApplication", {"nib"}) == [1]
