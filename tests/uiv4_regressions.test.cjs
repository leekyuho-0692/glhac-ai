const {test}=require('node:test'),assert=require('node:assert/strict');
const {harness,signup,between,source}=require('./fixtures/uiv4_frontend.cjs');
const {createHash}=require('node:crypto');
const digest=s=>createHash('sha256').update(s).digest('hex');
const file=(name,content)=>({name,size:Buffer.byteLength(content),arrayBuffer:async()=>new TextEncoder().encode(content).buffer});
function uploadHarness(){
 const h=harness();h.c.load('ent-apply-more.js');h.c.run('ENTA_pollBulk=async()=>{}');
 h.docs=[];h.posts=[];h.c.apiFetch=async(url,opts={})=>{
  if(opts.method==='POST'){const body=JSON.parse(opts.body);h.posts.push(body);return{ok:true,json:async()=>({document_id:'d'+h.posts.length,autoparse_queued:true})};}
  return{ok:true,json:async()=>h.docs};
 };return h;
}
test('H1 same name and same size with changed bytes uploads a new version',async()=>{
 const h=uploadHarness();await h.c.App.entaUpload([file('same.txt','AAAA')]);await h.c.App.entaUpload([file('same.txt','BBBB')]);
 assert.equal(h.posts.length,2);assert.equal(h.c.S.entaBulk.items[1].newVersion,true);
 assert.match(h.c.run('ENTA_applyPanel()'),/새 버전/);
 assert.equal(h.c.S.entaBulk.items[0].file_hash,digest('AAAA'));
});
test('H1 exact contents skip within a batch and after a page reload via server file_hash',async()=>{
 const h=uploadHarness();await h.c.App.entaUpload([file('same.txt','AAAA'),file('same.txt','AAAA')]);
 assert.equal(h.posts.length,1);assert.equal(h.c.S.entaBulk.items[1].status,'duplicate');
 const fresh=uploadHarness();fresh.docs=[{document_id:'existing',filename:'same.txt',file_hash:digest('AAAA')}];
 await fresh.c.App.entaUpload([file('same.txt','AAAA')]);assert.equal(fresh.posts.length,0);
 await fresh.c.App.entaUpload([file('same.txt','BBBB')]);assert.equal(fresh.posts.length,1);assert.equal(fresh.c.S.entaBulk.items[1].newVersion,true);
});
test('H1 missing crypto or legacy hash cannot cause a name/size skip',async()=>{
 for(const noCrypto of [false,true]){
  const h=uploadHarness();if(noCrypto)h.c.crypto=undefined;h.docs=[{document_id:'old',filename:'same.txt'}];
  await h.c.App.entaUpload([file('same.txt','AAAA'),file('same.txt','BBBB')]);assert.equal(h.posts.length,2);
 }
});
test('H1 retry, unreadable and oversized files retain upload safeguards',async()=>{
 const h=uploadHarness();const broken=file('bad.txt','AAAA');broken.arrayBuffer=async()=>{throw new Error('read')};
 await h.c.App.entaUpload([broken,{...file('large.txt','A'),size:21*1024*1024}]);assert.equal(h.posts.length,0);
 assert.ok(h.c.S.entaBulk.items.every(i=>i.status==='failed'));
 h.c.S.entaBulk.items[0].file=file('bad.txt','AAAA');await h.c.App.entaRetry(0);assert.equal(h.posts.length,1);
});
test('H1 switching company during hashing cancels before POST',async()=>{
 const h=uploadHarness();let release;const f=file('same.txt','AAAA');f.arrayBuffer=()=>new Promise(r=>release=r);
 const pending=h.c.App.entaUpload([f]);while(!release)await new Promise(setImmediate);
 h.c.RS.cases=[{case_id:'c2'}];release(new TextEncoder().encode('AAAA').buffer);await pending;
 assert.equal(h.posts.length,0);assert.equal(h.c.S.entaBulk.cancelled,true);
});
test('H1 successful signup consumes entry parameters and reload restores the saved case',async()=>{
 for(const query of ['?signup=ent','?ref=INVITE','?signup=ent&ref=INVITE&lang=id']){
  const h=signup('en');h.c.location=new URL('http://localhost/ui/v4/'+query+'#documents');
  h.c.history.state={keep:'state'};h.c.S.urlRef=h.c.location.searchParams.get('ref');
  let saved;h.c.authSave=()=>{saved={...h.c.AUTH}};
  const payload=await h.submit();
  assert.equal(payload.invite_code,query.includes('ref=')?'INVITE':undefined);
  assert.equal(h.c.location.searchParams.has('signup'),false);
  assert.equal(h.c.location.searchParams.has('ref'),false);
  assert.equal(h.c.location.hash,'#documents');
  assert.equal(h.c.history.state.keep,'state');
  if(query.includes('lang='))assert.equal(h.c.location.searchParams.get('lang'),'id');
  const fresh=harness('en');fresh.c.location=new URL(h.c.location);fresh.c.AUTH=saved;
  fresh.c.S.role=null;fresh.c.S.auth='login';fresh.c.RS.cases=[];
  fresh.c.run(between('async function restoreSession(){','const App ='));
  fresh.c.uiRoleFromServer=()=> 'ent';
  fresh.c.apiFetch=async()=>({ok:true,json:async()=>({role:'applicant',org_id:'org1'})});
  fresh.c.enterRole=async role=>{fresh.c.S.role=role;fresh.c.RS.cases=[{case_id:'persisted-case'}]};
  fresh.c.run(between('(function(){ try{ const q=new URLSearchParams(location.search);','render();\n// 모듈'));
  assert.equal(fresh.c.S.auth,'login');
  await fresh.c.restoreSession();
  assert.equal(fresh.c.S.role,'ent');assert.equal(fresh.c.RS.cases[0].case_id,'persisted-case');
 }
});
test('H1 rejected signup retains its invitation link and entered state',async()=>{
 const h=signup();h.c.location=new URL('http://localhost/ui/v4/?signup=ent&ref=INVITE');
 h.c.fetch=async()=>({ok:false,status:422,json:async()=>({detail:{code:'EMAIL_CONSENT_REQUIRED'}})});
 await h.c.App.submitEnt({preventDefault(){},target:{querySelector:()=>({})}});
 assert.equal(h.c.location.search,'?signup=ent&ref=INVITE');
 assert.equal(h.dom['es-name'].value,'식품 기업');
});
for(const lang of ['ko','en','id'])test('H2 '+lang+' option codes survive translated labels and enter the signup request',async()=>{
 const h=signup(lang);assert.equal(h.options.find(o=>o.value==='cosmetics').label,{ko:'화장품',en:'Cosmetics',id:'Kosmetik'}[lang]);
 assert.deepEqual((await h.submit()).sectors,['cosmetics']);
 if(lang!=='ko')assert.doesNotMatch(h.html,/[가-힣]/);
});
function stageHarness(){const h=harness();h.c.S.role='cons';h.c.load('stage-list.js');h.c.RS.sl={'all||':{items:Array.from({length:51},(_,i)=>({case_id:'c'+i,company_name:'Company '+i,step8:0,done:i===50}))}};return h;}
test('M3 direct entry initializes pagination and renders real rows',()=>{
 const {c}=stageHarness();let html=c.run('SL_view()');assert.equal(c.S.slPage,0);assert.match(html,/1\/2/);assert.match(html,/Company 0/);assert.doesNotMatch(html,/NaN/);
 assert.doesNotMatch(html,/<span class="count">[^<]*<button/);assert.match(html,/class="sl-pagination"/);
});
test('M3 complete filter, last-page bounds, empty results and invalid page recover',()=>{
 const {c}=stageHarness();c.run('SL_view()');c.App.slPage(1);c.App.slPage(1);assert.equal(c.S.slPage,1);
 let html=c.run('SL_view()');assert.match(html,/2\/2/);assert.match(html,/disabled onclick="App.slPage\(1\)/);
 c.App.slDone();html=c.run('SL_view()');assert.equal(c.S.slPage,0);assert.match(html,/Company 50/);assert.doesNotMatch(html,/Company 49/);
 c.RS.sl['all||'].items=[];c.S.slPage=NaN;html=c.run('SL_view()');assert.match(html,/1\/1/);assert.doesNotMatch(html,/NaN/);
});
test('M4 A then B uses the same selection as company detail',()=>{
 const {c}=stageHarness();c.S.selCompany='A';c.App.slOpenCase('B');assert.equal(c.S.selCompany,'B');assert.equal(c.S.view,'cons-status');assert.equal(c.S.selCase,undefined);
 c.RS.cases=[{case_id:'A',company_name:'Company A'},{case_id:'B',company_name:'Company B'}];c.RS.consPre={A:{},B:{}};c._stepper8=()=>'';
 c.run(between("VIEWS['cons-status'] =",'App.selCompany ='));
 assert.match(c.VIEWS['cons-status'](),/<h2[^>]*>Company B<\/h2>/);
 assert.doesNotMatch(c.VIEWS['cons-status'](),/<h2[^>]*>Company A<\/h2>/);
});
function membersHarness(){const h=harness();h.c.AUTH.role='admin';h.c.load('rbac.js');h.c.run(between('async function loadAdmMembers(){','App.memTab ='));return h;}
test('M5 operator does not request or render admin-only signups',async()=>{
 const {c}=membersHarness(),calls=[];c.AUTH.role='operator';c.apiFetch=async p=>{calls.push(p);return{ok:true,json:async()=>({items:[]})}};
 await c.loadAdmMembers();const html=c.VIEWS['adm-members']();assert.deepEqual(calls,['/admin/users']);
 assert.doesNotMatch(html,/data-member-panel="signups"|대기 중인 가입 신청이 없습니다/);assert.match(html,/권한 없음/);assert.match(html,/data-member-panel="users"/);
});
for(const failure of [403,500,'network','malformed'])test('M5 '+failure+' is distinct from a successful empty panel',async()=>{
 const {c}=membersHarness();c.apiFetch=async p=>{
  if(p.includes('staff-signups')){if(failure==='network')throw Error('offline');return{ok:failure==='malformed',status:failure,json:async()=>({oops:1})};}
  return{ok:true,json:async()=>({items:[{username:'Keep me',role:'consultant'}]})};
 };await c.loadAdmMembers();const html=c.VIEWS['adm-members']();assert.match(html,/Keep me/);assert.doesNotMatch(html,/대기 중인 가입 신청이 없습니다/);assert.match(html,failure===403?/권한 없음/:/조회에 실패/);
});
test('M5 successful zero signups renders the actual empty message',async()=>{
 const {c}=membersHarness();c.apiFetch=async()=>({ok:true,json:async()=>({items:[]})});await c.loadAdmMembers();assert.match(c.VIEWS['adm-members'](),/대기 중인 가입 신청이 없습니다/);
});
for(const lang of ['ko','en','id'])test('M6 '+lang+' distinguishes unverified, unavailable, success and actual mismatches',()=>{
 const {c}=harness(lang);c.run(between('function _docVerifyBadge(d){','async function loadEntDocs'));
 for(const doc of [{},{verify:{verdict:'unknown'}},{fields:{error:'LLM_UNAVAILABLE'},verify:{verdict:'warn',field_issues:[{field:'error',issue:'not in text'}]}}]){
  const html=c._docVerifyBadge(doc);assert.match(html,new RegExp(c.run("t('docs.ai_unchecked')")));assert.doesNotMatch(html,/class="badge attn"|title=""/);
 }
 for(const verdict of ['fail','warn'])assert.match(c._docVerifyBadge({verify:{verdict,field_issues:[{field:'company_name'}]}}),/class="badge attn"/);
 assert.match(c._docVerifyBadge({verify:{verdict:'ok'}}),/class="badge strong"/);
 assert.match(c._docVerifyBadge({fields:{error:'LLM_UNAVAILABLE'},verify:{verdict:'fail',source:'llm'}}),/class="badge attn"/);
});
for(const lang of ['en','id'])test('L1 '+lang+' roles, uploads, invitations and known system messages translate without touching data',()=>{
 const {c}=harness(lang);c.load('ent-apply-more.js');c.load('i18n-dict.js');c.load('i18n-dom.js');c.load('uiv4-audit-i18n.js');c.load('ux-focus.js');
 for(const role of ['applicant','consultant','penyelia_halal','pendamping_pph','auditor','fatwa_liaison','admin','operator']){c.AUTH.role=role;assert.doesNotMatch(c.UX_roleLabel(),/[가-힣]/);}
 c.AUTH.role='applicant';assert.doesNotMatch(c.run('ENTA_applyPanel()'),/[가-힣]/);
 assert.doesNotMatch(c.UIV4_systemText('AI 추출값입니다 — 원본 서류와 대조한 뒤 반영하세요.'),/[가-힣]/);
 c.run("I18D_remember({company_name:'식품 기업',note:'소기업 원료',filename:'화장품.pdf'})");assert.equal(c.run("I18D_tr('식품 기업')"),'식품 기업');assert.equal(c.run("I18D_tr('소기업 원료')"),'소기업 원료');
 c.RS.entaCheck={};c.RS.entDocs={c1:[]};c.App.setLang('ko');assert.equal(c.RS.entaCheck,null);assert.deepEqual(Object.keys(c.RS.entDocs),[]);
});
test('L1 added translation dictionaries have all three languages and identical placeholders',()=>{
 const {c}=harness();c.load('ent-apply-more.js');c.load('i18n-dict.js');c.load('i18n-dom.js');c.load('uiv4-audit-i18n.js');
 const entries=c.run('Object.entries({...UIV4_I18N,...ENTA_I18N,...UIV4_AUDIT_I18N})');
 for(const [key,values] of entries){assert.equal(values.length,3,key);for(const v of values)assert.ok(v,key);assert.doesNotMatch(values[1]+values[2],/[가-힣]/,key);const placeholders=v=>[...v.matchAll(/\{\w+\}/g)].map(m=>m[0]).sort();assert.deepEqual(placeholders(values[0]),placeholders(values[1]),key);assert.deepEqual(placeholders(values[0]),placeholders(values[2]),key);}
});

for(const lang of ['en','id'])test('L1 '+lang+' staff signup translates validation, duplicate and success messages',async()=>{
 const {c,dom,notices}=harness(lang);
 c.run(between('function consSignup(){','const NAV ='));
 c.renderShell=()=>'';
 c.load('users.js');
 const form=c.consSignup();assert.match(form,/value="consultant"/);assert.match(form,/value="auditor"/);assert.doesNotMatch(form,/[가-힣]/);
 c.run('Object.assign(App,({'+between('  dupReset()','  /* upload + autofill */')+'}));');
 dom['cs-email']={value:'staff@example.com',checkValidity:()=>false,focus(){}};dom['dup-msg']={};
 await c.App.dupCheck();assert.equal(dom['dup-msg'].textContent,c.run("t('signup.email_format')"));
 dom['cs-email'].checkValidity=()=>true;c.fetch=async()=>({ok:true,json:async()=>({available:false})});
 await c.App.dupCheck();assert.doesNotMatch(dom['dup-msg'].innerHTML,/[가-힣]/);assert.equal(c.S.dupEmail,null);
 c.fetch=async()=>({ok:true,json:async()=>({available:true})});await c.App.dupCheck();
 assert.equal(c.S.dupEmail,'staff@example.com');assert.doesNotMatch(dom['dup-msg'].textContent,/[가-힣]/);
 dom['cs-pw']={value:'Passw0rd!23'};dom['cs-pw2']={value:'Passw0rd!23'};
 c.apiCode=()=> 'SIGNUP_PENDING';c.fetch=async()=>({status:409,json:async()=>({})});
 const event={preventDefault(){},target:{querySelector:()=>({})}};
 await c.App.submitCons(event);assert.equal(notices.at(-1),c.run("t('signup.pending')"));
 c.fetch=async()=>({status:200,ok:true});await c.App.submitCons(event);
 assert.equal(c.S.auth,'login');assert.equal(notices.at(-1),c.run("t('signup.staff_submitted')"));
});
for(const lang of ['en','id'])test('L1 '+lang+' registration autofill translates labels and preserves company data',async()=>{
 const {c,dom}=harness(lang);
 c.run('Object.assign(App,({'+between('  bizDrop(e)','  /* 오디터 배정')+'}));');
 dom['biz-up']={files:[]};dom['biz-msg']={};await c.App.bizAutofill();
 assert.equal(dom['biz-msg'].textContent,c.run("t('signup.biz_required')"));
 dom['biz-up'].files=[{name:'company.pdf'}];
 c.FileReader=class{readAsDataURL(){this.result='data:application/pdf;base64,QQ==';this.onload();}};
 for(const id of ['es-name','es-biz','es-ceo','es-addr','es-name-en'])dom[id]={classList:{remove(){}}};c.markAuto=()=>{};
 c.fetch=async()=>({ok:true,json:async()=>({ocr_available:true,fields:{company_name:'식품 기업'},filled:['company_name'],missing:['nib']})});
 await c.App.bizAutofill();assert.equal(dom['es-name'].value,'식품 기업');assert.doesNotMatch(dom['biz-msg'].textContent,/[가-힣]/);
 assert.match(dom['biz-msg'].textContent,new RegExp(c.run("t('signup.company_name')")));
 assert.doesNotMatch(dom['es-name-en'].placeholder,/[가-힣]/);
});
for(const lang of ['en','id'])test('L1 '+lang+' company and consultant vaults translate labels and retain filenames',()=>{
 const {c}=harness(lang);c.run(between('const DOC_TYPES =','App.docSearch ='));
 c.run(between('function _docVaultBody(cid, docs){','/* ===== P4 슬라이스11'));
 c.RS.entDocs.c1=[{document_id:'d1',filename:'식품 원료.pdf',doc_type:'material_list',has_file:true,verify:{verdict:'unknown'}}];
 for(const view of ['ent-docs','cons-docs']){
  const html=c.VIEWS[view]();assert.match(html,/식품 원료.pdf/);
  assert.doesNotMatch(html.replaceAll('식품 원료.pdf','').replaceAll('식품 기업',''),/[가-힣]/);
  assert.doesNotMatch(html,/docs\.type\./);
 }
});
test('L1 sales profile labels follow language changes after module loading',()=>{
 const {c}=harness('en');c.load('pay-docs.js');
 for(const lang of ['en','id','ko']){
  c.S.lang=lang;const html=c.run("PAYD_meForm({profile:{company_name:'식품 기업'}})");
  assert.ok(html.includes('<label>'+c.run("t('ui.text_033')")+'</label>'));
  assert.match(html,/value="식품 기업"/);
  if(lang!=='ko')assert.doesNotMatch(html.replace('식품 기업',''),/[가-힣]/);
 }
});

test('L1 late document responses cannot overwrite the newly selected language',async()=>{
 const {c}=harness('ko');c.run(between('async function loadEntDocs(cid){',"VIEWS['ent-docs'] ="));
 let finish;c.apiFetch=()=>new Promise(r=>finish=r);const pending=c.loadEntDocs('c1');
 c.S.lang='en';finish({ok:true,json:async()=>[{doc_type_ko:'한글 응답'}]});await pending;
 assert.equal(c.RS.entDocs.c1,undefined);
 let requested;c.apiFetch=async p=>{requested=p;return{ok:true,json:async()=>[{doc_type_ko:'English response'}]}};
 await c.loadEntDocs('c1');assert.match(requested,/lang=en/);assert.equal(c.RS.entDocs.c1[0].doc_type_ko,'English response');
});
for(const lang of ['en','id'])test('L1 '+lang+' audit summaries and compliance notes are translated as system text',()=>{
 const {c}=harness(lang);c.load('i18n-dict.js');c.load('i18n-dom.js');c.load('uiv4-audit-i18n.js');c.load('ops-audit.js');c.load('audit-chain.js');c.AUTH.role='admin';
 c.RS.opsa.logs={items:[],total:0};c.RS.opsa.verify={integrity_ok:true,checked:3,legacy:2,epoch:'2026-01-01',tampered_count:0};
 assert.doesNotMatch(c.run("I18D_trUI(OPSA_renderLogs().replace(/<[^>]+>/g,' '))"),/[가-힣]/);
 for(const status of ['ok','ok_with_legacy','broken']){c.RS.ach.c1={status,count:3,verified:1,legacy:2,epoch:'2026-01-01',legacy_until:'2025-12-31',broken_at:'test-id'};assert.doesNotMatch(c.run("I18D_trUI(ACH_panel('c1').replace(/<[^>]+>/g,' '))"),/[가-힣]/);}
 for(const key of ['audit.psre_note','audit.tsa_note','audit.pii_off','audit.headers','audit.secret_default','audit.secret_custom','audit.lph_no']){const ko=c.run(`UIV4_AUDIT_I18N[${JSON.stringify(key)}][0]`);assert.doesNotMatch(c.UIV4_systemText(ko),/[가-힣]/);}
});

for(const lang of ['ko','en','id'])test('L1 '+lang+' assignment empty, loading, demo-only and real auditor states use keys',()=>{
 const {c}=harness(lang);c.run(between("VIEWS['adm-assign'] =",'App.selAssign ='));
 const clean=html=>html.replaceAll('식품 기업','').replaceAll('심사원 이름','');
 const check=html=>{if(lang!=='ko')assert.doesNotMatch(clean(html),/[가-힣]/);assert.doesNotMatch(html,/\bassign\.\w+/)};
 c.RS.cases=[];check(c.VIEWS['adm-assign']());
 c.RS.cases=[{case_id:'c1',company_name:'식품 기업'}];c.RS.admAud=null;c.RS._admAudLoading=true;
 assert.match(c.VIEWS['adm-assign'](),new RegExp(c.run("t('members.loading')")));check(c.VIEWS['adm-assign']());
 c.RS.admAud={auditors:[{user_id:'demo',is_demo:true}]};
 let html=c.VIEWS['adm-assign']();check(html);
 assert.ok(html.includes(c.run("t('assign.account_hint')")));assert.ok(html.includes(c.run("t('assign.no_auditors')")));
 assert.match(html,/disabled/);assert.match(html,/식품 기업/);
 c.RS.admAud.auditors.push({user_id:'real',username:'auditor',display_name:'심사원 이름',load:2,capacity:5});
 html=c.VIEWS['adm-assign']();check(html);assert.match(html,/심사원 이름/);assert.match(html,/2\/5/);
 assert.ok(!html.includes(c.run("t('assign.account_hint')")));
});

for(const lang of ['ko','en','id'])test('L1 '+lang+' application and profile render without DOM translation or empty-case reload loops',async()=>{
 const {c}=harness(lang),calls=[];
 c.apiFetch=async url=>{calls.push(url);return{ok:true,json:async()=>({case_id:'c1',items:[]})}};
 c.run(between("VIEWS['ent-apply'] =",'App.entAddProduct ='));c.load('ent-profile.js');
 const check=html=>{if(lang!=='ko')assert.doesNotMatch(html.replaceAll('식품 기업','').replaceAll('제품 이름','').replaceAll('감독자 이름',''),/[가-힣]/);assert.doesNotMatch(html,/\b(?:profile|application)\.\w+/)};
 c.RS.cases=[];check(c.VIEWS['ent-apply']());
 assert.ok(c.VIEWS['ent-apply']().includes(c.run("t('ui.text_111')")));
 assert.equal(calls.length,0);assert.equal(c.ENTP_profileHtml(),'');
 await c.ENTP_loadCase('');assert.equal(calls.length,0);
 c.RS.cases=[{case_id:'c1',company_name:'식품 기업',status:'onboarding'}];
 c.RS._entProdLoading='c1';c.RS._entpLoading='case';c.RS.entpPen={items:[]};
 check(c.VIEWS['ent-apply']());
 c.RS.entProd={c1:[]};c.RS.entpCase={case_id:'c1',company_name:'식품 기업'};
 check(c.VIEWS['ent-apply']());
 c.RS.entpPen={items:[{penyelia_id:'p1',name:'감독자 이름',status:'active'}],active_count:1};
 c.RS.entProd.c1=[{product_id:'p1',name:'제품 이름'}];check(c.VIEWS['ent-apply']());
 c.RS.cases[0].status='submitted';c.RS.cases[0].scheme='logistics';check(c.VIEWS['ent-apply']());
 c.S.entpOpen=false;check(c.ENTP_section());c.S.entpOpen=true;
 for(const status of [403,500]){c.RS.entpCase={case_id:'c1',error:true,status};c.RS.entpPen={error:true,status};check(c.ENTP_section());}
});

test('L1 profile field labels change language after module loading and retain user values',()=>{
 const {c}=harness('ko');c.load('ent-profile.js');
 c.RS.entpCase={case_id:'c1',company_name:'식품 기업',halal_supervisor:'감독자 이름'};
 for(const lang of ['en','id','ko']){
  c.S.lang=lang;const html=c.ENTP_profileHtml();
  assert.ok(html.includes(c.run("t('role.penyelia_halal')")));
  assert.match(html,/value="식품 기업"/);assert.match(html,/value="감독자 이름"/);
  if(lang!=='ko')assert.doesNotMatch(html.replaceAll('식품 기업','').replaceAll('감독자 이름',''),/[가-힣]/);
 }
});
