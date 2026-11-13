"""운영 패키지 회귀: 실제 추출→집계→재처리/업로드 저장 경로, 외부 AI는 고정 응답."""
import base64
import copy
import io
import json
from time import perf_counter
import zipfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import app.main as m
from app import intake, models

KO = '닥토필 프로페셔널 포 페이스'
EN = 'DactorPeel Professional for Face'
PAIR_TEXT = f'Product list\n제품명(국문): {KO}\nProduct name (English): {EN}\n'
NAMES = ['Water', 'Alcohol Denat.', 'Glycerin', 'Butylene Glycol', 'Propanediol',
         'Betaine', 'Niacinamide', 'Panthenol', 'Allantoin', 'Sodium Hyaluronate',
         'Carbomer', 'Arginine', 'Citric Acid', 'Sodium Citrate', 'Disodium EDTA',
         'Ethylhexylglycerin', 'Caprylyl Glycol', 'Pentylene Glycol', 'Sodium PCA',
         'Trehalose', 'Glucose', 'Fructose', 'Sucrose', '1,2-Hexanediol', 'Lactobacillus Ferment Lysate',
         'Sodium Lactate', 'Lactic Acid', 'Adenosine', 'Tocopherol', 'Silica']


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(m, '_AUTOPARSE', False)
    monkeypatch.setattr(m, 'verify_enqueue', lambda *a, **kw: False)
    monkeypatch.setattr(intake, '_ZIP_LOOKUP', False)
    monkeypatch.setattr(intake.ai_local, 'provider', lambda: 'openai')
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *a, **kw: pytest.fail('가짜 AI 응답 필요'))


@pytest.fixture
def case():
    with m.SessionLocal() as db:
        c = models.CaseApplication(org_id='org_demo', company_name='PT Package Regression')
        db.add(c)
        db.commit()
        yield db, c


def reply(monkeypatch, kind, fields):
    monkeypatch.setattr(intake.ai_local, 'llm_json', lambda *a, **kw: {
        'doc_type': kind, 'confidence': .95, 'fields': copy.deepcopy(fields)})


def reprocess(case, monkeypatch, text, fields, kind='coa_msds', doc=None, apply=True):
    db, c = case
    reply(monkeypatch, kind, fields)
    if doc is None:
        doc = models.DocumentAsset(case_id=c.case_id, filename=('MSDS.txt' if kind=='coa_msds' else 'product_list.txt'),
            doc_type=kind, content_b64=base64.b64encode(text.encode()).decode())
        db.add(doc)
        db.commit()
    result = m._reprocess_doc(db, doc, c, apply=apply)
    return doc, result


def names(db, c, model):
    return [row.name for row in db.query(model).filter_by(case_id=c.case_id)]


@pytest.mark.parametrize('source_name', [None, EN], ids=['missing-product', 'matching-product'])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_msds_30_registers_12_and_defers_18_with_explicit_unmatched_names(case, monkeypatch, source_name):
    db, c = case
    db.add(models.Product(case_id=c.case_id, name=EN))
    db.commit()
    text = 'Material Safety Data Sheet\nComposition / Ingredients\n'+'\n'.join(name + ' | 1%' for name in NAMES)
    if source_name:
        text = f'Product name: {source_name}\n' + text
    doc, result = reprocess(case, monkeypatch, text, {'material_names': NAMES})
    meta = doc.fields['_field_meta']['material_names']
    assert meta['accepted'] and meta['checks']['in_source']
    assert meta['review']  # 인용 누락 review 자체가 전체 성분을 차단해서는 안 된다.
    safe = {
        'Water', 'Butylene Glycol', 'Niacinamide', 'Panthenol', 'Arginine',
        'Citric Acid', 'Sodium Citrate', 'Glucose', 'Fructose', 'Sucrose',
        'Sodium Lactate', 'Lactic Acid',
    }
    newly_pending = {
        'Propanediol', 'Betaine', 'Carbomer', 'Disodium EDTA', 'Caprylyl Glycol',
        'Pentylene Glycol', 'Sodium PCA', 'Trehalose', '1,2-Hexanediol', 'Adenosine', 'Silica',
    }
    assert set(names(db,c,models.Material)) == safe
    assert result['applied']['materials'] == 12
    pending = m._pending_extractions(db,c.case_id)
    assert len(pending) == 18
    assert all(x['kind']=='material_names' for x in pending)
    assert {x['value'] for x in pending} == {
        'Alcohol Denat.', 'Glycerin', 'Lactobacillus Ferment Lysate',
        'Allantoin', 'Tocopherol', 'Ethylhexylglycerin', 'Sodium Hyaluronate',
    } | newly_pending
    assert {x['value'] for x in pending
            if m.screening.screen_merged(x['value'])['decision_by'] == 'unmatched_default'} == newly_pending
    expected_links = 12 if source_name else 0
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == expected_links
    assert m._package_check(db,c)['items']['material']['ok']
    before = {x.material_id for x in db.query(models.Material).filter_by(case_id=c.case_id)}
    _, again = reprocess(case,monkeypatch,text,{'material_names':NAMES},doc=doc)
    assert again['applied']['materials'] == 0
    assert {x.material_id for x in db.query(models.Material).filter_by(case_id=c.case_id)} == before
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == expected_links


@pytest.mark.parametrize('kind',['coa_msds','material_list'])
def test_manual_material_and_normalized_duplicate_survive(case,monkeypatch,kind):
    db,c=case
    manual=models.Material(case_id=c.case_id,name='WATER',supplier='Human supplier',note='Confirmed',screen_result='CLEARED')
    db.add(manual);db.commit();mid=manual.material_id
    text='Composition / Ingredients\nWater | 85%\nGlycerin | 5%\nAlcohol Denat. | 10%'
    reprocess(case,monkeypatch,text,{'material_names':['Water','Glycerin','Alcohol Denat.']},kind=kind)
    db.refresh(manual)
    assert (manual.material_id,manual.name,manual.supplier,manual.note,manual.screen_result)==(mid,'WATER','Human supplier','Confirmed','CLEARED')
    assert names(db,c,models.Material)==['WATER']
    assert {'Glycerin', 'Alcohol Denat.'} <= {x['value'] for x in m._pending_extractions(db,c.case_id)}


def test_coa_test_rows_and_absent_names_never_become_materials(case,monkeypatch):
    db,c=case
    text='Certificate of Analysis\nParameter | Specification | Result\nEthanol | <0.5% | 0.02%\nViscosity | 100 | 80\n\nIngredients\nWater | 95%\nGlycerin | 5%'
    reprocess(case,monkeypatch,text,{'material_names':['Water','Glycerin','Ethanol','Viscosity']})
    assert names(db,c,models.Material)==['Water']
    assert {x['value'] for x in m._pending_extractions(db,c.case_id)}=={'Glycerin','Ethanol','Viscosity'}
    reprocess(case,monkeypatch,'Ingredients\nWater | 100%',{'material_names':['Invented ingredient']})
    assert names(db,c,models.Material)==['Water']


def test_ingredient_context_has_priority_over_measurement_tokens(case,monkeypatch):
    db,c=case
    reprocess(case,monkeypatch,'Ingredients\nWater | 80%\nGlycerin | 5%\nAlcohol Denat. | 10%',
              {'material_names':['Water','Glycerin','Alcohol Denat.']})
    assert names(db,c,models.Material)==['Water']
    assert m._pending_extractions(db,c.case_id)


@pytest.mark.parametrize('apply,frozen',[(False,False),(True,True)])
def test_apply_false_and_frozen_scope_do_not_register(case,monkeypatch,apply,frozen):
    db,c=case;c.scope_frozen=frozen;db.commit()
    reprocess(case,monkeypatch,'Ingredients\nWater | 100%',{'material_names':['Water']},apply=apply)
    assert not names(db,c,models.Material)
    pending=m._pending_extractions(db,c.case_id)
    assert any(x['value']=='Water' and x['requires_scope_unlock']==frozen for x in pending)


def test_reviewer_renames_and_dismissals_are_not_resurrected(case,monkeypatch):
    db,c=case
    db.add(models.Material(case_id=c.case_id,name='Purified Water',note='Reviewer confirmed'))
    m.sm.record_event(db,c,c.status,c.status,'extraction.review','auditor','auditor1',
        {'items':[{'kind':'material_names','value':'Water'},{'kind':'material_names','value':'Glycerin'}],
         'renames':{'Water':'Purified Water'},'dismiss':['Glycerin'],'reason':'Reviewed source'})
    db.commit()
    reprocess(case,monkeypatch,'Ingredients\nWater | 90%\nGlycerin | 5%\nSalt | 5%',{'material_names':['Water','Glycerin','Salt']})
    assert set(names(db,c,models.Material))=={'Purified Water','Salt'}
    assert not m._pending_extractions(db,c.case_id)


def test_reprocess_does_not_invent_multiple_product_links(case,monkeypatch):
    db,c=case
    db.add_all([models.Product(case_id=c.case_id,name='Face Serum'),models.Product(case_id=c.case_id,name='Body Serum')])
    db.commit()
    reprocess(case,monkeypatch,'Ingredients\nWater | 100%',{'material_names':['Water']})
    assert names(db,c,models.Material)==['Water']
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count()==0


def test_bilingual_product_pair_is_one_product_with_persisted_alias(case,monkeypatch):
    db,c=case
    fields={'product_names':[KO,EN], 'product_name_ko':KO, 'product_name_en':EN}
    doc,result=reprocess(case,monkeypatch,PAIR_TEXT,fields,kind='product_list')
    products=db.query(models.Product).filter_by(case_id=c.case_id).all()
    assert len(products)==1
    assert products[0].name==KO
    assert (c.profile_ext or {})['product_aliases'][products[0].product_id]==[EN]
    assert result['applied']['products']==1
    reprocess(case,monkeypatch,f'Material Safety Data Sheet\nProduct name: {EN}',{'product_names':[EN]})
    assert len(names(db,c,models.Product))==1
    assert not [x for x in m._pending_extractions(db,c.case_id) if x['kind']=='product_names']
    reprocess(case,monkeypatch,PAIR_TEXT,fields,kind='product_list',doc=doc)
    assert len(names(db,c,models.Product))==1


@pytest.mark.parametrize('second_kind',['product_list','coa_msds'])
def test_separate_document_language_variant_is_pending_not_merged_or_created(case,monkeypatch,second_kind):
    db,c=case
    reprocess(case,monkeypatch,'Product list\n'+KO,{'product_names':[KO]},kind='product_list')
    reprocess(case,monkeypatch,'Product name: '+EN,{'product_names':[EN]},kind=second_kind)
    assert names(db,c,models.Product)==[KO]
    pending=m._pending_extractions(db,c.case_id)
    assert len(pending)==1 and pending[0]['value']==EN
    assert pending[0]['reason']=='possible_same_product'
    assert not (c.profile_ext or {}).get('product_aliases')


def test_earlier_msds_name_is_marked_when_korean_product_arrives_later(case,monkeypatch):
    db,c=case
    reprocess(case,monkeypatch,'Product name: '+EN,{'product_names':[EN]})
    reprocess(case,monkeypatch,'Product list\n'+KO,{'product_names':[KO]},kind='product_list')
    assert names(db,c,models.Product)==[KO]
    pending=m._pending_extractions(db,c.case_id)
    assert len(pending)==1 and pending[0]['value']==EN
    assert pending[0]['reason']=='possible_same_product'


@pytest.mark.parametrize('existing',[EN,KO])
def test_pair_keeps_existing_human_product_name(case,monkeypatch,existing):
    db,c=case
    p=models.Product(case_id=c.case_id,name=existing,description='Human description')
    db.add(p);db.commit();pid=p.product_id
    reprocess(case,monkeypatch,PAIR_TEXT,{'product_names':[KO,EN]},kind='product_list')
    assert names(db,c,models.Product)==[existing]
    db.refresh(p)
    assert p.product_id==pid and p.description=='Human description'
    assert c.profile_ext['product_aliases'][pid]==[KO if existing==EN else EN]


def test_already_distinct_products_are_never_merged(case,monkeypatch):
    db,c=case
    db.add_all([models.Product(case_id=c.case_id,name=KO),models.Product(case_id=c.case_id,name=EN)])
    db.commit()
    before={p.product_id for p in db.query(models.Product).filter_by(case_id=c.case_id)}
    reprocess(case,monkeypatch,PAIR_TEXT,{'product_names':[KO,EN]},kind='product_list')
    assert {p.product_id for p in db.query(models.Product).filter_by(case_id=c.case_id)}==before
    assert not (c.profile_ext or {}).get('product_aliases')


@pytest.mark.parametrize('text',[f'제품명: {KO} ({EN})',f'Product name: {EN} / {KO}'])
def test_inline_bilingual_product_names_need_no_new_ai_field(case,monkeypatch,text):
    db,c=case
    reprocess(case,monkeypatch,text,{'product_names':[KO,EN]},kind='product_list')
    assert names(db,c,models.Product)==[KO]


def test_multiple_explicit_pairs_in_one_document_keep_products_separate(case,monkeypatch):
    db,c=case
    body_ko,body_en='닥토필 프로페셔널 포 바디','DactorPeel Professional for Body'
    text=PAIR_TEXT+f'제품명(국문): {body_ko}\nProduct name (English): {body_en}\n'
    reprocess(case,monkeypatch,text,{'product_names':[KO,EN,body_ko,body_en]},kind='product_list')
    products=db.query(models.Product).filter_by(case_id=c.case_id).all()
    assert {p.name for p in products}=={KO,body_ko}
    assert {p.name:c.profile_ext['product_aliases'][p.product_id] for p in products}=={KO:[EN],body_ko:[body_en]}


def test_ambiguous_pairing_does_not_merge_different_products(case,monkeypatch):
    db,c=case
    text=PAIR_TEXT+f'\n제품명(국문): {KO}\nProduct name (English): Different Body Serum\n'
    reprocess(case,monkeypatch,text,{'product_names':[KO,EN,'Different Body Serum']},kind='product_list')
    assert not names(db,c,models.Product)
    assert not (c.profile_ext or {}).get('product_aliases')
    pending = m._pending_extractions(db,c.case_id)
    assert {x['value'] for x in pending} == {KO, EN, 'Different Body Serum'}
    assert all(x['reason'] == 'possible_same_product' for x in pending)


def test_distinct_document_names_in_one_upload_stay_pending(case,monkeypatch):
    db,c=case
    docs=[{'doc_type':'product_list','filename':filename,'text_full':text,
           'fields':{'product_names':[name]},'confidence':.95}
          for filename,name,text in [('ko.txt',KO,'Product name: '+KO),('en.txt',EN,'Product name: '+EN)]]
    m._apply_intake_autofill(db,c,{'classified':docs,'extracted':intake.aggregate_fields(docs)})
    db.commit()
    assert names(db,c,models.Product)==[KO]
    assert any(x['value']==EN and x['reason']=='possible_same_product' for x in m._pending_extractions(db,c.case_id))


