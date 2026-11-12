/* Real renderers and translations, without the asynchronous DOM translator. */
const test=require('node:test'), assert=require('node:assert/strict');
const {harness,between}=require('./fixtures/uiv4_frontend.cjs');
function setup(lang='ko'){
  const h=harness(lang),{c}=h; c.t=c.run('t');
  c.VIEWS['aud-detail']=()=>'<p>detail</p>';
  c.VIEWS['cons-status']=()=>'<p>company</p>';
  c.VIEWS['adm-flow']=()=>'<p>flow</p>';
  c.load('aud-intake.js');
  c.load('package-check.js');
  c.load('aud-docproc.js');
  return h;
}
function sample(){return {
  case_id:'c1', company:{company_name:'Intake Company',nib:null,profile_ext:{cp_name:'Contact Two',company_name_en:'English company'},org_profile_ext:{company_email:'company@example.com'}},
  contacts:{pic:{name:'Contact One'},cp:{name:'Contact Two',title:'Director',email:'cp@example.com',phone:'123'}},
  consent:{notify_consent:true,email_consent:{agreed:true,at:'2026-11-12',version:'1',channel:'signup'}},
  facilities:[{name:'Plant',address:'Busan',profile_ext:{phone:'0123'},scope_frozen:true,in_frozen_snapshot:true}],
  products:[{name:'Tea',category:'Drink',description:'Product description',material_count:1,status:'draft'}],
  materials:[{name:'Sugar',origin:'Indonesia',screen_result:'NEEDS_EVIDENCE',screen_status:'mushbooh',screen_severity:'high',declared_cert_no:'DECL',cert_no:null,evidence:[{filename:'Evidence',review_status:'pending'}]}],
  documents:[{document_id:'d1',filename:`doc ');window.bad=1;//.txt`,has_file:true,review_status:'pending',fields:{name:'<script>bad()</script>',address:'Street',city:'Seoul',phone:'1'},sources:{name:'structural',address:'llm',city:'manual',phone:'filename'},verify:{verdict:'warn',field_issues:[{field:'address',issue:'Check value'}]},truncated:true}],
  pending:{items:[{value:'Candidate',filename:'source.txt'}],conflicts:[{field:'address',current:'Old',proposed:'New',filename:'source.txt',reason:'value_conflict'}]},
  history:[],application:{status:'application_draft',draft_state:'saved',submitted_at:null,formal_application:{fields:{process_summary:'Mix'},pledge:true,signed:true}},
  summary:{problem_document_count:1,pending_count:1,conflict_count:1}
};}
for(const lang of ['ko','en','id']){
  test(`intake sections, empty values, sources and escaping: ${lang}`,()=>{
    const {c}=setup(lang); c.data=sample();
    const html=c.run('INTAKE_render(data)');
    for(const id of ['company','contacts','consent','facilities','products','materials','documents','pending','history','formal']) assert.ok(html.includes(`id="intake-${id}"`));
    for(const value of ['Contact Two','cp@example.com','Busan','Indonesia','DECL','company@example.com','Old','New','Check value']) assert.ok(html.includes(value),value);
    for(const source of ['structural','llm','manual','filename']) assert.ok(html.includes(`data-source="${source}"`));
    assert.ok(html.includes(c.t('intake.empty')));
    assert.ok(html.includes(c.t('intake.no_history')));
    assert.ok(html.includes(c.t('intake.truncated')));
    assert.ok(html.includes('App.ctlPreassessDocx'));
    assert.ok(!html.includes('<script>bad()'));
    assert.match(html,/&lt;script&gt;/);
    const onclicks=[...html.matchAll(/onclick="([^"]*)"/g)].map(m=>m[1]);
    assert.ok(onclicks.every(s=>!s.includes('window.bad')),'file names are never interpolated into handlers');
    assert.ok(!html.includes('intake.field.')&&!html.includes('intake.value.'),'no untranslated keys');
    if(lang!=='ko') assert.ok(!/[가-힣]/.test(html),html);
  });
  test(`applicant candidate panel exposes original selection and discussion only: ${lang}`,()=>{
    const {c}=setup(lang); c.s={cid:'c1',pending:[{value:'Candidate',name:'Renamed',selected:true}],conflicts:sample().pending.conflicts};
    for(const kind of ['material','product']){
      c.kind=kind; const html=c.run('PKG_pendingTable(s,kind)');
      assert.ok(!html.includes("'dismiss'"));
      assert.ok(!html.includes('oninput='));
      assert.ok(html.includes(c.t('intake.reviewer_notice')));
      assert.ok(html.includes('App.intakeMemo'));
      assert.ok(html.includes('Old')&&html.includes('New'));
      assert.ok(html.includes('Candidate')&&!html.includes('Renamed'));
      if(lang!=='ko') assert.ok(!/[가-힣]/.test(html),html);
    }
  });
}
test('aud-docproc renders a nonempty candidate list with conflicts and required reason',()=>{
  const {c}=setup(); c.AUTH.role='auditor';
  c.RS.auddDocs={c1:{rows:[]}};
  c.RS.auddPend={c1:{items:[{kind:'material_names',value:'Pending',filename:'source.txt'}],conflicts:sample().pending.conflicts}};
  const html=c.AUDD_aiqPanel('c1');
  assert.ok(html.includes('Pending')&&html.includes('Old')&&html.includes('New'));
  assert.match(html,/id="audd-apply-reason"[^>]*required/);
  assert.ok(html.includes('audd-name-0')&&html.includes('audd-dismiss-0'));
});
test('reviewer sends reason and original-to-renamed value; empty reason makes no request',async()=>{
  const {c,dom,notices}=setup(); c.AUTH.role='auditor';
  c.RS.auddPend={c1:{items:[{value:'Original'}]}};
  c.document.querySelectorAll=()=>[{checked:true,value:'0'}];
  dom['audd-apply-reason']={value:''}; dom['audd-name-0']={value:'Corrected'};
  const calls=[];
  c.apiFetch=async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({items:[]})}};
  await c.App.auddApply('c1'); assert.equal(calls.length,0); assert.ok(notices.includes(c.t('intake.reason_required')));
  dom['audd-apply-reason'].value='Compared with original';
  await c.App.auddApply('c1');
  const body=JSON.parse(calls[0].options.body);
  assert.equal(body.reason,'Compared with original'); assert.equal(body.renames.Original,'Corrected');
});
test('applicant apply never sends renames or dismiss even with stale UI state',async()=>{
  const {c}=setup(); const calls=[];
  c.apiFetch=async(path,options)=>{ if(options) calls.push(JSON.parse(options.body)); return {ok:true,json:async()=>({items:[]})}; };
  c.s={cid:'c1',pending:[{value:'Original',name:'Tampered',selected:true,dismiss:true}]};
  await c.PKG_pendingApply(c.s,'material');
  assert.deepEqual(calls[0],{values:['Original']});
});
test('RBAC allows assigned auditor candidate review and entry points reuse one view',()=>{
  const {c}=setup(); c.load('rbac.js');c.AUTH.role='auditor';
  c.run("RBAC.actions=new Set([])");
  assert.equal(c.canCall('POST','/cases/{}/pending-extractions/apply'),true);
  for(const [view,selection] of [['aud-detail','audCo'],['cons-status','selCompany'],['adm-flow','selFlow']]){
    c.S[selection]='c1'; assert.ok(c.VIEWS[view]().includes('App.intakeOpen'));
  }
  c.load('stage-list.js');c.data=[{case_id:'c1',company_name:'Company'}];
  assert.ok(c.run("SL_detail('all',data)").includes('App.intakeOpen'));
  assert.match(between("VIEWS['aud-pre'] =",'App.selPre ='),/INTAKE_button\(c.case_id\)/);
  assert.match(between("VIEWS['aud-pre'] =",'App.selPre ='),/INTAKE_button\(cur\)/);
});
test('intake fetch is one request, failure is visible, logout clears personal data',async()=>{
  const {c}=setup(); const calls=[]; c.S.intakeCase='c1';
  c.apiFetch=async path=>{calls.push(path); return {ok:true,json:async()=>sample()}};
  await c.INTAKE_load('c1');assert.deepEqual(calls,['/cases/c1/intake-view']);
  assert.ok(c.VIEWS['aud-intake']().includes('Intake Company'));
  c.apiFetch=async()=>({ok:false,status:403}); await c.App.intakeRefresh();
  assert.ok(c.VIEWS['aud-intake']().includes('role="alert"'));
  c.App.logout();assert.equal(c.S.intakeCase,null);assert.equal(Object.keys(c.RS.intake).length,0);
});
test('code-like company names and extracted free text remain verbatim in every language',()=>{
  for(const lang of ['ko','en','id']){
    const {c}=setup(lang);
    for(const field of ['company_name','name','filename','description','note','reason','material_names']){
      c.field=field;
      assert.equal(c.run("INTAKE_value('pending',field)"),'pending');
      assert.equal(c.run("INTAKE_value('halal',field)"),'halal');
    }
    assert.equal(c.run("INTAKE_value('pending','review_status')"),c.t('intake.value.pending'));
  }
});
