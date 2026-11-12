/* R7 f4. Reuses the audit's source_probes.cjs VM approach and the repo harness.
 * R7_F4_ROOT points at an untouched git-archive copy for red/green comparisons.
 * Node VM checks are not a substitute for real-browser geometry verification. */
const {test}=require('node:test'), assert=require('node:assert/strict');
const fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const root=process.env.R7_F4_ROOT||path.resolve(__dirname,'..');
const {harness,between,source}=require(path.join(root,'tests/fixtures/uiv4_frontend.cjs'));
const legacy=fs.readFileSync(path.join(root,'app/static/index.html'),'utf8');
function section(text,a,b){const i=text.indexOf(a),j=text.indexOf(b,i);assert.ok(i>=0&&j>i,a);return text.slice(i,j);}
const entities={'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"};
const decode=s=>s.replace(/&amp;|&lt;|&gt;|&quot;|&#39;/g,k=>entities[k]);

for(const filename of ['process-flow.txt',`process 'single' "double" &quot; <tag> \\.txt`])test('N1 evidence click preserves '+filename,async()=>{
  const {c}=harness(),calls=[];let click;
  c.document.addEventListener=(type,listener)=>{if(type==='click')click=listener;};
  c.App.docDownload=(...args)=>calls.push(args);
  c.load('cons-sjph.js');c.run('CSJ_cache("c1").ev={t:1,items:[]}');
  c.item={document_id:'doc1',filename,uploaded:true};
  c.run('CSJ_cache("c1").ev.items=[item]');
  const html=c.run('CSJ_ev("c1")');
  const button=html.match(/<button class="link"([^>]*)>/)[1];
  const attrs=Object.fromEntries([...button.matchAll(/([\w-]+)="([^"]*)"/g)].map(m=>[m[1],decode(m[2])]));
  if(attrs.onclick) vm.runInContext(attrs.onclick,c); // reproduces Unexpected end of input on old code
  else {
    assert.equal(typeof click,'function');
    const dataset=Object.fromEntries(Object.entries(attrs).filter(([k])=>k.startsWith('data-')).map(([k,v])=>[k.slice(5).replace(/-([a-z])/g,(_,a)=>a.toUpperCase()),v]));
    await click({target:{closest:()=>({dataset})},preventDefault(){}});
  }
  assert.deepEqual(calls,[['doc1',filename]]);
  assert.ok(!Object.keys(attrs).some(k=>k.startsWith('on')));
});
test('Storage validation: SJPH save re-reads accepted data and preserves server rejection',async()=>{
  const {c,dom,notices}=harness(),calls=[];c.load('cons-sjph.js');
  c.run('CSJ_cache("c1").sjph={t:1,elements:[{element:"process",status:"gap",note:"old"}]}');
  dom['csj-st-0']={value:'ok'};dom['csj-note-0']={value:'수정 후 확인'};
  c.apiFetch=async(p,options={})=>{calls.push([p,options]);return{ok:true,status:200,json:async()=>({elements:[{element:'process',status:'ok',note:'수정 후 확인'}]})}};
  await c.App.csjSaveEl('c1',0);
  assert.deepEqual(JSON.parse(calls[0][1].body),{element:'process',status:'ok',note:'수정 후 확인'});
  assert.equal(calls[1][0],'/cases/c1/sjph?lang=ko');
  assert.equal(c.run('CSJ_cache("c1").sjph.elements[0].note'),'수정 후 확인');
  const count=notices.length;c.apiFetch=async()=>({ok:false,status:409,json:async()=>({detail:{code:'SCOPE_FROZEN'}})});
  await c.App.csjSaveEl('c1',0);
  assert.match(notices[count],/SCOPE_FROZEN/);assert.doesNotMatch(notices[count],/저장했습니다/);
});

test('N3 both case tables allocate readable columns inside a scroll container',()=>{
  const {c}=harness();c.load('stage-list.js');
  c.items=[{case_id:'c1',company_name:'아주 긴 한글 할랄 인증 기업',status:'document_pre_audit_in_review'}];
  const html=c.run('SL_detail("all",items)');
  assert.match(html,/class="[^"]*sl-table/);
  assert.match(source,/\.sl-table\s*\{[^}]*min-width:\s*(?:9\d\d|1\d{3})px/);
  assert.match(source,/\.sl-table[^}]*td:first-child[^}]*min-width:\s*1[6-9]\dpx/);
  const list=section(legacy,'SCREEN.caseList=','async function resendLogiSync');
  assert.match(list,/class="[^"]*case-table-scroll/);
  assert.match(legacy,/\.case-table-scroll\s*\{[^}]*overflow-x:\s*auto/);
  assert.match(legacy,/\.case-table\s*\{[^}]*min-width:\s*[7-9]\d\dpx/);
});