def test_bilingual_pair_cannot_bypass_structural_rejection():
    agg=intake.aggregate_fields([{'doc_type':'product_list','text_full':PAIR_TEXT,
        'fields':{'product_names':[KO,EN],'_sources':{'product_names':'structural_reject'}}}])
    assert agg['products']==[] and agg['product_aliases']==[]


def test_ai_autofill_never_registers_a_material_absent_from_source():
    agg=intake.aggregate_fields([{'doc_type':'material_list','text_full':'Ingredients\nWater | 100%',
        'fields':{'material_names':['Glycerin'],'_sources':{'material_names':'ai'}}}])
    assert agg['materials']==[]


def test_part_of_another_ingredient_name_is_not_ai_source_evidence():
    agg=intake.aggregate_fields([{'doc_type':'material_list','text_full':'Ingredients\nEthylhexylglycerin',
        'fields':{'material_names':['Glycerin'],'_sources':{'material_names':'ai'}}}])
    assert agg['materials']==[]


@pytest.mark.parametrize('text,fields',[
    (f'Product list\n{KO}\nUnrelated Serum',{'product_names':[KO,'Unrelated Serum']}),
    (f'Product list\n{KO}\n{EN}',{'product_names':[KO,EN]}),
    (PAIR_TEXT.replace(EN,'Unrelated Serum'),{'product_names':[KO,EN]}),
])
def test_same_document_does_not_merge_by_language_alone(case,monkeypatch,text,fields):
    db,c=case
    reprocess(case,monkeypatch,text,fields,kind='product_list')
    assert not (c.profile_ext or {}).get('product_aliases')
    if EN not in fields['product_names'] or EN in text:
        assert len(names(db,c,models.Product))==2


@pytest.mark.parametrize('route',['parse-file','intake-zip'])
def test_upload_routes_share_material_and_alias_rules(case,monkeypatch,route):
    db,c=case
    text=PAIR_TEXT+'\nIngredients\nWater | 85%\nGlycerin | 5%\nAlcohol Denat. | 10%'
    fields={'product_names':[KO,EN], 'product_name_ko':KO, 'product_name_en':EN,
            'material_names':['Water','Glycerin','Alcohol Denat.']}
    reply(monkeypatch,'material_list',fields)
    with TestClient(m.app) as client:
        token=client.post('/auth/login',json={'username':'applicant1','password':'pw'}).json()['token']
        raw=text.encode()
        if route=='intake-zip':
            out=io.BytesIO()
            with zipfile.ZipFile(out,'w') as archive:archive.writestr('ingredients.txt',raw)
            body={'zip_b64':base64.b64encode(out.getvalue()).decode()}
        else:body={'doc_type':'material_list','filename':'ingredients.txt','file_b64':base64.b64encode(raw).decode()}
        response=client.post(f'/cases/{c.case_id}/{route}',headers={'Authorization':'Bearer '+token},json=body)
        assert response.status_code==200,response.text
    db.expire_all()
    assert names(db,c,models.Material)==['Water']
    assert names(db,c,models.Product)==[KO]
    product=db.query(models.Product).filter_by(case_id=c.case_id).one()
    assert c.profile_ext['product_aliases'][product.product_id]==[EN]


@pytest.mark.parametrize('name', ['Prosciutto', 'Vodka'])
def test_p1_1_risky_test_row_survives_and_blocks_http(case, monkeypatch, name):
    db, c = case
    risk = m.screening.screen_merged(name)
    assert (risk['status'], risk['severity'], risk['matched_uid']) == ('haram', 'high', None)
    c.status, c.pathway = 'self_declaration_submitted', 'reguler'
    db.commit()
    reprocess(case, monkeypatch, f'Material Safety Data Sheet\nParameter | Result\n{name} | Present',
              {'material_names': [name]})
    assert not names(db, c, models.Material)
    assert name in {x['value'] for x in m._pending_extractions(db, c.case_id)}
    with TestClient(m.app) as client:
        token = client.post('/auth/login', json={'username': 'admin', 'password': 'admin'}).json()['token']
        response = client.post(f'/cases/{c.case_id}/transition',
                               headers={'Authorization': 'Bearer ' + token},
                               json={'to_state': 'committee_verification'})
    assert response.status_code == 409, response.text
    assert any(x['code'] == 'PENDING_CRITICAL_MATERIAL' and name in x['materials']
               for x in response.json()['detail']['blockers'])
    db.refresh(c)
    assert c.status == 'self_declaration_submitted'


@pytest.mark.parametrize('name', ['Prosciutto', 'Vodka', 'Glycerin', 'Allantoin'])
def test_p1_1_explicit_screening_risks_stay_pending(case, name):
    db, c = case
    risk = m.screening.screen_merged(name)
    assert (risk['status'], risk['severity']) != ('halal', 'low')
    docs = [{'doc_type': 'material_list', 'filename': 'ingredients.txt',
             'text_full': f'Ingredients\nWater | 90%\n{name} | 10%',
             'fields': {'material_names': ['Water', name], '_sources': {'material_names': 'ai'}}}]
    m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
    db.commit()
    assert names(db, c, models.Material) == ['Water']
    assert name in {x['value'] for x in m._pending_extractions(db, c.case_id)}


@pytest.mark.parametrize('text,candidates', [
    ('Certificate of Analysis\nTest results\npH: 6.5\nViscosity: 100 cP', ['pH', 'Viscosity']),
    ('Certificate of Analysis\nTest items | Specifications | Results\npH | 6-7 | 6.5\nViscosity | 100 | 100 cP',
     ['pH', 'Viscosity']),
    ('Ingredients\nWater | 100%\nTest items | Specifications | Results\npH | 6-7 | 6.5\nViscosity | 100 | 100 cP',
     ['pH', 'Viscosity']),
    ('Water | 100\nSilica | 10', ['Water', 'Silica']),
    ('Water is used to clean the laboratory.', ['Water']),
    ('This ingredient supplier uses Water for cleaning.', ['Water']),
])
def test_p1_2_non_ingredient_evidence_stays_pending(case, text, candidates):
    db, c = case
    docs = [{'doc_type': 'coa_msds', 'filename': 'coa.txt', 'text_full': text,
             'fields': {'material_names': candidates, '_sources': {'material_names': 'ai'}}}]
    m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
    db.commit()
    assert not names(db, c, models.Material)
    assert set(candidates) <= {x['value'] for x in m._pending_extractions(db, c.case_id)}


@pytest.mark.parametrize('header', ['Ingredients', 'Composition', '성분', '전성분', '원료명'])
def test_p1_2_explicit_ingredient_headers_allow_safe_materials(case, monkeypatch, header):
    db, c = case
    reprocess(case, monkeypatch, header + '\nWater | 90%\nSalt | 10%', {'material_names': ['Water', 'Salt']})
    assert set(names(db, c, models.Material)) == {'Water', 'Salt'}
    assert not m._pending_extractions(db, c.case_id)


@pytest.mark.parametrize('action', ['delete', 'rename'])
@pytest.mark.parametrize('origin', ['auto', 'manual'])
def test_p1_3_user_material_edits_are_respected_on_reprocessing(case, monkeypatch, action, origin):
    db, c = case
    user = {'role': 'applicant', 'uid': 'applicant1', 'org_id': 'org_demo'}
    if origin == 'manual':
        m.add_material(c.case_id, m.schemas.MaterialCreate(name='Water'), user=user, db=db)
    doc, _ = reprocess(case, monkeypatch, 'Ingredients\nWater | 100%', {'material_names': ['Water']})
    material = db.query(models.Material).filter_by(case_id=c.case_id).one()
    mid = material.material_id
    if action == 'delete':
        m.delete_material(mid, user=user, db=db)
        assert db.query(models.AuditLog).filter_by(case_id=c.case_id, action='material.delete').one().meta['name'] == 'Water'
    else:
        m.rename_material(mid, m.schemas.MaterialRenameReq(name='Purified Water'), user=user, db=db)
        assert db.query(models.WorkflowEvent).filter_by(case_id=c.case_id, action='material.rename').one().payload['from'] == 'Water'
    for candidate in ('Water', 'WATER'):
        _, result = reprocess(case, monkeypatch, 'Ingredients\nWater | 100%', {'material_names': [candidate]}, doc=doc)
        assert result['applied']['materials'] == 0
        assert names(db, c, models.Material) == ([] if action == 'delete' else ['Purified Water'])
    if action == 'rename':
        assert db.query(models.Material).filter_by(case_id=c.case_id).one().material_id == mid


@pytest.mark.parametrize('action', ['delete', 'rename'])
def test_p1_3_other_case_history_does_not_block_autofill(case, monkeypatch, action):
    db, c = case
    other = models.CaseApplication(org_id='org_demo', company_name='Other case')
    db.add(other)
    db.commit()
    user = {'role': 'applicant', 'uid': 'applicant1', 'org_id': 'org_demo'}
    added = m.add_material(other.case_id, m.schemas.MaterialCreate(name='Water'), user=user, db=db)
    if action == 'delete':
        m.delete_material(added['material_id'], user=user, db=db)
    else:
        m.rename_material(added['material_id'], m.schemas.MaterialRenameReq(name='Purified Water'), user=user, db=db)
    reprocess(case, monkeypatch, 'Ingredients\nWater | 100%', {'material_names': ['Water']})
    assert names(db, c, models.Material) == ['Water']


def paired_document(ko, en, filename):
    return {'doc_type': 'product_list', 'filename': filename,
            'text_full': f'제품명: {ko} / {en}', 'fields': {'product_names': [ko, en]}}


@pytest.mark.parametrize('existing', [None, '얼굴용 세럼', 'Shared Serum'])
@pytest.mark.parametrize('reverse', [False, True])
def test_p1_4_conflicting_aliases_across_documents_stay_pending(case, existing, reverse):
    db, c = case
    if existing:
        db.add(models.Product(case_id=c.case_id, name=existing))
        db.commit()
    docs = [paired_document('얼굴용 세럼', 'Shared Serum', 'face.txt'),
            paired_document('바디용 세럼', 'Shared Serum', 'body.txt')]
    if reverse:
        docs.reverse()
    m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
    db.commit()
    assert names(db, c, models.Product) == ([existing] if existing else [])
    assert not (c.profile_ext or {}).get('product_aliases')
    pending = m._pending_extractions(db, c.case_id)
    assert {x['value'] for x in pending} == {'얼굴용 세럼', '바디용 세럼', 'Shared Serum'}
    assert all(x['reason'] == 'possible_same_product' for x in pending)


@pytest.mark.parametrize('reverse', [False, True])
def test_p1_4_later_conflicting_document_cannot_extend_existing_alias(case, reverse):
    db, c = case
    ko_names = ['얼굴용 세럼', '바디용 세럼']
    if reverse:
        ko_names.reverse()
    for i, ko in enumerate(ko_names):
        docs = [paired_document(ko, 'Shared Serum', f'{i}.txt')]
        m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
        db.commit()
    product = db.query(models.Product).filter_by(case_id=c.case_id).one()
    assert product.name == ko_names[0]
    assert c.profile_ext['product_aliases'][product.product_id] == ['Shared Serum']
    assert any(x['value'] == ko_names[1] and x['reason'] == 'possible_same_product'
               for x in m._pending_extractions(db, c.case_id))


def test_p1_4_repeated_identical_pair_is_not_a_conflict(case):
    db, c = case
    docs = [paired_document(KO, EN, f'{i}.txt') for i in range(2)]
    m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
    db.commit()
    product = db.query(models.Product).filter_by(case_id=c.case_id).one()
    assert product.name == KO
    assert c.profile_ext['product_aliases'][product.product_id] == [EN]
    assert not m._pending_extractions(db, c.case_id)


@pytest.mark.parametrize('kind', ['coa_msds', 'product_list'])
def test_p2_5_apply_false_keeps_product_candidates_pending(case, monkeypatch, kind):
    db, c = case
    reprocess(case, monkeypatch, PAIR_TEXT, {'product_names': [KO, EN]}, kind=kind, apply=False)
    assert not names(db, c, models.Product)
    assert {x['value'] for x in m._pending_extractions(db, c.case_id)} == {KO, EN}


@pytest.mark.parametrize('route', ['reprocess', 'preview', 'intake'])
def test_p2_6_reextract_clears_product_pending(case, monkeypatch, route):
    db, c = case
    db.add(models.Product(case_id=c.case_id, name=KO))
    db.commit()
    text = 'Product list\nFace Serum\nBody Lotion'
    doc, _ = reprocess(case, monkeypatch, text, {'product_names': ['Face Serum']}, kind='product_list')
    assert [x['value'] for x in doc.fields['_autofill']['product_pending']] == ['Face Serum']
    if route == 'intake':
        docs = [{'filename': doc.filename, 'doc_type': 'product_list', 'text_full': text,
                 'fields': {'product_names': ['Body Lotion']}}]
        m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
        db.commit()
    else:
        reprocess(case, monkeypatch, text, {'product_names': ['Body Lotion']},
                  kind='product_list', doc=doc, apply=route != 'preview')
    db.refresh(doc)
    assert 'Face Serum' not in {x['value'] for x in doc.fields['_autofill']['product_pending']}
    assert {x['value'] for x in m._pending_extractions(db, c.case_id)} == {'Body Lotion'}
    assert names(db, c, models.Product) == [KO]


def test_p2_6_stale_stored_pending_is_hidden_on_read(case):
    db, c = case
    db.add(models.DocumentAsset(case_id=c.case_id, filename='old.txt', doc_type='coa_msds',
        text_full='Product name: Body Lotion', fields={'product_names': ['Body Lotion'],
        '_autofill': {'product_pending': [{'kind': 'product_names', 'value': 'Face Serum',
                                         'reason': 'possible_same_product'}]}}))
    db.commit()
    assert {x['value'] for x in m._pending_extractions(db, c.case_id)} == {'Body Lotion'}


@pytest.mark.parametrize('stored_source', [False, True])
@pytest.mark.parametrize('text', ['', 'Test results\nProsciutto | Present'])
def test_structural_lists_bypass_source_and_screening_gates(monkeypatch, stored_source, text):
    def unexpected(*args, **kwargs):
        pytest.fail('구조 파서의 목록에는 AI 자동등록 게이트를 적용하면 안 된다')
    monkeypatch.setattr(intake, '_material_in_source', unexpected)
    monkeypatch.setattr(m.screening, 'screen_merged', unexpected)
    sources = {'material_names': 'structural', 'product_names': 'structural'}
    doc = {'doc_type': 'material_list', 'text_full': text,
           'fields': {'material_names': ['Prosciutto', 'Vodka', 'Unregistered Material'],
                      'product_names': ['Original Product']}}
    if stored_source:
        doc['fields']['_sources'] = sources
    else:
        doc['field_sources'] = sources
    agg = intake.aggregate_fields([doc])
    assert agg['materials'] == ['Prosciutto', 'Vodka', 'Unregistered Material']
    assert agg['products'] == ['Original Product']
    assert agg['pending'] == []


