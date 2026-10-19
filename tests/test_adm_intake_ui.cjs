/* Run with node: queue membership, pagination boundaries and duplicate-click guard. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const messages = [];
const context = vm.createContext({
  App: {}, RS: {cases: []}, S: {}, VIEWS: {},
  esc: value => String(value ?? ''), _s8label: () => '접수',
  canCall: () => true, render: () => {}, toast: text => messages.push(text),
  loadAdm: async () => true,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/static/v4/js/adm-review.js'), 'utf8'), context);
const html = () => vm.runInContext('ADMR_intakeHtml()', context);
context.RS.cases = Array.from({length: 21}, (_, i) => ({
  case_id: `pending-${String(i).padStart(2, '0')}`, company_name: `Pending ${i}`,
  step8: 0, status: 'onboarding', intake_pending: true,
  created_at: String(i).padStart(3, '0'),
}));
context.RS.cases.push({case_id:'done',company_name:'Approved company',step8:0,status:'application_draft',company_decision:'approved',intake_pending:false});
assert.equal((html().match(/App.admrApprove\(/g) || []).length, 10);
assert.match(html(), /1 \/ 3 페이지/);
assert.match(html(), /disabled>승인 완료/);
context.App.admrPage('pending', 3);
assert.equal((html().match(/App.admrApprove\(/g) || []).length, 1);
assert.match(html(), /21–21 \/ 21건/);
context.RS.cases = context.RS.cases.filter(c => c.case_id !== 'pending-20');
assert.match(html(), /2 \/ 2 페이지/); // Last row removed: clamp rather than show an empty page.

(async () => {
  context.App.admrPage('pending', 1);
  let finish, requests = 0;
  context.apiFetch = () => { requests++; return new Promise(resolve => { finish = resolve; }); };
  const first = context.App.admrApprove('pending-00');
  assert.match(html(), /disabled[^>]*onclick="App.admrApprove\('pending-00'\)/);
  await context.App.admrApprove('pending-00');
  assert.equal(requests, 1);
  finish({ok:true, json:async()=>({transitioned_to:'application_draft'})});
  await first;
  assert.equal(context.RS.cases.find(c=>c.case_id==='pending-00').intake_pending, false);
  assert.ok(!html().includes("App.admrApprove('pending-00')"));
  context.apiFetch = async () => ({ok:false,status:409,json:async()=>({detail:{message:'승인 불가',blockers:[{code:'TEST_BLOCKER'}]}})});
  await context.App.admrApprove('pending-01');
  assert.equal(context.RS.cases.find(c=>c.case_id==='pending-01').intake_pending,true);
  assert.ok(messages.some(text=>text.includes('TEST_BLOCKER')));
  const index=fs.readFileSync(path.join(__dirname,'../app/static/v4/index.html'),'utf8');
  const start=index.indexOf('async function loadAdm(){');
  const end=index.indexOf('/* ===== P7-2:',start);
  context.AUTH={username:'admin'};
  context.S8_LABELS=['접수','사전심사','계약','모의심사','현장심사','심의','발급','등록'];
  context.pad=n=>String(n).padStart(2,'0');
  vm.runInContext(index.slice(start,end),context);
  const many=Array.from({length:501},(_,i)=>({case_id:'case-'+i,company_name:'Company '+i,step8:0,status:'onboarding',intake_pending:true}));
  const requested=[];
  context.apiFetch=async url=>{
    requested.push(url);
    return {ok:true,json:async()=>url==='/me/nav-counts'?{staff_signup_pending:0}:{items:url.includes('offset=')?many.slice(500):many.slice(0,500),total:501}};
  };
  assert.equal(await context.loadAdm(),true);
  assert.equal(context.RS.cases.length,501);
  assert.ok(requested.includes('/cases?limit=500&offset=500'));
  const companyHtml=()=>context.VIEWS['adm-home']();
  assert.equal((companyHtml().match(/<tr><td><b>/g)||[]).length,10);
  context.App.admCompanyPage(51);
  assert.equal((companyHtml().match(/<tr><td><b>/g)||[]).length,1);
  assert.match(companyHtml(),/51 \/ 51 페이지/);
  context.apiFetch=async()=>({ok:false});
  assert.equal(await context.loadAdm(),false);
  assert.equal(context.RS.cases.length,501); // Failed refresh preserves the visible data.
  const assignStart=index.indexOf("VIEWS['adm-assign'] = () => {");
  const assignEnd=index.indexOf('App.selAssign =',assignStart);
  vm.runInContext(index.slice(assignStart,assignEnd),context);
  context.RS.admAud={auditors:[
    {user_id:'demo',username:'auditor1',is_demo:true,load:0,capacity:8},
    {user_id:'real',username:'real@example.com',display_name:'실제 심사원',is_demo:false,load:0,capacity:8},
  ]};
  let assignment=context.VIEWS['adm-assign']();
  assert.ok(!assignment.includes('<option value="demo">'));
  assert.match(assignment,/<option value="real">실제 심사원/);
  context.RS.admAud.auditors=context.RS.admAud.auditors.filter(a=>a.is_demo);
  assignment=context.VIEWS['adm-assign']();
  assert.match(assignment,/배정 가능한 실제 심사원 계정이 없습니다/);
  assert.match(assignment,/disabled[^>]*onclick="App.admAssignMain/);
  console.log('Admin intake UI checks passed');
})().catch(error => { console.error(error); process.exitCode=1; });
