"""Archive routing from ordinary upload fields and the legacy API error contract."""
import base64
import io
from pathlib import Path
import re
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

import app.main as m


ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "app/static/v4"


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setattr(m, "verify_enqueue", lambda did: False)
    monkeypatch.setattr(m, "autoparse_enqueue", lambda *a, **kw: False)
    with TestClient(m.app) as client:
        login = client.post("/auth/login", json={"username": "applicant1", "password": "pw"})
        assert login.status_code == 200, login.text
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        response = client.post("/cases", headers=headers, json={"company_name": "Synthetic Archive Upload"})
        assert response.status_code == 200, response.text
        yield client, headers, response.json()["case_id"]


@pytest.mark.parametrize("endpoint", ["parse-file", "documents"])
@pytest.mark.parametrize("filename", [
    "5. 원료 COA, MSDS.Zip", "아누리_1차_할랄_원료자료.Zip",
    "documents.zip", "documents.ZIP", "documents.rar", "documents.RaR",
    "documents.7z", "documents.7Z",
])
def test_archive_error_explains_zip_upload(case, endpoint, filename):
    client, headers, cid = case
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("synthetic.txt", "Synthetic document")
    response = client.post(f"/cases/{cid}/{endpoint}", headers=headers, json={
        "filename": filename, "doc_type": "material_list",
        "file_b64": base64.b64encode(archive.getvalue()).decode(),
    })
    assert response.status_code == 415, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "UNSUPPORTED_TYPE"
    assert detail["ext"] == filename.rsplit(".", 1)[-1].lower()
    assert "ZIP" in detail["message"]
    assert client.get(f"/cases/{cid}/documents", headers=headers).json() == []


@pytest.mark.parametrize("endpoint", ["parse-file", "documents"])
@pytest.mark.parametrize("ext", ["exe", "EXE", "bat"])
def test_other_unsupported_extensions_keep_original_detail(case, endpoint, ext):
    client, headers, cid = case
    response = client.post(f"/cases/{cid}/{endpoint}", headers=headers, json={
        "filename": "synthetic." + ext, "doc_type": "other",
        "file_b64": base64.b64encode(b"Synthetic unsupported upload").decode(),
    })
    assert response.status_code == 415, response.text
    detail = response.json()["detail"]
    assert detail == {"code": "UNSUPPORTED_TYPE", "ext": ext.lower()}
    assert "message" not in detail


def test_regular_upload_sources_route_archives_before_reading():
    index = (UI / "index.html").read_text()
    doc_upload = index.split("App.docUpload =", 1)[1].split("App.docVerify =", 1)[0]
    assert r"/\.(zip|rar|7z)$/i.test(f.name)" in doc_upload
    assert "App.entaZip(f,{cid,onProgress:" in doc_upload
    assert doc_upload.index("App.entaZip(") < doc_upload.index("new FileReader()")
    assert "intake-zip-stream" in doc_upload and "d.detail.message" in doc_upload
    inputs = re.findall(r'<input\b[^>]*\bid="doc-file"[^>]*>', index)
    assert len(inputs) == 2
    for tag in inputs:
        accepted = re.search(r'accept="([^"]+)"', tag)[1].split(",")
        assert {".zip", ".rar", ".7z"}.issubset(accepted)

    package = (UI / "js/package-check.js").read_text()
    material_upload = package.split("App.pkgMaterialFile =", 1)[1].split("App.pkgDocOk =", 1)[0]
    assert r"/\.(zip|rar|7z)$/i.test(file.name)" in material_upload
    assert "App.entaZip(file,{cid:s.cid,onProgress:" in material_upload
    assert material_upload.index("App.entaZip(") < material_upload.index("ENTA_fileB64(")
    assert re.search(r'accept="[^"]*\.zip,\.rar,\.7z"', package)
    for name in ["ent-apply-more", "package-check", "ent-tasks", "sjph-builder"]:
        assert f'js/{name}.js?v=20261119-zip' in index