@pytest.mark.parametrize('source', [None, 'ai', 'structural'])
def test_product_list_names_keep_original_autoregistration(source):
    agg = intake.aggregate_fields([{'doc_type': 'product_list',
        'fields': {'product_names': ['Original Product'], '_sources': {'product_names': source}}}])
    assert agg['products'] == ['Original Product']
    assert agg['pending'] == []


# 075d114 감사 원재료 엑셀과 같은 50개 표기, 정규화 뒤 47종.
STRUCTURAL_NAMES = [
    'Tepung terigu', 'Gula pasir', 'Lemak babi', 'Gelatin sapi', 'Minyak babi', 'Angciu',
    'Arak masak', 'Rhum', 'Mirin', 'Kuas bulu babi', 'Etanol', 'Alkohol', 'Lard', 'L a r d', 'Lardd',
    'Pork fat', 'Porc', 'Gelatine', 'Gélatine', 'E120', 'E-120', 'E 120', 'E471', 'Karmin', 'Kochineal',
    'Darah', 'Babi', 'B4bi', 'Wine vinegar', 'Cuka anggur', 'Shortening', 'Emulsifier (E471)', 'Rum flavour',
    'Bacon flavour', 'Ham', 'Hamburger', 'Champagne', 'Sake', 'Tuak', 'Brem', 'Khamr', 'Collagen', 'Kolagen',
    'Plasma', 'L-cysteine', 'E920', 'Whey', 'Rennet', 'Pepsin', 'Lipase',
]


@pytest.mark.parametrize('route', ['parse-file', 'intake-zip', 'reprocess'])
def test_structural_xlsx_registers_47_and_keeps_existing_review_blockers(case, monkeypatch, route):
    db, c = case
    wb = Workbook()
    ws = wb.active
    ws.title = 'Daftar Bahan'
    ws.append(['Daftar Bahan Halal PT MAJU'])
    ws.append(['No', 'Nama Bahan', 'Jenis Bahan', 'Produsen', 'Negara', 'No. Sertifikat Halal'])
    for index, name in enumerate(STRUCTURAL_NAMES, 1):
        ws.append([index, name, 'Bahan', 'PT X', 'ID', ''])
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    raw = out.getvalue()
    # AI 단독 MSDS와 별개인 구조 파서 경로를 실제 엑셀로 검증한다.
    monkeypatch.setattr(intake.ai_local, 'provider', lambda: 'none')
    if route == 'reprocess':
        doc = models.DocumentAsset(case_id=c.case_id, filename='Daftar Bahan.xlsx',
            doc_type='material_list', content_b64=base64.b64encode(raw).decode())
        db.add(doc)
        db.commit()
        result = m._reprocess_doc(db, doc, c, apply=True)
        assert result['applied']['materials'] == 47
    else:
        with TestClient(m.app) as client:
            token = client.post('/auth/login', json={'username': 'applicant1', 'password': 'pw'}).json()['token']
            if route == 'intake-zip':
                archive_data = io.BytesIO()
                with zipfile.ZipFile(archive_data, 'w') as archive:
                    archive.writestr('Daftar Bahan.xlsx', raw)
                body = {'zip_b64': base64.b64encode(archive_data.getvalue()).decode()}
            else:
                body = {'doc_type': 'material_list', 'filename': 'Daftar Bahan.xlsx',
                        'file_b64': base64.b64encode(raw).decode()}
            response = client.post(f'/cases/{c.case_id}/{route}',
                                   headers={'Authorization': 'Bearer ' + token}, json=body)
            assert response.status_code == 200, response.text
            key = 'materials_added' if route == 'parse-file' else 'materials'
            assert response.json()['applied'][key] == 47
    db.expire_all()
    rows = db.query(models.Material).filter_by(case_id=c.case_id).all()
    expected = {models.normalize_material_name(name) for name in STRUCTURAL_NAMES}
    assert len(rows) == len(expected) == 47
    assert {row.normalized_name for row in rows} == expected
    docs = db.query(models.DocumentAsset).filter_by(case_id=c.case_id).all()
    assert any(doc.fields['_sources']['material_names'] == 'structural' for doc in docs)
    assert not expected & {models.normalize_material_name(item['value'])
                           for item in m._pending_extractions(db, c.case_id)}
    assert any(row.screen_status == 'haram' and row.screen_result == 'BLOCK' for row in rows)
    assert any(blocker['code'] == 'HARAM_MATERIAL' and blocker['materials']
               for blocker in m.sm.material_safety_blockers(db, c))


@pytest.mark.usefixtures('without_kma_catalogue')
def test_unregistered_ai_ingredient_stays_pending_with_existing_review_status(case, monkeypatch):
    db, c = case
    name = '1,2-Hexanediol'
    risk = m.screening.screen_merged(name)
    assert risk['matched_uid'] is None and risk['decision_by'] == 'unmatched_default'
    assert risk['status'] == 'mushbooh' and risk['v1_risk'] == risk['severity'] == 'low'
    _, result = reprocess(case, monkeypatch, f'Composition / Ingredients\n{name} | 1%', {'material_names': [name]})
    assert risk['result'] == 'NEEDS_EVIDENCE'
    assert result['applied']['materials'] == 0
    assert result['applied']['pending'] == 1
    assert not names(db, c, models.Material)
    assert [item['value'] for item in m._pending_extractions(db, c.case_id)] == [name]


@pytest.mark.parametrize('failure', [RuntimeError('screening unavailable'), None, {},
    {'result': 'UNKNOWN', 'status': {}, 'severity': 'low'},
    {'result': 'UNKNOWN', 'status': 'unknown', 'severity': None},
    {'result': 'UNKNOWN', 'status': 'unknown', 'severity': 'unavailable'},
    {'result': 'PASS', 'status': 'halal', 'severity': 'low', 'error': 'screening failed'},
])
def test_r5_screening_failures_keep_ai_candidates_pending(case, monkeypatch, failure):
    db, c = case
    def unavailable(*args, **kwargs):
        if isinstance(failure, Exception):
            raise failure
        return failure
    monkeypatch.setattr(m.screening, 'screen_merged', unavailable)
    doc = {'doc_type': 'coa_msds', 'filename': 'MSDS.txt', 'text_full': 'Ingredients\n1,2-Hexanediol | 1%',
           'fields': {'material_names': ['1,2-Hexanediol'], '_sources': {'material_names': 'ai'}}}
    agg = intake.aggregate_fields([doc])
    assert agg['materials'] == []
    assert [item['value'] for item in agg['pending']] == ['1,2-Hexanediol']
    m._apply_intake_autofill(db, c, {'classified': [doc], 'extracted': agg})
    db.commit()
    assert not names(db, c, models.Material)
    assert [item['value'] for item in m._pending_extractions(db, c.case_id)] == ['1,2-Hexanediol']


def test_r5_screening_exception_with_tail_risks_preserves_pending(case, monkeypatch):
    db, c = case
    def unavailable(*args, **kwargs):
        raise RuntimeError('screening unavailable')
    monkeypatch.setattr(m.screening, 'screen_merged', unavailable)
    db.add(models.DocumentAsset(case_id=c.case_id, filename='MSDS.txt', doc_type='coa_msds',
        text_full='Ingredients\nAlcohol Denat. | 10%', fields={'material_names': ['Alcohol Denat.'],
        '_tail_screening': {'risk_hits': [{'name': 'Ethanol', 'uid': 'ing.ethanol'}]}}))
    db.commit()
    assert {'Alcohol Denat.', 'Ethanol'} <= {item['value'] for item in m._pending_extractions(db, c.case_id)}
    assert not names(db, c, models.Material)


def test_r5_screening_exception_after_aggregation_defers_registration(case, monkeypatch):
    db, c = case
    doc = {'doc_type': 'coa_msds', 'filename': 'MSDS.txt', 'text_full': 'Ingredients\nWater | 100%',
           'fields': {'material_names': ['Water'], '_sources': {'material_names': 'ai'}}}
    agg = intake.aggregate_fields([doc])
    assert agg['materials'] == ['Water']
    def unavailable(*args, **kwargs):
        raise RuntimeError('screening unavailable during registration')
    monkeypatch.setattr(m.screening, 'screen_merged', unavailable)
    m._apply_intake_autofill(db, c, {'classified': [doc], 'extracted': agg})
    db.commit()
    assert not names(db, c, models.Material)
    assert [item['value'] for item in m._pending_extractions(db, c.case_id)] == ['Water']


@pytest.mark.parametrize('overrides', [
    {'status': 'haram'}, {'status': 'mushbooh'}, {'status': 'syubhat'},
    {'severity': 'high'}, {'severity': 'critical'}, {'result': 'BLOCK'},
    {'status': 'halal', 'v1_risk': 'medium'}, {'status': 'halal', 'v1_risk': 'high'},
    {'status': 'mushbooh', 'decision_by': 'unmatched_default', 'v1_risk': 'high'},
    {'status': 'mushbooh', 'decision_by': 'unmatched_default', 'matched_uid': 'ing.risk'},
])
def test_explicit_screening_risk_cannot_be_treated_as_unregistered(monkeypatch, overrides):
    risk = {'result': 'NEEDS_EVIDENCE', 'status': 'unknown', 'severity': 'low',
            'v1_risk': 'low', 'matched_uid': None, **overrides}
    monkeypatch.setattr(m.screening, 'screen_merged', lambda *a, **kw: risk)
    agg = intake.aggregate_fields([{'doc_type': 'coa_msds', 'text_full': 'Ingredients\nCandidate | 1%',
        'fields': {'material_names': ['Candidate'], '_sources': {'material_names': 'ai'}}}])
    assert agg['materials'] == []
    assert [item['value'] for item in agg['pending']] == ['Candidate']


def _round3_ingest(case, monkeypatch, route, text, candidates):
    db, c = case
    fields = {'material_names': candidates, '_sources': {'material_names': 'ai'}}
    if route == 'aggregate':
        doc = {'doc_type': 'coa_msds', 'filename': 'MSDS.txt', 'text_full': text, 'fields': fields}
        agg = intake.aggregate_fields([doc])
        applied = m._apply_intake_autofill(db, c, {'classified': [doc], 'extracted': agg})
        db.commit()
        return applied
    if route == 'reprocess':
        return reprocess(case, monkeypatch, text, fields)[1]['applied']
    reply(monkeypatch, 'coa_msds', {'material_names': candidates})
    with TestClient(m.app) as client:
        token = client.post('/auth/login', json={'username': 'applicant1', 'password': 'pw'}).json()['token']
        response = client.post(f'/cases/{c.case_id}/parse-file',
            headers={'Authorization': 'Bearer ' + token}, json={
                'doc_type': 'coa_msds', 'filename': 'MSDS.txt',
                'file_b64': base64.b64encode(text.encode()).decode()})
        assert response.status_code == 200, response.text
    db.expire_all()
    applied = response.json()['applied']
    return {'materials': applied['materials_added'], 'pending': applied['materials_pending']}


@pytest.mark.parametrize('route', ['aggregate', 'parse-file', 'reprocess'])
@pytest.mark.parametrize('text,candidates,registered', [
    ('Composition\nWater 100%\n9. Physical and chemical properties\npH: 6.5\nViscosity: 100 cP',
     ['Water', 'pH', 'Viscosity'], {'Water'}),
    *[(f'Composition\nSalt | 100%\n{heading}\nWater | 70%', ['Salt', 'Water'], {'Salt'}) for heading in (
        'SECTION 4: First aid measures', '4. 응급조치요령', '12. Arbitrary custom title',
        'SECTION 8 Exposure controls', '7 Other details', 'IV. Custom section',
        '(12) Arbitrary custom title', '[12] Arbitrary custom title', '12.1 Arbitrary custom title',
        'Physical and chemical properties', 'Handling and storage', '# Custom title',
        'Test items | Specifications | Results', 'Name | Result',
    )],
    ('Ingredient suppliers use Water for cleaning equipment.', ['Water'], set()),
    ('Ingredients\nIngredient suppliers use Water for cleaning equipment.', ['Water'], set()),
    ('Ingredients\nSalt | 100%\nWater is used for cleaning equipment.\nWater | 70%', ['Salt', 'Water'], {'Salt'}),
    ('Ingredients\nSalt | 100%\n\nIngredient suppliers use Water for cleaning equipment.\nWater | 70%',
     ['Salt', 'Water'], {'Salt'}),
])
def test_round3_only_ingredient_rows_autoregister(case, monkeypatch, route, text, candidates, registered):
    db, c = case
    applied = _round3_ingest(case, monkeypatch, route, 'Material Safety Data Sheet\n' + text, candidates)
    assert set(names(db, c, models.Material)) == registered
    assert applied['materials'] == len(registered)
    assert applied['pending'] == len(set(candidates) - registered)
    assert {item['value'] for item in m._pending_extractions(db, c.case_id)} == set(candidates) - registered


@pytest.mark.parametrize('text', [
    'Ingredients\nWater | 90%\nSalt | 10%',
    'Composition\nWater 7732-18-5 90%\nSalt 7647-14-5 10%',
    'Ingredients | CAS No. | %\nWater | 7732-18-5 | 90\nSalt | 7647-14-5 | 10',
    'Ingredients\nWater | | 90%\nSalt | 10%',
    'Composition\nWater, Salt',
    'Ingredients: Water, Salt',
    '3. Composition/information on ingredients\nWater, Salt',
    'SECTION 3: Composition\nWater | 90%\nSalt | 10%',
    'Ingredients\n- Water | 90%\n- Salt | 10%',
    'Ingredients\n1,2-Hexanediol | 1%\nWater | 89%\nSalt | 10%',
    'Ingredients\nAlcohol Denat. | 10%\nWater | 80%\nSalt | 10%',
])
def test_round3_qualified_rows_and_comma_lists_remain_ingredient_evidence(text):
    agg = intake.aggregate_fields([{'doc_type': 'coa_msds', 'text_full': text,
        'fields': {'material_names': ['Water', 'Salt'], '_sources': {'material_names': 'ai'}}}])
    assert agg['materials'] == ['Water', 'Salt']
    assert agg['pending'] == []


@pytest.mark.parametrize('route', ['aggregate', 'parse-file', 'reprocess'])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_round3_real_unmatched_hexanediol_never_autoregisters(case, monkeypatch, route):
    db, c = case
    risk = m.screening.screen_merged('1,2-Hexanediol')
    assert (risk['result'], risk['status'], risk['severity'], risk['decision_by']) == (
        'NEEDS_EVIDENCE', 'mushbooh', 'low', 'unmatched_default')
    applied = _round3_ingest(case, monkeypatch, route, 'Ingredients\n1,2-Hexanediol | 1%', ['1,2-Hexanediol'])
    assert applied['materials'] == 0
    assert applied['pending'] == 1
    assert not names(db, c, models.Material)
    assert [item['value'] for item in m._pending_extractions(db, c.case_id)] == ['1,2-Hexanediol']


