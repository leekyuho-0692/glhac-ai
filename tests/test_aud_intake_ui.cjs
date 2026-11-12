/* Real renderers and translations, without the asynchronous DOM translator. */
const test=require('node:test'), assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const {harness,between}=require('./fixtures/uiv4_frontend.cjs');
function setup(lang='ko'){
  const h=harness(lang),{c}=h; c.t=c.run('t');
  c.VIEWS['aud-detail']=()=>'<p>detail</p>';
  c.VIEWS['cons-status']=()=>'<p>company</p>';
  c.VIEWS['adm-flow']=()=>'<p>flow</p>';
  c.load('ent-profile.js');
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

// Read the shared domain dictionary: statuses/document types/material types have one source of truth.
const terms=JSON.parse(fs.readFileSync(path.join(__dirname,'../app/domain_dict.json'),'utf8'));
function domainLabels(){
  const labels={},value_labels={};
  for(const lang of ['ko','en','id']){
    labels[lang]={};value_labels[lang]={};
    for(const term of terms){
      const a=term.actions||{},label=term.labels?.[lang];if(!label)continue;
      if(term.axis==='EXTRACT_FIELD' && a.extract_field)labels[lang][a.extract_field]=label;
      const code={ENUM:a.enum_code,SOURCE:a.screen_source,MATERIAL:a.material_type,DOC:a.doc_type}[term.axis];
      if(code && (term.axis!=='DOC'||!value_labels[lang][code]||a.required))value_labels[lang][code]=label;
    }
  }
  return {labels,value_labels};
}
function visibleText(html){return html.replace(/<[^>]*>/g,' ').replace(/\s+/g,' ').trim();}
function localizedSample(){
  const data=sample();Object.assign(data,domainLabels());
  const at='2026-11-12T03:47:59.052652+00:00';
  Object.assign(data.company,{due_date:'2026-11-12',scheme:'product',pathway:'reguler',is_msme:'True'});
  Object.assign(data.company.profile_ext,{pic_name:'Contact One',cp_email:'cp@example.com',
    email_consent:{agreed:'True',at},sectors:['food']});
  Object.assign(data.company.org_profile_ext,{pic_name:'Contact One',cp_name:'Contact Two',email_consent:{agreed:'False',at}});
  data.consent={notify_consent:'True',email_consent:{agreed:true,at,version:'2026-11-11',channel:'signup'},org_email_consent:{agreed:'False',at}};
  data.facilities[0].profile_ext.pic_name='Factory Contact';
  Object.assign(data.application,{status:'consultant_review',draft_state:'completed',submitted_at:at});
  Object.assign(data.application.formal_application,{submitted_at:'2026-11-12T03:47:59.052652',submitted_role:'applicant'});
  Object.assign(data.documents[0],{doc_type:'material_list',confidence:.95,uploader_role:'auditor',uploaded_at:'2026-11-12T03:47:59Z',
    truncations:[{kind:'text_chars',limit:1000,total:2000}],problem_reasons:[
      {code:'LOW_CONFIDENCE'},
      {code:'FIELDS_MISSING',params:{fields:'nib, issuer, cert_no, expiry_date'},
        explanations:{ko:'필수 항목: nib, issuer, cert_no, expiry_date',en:'Required: nib, issuer, cert_no, expiry_date',id:'Wajib: nib, issuer, cert_no, expiry_date'}},
      {code:'VERIFY_WARN',params:{fields:'material_names / product_names (scope pending)'},
        explanations:{ko:'확인 필요: material_names / product_names (scope pending)',en:'Check: material_names / product_names (scope pending)',id:'Periksa: material_names / product_names (scope pending)'}}]});
  data.documents[0].verify={verdict:'warn',at,summary:'AI 검증을 하지 못함',field_issues:[
    {field:'company_name',issue:'company_name_mismatch',code:'company_name_mismatch'},
    {field:'document',code:'partial_verification',issue:'검증 입력 한도 초과 — 전체 원문·추출값의 담당자 검토 필요'},
    {field:'address',issue:'원문에 없는 값'}]};
  data.pending.items=[{kind:'material_names',value:'Candidate',reason:'llm',requires_scope_unlock:'False'}];
  data.history=[{actor_name:'Reviewer',actor_role:'consultant',at:'2026-11-12T03:47:59.052652',action:'material.add',details:{name:'New material',evidence_submitted:'False',created_at:at}}];
  data.materials[0].mat_type='raw';
  data.materials[0].evidence[0].verified_at=at;
  return data;
}
for(const lang of ['ko','en','id']){
  test(`intake visible text localizes metadata, timestamps and booleans: ${lang}`,()=>{
    const {c}=setup(lang);c.data=localizedSample();c.S.intakeCase='c1';c.RS.intake={c1:c.data};
    const before=JSON.stringify(c.data),html=c.run('INTAKE_render(data)'),text=visibleText(html);
    assert.doesNotMatch(text,/\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}|\b(?:true|false)\b/i);
    assert.doesNotMatch(text,/\b[a-zA-Z]+(?:_[a-zA-Z]+)+\b|\b(?:intake|role)\.[a-z_.]+/);
    assert.doesNotMatch(text,/\b(?:consultant_review|completed|material\.add|llm|text_chars)\b/);
    for(const label of ['intake.field.confidence','intake.field.kind','intake.field.limit','intake.field.evidence_submitted','intake.field.requires_scope_unlock','intake.value.completed','intake.kind.text_chars','intake.action.material.add','intake.yes','intake.no'])assert.ok(text.includes(c.t(label)),label);
    for(const value of ['consultant_review','material_list','raw'])assert.ok(text.includes(c.data.value_labels[lang][value]),value);
    assert.ok(text.includes('95%'));assert.ok(!text.includes('0.95'));
    assert.ok(text.includes(c.t('role.auditor'))&&text.includes(c.t('role.consultant')));
    if(lang!=='en')assert.doesNotMatch(text,/\b(?:Confidence|Kind|Limit|Evidence submitted|Requires scope unlock)\b/);
    assert.doesNotMatch(text,/\b(?:nib|issuer)\b/);
    if(lang!=='ko')assert.doesNotMatch(text,/[가-힣]/);
    const company=html.match(/<section id="intake-company"[^>]*>([\s\S]*?)<\/section>/)[1];
    for(const duplicate of ['Contact One','Contact Two','cp@example.com',c.t('intake.field.email_consent')])assert.ok(!company.includes(duplicate),duplicate);
    assert.equal((text.match(/Contact One/g)||[]).length,1);
    assert.equal((text.match(/Contact Two/g)||[]).length,1);
    assert.ok(html.includes('Factory Contact'),'facility contacts remain visible');
    assert.equal(JSON.stringify(c.data),before,'rendering does not mutate the projection');
    assert.deepEqual(Array.from(c.run('INTAKE_unknownKeys')),[]);
    assert.deepEqual(Array.from(c.run('INTAKE_unknownValues')),[]);
  });
  test(`unknown keys/codes are readable and recorded; user text stays intact: ${lang}`,()=>{
    const {c}=setup(lang);
    assert.equal(c.run("INTAKE_label('future_custom_key')"),'Future custom key');
    assert.equal(c.run("INTAKE_text('future_status_code','status')"),'Future status code');
    assert.ok(c.run("INTAKE_unknownKeys.has('future_custom_key')"));
    assert.ok(c.run("INTAKE_unknownValues.has('status:future_status_code')"));
    for(const field of ['name','company_name','description','filename','note','material_names']){
      c.field=field;
      for(const value of ['consultant_review','material_list','True','False','User_Name.txt']){
        c.value=value;assert.equal(c.run('INTAKE_text(value,field)'),value);
      }
    }
    assert.equal(c.run("INTAKE_text('False','agreed')"),c.t('intake.no'));
    assert.equal(c.run("INTAKE_text('True','custom_flag')"),c.t('intake.yes'));
    assert.equal(c.run("INTAKE_text(t('intake.no_history'),'actor_role')"),c.t('intake.no_history'));
    assert.equal(c.run("INTAKE_text(0,'confidence')"),'0%');
    assert.equal(c.run("INTAKE_text(1,'confidence')"),'100%');
    assert.equal(c.run("INTAKE_text('2026-02-30T03:47:00Z','at')"),c.t('intake.invalid_date'));
    assert.equal(c.run("INTAKE_text(null,'at')"),c.t('intake.empty'));
  });
}
for(const [zone,time] of [['Asia/Seoul','12:47'],['Asia/Jakarta','10:47'],['Asia/Makassar','11:47'],['UTC','03:47']]){
  test(`UTC, naive UTC and date-only formatting in browser timezone ${zone}`,()=>{
    const script=`const {harness}=require('./tests/fixtures/uiv4_frontend.cjs');
      const out={};for(const lang of ['ko','en','id']){const {c}=harness(lang);c.load('aud-intake.js');
        out[lang]=['2026-11-12T03:47:59.052652+00:00','2026-11-12T03:47:59Z','2026-11-12T03:47:59.052652','2026-11-12 03:47:59','2026-11-12T12:47:59+09:00','2026-11-12','2026-11-12T23:30:00Z'].map(value=>c.INTAKE_date(value));}
      process.stdout.write(JSON.stringify(out));`;
    const out=JSON.parse(execFileSync(process.execPath,['-e',script],{cwd:path.join(__dirname,'..'),env:{...process.env,TZ:zone},encoding:'utf8'}));
    for(const lang of ['ko','en','id']){
      for(const value of out[lang].slice(0,5))assert.equal(value,out[lang][0]);
      assert.doesNotMatch(out[lang].join(' '),/T\d{2}:|\+00:00|\.052652/);
    }
    assert.equal(out.ko[0],'2026-11-12 '+time);
    assert.equal(out.ko[5],'2026-11-12');
    assert.equal(out.en[5],'Nov 12, 2026');assert.equal(out.id[5],'12 Nov 2026');
    assert.ok(out.en[0].includes(time));assert.ok(out.id[0].includes(time.replace(':','.')));
    assert.equal(out.ko[6],zone==='UTC'?'2026-11-12 23:30':'2026-11-13 '+({'Asia/Seoul':'08:30','Asia/Jakarta':'06:30','Asia/Makassar':'07:30'}[zone]));
  });
}
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