def run_frontend(script):
    prelude = r"""
const assert=require('node:assert/strict');
const {harness,between}=require('./tests/fixtures/uiv4_frontend.cjs');
const file=name=>({name,size:4,arrayBuffer:async()=>new TextEncoder().encode('fake').buffer});
const stream=events=>new Response(events.map(e=>'data: '+JSON.stringify(e)+'\n\n').join(''));
const events=[{type:'start',file_count:2},{type:'progress',index:1,total:2,filename:'one.txt'},
  {type:'result',classified:[{filename:'one.txt'},{filename:'two.txt'}],file_count:2}];
function screen(lang='ko'){
  const h=harness(lang),c=h.c;
  c.load('ent-apply-more.js'); c.load('package-check.js');
  c.run(between('App.docUpload =','App.docVerify ='));
  c._docTypeForCase=()=> 'material_list';
  c.FileReader=class {constructor(){throw Error('Archive reached FileReader');}};
  c.loadEntDocs=async cid=>{assert.equal(c.RS.entDocs[cid],undefined);c.RS.entDocs[cid]=['fresh'];};
  c.PKG_load=async cid=>{c.RS.lastPackage=cid;};
  return h;
}
"""
    result = subprocess.run(
        ["node", "--unhandled-rejections=strict", "-e", prelude +
         "\n(async()=>{\n" + script + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"],
        cwd=ROOT, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_regular_uploads_use_selected_case_and_localized_progress():
    run_frontend(r"""
for(const lang of ['ko','en','id']) for(const name of ['5. 원료 COA, MSDS.Zip','bundle.RAR','bundle.7Z']){
  for(const source of ['docs','materials']){
    const {c,dom,notices}=screen(lang),posts=[];
    c.S.view='cons-docs'; c.AUTH.role='consultant';
    c.RS.entDocs.c2=['stale'];
    c.apiFetch=async(path,opts={})=>{
      if(opts.method==='POST'){posts.push({path,body:JSON.parse(opts.body)});return stream(events);}
      assert.equal(path,'/cases/c2/pending-extractions');
      return new Response(JSON.stringify({items:[{kind:'material_names',value:'Review me'}]}));
    };
    if(source==='docs'){
      dom['doc-file']={files:[file(name)]}; await c.App.docUpload('c2');
      for(const key of ['upload.text_028','upload.text_032','upload.text_033'])
        assert.ok(notices.includes(c.ENTA_t(key)),key);
      assert.ok(notices.includes(c.ENTA_t('upload.processing',{index:1,total:2,filename:'one.txt'})));
      assert.ok(notices.at(-1).includes(c.ENTA_t('upload.materials_pending',{count:1})));
      if(lang!=='ko')assert.doesNotMatch(notices.join(' '),/[가-힣]/);
    }else{
      dom['pk-material']={innerHTML:''};
      c.S.pkgMaterial={cid:'c2',rows:[],pending:[],failed:[],busy:false};
      c.INTAKE_conflicts=()=>'';
      await c.App.pkgMaterialFile(file(name));
      assert.equal(c.S.pkgMaterial.busy,false);
      assert.ok(c.S.pkgMaterial.fileResult.includes(c.ENTA_t('upload.archive_done',{count:2})));
      assert.ok(c.S.pkgMaterial.fileResult.includes(c.run("t('upload.archive_review')")));
      assert.ok(dom['pk-material'].innerHTML.includes('Review me'));
      if(lang!=='ko')assert.doesNotMatch(c.S.pkgMaterial.fileResult.slice(name.length),/[가-힣]/);
    }
    assert.equal(posts.length,1);
    assert.equal(posts[0].path,'/cases/c2/intake-zip-stream');
    assert.ok(posts[0].body.zip_b64 && !posts[0].body.file_b64);
    assert.equal(c.RS.entDocs.c2[0],'fresh');
    assert.equal(c.RS.lastPackage,'c2');
  }
}
""")


def test_zip_progress_errors_and_original_case_guard():
    run_frontend(r"""
for(const failure of ['http','stream','missing_result','read']){
  const {c}=screen(),progress=[];
  c.apiFetch=async()=>failure==='http'?new Response(JSON.stringify({detail:{code:'UNSUPPORTED_TYPE'}}),{status:415}):
    stream(failure==='stream'?[{type:'error',code:'CONTENT_MISMATCH'}]:events.slice(0,2));
  const f=file('bundle.Zip'); if(failure==='read') f.arrayBuffer=async()=>{throw Error('read');};
  const result=await c.App.entaZip(f,{cid:'c2',onProgress:text=>{
    assert.equal(text,c.S.entaProg);progress.push(text);
  }});
  assert.equal(result,null);
  assert.ok(progress.at(-1).startsWith(c.ENTA_t('upload.zip_error')),failure);
}
for(const explicit of [false,true]){
  const {c}=screen(),posts=[];
  c.apiFetch=async(path,opts={})=>{if(opts.method==='POST'){posts.push(path);return stream(events);}return new Response('{}');};
  const progress=[],result=await c.App.entaZip(file('bundle.Zip'),{...(explicit?{cid:'c2'}:{}),
    onProgress:text=>{assert.equal(text,c.S.entaProg);progress.push(text);}});
  assert.ok(result);
  assert.equal(posts[0],'/cases/'+(explicit?'c2':'c1')+'/intake-zip-stream');
  assert.ok(progress.includes(c.ENTA_t('upload.starting',{count:2})));
  assert.ok(progress.includes(c.ENTA_t('upload.text_032')));
}
for(const change of ['case','owner']){
  const {c}=screen(); let posts=0;
  c.apiFetch=async()=>{posts++;return stream(events);};
  const f=file('bundle.Zip'); f.arrayBuffer=async()=>{
    if(change==='case')c.RS.cases=[{case_id:'changed'}]; else c.AUTH.username='someone_else';
    return new ArrayBuffer(4);
  };
  assert.equal(await c.App.entaZip(f,change==='owner'?{cid:'c2'}:{}),null);
  assert.equal(posts,0);
}
""")


def test_archive_permission_and_media_guards_do_not_upload():
    run_frontend(r"""
for(const lang of ['ko','en','id']){
  const {c,dom,notices}=screen(lang);
  c.canCall=(method,path)=>!path.includes('intake-zip-stream');
  c.apiFetch=async()=>{throw Error('Upload without ZIP permission');};
  dom['doc-file']={files:[file('bundle.Zip')]};
  await c.App.docUpload('c2');
  c.S.pkgMaterial={cid:'c2',busy:false}; await c.App.pkgMaterialFile(file('bundle.Zip'));
  assert.equal(notices.length,2);
  assert.ok(notices.every(text=>text===c.run("t('upload.zip_permission')")));
  if(lang!=='ko') assert.doesNotMatch(notices.join(' '),/[가-힣]/);
}
const {c,notices}=screen(); c.load('ent-tasks.js'); c.load('sjph-builder.js');
c.apiFetch=async()=>{throw Error('Media archive reached API');};
let selected;
c.document.createElement=()=>({style:{},files:[selected],click(){this.onchange();},remove(){}});
c.document.body={appendChild(){}};
for(const name of ['image.Zip','stamp.RAR','evidence.7Z']){
  selected=file(name);
  c.App.enttUpload(0,selected); await c.App.sjbImg('c2',0); await c.App.sjbStamp('c2',0);
}
assert.equal(notices.length,9);
assert.ok(notices.every(text=>text==='압축 파일은 서류 올리기의 ZIP 버튼으로 올려 주세요.'));
""")


def test_regular_file_error_prefers_server_message():
    run_frontend(r"""
for(const message of ['Use ZIP upload for this archive.',null]){
  const {c,dom,notices}=screen(); let reading;
  c.FileReader=class {readAsDataURL(){this.result='data:;base64,ZmFrZQ==';reading=this.onload();}};
  c.apiFetch=async(path,opts)=>{
    assert.equal(path,'/cases/c2/parse-file');
    assert.equal(JSON.parse(opts.body).filename,'unsupported.exe');
    return new Response(JSON.stringify({detail:{code:'UNSUPPORTED_TYPE',...(message?{message}:{})}}),{status:415});
  };
  dom['doc-file']={files:[file('unsupported.exe')]};
  await c.App.docUpload('c2'); await reading;
  assert.equal(notices.at(-1),message||c.run("t('ui.text_128')")+' (UNSUPPORTED_TYPE)');
}
""")


def test_document_archive_double_click_and_busy_release():
    run_frontend(r"""
for(const lang of ['ko','en','id']) for(const outcome of ['success','empty','error']){
  const {c,dom,notices}=screen(lang);
  c.S.view='cons-docs'; c.S.selDocCo='c2';
  c.RS.cases=[{case_id:'c1'},{case_id:'c2'}];
  dom['doc-file']={files:[file('bundle.Zip')]};
  let finish,fail,calls=0;
  c.App.entaZip=()=>{calls++;return new Promise((resolve,reject)=>{finish=resolve;fail=reject;});};
  const first=c.App.docUpload('c2');
  assert.equal(c.S.docZipBusy,true);
  await c.App.docUpload('c2');
  assert.equal(calls,1);
  assert.equal(notices.at(-1),c.run("t('upload.zip_busy')"));
  if(lang!=='ko') assert.doesNotMatch(notices.at(-1),/[가-힣]/);
  if(outcome==='error') fail(Error('Specific upload failure'));
  else finish(outcome==='success'?{classified:[]}:null);
  await first;
  assert.equal(c.S.docZipBusy,false,outcome);
  if(outcome==='error') assert.equal(notices.at(-1),'Specific upload failure');
  const next=c.App.docUpload('c2');
  assert.equal(calls,2);
  finish(null); await next;
  assert.equal(c.S.docZipBusy,false);
}
""")


def test_document_archive_navigation_only_invalidates_original_cache():
    run_frontend(r"""
for(const view of ['ent-docs','cons-docs']) for(const change of ['view','case','fallback','owner']){
  const {c,dom,notices}=screen();
  c.S.view=view; c.S.selDocCo='c2';
  c.RS.cases=[{case_id:'c2'},{case_id:'c1'}];
  if(change==='fallback') c.S.selDocCo='missing';
  c.RS.entDocs={c2:['stale'],c1:['keep']};
  dom['doc-file']={files:[file('bundle.Zip')]};
  let finish,renders=0;
  const requests=[],refreshes=[];
  c.render=()=>{renders++;};
  c.loadEntDocs=async()=>{refreshes.push('documents');};
  c.loadEnt=async()=>{refreshes.push('cases');};
  c.PKG_load=async()=>{refreshes.push('package');};
  c.apiFetch=async(path,opts={})=>{
    requests.push({path,method:opts.method});
    assert.equal(opts.method,'POST','Stale upload fetched additional data');
    assert.equal(path,'/cases/c2/intake-zip-stream');
    return new Promise(resolve=>{finish=resolve;});
  };
  const job=c.App.docUpload('c2');
  while(!finish) await new Promise(setImmediate);
  if(change==='view') c.S.view='ent-home';
  else if(change==='owner') c.AUTH.username='another_user';
  else if(change==='case' && view==='cons-docs') c.S.selDocCo='c1';
  else c.RS.cases=[{case_id:'c1'},{case_id:'c2'}];
  const beforeRender=renders,beforeNotice=notices.length;
  finish(stream(events)); await job;
  assert.deepEqual(requests,[{path:'/cases/c2/intake-zip-stream',method:'POST'}]);
  assert.deepEqual(refreshes,[]);
  assert.equal(renders,beforeRender,view+' '+change);
  assert.equal(notices.length,beforeNotice,view+' '+change);
  assert.equal(c.RS.entDocs.c2,undefined);
  assert.deepEqual(c.RS.entDocs.c1,['keep']);
  assert.equal(c.S.docZipBusy,false);
}
""")


def test_document_archive_navigation_during_refresh_stops_later_updates():
    run_frontend(r"""
for(const phase of ['documents','cases','package']){
  const {c,dom}=screen();
  c.S.view='cons-docs'; c.S.selDocCo='c2'; c.RS.cases=[{case_id:'c1'},{case_id:'c2'}];
  dom['doc-file']={files:[file('bundle.Zip')]};
  c.App.entaZip=async()=>({classified:[]});
  const called=[]; let finish,renders=0;
  c.render=()=>{renders++;};
  for(const [name,fn] of [['documents','loadEntDocs'],['cases','loadEnt'],['package','PKG_load']]){
    c[fn]=async()=>{
      called.push(name);
      if(name===phase) await new Promise(resolve=>{finish=resolve;});
      if(name==='documents') c.RS.entDocs.c2=['fresh'];
    };
  }
  const job=c.App.docUpload('c2');
  while(!finish) await new Promise(setImmediate);
  c.S.selDocCo='c1';
  finish(); await job;
  assert.deepEqual(called,['documents','cases','package'].slice(0,['documents','cases','package'].indexOf(phase)+1));
  assert.equal(renders,0);
  assert.equal(c.RS.entDocs.c2,undefined);
  assert.equal(c.S.docZipBusy,false);
}
""")


def test_material_archive_busy_guard_and_specific_catch_errors():
    run_frontend(r"""
for(const lang of ['ko','en','id']) for(const outcome of ['message','progress','generic','empty']){
  const {c,dom}=screen(lang);
  dom['pk-material']={innerHTML:''}; c.INTAKE_conflicts=()=>'';
  const s=c.S.pkgMaterial={cid:'c2',rows:[],pending:[],failed:[],busy:false,fileResult:'Stale prior error'};
  let finish,fail,calls=0;
  const detail=c.ENTA_t('upload.zip_error')+c.ENTA_t('upload.mismatch');
  c.App.entaZip=(f,options)=>{
    calls++;
    options.onProgress(outcome==='generic'?c.ENTA_t('upload.text_028'):detail);
    return new Promise((resolve,reject)=>{finish=resolve;fail=reject;});
  };
  const job=c.App.pkgMaterialFile(file('bundle.Zip'));
  assert.equal(s.busy,true);
  await c.App.pkgMaterialFile(file('bundle.Zip'));
  assert.equal(calls,1);
  if(outcome==='empty') finish(null);
  else fail(outcome==='message'?Error('Specific exception'):{});
  await job;
  assert.equal(s.busy,false);
  const expected=outcome==='message'?c.ENTA_t('upload.zip_error')+'Specific exception':
    outcome==='generic'?c.ENTA_t('upload.zip_error')+c.ENTA_t('upload.file_error'):detail;
  assert.equal(s.fileResult,expected);
  if(lang!=='ko') assert.doesNotMatch(s.fileResult,/[가-힣]/);
}
""")