@pytest.mark.parametrize('route', ['aggregate', 'parse-file', 'reprocess'])
@pytest.mark.parametrize('stage', ['aggregate', 'save'])
@pytest.mark.parametrize('overrides', [
    {'error': 'screening failed'}, {'error': ''}, {'error': None}, {'error': False},
    {'result': 'UNKNOWN', 'status': None, 'severity': None},
    {'result': 'UNKNOWN'}, {'result': None}, {'result': {}}, {'result': 'INVALID'},
    {'status': None}, {'status': 'unknown'}, {'status': {}}, {'status': 'INVALID'},
    {'severity': None}, {'severity': 'unknown'}, {'severity': {}}, {'severity': 'INVALID'},
    {'status': 'mushbooh'}, {'result': 'NEEDS_EVIDENCE'}, {'severity': 'medium'},
    {'decision_by': 'unmatched_default'}, {'decision_by': None}, {'decision_by': 'INVALID'},
])
def test_round3_screening_failures_and_uncertainty_are_pending_at_each_stage(
        case, monkeypatch, route, stage, overrides):
    db, c = case
    real_screen = m.screening.screen_merged
    valid = real_screen('Water')
    assert (valid['result'], valid['status'], valid['severity']) == ('PASS', 'halal', 'low')
    calls = {'aggregate': 0, 'save': 0}

    def injected(*args, **kwargs):
        # 집계는 이름만, 저장 단계는 기존 7개 인자를 전달한다.
        current = 'save' if len(args) > 1 else 'aggregate'
        calls[current] += 1
        return {**valid, **overrides} if current == stage else real_screen(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(m.screening, 'screen_merged', injected)
        applied = _round3_ingest(case, patch, route, 'Ingredients\nWater | 100%', ['Water'])
    assert calls[stage] > 0
    assert calls['save'] == (0 if stage == 'aggregate' else 1)
    assert applied['materials'] == 0
    assert applied['pending'] == 1
    assert not names(db, c, models.Material)
    # 장애가 끝나도 미등록 후보는 확인대기 정본에 남아 있어야 한다.
    assert [item['value'] for item in m._pending_extractions(db, c.case_id)] == ['Water']


@pytest.mark.parametrize('failure', [None, {},
    {'result': 'PASS', 'status': 'halal', 'severity': 'low', 'error': None},
    {'result': 'UNKNOWN', 'status': None, 'severity': None},
])
def test_round3_structural_save_also_validates_response_without_changing_parser(case, monkeypatch, failure):
    db, c = case
    doc = {'doc_type': 'material_list', 'filename': 'ingredients.txt', 'text_full': 'Water',
           'fields': {'material_names': ['Water'], '_sources': {'material_names': 'structural'}}}
    agg = intake.aggregate_fields([doc])
    assert agg['materials'] == ['Water'] and agg['pending'] == []
    monkeypatch.setattr(m.screening, 'screen_merged', lambda *args, **kwargs: failure)
    applied = m._apply_intake_autofill(db, c, {'classified': [doc], 'extracted': agg})
    db.commit()
    assert applied['materials'] == 0 and applied['pending'] == 1
    assert not names(db, c, models.Material)
    assert [item['value'] for item in m._pending_extractions(db, c.case_id)] == ['Water']


@pytest.mark.parametrize('boundary', [
    '9. Physical and chemical properties', 'SECTION 4: First aid measures', '4. 응급조치요령',
    'Ingredient suppliers use Water for cleaning equipment.',
])
def test_round3_autofill_boundaries_do_not_change_risk_scan_or_measurement_exclusions(boundary):
    text = f'Composition\nWater | 100%\n{boundary}\nEthanol Result: 70%'
    excluded = []
    assert intake._material_scan_text(text, excluded) == text
    assert excluded == []
    assert intake._material_scan_text(text, excluded, material_only=True) == 'Water'
    assert excluded == []
    text += '\n\nTest | Result\nEthanol | 0.02%'
    scanned = intake._material_scan_text(text, excluded)
    assert 'Ethanol Result: 70%' in scanned
    assert scanned.endswith('        | 0.02%')
    assert len(excluded) == 1
    assert excluded[0]['row'] == 'Ethanol | 0.02%'
    assert excluded[0]['reason'] == 'test_table_result'


ROUND4_HEADINGS = [
    'FIRST AID', 'Test solution', 'Solubility', 'Pertolongan pertama',
    'first aid', 'test solution', 'solubility', 'pertolongan pertama',
    'First aid:', '응급조치', 'Sifat fisik', 'sifat fisik',
    'Unlisted heading', '알 수 없는 제목', 'Judul lain',
]


@pytest.mark.parametrize('route', ['aggregate', 'parse-file', 'reprocess'])
@pytest.mark.parametrize('heading', ROUND4_HEADINGS)
@pytest.mark.parametrize('prefix', ['', 'Salt 100%\n'])
@pytest.mark.parametrize('tail', ['Water\nSalt', 'Water 90%\nSalt 10%'])
def test_round4_unknown_row_closes_context_until_a_new_ingredient_heading(
        case, monkeypatch, route, heading, prefix, tail):
    db, c = case
    text = f'Material Safety Data Sheet\nComposition\n{prefix}{heading}\n{tail}'
    applied = _round3_ingest(case, monkeypatch, route, text, ['Salt', 'Water'])
    expected = {'Salt'} if prefix else set()
    assert set(names(db, c, models.Material)) == expected
    assert applied['materials'] == len(expected)
    assert {x['value'] for x in m._pending_extractions(db, c.case_id)} == {'Salt', 'Water'} - expected


@pytest.mark.parametrize('header', [
    'Ingredients', 'Composition', '전성분', '성분', '원료명', 'Komposisi', 'Bahan', 'Bahan baku',
])
@pytest.mark.parametrize('unknown', ['Water', 'Salt', '1,2-Hexanediol', 'Test solution'])
def test_round4_unqualified_first_row_never_opens_context(header, unknown):
    text = f'{header}\n{unknown}\nWater 90%\nSalt 10%'
    agg = intake.aggregate_fields([{'doc_type': 'coa_msds', 'text_full': text,
        'fields': {'material_names': ['Water', 'Salt']}}])
    assert agg['materials'] == []
    assert {x['value'] for x in agg['pending']} == {'Water', 'Salt'}


@pytest.mark.parametrize('row', ['Water 100%', 'Water 100', 'Water 7732-18-5', 'Water | 100%',
                                  'Water\t100', 'Water, Salt'])
def test_round4_only_a_new_ingredient_heading_reopens_context(row):
    text = f'Ingredients\nUnlisted heading\nSalt 100%\nKomposisi\n{row}'
    agg = intake.aggregate_fields([{'doc_type': 'coa_msds', 'text_full': text,
        'fields': {'material_names': ['Water', 'Salt']}}])
    assert set(agg['materials']) == ({'Water', 'Salt'} if ',' in row else {'Water'})


@pytest.mark.parametrize('heading', ROUND4_HEADINGS)
def test_round4_strict_autofill_does_not_change_risk_scan(heading):
    text = f'Composition\nSalt 100%\n{heading}\nWater\nEthanol Result: 70%'
    exclusions = []
    assert intake._material_scan_text(text, exclusions) == text
    assert exclusions == []
    assert intake._material_scan_text(text, material_only=True) == 'Salt'


@pytest.fixture
def round4_http():
    with TestClient(m.app) as client:
        headers = {}
        for role in ('applicant', 'operator'):
            response = client.post('/auth/login', json={'username': role + '1', 'password': 'pw'})
            assert response.status_code == 200, response.text
            headers[role] = {'Authorization': 'Bearer ' + response.json()['token']}
        yield client, headers


def _round4_product_request(case, monkeypatch, http, route, text, fields,
                            kind='coa_msds', doc_id=None, apply=True):
    db, c = case
    client, headers = http
    reply(monkeypatch, kind, fields)
    if route == 'reprocess':
        response = client.post(f'/documents/{doc_id}/reprocess?apply={str(apply).lower()}',
                               headers=headers['operator'])
    else:
        if route.startswith('intake-zip'):
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w') as archive:
                archive.writestr('MSDS.txt', text)
                archive.writestr('MSDS-copy.txt', text + '\n')
            body = {'zip_b64': base64.b64encode(out.getvalue()).decode()}
        else:
            body = {'doc_type': kind, 'filename': 'MSDS.txt' if kind == 'coa_msds' else 'product_list.txt',
                    'file_b64': base64.b64encode(text.encode()).decode()}
        response = client.post(f'/cases/{c.case_id}/{route}', headers=headers['applicant'], json=body)
    assert response.status_code == 200, response.text
    if route.endswith('-stream'):
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1]['type'] == 'result', response.text
        assert not any(event['type'] == 'error' for event in events), response.text
    db.expire_all()
    return response


def _round4_pending_products(case, http):
    _, c = case
    client, headers = http
    response = client.get(f'/cases/{c.case_id}/pending-extractions', headers=headers['applicant'])
    assert response.status_code == 200, response.text
    return [item['value'] for item in response.json()['items'] if item['kind'] == 'product_names']


@pytest.mark.parametrize('route', ['reprocess', 'parse-file', 'intake-zip', 'intake-zip-stream'])
@pytest.mark.parametrize('material_row', ['Water', 'Water 100%'])
@pytest.mark.parametrize('representative', ['얼굴용 세럼', 'Face Serum'])
def test_round4_deleted_product_and_alias_never_return_on_any_http_intake(
        case, monkeypatch, round4_http, route, material_row, representative):
    db, c = case
    ko, en = '얼굴용 세럼', 'Face Serum'
    text = f'제품명(국문): {ko}\nProduct name (English): {en}\n\nIngredients\n{material_row}'
    fields = {'product_names': [ko, en], 'material_names': ['Water']}
    if representative == en:
        db.add(models.Product(case_id=c.case_id, name=en))
        db.commit()
    _round4_product_request(case, monkeypatch, round4_http, 'parse-file', text, fields)
    product = db.query(models.Product).filter_by(case_id=c.case_id).one()
    pid = product.product_id
    doc_id = db.query(models.DocumentAsset).filter_by(case_id=c.case_id).one().document_id
    alias = en if representative == ko else ko
    assert product.name == representative
    assert c.profile_ext['product_aliases'][pid] == [alias]
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == int(material_row != 'Water')
    client, headers = round4_http
    deleted = client.delete(f'/cases/{c.case_id}/products/{pid}', headers=headers['applicant'])
    assert deleted.status_code == 200, deleted.text
    db.expire_all()
    audit = db.query(models.AuditLog).filter_by(case_id=c.case_id, action='product.delete').one()
    assert audit.meta['name'] == representative and audit.meta['aliases'] == [alias]
    assert not names(db, c, models.Product)
    assert _round4_pending_products(case, round4_http) == []
    for _ in range(2):
        response = _round4_product_request(case, monkeypatch, round4_http, route, text, fields, doc_id=doc_id)
        assert not names(db, c, models.Product)
        assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == 0
        assert _round4_pending_products(case, round4_http) == []
        if route == 'parse-file':
            assert response.json()['applied']['products_pending'] == 0
        elif route == 'intake-zip':
            assert response.json()['extracted']['products'] == []
            assert response.json()['extracted']['product_aliases'] == []
    # 다른 파일의 단독 별칭·대소문자 변형도 제품 목록이라는 이유로 다시 등록하지 않는다.
    for kind in ('product_list', 'coa_msds'):
        response = _round4_product_request(case, monkeypatch, round4_http, 'parse-file',
            'Product name: FACE SERUM', {'product_names': ['FACE SERUM']}, kind=kind)
        assert response.json()['applied']['products_pending'] == 0
    assert not names(db, c, models.Product)
    assert _round4_pending_products(case, round4_http) == []


@pytest.mark.parametrize('kind', ['coa_msds', 'product_list'])
@pytest.mark.parametrize('dismissed', [KO, EN])
@pytest.mark.parametrize('route', ['reprocess', 'parse-file', 'intake-zip'])
def test_round4_rejected_half_of_a_pair_is_never_an_alias_or_registration_evidence(
        case, monkeypatch, round4_http, kind, dismissed, route):
    db, c = case
    survivor = EN if dismissed == KO else KO
    text = PAIR_TEXT if kind == 'product_list' else PAIR_TEXT.replace('Product list', 'Material Safety Data Sheet')
    fields = {'product_names': [KO, EN], 'product_name_ko': KO, 'product_name_en': EN}
    doc = models.DocumentAsset(case_id=c.case_id, filename='MSDS.txt', doc_type=kind,
                              content_b64=base64.b64encode(text.encode()).decode())
    db.add(doc)
    db.commit()
    _round4_product_request(case, monkeypatch, round4_http, 'reprocess', text, fields,
                            kind=kind, doc_id=doc.document_id, apply=False)
    assert set(_round4_pending_products(case, round4_http)) == {KO, EN}
    client, headers = round4_http
    response = client.post(f'/cases/{c.case_id}/pending-extractions/apply', headers=headers['operator'],
        json={'dismiss': [dismissed], 'reason': '이 이름은 다른 제품이므로 제외'})
    assert response.status_code == 200, response.text
    assert _round4_pending_products(case, round4_http) == [survivor]
    for _ in range(2):
        _round4_product_request(case, monkeypatch, round4_http, route, text, fields,
                                kind=kind, doc_id=doc.document_id)
        assert not (c.profile_ext or {}).get('product_aliases')
        assert names(db, c, models.Product) == ([survivor] if kind == 'product_list' else [])
        assert _round4_pending_products(case, round4_http) == ([] if kind == 'product_list' else [survivor])


def test_round4_rejected_english_name_cannot_attach_to_existing_human_product(case, monkeypatch, round4_http):
    db, c = case
    product = models.Product(case_id=c.case_id, name=KO)
    db.add(product)
    db.commit()
    pid = product.product_id
    doc, _ = reprocess(case, monkeypatch, PAIR_TEXT, {'product_names': [KO, EN]}, apply=False)
    assert _round4_pending_products(case, round4_http) == [EN]
    client, headers = round4_http
    response = client.post(f'/cases/{c.case_id}/pending-extractions/apply', headers=headers['operator'],
        json={'dismiss': [EN], 'reason': '영문명은 다른 제품이므로 제외'})
    assert response.status_code == 200, response.text
    _round4_product_request(case, monkeypatch, round4_http, 'reprocess', PAIR_TEXT,
                            {'product_names': [KO, EN.upper()]}, doc_id=doc.document_id)
    assert names(db, c, models.Product) == [KO]
    assert db.query(models.Product).filter_by(case_id=c.case_id).one().product_id == pid
    assert not (c.profile_ext or {}).get('product_aliases')
    assert _round4_pending_products(case, round4_http) == []