for(const lang of ['ko','en','id'])test('N4 '+lang+' eight-step layout respects responsive CSS',()=>{
  const {c}=harness(lang);c.run(between('function _stepper8(','VIEWS[\'ent-home\'] ='));
  const html=c._stepper8(2,false,false);
  assert.equal((html.match(/class="st /g)||[]).length,8);
  assert.doesNotMatch(html,/style="grid-template-columns/);
  assert.match(source,/\.stepper,\.dist\{grid-template-columns:repeat\(4,minmax\(0,1fr\)\)/);
  assert.match(source,/\.st-label\{[^}]*overflow-wrap:\s*anywhere/);
});

function sdc(pathway='undetermined',role='pen',decision=null){
  const h=harness();h.c.S.role=role;
  const item={case_id:'c1',company_name:'업체 원문',status:'pathway_determination',pathway,decision};
  h.c.RS.cases=[item];h.c.RS.pen={items:[item]};
  h.c.canCall=(m,p)=>role==='pen'?p.includes('/verify'):!p.includes('/verify');
  h.c.load('self-declare.js');h.c.load('pen-workspace.js');return h;
}
for(const pathway of ['undetermined','reguler','self_declare'])test('N5 pathway '+pathway+' controls and direct actions share a guard',async()=>{
  const {c,notices}=sdc(pathway),calls=[];c.apiFetch=async p=>{calls.push(p);return {ok:true,json:async()=>({}),text:async()=>''};};
  c.loadPen=async()=>{};
  const html=c.VIEWS['pen-home']();
  if(pathway==='self_declare') assert.match(html,/onclick="App.sdcVerify\(/);
  else {
    assert.doesNotMatch(html,/onclick="App.sdc(?:Verify|Preview|Pdf)/);
    await c.App.sdcVerify('c1','verified');await c.App.sdcPreview('c1');await c.App.sdcPdf('c1');
    c.App.sdcVerifyRejectOpen('c1');
    assert.deepEqual(calls,[],'programmatic/stale clicks cannot bypass the UI guard');
    assert.ok(notices.length>=3);
  }
  if(pathway==='undetermined') assert.doesNotMatch(html,/추가 검증은 필요 없습니다/);
});
test('N5 completed verification is read-only, but self-declaration remains available',()=>{
  const {c}=sdc('self_declare','pen','verified');
  const html=c.VIEWS['pen-home']();
  assert.doesNotMatch(html,/onclick="App.sdcVerify/);
  assert.match(html,/onclick="App.sdcPreview/);
});
test('N5 consultant cannot assign on an undetermined or regular pathway',async()=>{
  for(const p of ['undetermined','reguler']) {
    const {c,dom}=sdc(p,'cons');dom['sdc-pend']={value:'pendamping1'};let calls=0;
    c.apiFetch=async()=>{calls++;return{ok:true,json:async()=>({})}};
    assert.doesNotMatch(c.SDC_blocks('c1'),/onclick="App.sdcAssign/);
    await c.App.sdcAssign('c1');assert.equal(calls,0);
  }
});
test('N5 nested server restrictions are visible and decisions refresh from the server',async()=>{
  const {c,notices}=sdc('self_declare');let loaded=0;
  assert.doesNotThrow(()=>c.SDC_err('PDF',{status:500,data:{detail:null}}));
  c.apiFetch=async()=>({ok:false,status:400,json:async()=>({detail:{code:'NOT_SELF_DECLARE',hint:'Current pathway is reguler'}})});
  await c.App.sdcPreview('c1');
  assert.match(notices.join('\n'),/NOT_SELF_DECLARE/);assert.match(notices.join('\n'),/Current pathway is reguler/);
  c.apiFetch=async()=>({ok:true,status:200,json:async()=>({decision:'verified'})});
  c.loadPen=async()=>{loaded++;c.RS.pen.items[0].decision='verified'};
  await c.App.sdcVerify('c1','verified');assert.equal(loaded,1);
  assert.doesNotMatch(c.VIEWS['pen-home'](),/onclick="App.sdcVerify/);
});

async function materialHTML(rows){
  const c=vm.createContext({SCREEN:{},CURCASE:'c1',ROLE:'client',RBAC_ACTIONS:{},MAT_OPEN:new Map(),
    API:async()=>rows,ENUMS:async()=>({}),T:String,pageSlice:(_,items)=>({items}),pager:()=>'',
    matCategoryChecklist:()=>'',scol:s=>s==='haram'?'bad':s==='mushbooh'?'warn':'ok',rcol:s=>s==='high'?'bad':'ok',
    bTag:(color,s)=>`<b class="t-${color}">${s}</b>`,bSpan:String,statusColor:s=>s==='haram'?'bad':'ok'});
  for(const name of ['esc','safeClick','CAN'])vm.runInContext(legacy.match(new RegExp('^function '+name+'\\(.*$','m'))[0],c);
  vm.runInContext(section(legacy,'SCREEN.materials=async()=>{','async function exportMaterialsXlsx'),c);
  return c.SCREEN.materials();
}
for(const [status,severity,kind] of [['mushbooh','high','high'],['halal','critical','high'],['haram','low','high'],['mushbooh','medium','suspect'],['unknown','unknown','unknown']])test('N6 '+status+'/'+severity+' summary never claims safety',async()=>{
  const html=await materialHTML([{material_id:'m1',name:'Gelatin',status,severity,evidence:[]}]);
  assert.doesNotMatch(html,/의심 없음/);
  assert.match(html,new RegExp('data-risk="'+kind+'"'));
  assert.match(html,new RegExp('data-risk-summary="'+kind+'"'));
});
test('N6 empty and mixed lists distinguish unassessed from safe',async()=>{
  assert.doesNotMatch(await materialHTML([]),/의심 없음/);
  const html=await materialHTML([{name:'safe',status:'halal',severity:'low'},{name:'unknown',status:'unknown'}]);
  assert.doesNotMatch(html,/의심 없음/);assert.match(html,/미판정/);
  assert.match(await materialHTML([{name:'safe',status:'halal',severity:'low'}]),/의심 없음/);
});
test('N6 conflicting result and status never weaken a blocking or unknown finding',async()=>{
  for(const [result,kind] of [['BLOCK','high'],['UNKNOWN','unknown'],['NEEDS_EVIDENCE','suspect']]){
    const html=await materialHTML([{name:'conflict',status:'halal',result,severity:'low'}]);
    assert.doesNotMatch(html,/의심 없음/);assert.match(html,new RegExp('data-risk-summary="'+kind+'"'));
  }
});

for(const [lang,index,weekday,month] of [['en',0,'Sun','November'],['ko',1,'일','11월'],['id',2,'Min','November']])test('N7 '+lang+' calendars use the same locale-specific weekday/month format',async()=>{
  const c=vm.createContext({LANG:index});
  vm.runInContext(section(legacy,'const OC_DOW=','function ocPad'),c);
  assert.equal(c.ocDow()[0],weekday);assert.match(c.ocMonLbl(2026,10),new RegExp(month));
  assert.equal(c.ocDow().length,7);
  const ops=section(legacy,'SCREEN.opsCalendar=','let WFMON_STAGE=');
  assert.match(ops,/ocDow\(\)/);assert.match(ops,/ocMonLbl\(Y,M-1\)/);
});

for(const lang of ['en','id'])test('L1 '+lang+' uses API translations for material labels and placement reasons, including language switches',async()=>{
  const {c}=harness(lang),calls=[];c.RS.mats={};
  c.apiFetch=async p=>{calls.push(p);return{ok:true,json:async()=>({sections:[],slots:[]})}};
  c.run(between('async function loadMats(','const _sevAttn'));
  c.load('sjph-builder.js');await c.loadMats('c1');await c.SJB_load('c1');
  assert.ok(calls.some(p=>p.endsWith('/materials?lang='+lang)));
  assert.ok(calls.some(p=>p.endsWith('/manual-placement?lang='+lang)));
  c.load('i18n-dict.js');c.load('i18n-dom.js');c.RS.sjb.c1={cached:true};c.RS.mats.c1=[{cached:true}];
  c.App.setLang('ko');assert.ok(!c.RS.sjb.c1 && !c.RS.mats.c1);
});
test('L1 late Korean responses cannot replace the newly selected language',async()=>{
  const {c}=harness('ko');c.RS.mats={};const pending=[];
  c.apiFetch=p=>new Promise(resolve=>pending.push({p,resolve}));
  c.run(between('async function loadMats(','const _sevAttn'));c.load('sjph-builder.js');
  const loads=[c.loadMats('c1'),c.SJB_load('c1')];
  c.S.lang='en';c.RS.mats.c1=[{mat_type_label:'Raw material'}];c.RS.sjb.c1={P:{slots:[{reason:'dictionary'}]}};
  for(const r of pending)r.resolve({ok:true,json:async()=>({reason:'사전'})});
  await Promise.all(loads);
  assert.equal(c.RS.mats.c1[0].mat_type_label,'Raw material');assert.equal(c.RS.sjb.c1.P.slots[0].reason,'dictionary');
});
test('L1 operations notification requests include the selected language',async()=>{
  const {c}=harness('id'),calls=[];
  c.apiFetch=async p=>{calls.push(p);return{ok:true,json:async()=>({items:[]})}};
  // Only load the notification fetcher; unrelated operations tabs are not fixtures.
  c.RS.opsa={};c.run('const OPSA_LIMIT=100;'+section(fs.readFileSync(path.join(root,'app/static/v4/js/ops-audit.js'),'utf8'),'async function OPSA_loadNotifs(){','function OPSA_channelBadge'));
  await c.OPSA_loadNotifs();assert.match(calls[0],/[?&]lang=id/);
});
test('N2 report error can retry on the same date and recovers without losing filters',async()=>{
  const {c}=harness();c.load('report.js');c.S.rpGran='weekly';c.S.rpDate='2026-11-12';
  c.RS.rp={error:true,status:500};c.RS.rpKey='weekly|2026-11-12';c.RS.rpList={items:[]};
  assert.match(c.VIEWS['adm-report'](),/onclick="App.rpRetry\(\)"/);
  c.apiFetch=async()=>({ok:true,json:async()=>({metrics:{new_cases:1},trend:[],period:{label:'week'}})});
  await c.App.rpRetry();assert.equal(c.RS.rp.metrics.new_cases,1);assert.equal(c.S.rpGran,'weekly');assert.equal(c.S.rpDate,'2026-11-12');
});

// Optional standalone browser geometry proof, using the real renderers/styles.
// R7_F4_LAYOUT_HTML=/absolute/path/layout.html node tests/test_r7_f4_ui.cjs
// Open the resulting file in a browser; its visible result table measures each
// iframe at its own viewport, including scroll access to the last table column.
async function writeLayoutArtifact(output){
  const rows=[{case_id:'f4-layout',company_name:'할랄 인증 검증을 위한 아주 긴 한국어 업체명',scheme:'product',sector:'food',step8:3,
    status:'document_pre_audit_in_review',pathway:'reguler',next_action:'준비 서류를 검토하고 확인합니다',main_auditor:'오디터 검증',consultant:'컨설턴트 검증'}];
  const entries=[];
  for(const lang of ['ko','en','id']) {
    const {c}=harness(lang);c.load('stage-list.js');c.run(between('function _stepper8(','VIEWS[\'ent-home\'] ='));
    c.S8_LABELS=c.run("Array.from({length:8},(_,i)=>t('stage.step'+i))");c.items=rows;
    const stepper='<section class="panel">'+c._stepper8(3,false,false)+'</section>';
    const html=stepper+c.run('SL_detail("all",items)');
    const old=vm.createContext({LANG:{en:0,ko:1,id:2}[lang],SCREEN:{},FILTERS:{},CASE_SORT:'recent',ROLE:'client',
      loadCases:async()=>rows,pageSlice:(_,items)=>({items}),pager:()=>'',filterChips:()=>'',contractTurnRole:()=>'',
      bTag:(color,s)=>`<span class="tag t-${color}">${c.esc(s)}</span>`,statusColor:()=> 'warn',draftBadge:()=>'',schemeBadge:()=> 'Product',
      short:s=>s,esc:c.esc,scopeLabel:()=> 'assigned'});
    vm.runInContext(section(legacy,'var EN_STRINGS={','\nfunction T('),old);
    vm.runInContext(section(legacy,'const UI_STRINGS={','/* 전 언어 렌더 후처리'),old);
    for(const name of ['_dualSplit','T'])vm.runInContext(legacy.match(new RegExp('^function '+name+'\\(.*$','m'))[0],old);
    vm.runInContext(section(legacy,'SCREEN.caseList=','async function resendLogiSync'),old);
    const oldHTML=await old.SCREEN.caseList();
    for(const width of [390,768,1440])for(const [ui,body,css] of [['v4',html,source],['legacy',oldHTML,legacy]]){
      const doc=`<!doctype html><html lang="${lang}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${section(css,'<style>','</style>').slice(7)}</style><body><main style="padding:16px;min-width:0">${body}</main></body></html>`;
      entries.push({id:ui+'-'+lang+'-'+width,width,ui,doc});
    }
  }
  const escape=s=>s.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
  const check=()=>{
    let pass=0,fail=0;const reports=[];
    document.querySelectorAll('iframe').forEach(frame=>{
      const d=frame.contentDocument,win=frame.contentWindow;
      const v4=frame.dataset.ui==='v4',table=d.querySelector('table');
      const wrap=table.parentElement,cell=table.querySelector('tbody tr').cells[v4?0:1];
      const errors=[];
      if(d.documentElement.scrollWidth>d.documentElement.clientWidth+1)errors.push('page overflow');
      if(cell.getBoundingClientRect().width<150)errors.push('compressed company');
      if(table.getBoundingClientRect().width>wrap.clientWidth+1 && win.getComputedStyle(wrap).overflowX!=='auto')errors.push('missing inner scroll');
      wrap.scrollLeft=wrap.scrollWidth;const last=table.querySelector('tbody tr').lastElementChild.getBoundingClientRect(),box=wrap.getBoundingClientRect();
      if(last.right>box.right+2)errors.push('last action inaccessible');wrap.scrollLeft=0;
      if(v4){
        const labels=[...d.querySelectorAll('.st-label')];
        const rects=labels.map(label=>{const r=d.createRange();r.selectNodeContents(label);return [...r.getClientRects()];});
        rects.forEach((rs,i)=>rs.forEach(r=>rects.slice(i+1).flat().forEach(s=>{
          if(Math.min(r.right,s.right)-Math.max(r.left,s.left)>1 && Math.min(r.bottom,s.bottom)-Math.max(r.top,s.top)>1)errors.push('overlapping labels');
        })));
        const columns=win.getComputedStyle(d.querySelector('.stepper')).gridTemplateColumns.split(' ').length;
        if(frame.width==='390'&&columns!==4)errors.push('mobile grid '+columns);
      }
      if(errors.length)fail++;else pass++;
      reports.push(frame.title+': '+(errors.length?'FAIL '+errors.join(', '):'PASS'));
    });
    document.getElementById('results').textContent=`${pass} passed / ${fail} failed\n`+reports.join('\n');
  };
  fs.writeFileSync(output,`<!doctype html><html><meta charset="utf-8"><title>R7 f4 layout regression</title><style>body{font:14px system-ui;padding:16px}pre{padding:16px;background:#f2f5f7}iframe{display:block;border:1px solid #999;margin-bottom:24px}</style><h1>R7 f4 — actual renderer geometry</h1><pre id="results">Measuring…</pre>${entries.map(e=>`<h2>${e.id}</h2><iframe title="${e.id}" data-ui="${e.ui}" width="${e.width}" height="520" srcdoc="${escape(e.doc)}"></iframe>`).join('')}<script>window.addEventListener('load',${check.toString()});</script></html>`);
}
if(process.env.R7_F4_LAYOUT_HTML)writeLayoutArtifact(process.env.R7_F4_LAYOUT_HTML).catch(e=>{console.error(e);process.exitCode=1;});