@pytest.mark.parametrize('original', [KO, EN])
def test_round4_product_review_rename_is_respected_on_reprocess(case, monkeypatch, round4_http, original):
    db, c = case
    corrected = 'Corrected independent serum'
    doc, _ = reprocess(case, monkeypatch, PAIR_TEXT, {'product_names': [KO, EN]}, apply=False)
    client, headers = round4_http
    response = client.post(f'/cases/{c.case_id}/pending-extractions/apply', headers=headers['operator'],
        json={'renames': {original: corrected}, 'reason': '원본 대조 후 제품명 교정'})
    assert response.status_code == 200, response.text
    db.expire_all()
    pid = db.query(models.Product).filter_by(case_id=c.case_id).one().product_id
    for _ in range(2):
        _round4_product_request(case, monkeypatch, round4_http, 'reprocess', PAIR_TEXT,
                                {'product_names': [KO, EN]}, doc_id=doc.document_id)
        assert names(db, c, models.Product) == [corrected]
        assert db.query(models.Product).filter_by(case_id=c.case_id).one().product_id == pid
        assert not (c.profile_ext or {}).get('product_aliases')
        assert _round4_pending_products(case, round4_http) == [EN if original == KO else KO]


@pytest.mark.parametrize('action', ['product.delete', 'product.rename', 'extraction.review'])
def test_round4_product_history_is_case_scoped(case, monkeypatch, action):
    db, c = case
    other = models.CaseApplication(org_id=c.org_id, company_name='Other product case')
    db.add(other)
    db.flush()
    if action == 'product.delete':
        db.add(models.AuditLog(case_id=other.case_id, resource_type='product', resource_id='deleted',
                               action=action, actor_role='applicant', meta={'name': KO, 'aliases': [EN]}))
    else:
        m.sm.record_event(db, other, other.status, other.status, action, 'operator', 'operator1',
            {'from': KO, 'aliases': [EN], 'items': [{'kind': 'product_names', 'value': KO},
                {'kind': 'product_names', 'value': EN}], 'dismiss': [KO, EN]})
    db.commit()
    reprocess(case, monkeypatch, PAIR_TEXT, {'product_names': [KO, EN]})
    product = db.query(models.Product).filter_by(case_id=c.case_id).one()
    assert product.name == KO
    assert c.profile_ext['product_aliases'][product.product_id] == [EN]


@pytest.mark.parametrize('history', ['legacy_delete', 'rename'])
def test_round4_product_history_covers_aliases_across_repeated_processing(case, monkeypatch, history):
    db, c = case
    if history == 'legacy_delete':
        c.profile_ext = {'product_aliases': {'old-id': [EN]}}
        db.add(models.AuditLog(case_id=c.case_id, resource_type='product', resource_id='old-id',
                               action='product.delete', actor_role='applicant', meta={'name': KO}))
    else:
        db.add(models.Product(case_id=c.case_id, name='Corrected serum'))
        m.sm.record_event(db, c, c.status, c.status, 'product.rename', 'applicant', 'applicant1',
                           {'from': KO, 'to': 'Corrected serum', 'aliases': [EN]})
    db.commit()
    for _ in range(2):
        reprocess(case, monkeypatch, PAIR_TEXT, {'product_names': [KO, EN]}, kind='product_list')
        assert names(db, c, models.Product) == ([] if history == 'legacy_delete' else ['Corrected serum'])
        assert m._pending_extractions(db, c.case_id) == []
        assert m.sm.rejected_product_autofill_names(db, c.case_id) == {m._norm_material(KO), m._norm_material(EN)}


@pytest.mark.parametrize('frozen', [False, True])
def test_round4_rejected_products_do_not_return_in_stored_or_frozen_pending(case, monkeypatch, frozen):
    db, c = case
    m.sm.record_event(db, c, c.status, c.status, 'extraction.review', 'operator', 'operator1',
        {'items': [{'kind': 'product_names', 'value': EN}], 'dismiss': [EN], 'reason': '별개 제품'})
    c.scope_frozen = frozen
    db.add(models.DocumentAsset(case_id=c.case_id, doc_type='coa_msds', filename='old.txt',
        text_full=PAIR_TEXT, fields={'product_names': [KO, EN],
            '_autofill': {'product_pending': [{'kind': 'product_names', 'value': EN}]},
            '_scope_pending': [{'kind': 'product_names', 'value': EN}],
            '_rule_omissions': {'product_names': [EN]}}))
    db.commit()
    reprocess(case, monkeypatch, PAIR_TEXT, {'product_names': [KO, EN]})
    assert [x['value'] for x in m._pending_extractions(db, c.case_id)] == [KO]
    assert not names(db, c, models.Product)


@pytest.mark.parametrize('source', ['ai', 'structural', 'structural_reject'])
@pytest.mark.parametrize('kind', ['coa_msds', 'product_list'])
@pytest.mark.parametrize('rejected', [KO, EN])
def test_round4_surviving_label_only_name_uses_its_own_source_eligibility(source, kind, rejected):
    survivor = EN if rejected == KO else KO
    doc = {'doc_type': kind, 'text_full': PAIR_TEXT, 'fields': {
        'product_name_ko': KO, 'product_name_en': EN, '_sources': {'product_names': source}}}
    agg = intake.aggregate_fields([doc], rejected_product_names={m._norm_material(rejected)})
    eligible = source == 'structural' or (kind == 'product_list' and source != 'structural_reject')
    assert agg['products'] == ([survivor] if eligible else [])
    assert agg['product_aliases'] == []
    assert [x['value'] for x in agg['pending']] == ([] if eligible or source == 'structural_reject' else [survivor])


@pytest.mark.parametrize('kind', ['coa_msds', 'product_list'])
def test_round4_rejected_shared_alias_does_not_connect_unrelated_survivors(case, kind):
    db, c = case
    shared = 'Shared Serum'
    m.sm.record_event(db, c, c.status, c.status, 'extraction.review', 'operator', 'operator1',
        {'items': [{'kind': 'product_names', 'value': shared}], 'dismiss': [shared], 'reason': '다른 제품'})
    db.commit()
    docs = [paired_document(name, shared, str(i) + '.txt') for i, name in enumerate(['얼굴용 세럼', '바디용 세럼'])]
    for doc in docs:
        doc['doc_type'] = kind
    m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
    db.commit()
    assert set(names(db, c, models.Product)) == ({'얼굴용 세럼', '바디용 세럼'} if kind == 'product_list' else set())
    assert {x['value'] for x in m._pending_extractions(db, c.case_id)} == (
        set() if kind == 'product_list' else {'얼굴용 세럼', '바디용 세럼'})
    assert not (c.profile_ext or {}).get('product_aliases')


def test_round4_review_approval_is_not_a_rejection(case, monkeypatch):
    db, c = case
    db.add(models.Product(case_id=c.case_id, name=EN))
    m.sm.record_event(db, c, c.status, c.status, 'extraction.review', 'operator', 'operator1',
        {'items': [{'kind': 'product_names', 'value': EN}], 'dismiss': [], 'renames': {}, 'reason': '제품명 확인'})
    db.commit()
    assert m.sm.rejected_product_autofill_names(db, c.case_id) == set()
    reprocess(case, monkeypatch, PAIR_TEXT, {'product_names': [KO, EN]})
    product = db.query(models.Product).filter_by(case_id=c.case_id).one()
    assert product.name == EN
    assert c.profile_ext['product_aliases'][product.product_id] == [KO]


@pytest.mark.parametrize('separator', ['|', '｜', '\t'])
@pytest.mark.parametrize('name', ['FIRST AID', 'Test solution', 'Solubility',
                                 'Pertolongan pertama', 'Water'])
@pytest.mark.parametrize('metadata', ['', ' ', 'unknown', 'CAS No.', '%', 'w/w', 'wt. %'])
def test_round5_separators_without_values_close_material_context(separator, name, metadata):
    text = f'Composition\nSalt 100%\n{name}{separator}{metadata}\nWater{separator}70%'
    assert intake._material_scan_text(text, material_only=True) == 'Salt'


@pytest.mark.parametrize('separator', ['|', '｜', '\t'])
@pytest.mark.parametrize('heading', ['solubility', 'first aid', 'test', 'pertolongan pertama',
                                    '응급', '물성', 'properties', 'Sifat fisik'])
def test_round5_section_words_in_name_cell_override_numeric_evidence(separator, heading):
    text = f'Composition\nSalt 100%\nWater {heading}{separator}100%\nWater{separator}70%'
    assert intake._material_scan_text(text, material_only=True) == 'Salt'


@pytest.mark.parametrize('separator', ['|', '｜', '\t'])
@pytest.mark.parametrize('metadata', ['', ' ', 'w/w'])
def test_round5_table_evidence_must_be_outside_name_cell(separator, metadata):
    text = f'Composition\nSalt 100%\nWater 70%{separator}{metadata}\nWater{separator}30%'
    assert intake._material_scan_text(text, material_only=True) == 'Salt'


@pytest.mark.parametrize('separator', ['|', '｜', '\t'])
@pytest.mark.parametrize('header', [
    ['Name', 'CAS No.', '%'], ['Ingredient', 'CAS', 'Content'],
    ['Component', 'CAS Number', 'Concentration'], ['Chemical name', 'CAS No.', '%'],
    ['성분명', 'CAS', '함량'], ['원료명', 'CAS', '함량'],
])
def test_round5_separate_column_headers_preserve_material_context(separator, header):
    text = 'Ingredients\n' + separator.join(header) + '\n' + separator.join(['Water', '7732-18-5', '70'])
    assert intake._material_scan_text(text, material_only=True) == 'Water'


@pytest.mark.parametrize('text', [
    'Name | CAS No. | %\nWater | 7732-18-5 | 70',
    'Ingredients\nUnknown heading\nName | CAS No. | %\nWater | 7732-18-5 | 70',
    'Ingredients\n\nName | CAS No. | %\nWater | 7732-18-5 | 70',
    'Ingredients\nName | Unknown | %\nWater | 7732-18-5 | 70',
    'Ingredients\nName result | CAS No. | %\nWater | 7732-18-5 | 70',
    'Ingredients\nName\nWater | 7732-18-5 | 70',
])
def test_round5_only_exact_column_headers_in_open_context_are_allowed(text):
    assert intake._material_scan_text(text, material_only=True) == ''


def _round5_material_http(case, monkeypatch, http, route, text, candidates):
    db, c = case
    doc_id = None
    if route == 'reprocess':
        doc = models.DocumentAsset(case_id=c.case_id, filename='MSDS.txt', doc_type='coa_msds',
                                   content_b64=base64.b64encode(text.encode()).decode())
        db.add(doc)
        db.commit()
        doc_id = doc.document_id
    return _round4_product_request(case, monkeypatch, http, route, text,
                                   {'material_names': candidates}, doc_id=doc_id)


@pytest.mark.parametrize('route', ['parse-file', 'reprocess'])
@pytest.mark.parametrize('rows', [
    *[f'{heading} |\n{tail}' for heading in (
        'FIRST AID', 'Test solution', 'Solubility', 'Pertolongan pertama',
    ) for tail in ('Water |', 'Water | 7732-18-5 | 70')],
    'Water solubility | 100%',
    'Water |\nWater | 7732-18-5 | 70',
    'Water | unknown\nWater | 7732-18-5 | 70',
])
def test_round5_non_material_table_rows_stay_pending_over_http(
        case, monkeypatch, round4_http, route, rows):
    db, c = case
    text = 'Material Safety Data Sheet\nComposition\nSalt 100%\n' + rows
    response = _round5_material_http(case, monkeypatch, round4_http, route, text, ['Salt', 'Water'])
    assert response.status_code == 200, response.text
    assert names(db, c, models.Material) == ['Salt']
    assert {item['value'] for item in m._pending_extractions(db, c.case_id)} == {'Water'}


@pytest.mark.parametrize('route', ['parse-file', 'reprocess'])
def test_round5_separate_column_header_registers_cas_row_over_http(case, monkeypatch, round4_http, route):
    db, c = case
    text = 'Ingredients\nName | CAS No. | %\nWater | 7732-18-5 | 70'
    response = _round5_material_http(case, monkeypatch, round4_http, route, text, ['Water'])
    assert response.status_code == 200, response.text
    assert names(db, c, models.Material) == ['Water']
    assert m._pending_extractions(db, c.case_id) == []


FLAT_MSDS_NAMES = ['Lactobacillus Ferment Lysate', 'Water', 'Olive Oil PEG-8 Esters',
                   'Alcohol Denat.', 'Glycerin', 'Niacinamide', 'Limonene', 'Citral']
FLAT_MSDS = (
    'Material Safety Data Sheet 1. PRODUCT IDENTIFICATION Product name : Face Serum '
    '2. COMPOSITION/INFORMATION ON INGREDIENTS Ingredients : ' + ', '.join(FLAT_MSDS_NAMES) +
    ' 3. HAZARDS IDENTIFICATION Main Hazards : None '
    '4. First-AID MEASURES Eye contact : Rins immediately with plenty of water. '
    'INGESTION : Give to drink water. 9. PHYSICAL AND CHEMICAL PROPERTIES Solubility : water.'
)


@pytest.mark.parametrize('route', ['aggregate', 'parse-file', 'reprocess'])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_flat_msds_registers_only_dictionary_safe_ingredients(case, monkeypatch, route):
    db, c = case
    assert intake._material_scan_text(FLAT_MSDS, material_only=True).splitlines() == FLAT_MSDS_NAMES
    applied = _round3_ingest(case, monkeypatch, route, FLAT_MSDS, FLAT_MSDS_NAMES + ['Citral 3'])
    assert set(names(db, c, models.Material)) == {'Water', 'Niacinamide'}
    assert applied['materials'] == 2
    pending = {item['value'] for item in m._pending_extractions(db, c.case_id)}
    assert {'Alcohol Denat.', 'Glycerin', 'Citral'} <= pending
    assert not {'Water', 'Niacinamide'} & pending
    assert 'Citral 3' not in names(db, c, models.Material)


@pytest.mark.parametrize('route', ['aggregate', 'parse-file', 'reprocess'])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_flat_msds_first_aid_water_is_not_ingredient_evidence(case, monkeypatch, route):
    db, c = case
    text = FLAT_MSDS.replace('Lysate, Water,', 'Lysate,')
    evidence = intake._material_scan_text(text, material_only=True).splitlines()
    assert evidence == [name for name in FLAT_MSDS_NAMES if name != 'Water']
    applied = _round3_ingest(case, monkeypatch, route, text, FLAT_MSDS_NAMES)
    assert names(db, c, models.Material) == ['Niacinamide']
    assert applied['materials'] == 1
    assert 'Water' in {item['value'] for item in m._pending_extractions(db, c.case_id)}


@pytest.mark.parametrize('heading', ['Ingredients', 'Composition', '전성분', '성분', 'Komposisi', 'Bahan',
                                    'Bahan baku', 'Composition / Ingredients'])
@pytest.mark.parametrize('boundary', ['3. HAZARDS IDENTIFICATION', 'SECTION 4', 'SECTION 4: FIRST AID'])
def test_flat_msds_labels_end_before_section_number(heading, boundary):
    text = f'2. COMPOSITION {heading} : Salt, Citral, 1,2-Hexanediol {boundary} Eye contact : water.'
    text += '\nWater | 100%'
    assert intake._material_scan_text(text, material_only=True).splitlines() == [
        'Salt', 'Citral', '1,2-Hexanediol']


@pytest.mark.parametrize('label', ['Eye contact', 'Main Hazards', 'INGESTION', 'Solubility',
                                  'Custom label', '응급조치', 'Sifat fisik'])
def test_flat_msds_other_labels_end_ingredient_context(label):
    text = f'Ingredients: Salt, Citral; {label} : Water, Niacinamide\nWater | 100%'
    assert intake._material_scan_text(text, material_only=True) == 'Salt\nCitral'


def test_flat_msds_later_explicit_ingredient_label_can_reopen_context():
    text = 'Ingredients: Salt, Citral SECTION 4 Eye contact: water. Komposisi: Water, Niacinamide'
    assert intake._material_scan_text(text, material_only=True) == 'Salt\nCitral\nWater\nNiacinamide'


@pytest.mark.parametrize('text', [
    'FIRST AID Eye contact: Rinse with water, then consult a physician.',
    '9. PHYSICAL PROPERTIES Solubility: Water, alcohol.',
    'Ingredient suppliers use Water, Salt for cleaning equipment.',
    'Ingredients: 1,2-Hexanediol 4. FIRST AID Eye contact: Water, Salt',
])
def test_flat_msds_prose_and_numeric_comma_are_not_ingredient_lists(text):
    assert intake._material_scan_text(text, material_only=True) == ''


@pytest.mark.parametrize('route', ['aggregate', 'reprocess'])
@pytest.mark.parametrize('prefix', [
    pytest.param('Ingredients\nWater | 90%\nSalt | 10%\nDisclaimer: ', id='table-label'),
    pytest.param('Ingredients\nWater | 90%\nSalt | 10%\n', id='table-nonmaterial-row'),
    pytest.param('Ingredients\nWater | 90%\nSalt | 10%\n3. HAZARDS ', id='table-section'),
    pytest.param('Ingredients : Water 90%, Salt 10% 3. HAZARDS ', id='inline-section'),
    pytest.param('Ingredients : Water 90%, Salt 10%; Disclaimer: ', id='inline-label'),
])
def test_autofill_long_nonmaterial_tail_preserves_ingredient_evidence(case, monkeypatch, route, prefix):
    db, c = case
    text = prefix + 'x' * 20100
    assert intake._material_scan_text(text, material_only=True) == 'Water\nSalt'
    applied = _round3_ingest(case, monkeypatch, route, text, ['Water', 'Salt'])
    assert set(names(db, c, models.Material)) == {'Water', 'Salt'}
    assert applied['materials'] == 2
    assert applied['pending'] == 0
    assert m._pending_extractions(db, c.case_id) == []


@pytest.mark.parametrize('repeat', ['A/', 'A-', 'A ', '가/', '가-', '가 '])
@pytest.mark.parametrize('route', ['aggregate', 'reprocess'])
def test_flat_msds_long_repeated_tail_stays_pending_within_time_limit(case, monkeypatch, repeat, route):
    db, c = case
    text = ('Ingredients: Water, Salt, ' + repeat * 100000)[:200000]
    started = perf_counter()
    applied = _round3_ingest(case, monkeypatch, route, text, ['Water', 'Salt'])
    elapsed = perf_counter() - started
    assert elapsed < 3.0, (route, repeat, elapsed)
    assert applied['materials'] == 0
    assert names(db, c, models.Material) == []
    assert {item['value'] for item in m._pending_extractions(db, c.case_id)} == {'Water', 'Salt'}


@pytest.mark.parametrize('repeat', ['A/', 'A-', 'A ', '가/', '가-', '가 '])
@pytest.mark.parametrize('size', [10000, 20000, 40000, 200000])
def test_flat_msds_end_label_search_has_bounded_candidates(repeat, size):
    text = (repeat * 100000)[:size]
    started = perf_counter()
    boundary = intake._AUTOFILL_INLINE_END.search(text)
    elapsed = perf_counter() - started
    assert boundary is None
    assert elapsed < 3.0, (repeat, size, elapsed)


@pytest.mark.parametrize('repeat', ['Ingredients/', 'Ingredients / ', 'Composition/Ingredients/', '전성분/'])
def test_flat_msds_repeated_heading_without_colon_has_bounded_candidates(repeat):
    text = (repeat * 100000)[:200000]
    started = perf_counter()
    headers = list(intake._AUTOFILL_INLINE_MATERIAL_HEADING.finditer(text))
    evidence = intake._material_scan_text(text, material_only=True)
    elapsed = perf_counter() - started
    assert headers == []
    assert evidence == ''
    assert elapsed < 3.0, (repeat, elapsed)


@pytest.mark.parametrize('size', [19999, 20000, 20001])
@pytest.mark.parametrize('boundary', ['', '3. HAZARDS IDENTIFICATION Eye contact: Water'])
def test_flat_msds_oversized_section_preserves_prior_evidence_and_excludes_oversized_rows(size, boundary):
    value = 'Water,' + ' ' * (size - len('Water,Salt;')) + 'Salt;'
    assert len(value) == size
    text = 'Ingredients: Niacinamide, Salt\n\nIngredients:' + value + boundary + '\nPanthenol 100%'
    evidence = intake._material_scan_text(text, material_only=True).splitlines()
    # 20,000자 행 안전 상한 이내면 공백으로 구분된 짧은 Water·Salt 항목은 보존한다.
    # 그 뒤 Panthenol은 누적 상한 또는 명시적인 섹션 경계 이후이므로 근거가 아니다.
    assert evidence == ['Niacinamide', 'Salt'] + (['Water', 'Salt'] if size <= 20000 else [])
    assert 'Panthenol' not in evidence
    if size > 20000:
        assert 'Water' not in evidence


@pytest.mark.parametrize('route', ['aggregate', 'reprocess'])
@pytest.mark.parametrize('tail', ['A/' * 10001, 'Water' + ' ' * 20000 + '100%'], ids=['slashes', 'spaces'])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_flat_msds_oversized_later_section_registers_only_safe_prior_candidates(case, monkeypatch, route, tail):
    db, c = case
    prior = ['Water', 'Salt', 'Alcohol Denat.', 'Glycerin', '1,2-Hexanediol']
    beyond_limit = ['Niacinamide', 'Panthenol']
    text = ('Ingredients: ' + ', '.join(prior) + '\n\nIngredients: ' + tail +
            ', Niacinamide\nPanthenol 100%')
    evidence = intake._material_scan_text(text, material_only=True).splitlines()
    assert evidence == prior
    assert not set(beyond_limit) & set(evidence)
    applied = _round3_ingest(case, monkeypatch, route, text, prior + beyond_limit)
    assert applied['materials'] == 2
    assert set(names(db, c, models.Material)) == {'Water', 'Salt'}
    # 앞 구간의 위험·미확정 원료와 초과 구간에만 있는 안전 원료는 모두 미등록·확인 대기다.
    assert m.screening.screen_merged('1,2-Hexanediol')['decision_by'] == 'unmatched_default'
    assert {item['value'] for item in m._pending_extractions(db, c.case_id)} == {
        'Alcohol Denat.', 'Glycerin', '1,2-Hexanediol', 'Niacinamide', 'Panthenol'}


@pytest.mark.parametrize('count', [2000, 2001])
def test_flat_msds_multiline_limit_preserves_accepted_prefix_and_excludes_later_sections(count):
    text = 'Ingredients\n' + '\n'.join(['Water 90%'] * count)
    text += '\nSalt 10%\nIngredients: Niacinamide, Panthenol'
    evidence = intake._material_scan_text(text, material_only=True).splitlines()
    # 행과 개행을 합쳐 20,000자까지 보존하며, 이후 행이나 새 성분 머리줄로 재개하지 않는다.
    assert evidence == ['Water'] * 2000
    assert not {'Salt', 'Niacinamide', 'Panthenol'} & set(evidence)


def test_flat_msds_short_section_in_long_document_remains_evidence():
    text = 'A/' * 100000 + '\nIngredients: Water, Salt SECTION 4\n' + 'A/' * 100000
    assert intake._material_scan_text(text, material_only=True) == 'Water\nSalt'


@pytest.mark.parametrize('heading,boundary', [
    ('전성분', '3. 유해성·위험성'),
    ('전성분', '4. 응급조치요령'),
    ('성분', '5. 폭발·화재시 대처방법'),
    ('성분', '9. 물리화학적 특성'),
    ('Bahan', '3. Identifikasi bahaya'),
    ('Komposisi', '3. identifikasi bahaya'),
    ('Bahan/Komposisi', '4. Pertolongan pertama'),
    ('Ingredients', '3. hazards identification'),
])
def test_inline_multilingual_sections_preserve_numeric_ingredients_and_amounts(heading, boundary):
    text = (f'{heading} : 정제수 90%, 글리세린 3.5%, 1,2-헥산다이올 1%, '
            f'부틸렌글라이콜 5.5% {boundary} 물로 씻으세요.\n물 | 100%')
    assert intake._material_scan_text(text, material_only=True).splitlines() == [
        '정제수', '글리세린', '1,2-헥산다이올', '부틸렌글라이콜']
    assert intake._AUTOFILL_INLINE_END.search('1,2-헥산다이올 3.5%') is None


@pytest.mark.parametrize('route', ['aggregate', 'parse-file', 'reprocess'])
@pytest.mark.parametrize('text,candidates,registered', [
    ('전성분 : 정제수, 글리세린, 부틸렌글라이콜 3. 유해성·위험성 '
     '4. 응급조치요령 물로 씻으세요.',
     ['정제수', '글리세린', '부틸렌글라이콜', '물'], {'정제수', '부틸렌글라이콜'}),
    ('Bahan/Komposisi : Niacinamide, Glycerin, Salt 3. Identifikasi bahaya '
     '4. Pertolongan pertama Bilas dengan Water.',
     ['Niacinamide', 'Glycerin', 'Salt', 'Water'], {'Niacinamide', 'Salt'}),
    ('Komposisi : Niacinamide 3.5%, Glycerin 1%, Salt 95.5% 3. identifikasi bahaya '
     '4. pertolongan pertama Bilas dengan Water.',
     ['Niacinamide', 'Glycerin', 'Salt', 'Water'], {'Niacinamide', 'Salt'}),
])
def test_inline_multilingual_autofill_keeps_review_and_first_aid_candidates_pending(
        case, monkeypatch, route, text, candidates, registered):
    db, c = case
    applied = _round3_ingest(case, monkeypatch, route, text, candidates)
    assert set(names(db, c, models.Material)) == registered
    assert applied['materials'] == len(registered)
    assert {item['value'] for item in m._pending_extractions(db, c.case_id)} == set(candidates) - registered


def _single_product_ingest(case, monkeypatch, http, route, candidates, *, source_name=None):
    text = 'Ingredients: ' + ', '.join(candidates) + ' 3. HAZARDS IDENTIFICATION'
    if source_name:
        text = f'Product name: {source_name}\n' + text
    if route in ('aggregate', 'parse-file', 'reprocess'):
        return _round3_ingest(case, monkeypatch, route, text, candidates)
    if route != 'apply':
        return _round4_product_request(case, monkeypatch, http, route, text, {'material_names': candidates})
    db, c = case
    reprocess(case, monkeypatch, text, {'material_names': candidates}, apply=False)
    client, headers = http
    response = client.post(f'/cases/{c.case_id}/pending-extractions/apply',
        headers=headers['applicant'], json={'values': candidates})
    assert response.status_code == 200, response.text
    db.expire_all()
    return response.json()['applied']


@pytest.mark.parametrize('product_count', [0, 1, 2])
@pytest.mark.parametrize('route', [
    'aggregate', 'parse-file', 'reprocess', 'intake-zip', 'intake-zip-stream', 'apply',
])
@pytest.mark.parametrize('source_name', [None, 'Serum 0'], ids=['missing-product', 'matching-product'])
def test_material_registration_links_only_the_single_case_product(
        case, monkeypatch, round4_http, route, product_count, source_name):
    db, c = case
    products = [models.Product(case_id=c.case_id, name=f'Serum {i}') for i in range(product_count)]
    other = models.CaseApplication(org_id='org_demo', company_name='Other product case')
    db.add(other)
    db.flush()
    db.add_all(products + [models.Product(case_id=other.case_id, name='Other serum')])
    db.commit()
    _single_product_ingest(case, monkeypatch, round4_http, route, ['Water', 'Salt'], source_name=source_name)
    materials = db.query(models.Material).filter_by(case_id=c.case_id).all()
    assert {row.name for row in materials} == {'Water', 'Salt'}
    links = db.query(models.ProductMaterial).filter_by(case_id=c.case_id).all()
    should_link = product_count == 1 and source_name is not None
    expected = {(products[0].product_id, row.material_id) for row in materials} if should_link else set()
    assert {(row.product_id, row.material_id) for row in links} == expected
    assert m._package_check(db, c)['items']['material']['unlinked_products'] == (
        [] if should_link else [row.name for row in products])
    _single_product_ingest(case, monkeypatch, round4_http, route, ['Water', 'Salt'], source_name=source_name)
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == len(expected)
    assert db.query(models.ProductMaterial).filter_by(case_id=other.case_id).count() == 0


@pytest.mark.parametrize('initially_linked', [False, True])
@pytest.mark.parametrize('route', [
    'aggregate', 'parse-file', 'reprocess', 'intake-zip', 'intake-zip-stream', 'apply',
])
@pytest.mark.parametrize('source_name', [None, 'Face Serum'], ids=['missing-product', 'matching-product'])
def test_manual_matrix_unlink_survives_material_registration(
        case, monkeypatch, round4_http, route, initially_linked, source_name):
    db, c = case
    product = models.Product(case_id=c.case_id, name='Face Serum')
    water = models.Material(case_id=c.case_id, name='Water')
    db.add_all([product, water])
    db.flush()
    pid, mid = product.product_id, water.material_id
    if initially_linked:
        db.add(models.ProductMaterial(case_id=c.case_id, product_id=pid, material_id=mid))
    db.commit()
    client, headers = round4_http
    response = client.post(f'/cases/{c.case_id}/matrix/link', headers=headers['applicant'],
        json={'product_id': pid, 'material_id': mid, 'linked': False})
    assert response.status_code == 200, response.text
    db.expire_all()
    audit = db.query(models.AuditLog).filter_by(case_id=c.case_id, action='matrix.link').one()
    assert (audit.meta['product_id'], audit.meta['material_id'], audit.meta['linked']) == (pid, mid, False)
    assert audit.actor_role == 'applicant'
    _single_product_ingest(case, monkeypatch, round4_http, route, ['Water', 'Salt'], source_name=source_name)
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    expected = {(pid, salt.material_id)} if source_name else set()
    assert {(row.product_id, row.material_id) for row in
            db.query(models.ProductMaterial).filter_by(case_id=c.case_id)} == expected
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id, material_id=mid).count() == 0
    # 사람이 명시적으로 다시 잇는 동작과 기존 연결은 자동 연결의 제외 이력에 막히지 않는다.
    response = client.post(f'/cases/{c.case_id}/matrix/link', headers=headers['applicant'],
        json={'product_id': pid, 'material_id': mid, 'linked': True})
    assert response.status_code == 200, response.text
    db.expire_all()
    _single_product_ingest(case, monkeypatch, round4_http, route, ['Water', 'Salt'], source_name=source_name)
    assert {(row.product_id, row.material_id) for row in
            db.query(models.ProductMaterial).filter_by(case_id=c.case_id)} == expected | {(pid, mid)}


@pytest.mark.parametrize('history', ['audit', 'event'])
@pytest.mark.parametrize('action', ['matrix.link', 'matrix/link'])
@pytest.mark.parametrize('scope', ['same-case-human', 'other-case-human', 'same-case-system'])
@pytest.mark.parametrize('source_name', [None, 'Face Serum'], ids=['missing-product', 'matching-product'])
def test_auto_link_only_new_materials_and_respects_case_scoped_human_history(
        case, monkeypatch, history, action, scope, source_name):
    db, c = case
    other = models.CaseApplication(org_id='org_demo', company_name='Other history case')
    product = models.Product(case_id=c.case_id, name='Face Serum')
    water = models.Material(case_id=c.case_id, name='Water')
    db.add_all([other, product, water])
    db.flush()
    owner = other if scope == 'other-case-human' else c
    actor = 'system' if scope == 'same-case-system' else 'applicant'

    def record_unlink(material_id):
        payload = {'product_id': product.product_id, 'material_id': material_id, 'linked': False}
        if history == 'audit':
            m._audit(db, {'uid': 'applicant1', 'role': actor, 'org_id': 'org_demo'}, action,
                     'case', owner.case_id, owner.case_id, payload, commit=False)
        else:
            m.sm.record_event(db, owner, owner.status, owner.status, action, actor, 'applicant1', payload)

    record_unlink(water.material_id)
    db.commit()
    text = (f'Product name: {source_name}\n' if source_name else '') + 'Ingredients: Water, Salt'
    applied = _round3_ingest(case, monkeypatch, 'aggregate', text, ['Water', 'Salt'])
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert applied['materials'] == 1
    assert applied['links'] == int(source_name is not None)
    assert set(names(db, c, models.Material)) == {'Water', 'Salt'}
    # 기존 미연결 Water는 소급 연결하지 않고 출처가 확정된 새 Salt만 연결한다.
    expected = {(product.product_id, salt.material_id)} if source_name else set()
    assert {(row.product_id, row.material_id) for row in
            db.query(models.ProductMaterial).filter_by(case_id=c.case_id)} == expected

    # 신규 생성 ID를 실제 연결 후보로 넘겨 이력 범위 검사가 생략되어도 통과하는 것을 막는다.
    niacinamide = models.Material(case_id=c.case_id, name='Niacinamide')
    db.add(niacinamide)
    db.flush()
    record_unlink(niacinamide.material_id)
    added = m._auto_link_pm(db, c.case_id, single_product=True, material_ids=[niacinamide.material_id],
        material_sources={'niacinamide': {source_name}} if source_name else None)
    db.flush()
    should_link = source_name is not None and scope != 'same-case-human'
    if should_link:
        expected.add((product.product_id, niacinamide.material_id))
    assert added == int(should_link)
    assert {(row.product_id, row.material_id) for row in
            db.query(models.ProductMaterial).filter_by(case_id=c.case_id)} == expected
    assert db.query(models.ProductMaterial).filter_by(case_id=other.case_id).count() == 0


P1_ROUTES = ['aggregate', 'parse-file', 'reprocess', 'intake-zip', 'intake-zip-stream', 'apply']


def _p1_ingest(case, monkeypatch, http, route, text, fields):
    db, c = case
    if route == 'aggregate':
        doc = {'doc_type': 'coa_msds', 'filename': 'MSDS.txt', 'text_full': text, 'fields': fields}
        result = m._apply_intake_autofill(db, c, {
            'classified': [doc], 'extracted': intake.aggregate_fields([doc])})
        db.commit()
        return result
    if route == 'reprocess':
        return reprocess(case, monkeypatch, text, fields)[1]['applied']
    if route == 'apply':
        reprocess(case, monkeypatch, text, fields, apply=False)
        client, headers = http
        response = client.post(f'/cases/{c.case_id}/pending-extractions/apply',
            headers=headers['applicant'], json={'values': fields['material_names']})
        assert response.status_code == 200, response.text
        db.expire_all()
        return response.json()['applied']
    return _round4_product_request(case, monkeypatch, http, route, text, fields)


@pytest.mark.parametrize('route', P1_ROUTES)
@pytest.mark.parametrize('legacy_unlink', [False, True])
@pytest.mark.parametrize('source_name', [None, 'Face Serum'], ids=['missing-product', 'matching-product'])
def test_p1_registration_never_links_existing_material_without_unlink_audit(
        case, monkeypatch, round4_http, route, legacy_unlink, source_name):
    db, c = case
    product = models.Product(case_id=c.case_id, name='Face Serum')
    water = models.Material(case_id=c.case_id, name='Water')
    db.add_all([product, water])
    db.flush()
    if legacy_unlink:
        link = models.ProductMaterial(case_id=c.case_id, product_id=product.product_id,
                                      material_id=water.material_id)
        db.add(link)
        db.commit()
        # b9f4aa4의 linked=False와 동일하게 연결만 삭제하며 감사기록은 없다.
        db.delete(link)
    db.commit()
    assert db.query(models.AuditLog).filter_by(case_id=c.case_id, action='matrix.link').count() == 0
    text = (f'Product name: {source_name}\n' if source_name else '') + 'Ingredients\nSalt 100%'
    _p1_ingest(case, monkeypatch, round4_http, route, text, {'material_names': ['Salt']})
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    expected = {(product.product_id, salt.material_id)} if source_name else set()
    assert {(row.product_id, row.material_id) for row in
            db.query(models.ProductMaterial).filter_by(case_id=c.case_id)} == expected


@pytest.mark.parametrize('route', P1_ROUTES)
@pytest.mark.parametrize('source_name', ['Face Serum', 'Body Serum', None])
def test_p1_deleted_product_blocks_links_to_remaining_product(
        case, monkeypatch, round4_http, route, source_name):
    db, c = case
    face = models.Product(case_id=c.case_id, name='Face Serum')
    body = models.Product(case_id=c.case_id, name='Body Serum')
    water = models.Material(case_id=c.case_id, name='Water')
    db.add_all([face, body, water])
    db.flush()
    db.add(models.ProductMaterial(case_id=c.case_id, product_id=face.product_id,
                                  material_id=water.material_id))
    db.commit()
    client, headers = round4_http
    response = client.delete(f'/cases/{c.case_id}/products/{face.product_id}', headers=headers['applicant'])
    assert response.status_code == 200, response.text
    db.expire_all()
    fields = {'material_names': ['Water', 'Salt']}
    if source_name:
        fields['product_names'] = [source_name]
    _p1_ingest(case, monkeypatch, round4_http, route,
               (f'Product name: {source_name}\n' if source_name else '') +
               'Ingredients\nWater 90%\nSalt 10%', fields)
    assert names(db, c, models.Product) == ['Body Serum']
    assert set(names(db, c, models.Material)) == {'Water', 'Salt'}
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == 0


@pytest.mark.parametrize('route', P1_ROUTES)
@pytest.mark.parametrize('source_names,expected', [
    ([], False), ([' FACE-SERUM '], True), (['얼굴 세럼'], True),
    (['Body Serum'], False), (['Face Serum', 'Body Serum'], False),
])
def test_p1_links_require_source_product_name_or_alias_match(
        case, monkeypatch, round4_http, route, source_names, expected):
    db, c = case
    product = models.Product(case_id=c.case_id, name='Face Serum')
    db.add(product)
    db.flush()
    c.profile_ext = {'product_aliases': {product.product_id: ['얼굴 세럼']}}
    db.commit()
    text = ''.join(f'Product name: {name}\n' for name in source_names) + 'Ingredients\nSalt 100%'
    _p1_ingest(case, monkeypatch, round4_http, route, text,
               {'material_names': ['Salt'], 'product_names': source_names})
    assert names(db, c, models.Material) == ['Salt']
    assert db.query(models.Product).filter_by(case_id=c.case_id).count() == 1
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == int(expected)


@pytest.mark.parametrize('source_name,expected', [('Face Serum', 1), ('Body Serum', 0)])
def test_p1_pending_rename_keeps_original_document_product(case, monkeypatch, round4_http, source_name, expected):
    db, c = case
    db.add(models.Product(case_id=c.case_id, name='Face Serum'))
    db.commit()
    reprocess(case, monkeypatch, f'Product name: {source_name}\nIngredients\nSalt 100%',
              {'product_names': [source_name], 'material_names': ['Salt']}, apply=False)
    client, headers = round4_http
    response = client.post(f'/cases/{c.case_id}/pending-extractions/apply', headers=headers['operator'],
                           json={'renames': {'Salt': 'Sea Salt'}, 'reason': '원문 이름 확인'})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert names(db, c, models.Material) == ['Sea Salt']
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == expected


def test_p1_new_product_does_not_link_preexisting_material(case):
    db, c = case
    db.add(models.Material(case_id=c.case_id, name='Water'))
    db.commit()
    applied = m._apply_agg_to_case(db, c, {'products': ['Face Serum'], 'materials': ['Water']})
    db.flush()
    assert applied['products'] == 1
    assert applied['materials'] == 0
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == 0


def test_p1_conflicting_upsert_does_not_count_existing_row_as_created(case, monkeypatch):
    db, c = case
    db.add(models.Product(case_id=c.case_id, name='Face Serum'))
    db.commit()
    upsert = m._upsert_material

    def concurrent_insert(db, material):
        upsert(db, material)
        return upsert(db, material)

    monkeypatch.setattr(m, '_upsert_material', concurrent_insert)
    applied = m._apply_agg_to_case(db, c, {'materials': ['Water']})
    db.flush()
    assert names(db, c, models.Material) == ['Water']
    assert applied['materials'] == 0
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == 0


@pytest.mark.parametrize('route', ['aggregate', 'reprocess'])
@pytest.mark.parametrize('repeat', ['1', ' ', ',', '%', '1 ', '1,', '1%', '1 % ', ', % '])
def test_p1_200k_numeric_metadata_is_pending_within_three_seconds(case, monkeypatch, route, repeat):
    db, c = case
    prefix = 'Ingredients\nWater | '
    text = prefix + (repeat * 200000)[:199999 - len(prefix)] + 'x'
    assert len(text) == 200000
    started = perf_counter()
    applied = _round3_ingest(case, monkeypatch, route, text, ['Water'])
    elapsed = perf_counter() - started
    assert elapsed < 3.0, (route, repeat, elapsed)
    assert applied['materials'] == 0
    assert names(db, c, models.Material) == []
    assert {item['value'] for item in m._pending_extractions(db, c.case_id)} == {'Water'}


@pytest.mark.parametrize('metadata', ['100%', '90.5 %', '< 0.5%', '10 - 20 %',
                                     '10–20%', '7732-18-5', 'CAS No: 7732-18-5', '95% w/w'])
def test_p1_linear_numeric_metadata_preserves_valid_rows(metadata):
    assert intake._autofill_material_row('Water | ' + metadata) == ['Water']


def test_p1_row_and_cell_limits_apply_before_trimming_or_numeric_validation():
    assert intake._autofill_material_row('Water 100%'.ljust(1000)) == ['Water']
    assert intake._autofill_material_row('Water 100%'.ljust(1001)) == ['Water']
    # 공백 포함 행의 안전 상한은 20,000자이고, 내부 항목·수치 셀은 각각 300자다.
    assert intake._autofill_material_row('Water 100%'.ljust(20000)) == ['Water']
    assert intake._autofill_material_row('Water 100%'.ljust(20001)) == []
    assert intake._autofill_material_row('Water ' + '1' * 294) == ['Water']
    assert intake._autofill_material_row('Water ' + '1' * 295) == []
    assert intake._autofill_material_row('Water | ' + '1' * 300) == ['Water']
    assert intake._autofill_material_row('Water | ' + '1' * 301) == []


@pytest.mark.parametrize('route', ['aggregate', 'reprocess'])
@pytest.mark.parametrize('tail', [
    'For details of ingredients: ' + 'x' * 20100,
    'Ingredients: ' + 'Water' + ' ' * 20000 + '100%',
    'Water | ' + '1' * 199979 + 'x',
], ids=['prose-label', 'oversized-section', 'numeric-row'])
def test_p1_oversized_later_rows_preserve_previously_accepted_materials(case, monkeypatch, route, tail):
    db, c = case
    text = 'Ingredients\nWater 90%\nSalt 10%\n' + tail
    applied = _round3_ingest(case, monkeypatch, route, text, ['Water', 'Salt'])
    assert intake._material_scan_text(text, material_only=True) == 'Water\nSalt'
    assert set(names(db, c, models.Material)) == {'Water', 'Salt'}
    assert applied['materials'] == 2
    assert m._pending_extractions(db, c.case_id) == []


@pytest.mark.parametrize('suffix', ['Water 100%', 'x', '1,2-Hexanediol'])
def test_p1_prose_ingredient_label_without_list_never_opens_section(suffix):
    text = f'For details of ingredients: {suffix}\nWater 90%\nSalt 10%'
    assert intake._material_scan_text(text, material_only=True) == ''


def test_p1_material_budget_preserves_only_the_accepted_prefix():
    text = 'Ingredients\n' + 'Water 90%\n' * 2000 + 'Salt 10%\nIngredients\nNiacinamide 100%'
    assert intake._material_scan_text(text, material_only=True).splitlines() == ['Water'] * 2000


def test_p1_inline_row_limit_counts_trailing_whitespace():
    text = 'Ingredients: Water, Salt' + ' ' * 200000
    assert intake._material_scan_text(text, material_only=True) == ''


def test_p1_comma_list_checks_individual_cells_within_row_limit():
    values = ['Water', 'Salt'] * 40
    text = 'Ingredients\n' + ', '.join(values)
    assert intake._material_scan_text(text, material_only=True).splitlines() == values


def test_p1_each_material_uses_its_own_document_product(case):
    db, c = case
    product = models.Product(case_id=c.case_id, name='Face Serum')
    db.add(product)
    db.commit()
    docs = [{'doc_type': 'coa_msds', 'filename': name + '.txt',
             'text_full': f'Product name: {product_name}\nIngredients\n{name} 100%',
             'fields': {'material_names': [name], 'product_names': [product_name]}}
            for name, product_name in [('Water', 'Face Serum'), ('Salt', 'Body Serum')]]
    applied = m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
    db.flush()
    water = db.query(models.Material).filter_by(case_id=c.case_id, name='Water').one()
    assert applied['materials'] == 2
    assert {(row.product_id, row.material_id) for row in
            db.query(models.ProductMaterial).filter_by(case_id=c.case_id)} == {
                (product.product_id, water.material_id)}


@pytest.mark.parametrize('same_case', [False, True])
@pytest.mark.parametrize('source_name', [None, 'Face Serum'], ids=['missing-product', 'matching-product'])
def test_p1_product_deletion_gate_uses_case_not_actor(case, same_case, source_name):
    db, c = case
    other = models.CaseApplication(org_id='org_demo', company_name='Other deletion case')
    db.add_all([other, models.Product(case_id=c.case_id, name='Face Serum')])
    db.flush()
    owner = c if same_case else other
    m._audit(db, {'uid': 'auto', 'role': 'system', 'org_id': 'org_demo'}, 'product.delete',
             'product', 'old-product', owner.case_id, {}, commit=False)
    db.commit()
    applied = m._apply_agg_to_case(db, c, {'materials': ['Water']},
        material_sources={'water': {source_name}} if source_name else None)
    db.flush()
    assert applied['materials'] == 1
    expected_links = int(source_name is not None and not same_case)
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == expected_links


@pytest.mark.parametrize('route', P1_ROUTES)
@pytest.mark.parametrize('product_text,product_fields,expected', [
    ('Product name: Body Serum\n', {}, 0),
    ('제품명: Body Serum\n', {'product_names': []}, 0),
    ('Nama produk: Body Serum\n', {'product_names': ['   ']}, 0),
    ('', {}, 0),
    ('Body Serum\n', {}, 0),
    ('Product name:\n', {}, 0),
    ('Product name: FACE-SERUM\n', {}, 1),
    ('제품명: 얼굴 세럼\n', {}, 1),
    ('Nama produk: Face Serum\n', {'product_names': []}, 1),
    ('Product name (English): Face Serum\n', {}, 1),
    ('Product name: Face Serum\nProduct name: Body Serum\n', {}, 0),
    ('Product name: Face Serum\nProduct name:\n', {}, 0),
    ('Product name: Face Serum\nReference: Body Serum\n', {'product_names': ['Body Serum']}, 0),
])
def test_r3_missing_product_extraction_requires_explicit_matching_label(
        case, monkeypatch, round4_http, route, product_text, product_fields, expected):
    db, c = case
    product = models.Product(case_id=c.case_id, name='Face Serum')
    db.add(product)
    db.flush()
    c.profile_ext = {'product_aliases': {product.product_id: ['얼굴 세럼']}}
    db.commit()
    _p1_ingest(case, monkeypatch, round4_http, route,
               product_text + 'Ingredients\nSalt 100%',
               {'material_names': ['Salt'], **product_fields})
    assert names(db, c, models.Material) == ['Salt']
    assert names(db, c, models.Product) == ['Face Serum']
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == expected


@pytest.mark.parametrize('rename', [False, True])
def test_r3_pending_uncertain_product_preserves_operating_zero_links(
        case, monkeypatch, round4_http, rename):
    db, c = case
    db.add(models.Product(case_id=c.case_id, name='Face Serum'))
    db.commit()
    reprocess(case, monkeypatch, 'Ingredients\nSalt 100%', {'material_names': ['Salt']}, apply=False)
    before = db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count()
    assert before == 0
    client, headers = round4_http
    body = ({'renames': {'Salt': 'Sea Salt'}, 'reason': '원료명 확인'} if rename else {'values': ['Salt']})
    response = client.post(f'/cases/{c.case_id}/pending-extractions/apply',
                           headers=headers['operator'], json=body)
    assert response.status_code == 200, response.text
    db.expire_all()
    assert response.json()['applied']['materials'] == 1
    assert names(db, c, models.Material) == ['Sea Salt' if rename else 'Salt']
    # 운영 b9f4aa4와 같이, 원료 확인만으로 제품 귀속을 추측하여 연결하지 않는다.
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == before


@pytest.mark.parametrize('unknown_text', ['', 'Product name: Body Serum\n'])
@pytest.mark.parametrize('route', ['aggregate', 'apply'])
@pytest.mark.parametrize('reverse', [False, True])
def test_r3_shared_material_keeps_each_documents_uncertain_or_conflicting_product(
        case, monkeypatch, round4_http, unknown_text, route, reverse):
    db, c = case
    db.add(models.Product(case_id=c.case_id, name='Face Serum'))
    db.commit()
    product_texts = ['Product name: Face Serum\n', unknown_text]
    if reverse:
        product_texts.reverse()
    docs = [{'doc_type': 'coa_msds', 'filename': f'MSDS-{index}.txt',
             'text_full': product_text + 'Ingredients\nSalt 100%', 'fields': {'material_names': ['Salt']}}
            for index, product_text in enumerate(product_texts)]
    if route == 'aggregate':
        m._apply_intake_autofill(db, c, {'classified': docs, 'extracted': intake.aggregate_fields(docs)})
    else:
        for doc in docs:
            reprocess(case, monkeypatch, doc['text_full'], doc['fields'], apply=False)
        client, headers = round4_http
        response = client.post(f'/cases/{c.case_id}/pending-extractions/apply',
                               headers=headers['applicant'], json={'values': ['Salt']})
        assert response.status_code == 200, response.text
        db.expire_all()
    db.flush()
    assert names(db, c, models.Material) == ['Salt']
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == 0


INCI_60 = NAMES + [
    'Camellia Sinensis Leaf Extract', 'Centella Asiatica Extract', 'Chamomilla Recutita Flower Extract',
    'Ceramide NP', 'Aloe Barbadensis Extract', 'Sodium Acetylated Hyaluronate',
    'Glycyrrhiza Glabra Root Extract', 'Ceramide AP', 'Polygonum Cuspidatum Root Extract',
    'Portulaca Oleracea Extract', 'Houttuynia Cordata Extract', 'Ceramide EOP',
    'Lavandula Angustifolia Oil', 'Palmitoyl Tripeptide-1', 'Sodium Hyaluronate Crosspolymer',
    'Acrylates/C10-30 Alkyl Acrylate', 'Saccharomyces Ferment Filtrate', 'Bifida Ferment Lysate',
    'Galactomyces Ferment Filtrate', 'Hydrolyzed Hyaluronic Acid', 'Hydrolyzed Collagen',
    'Cetearyl Ethylhexanoate', 'Caprylic/Capric Triglyceride', 'Polyglyceryl-10 Laurate',
    'Polyglyceryl-10 Myristate', 'PEG-60 Hydrogenated Castor Oil', 'Acrylates Copolymer',
    'Sodium Polyacrylate', 'Cyclomethicone', 'Chlorphenesin',
]


@pytest.mark.parametrize('route', P1_ROUTES[:-1])
@pytest.mark.parametrize('salt_tail', [False, True])
@pytest.mark.usefixtures('without_kma_catalogue')
def test_r3_1182_character_inci_list_registers_twelve_and_preserves_salt(
        case, monkeypatch, round4_http, route, salt_tail):
    db, c = case
    line = ', '.join(INCI_60)
    assert len(INCI_60) == len(set(INCI_60)) == 60
    assert len(line) == 1182
    assert max(map(len, INCI_60)) <= 36
    candidates = INCI_60 + (['Salt'] if salt_tail else [])
    text = 'Ingredients\n' + line + ('\nSalt 10%' if salt_tail else '')
    assert intake._material_scan_text(text, material_only=True).splitlines() == candidates
    _p1_ingest(case, monkeypatch, round4_http, route, text, {'material_names': candidates})
    registered = set(names(db, c, models.Material))
    assert registered == {
        'Water', 'Butylene Glycol', 'Niacinamide', 'Panthenol', 'Arginine', 'Citric Acid',
        'Sodium Citrate', 'Glucose', 'Fructose', 'Sucrose', 'Sodium Lactate', 'Lactic Acid',
    } | ({'Salt'} if salt_tail else set())
    assert len(registered) == 12 + int(salt_tail)
    pending = {item['value'] for item in m._pending_extractions(db, c.case_id)}
    assert pending == set(INCI_60) - registered
    assert len(pending) == 48


@pytest.mark.parametrize('heading', ['Ingredients\n', 'Ingredients: '])
@pytest.mark.parametrize('oversized', ['x' * 301, 'Water | ' + '1' * 301], ids=['name', 'metadata'])
def test_r3_oversized_item_does_not_discard_other_comma_items_or_following_rows(heading, oversized):
    text = heading + 'Niacinamide 1%, ' + oversized + ', Salt 10%\nWater 89%'
    assert intake._material_scan_text(text, material_only=True).splitlines() == ['Niacinamide', 'Salt', 'Water']


@pytest.mark.parametrize('route', ['aggregate', 'reprocess'])
@pytest.mark.parametrize('oversized', [
    'Niacinamide ' + 'x' * 20001, 'Ingredients: Niacinamide ' + 'x' * 20001,
    'Niacinamide | ' + '1' * 301, 'Niacinamide ' + '1' * 301,
    'Niacinamide ' + 'x' * 19980,
], ids=['row', 'inline-row', 'metadata-cell', 'item', 'cell-near-section-limit'])
def test_r3_oversized_row_or_cell_preserves_open_ingredient_section(case, monkeypatch, route, oversized):
    db, c = case
    text = 'Ingredients\nWater 90%\n' + oversized + '\nSalt 10%'
    assert intake._material_scan_text(text, material_only=True) == 'Water\nSalt'
    applied = _round3_ingest(case, monkeypatch, route, text, ['Water', 'Salt', 'Niacinamide'])
    assert applied['materials'] == 2
    assert set(names(db, c, models.Material)) == {'Water', 'Salt'}
    assert {item['value'] for item in m._pending_extractions(db, c.case_id)} == {'Niacinamide'}


@pytest.mark.parametrize('route', P1_ROUTES)
@pytest.mark.parametrize('product_text', [
    pytest.param('Product name: Body Serum\n', id='review-counterexample'),
    pytest.param('제품명: Body Serum\n', id='korean-label'),
    pytest.param('Nama produk: Body Serum\n', id='indonesian-label'),
    pytest.param('Product name: Face Serum\nProduct name: Body Serum\n', id='second-label-conflicts'),
    pytest.param('Product name: Body Serum\nProduct name: Face Serum\n', id='first-label-conflicts'),
])
def test_r4_explicit_product_label_conflict_blocks_ai_link_on_all_routes(
        case, monkeypatch, round4_http, route, product_text):
    db, c = case
    db.add(models.Product(case_id=c.case_id, name='Face Serum'))
    db.commit()
    text = product_text + 'Reference: Face Serum\nIngredients\nSalt 100%'
    _p1_ingest(case, monkeypatch, round4_http, route, text,
               {'material_names': ['Salt'], 'product_names': ['Face Serum']})
    assert names(db, c, models.Material) == ['Salt']
    assert names(db, c, models.Product) == ['Face Serum']
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == 0


@pytest.mark.parametrize('route', P1_ROUTES)
@pytest.mark.parametrize('product_text,product_fields,expected', [
    pytest.param('Product name: Face Serum\nReference: Body Serum\n',
                 {'product_names': ['Face Serum']}, 1, id='reference-is-not-product-label'),
    pytest.param('Product name: Face Serum\nProduct name: FACE-SERUM\n',
                 {'product_names': ['Face Serum']}, 1, id='matching-labels'),
    pytest.param('Product name: Face Serum\n제품명: 얼굴 세럼\n',
                 {'product_names': ['Face Serum']}, 1, id='matching-alias-labels'),
    pytest.param('Product name: Face Serum\nProduct name:\n',
                 {'product_names': ['Face Serum']}, 0, id='empty-second-label'),
    pytest.param('Reference: Face Serum\n',
                 {'product_names': ['Face Serum']}, 1, id='no-label-ai-match'),
    pytest.param('Reference: Body Serum\n',
                 {'product_names': ['Body Serum']}, 0, id='no-label-ai-conflict'),
    pytest.param('Reference: Face Serum\n', {}, 0, id='no-label-no-ai-product'),
])
def test_r4_matching_product_labels_and_unlabelled_documents_keep_existing_behavior(
        case, monkeypatch, round4_http, route, product_text, product_fields, expected):
    db, c = case
    product = models.Product(case_id=c.case_id, name='Face Serum')
    db.add(product)
    db.flush()
    c.profile_ext = {'product_aliases': {product.product_id: ['얼굴 세럼']}}
    db.commit()
    _p1_ingest(case, monkeypatch, round4_http, route,
               product_text + 'Ingredients\nSalt 100%',
               {'material_names': ['Salt'], **product_fields})
    assert names(db, c, models.Material) == ['Salt']
    assert names(db, c, models.Product) == ['Face Serum']
    assert db.query(models.ProductMaterial).filter_by(case_id=c.case_id).count() == expected


@pytest.mark.parametrize('route', P1_ROUTES[:-1])
@pytest.mark.parametrize('label', ['Cleaning instructions:', 'Instructions:', '사용법:', 'Cara pakai:'])
def test_r4_long_instructions_close_ingredient_section_on_all_automatic_routes(
        case, monkeypatch, round4_http, route, label):
    db, c = case
    product = models.Product(case_id=c.case_id, name='Face Serum')
    db.add(product)
    db.commit()
    instructions = label + ' ' + 'Use potable water for rinsing the equipment after every production batch. ' * 5
    assert len(instructions) > 300
    if label == 'Cleaning instructions:':
        assert len(instructions) == 393
    text = f'Product name: Face Serum\nIngredients\nSalt 100%\n{instructions}\nWater | 100%'
    _p1_ingest(case, monkeypatch, round4_http, route, text,
               {'material_names': ['Salt', 'Water'], 'product_names': ['Face Serum']})
    assert names(db, c, models.Material) == ['Salt']
    salt = db.query(models.Material).filter_by(case_id=c.case_id, name='Salt').one()
    assert {(row.product_id, row.material_id) for row in
            db.query(models.ProductMaterial).filter_by(case_id=c.case_id)} == {
                (product.product_id, salt.material_id)}
    assert {item['value'] for item in m._pending_extractions(db, c.case_id)
            if item['kind'] == 'material_names'} == {'Water'}


@pytest.mark.parametrize('size', [20, 400, 21000])
@pytest.mark.parametrize('boundary', [
    'Cleaning instructions: {body}', 'Instructions: {body}', '사용법: {body}',
    'Cara pakai: {body}', 'Directions: {body}', '비고: {body}', 'Catatan: {body}',
    'Use {body} for rinsing', '{body}.',
])
def test_r4_nonmaterial_labels_and_prose_close_section_regardless_of_length(size, boundary):
    line = boundary.format(body='x' * size)
    text = f'Ingredients\nSalt 100%\n{line}\nWater | 100%'
    assert intake._material_scan_text(text, material_only=True) == 'Salt'
    assert intake._autofill_material_row_evidence(line) == ([], False)


@pytest.mark.parametrize('heading', ['Ingredients\n', 'Ingredients: '])
@pytest.mark.parametrize('compound', ['D-Panthenol', 'L-Arginine'])
def test_r4_compound_initials_in_ingredient_lists_are_not_section_numbers(heading, compound):
    text = heading + f'Water, {compound}\nSalt 10%'
    assert intake._material_scan_text(text, material_only=True).splitlines() == ['Water', compound, 'Salt']
