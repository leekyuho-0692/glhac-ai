const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const root=path.join(__dirname,'../app/static/v4/js');
function harness(){
  const c=vm.createContext({console,Map,Set,URLSearchParams,encodeURIComponent,
    AUTH:{username:'applicant1',org_id:'org1'}, S:{lang:'ko'},
    RS:{cases:[{case_id:'c1'}],entpCase:{case_id:'c1',company_name:'원래 회사',email:null,profile_ext:{country:'KR'}}},
    VIEWS:{'ent-apply':()=>'', 'ent-home':()=>'', 'ent-contract':()=>''},App:{logout(){},setLang(){}},
    I18N:{ko:{},en:{},id:{}}, I18N_DOM:{en:{'식품':'Food','검증':'Verification','기업':'Company'}},
    $:()=>null,esc:x=>String(x??''),canCall:()=>true,render(){},toast(x){c.lastToast=x},
    document:{addEventListener(){},getElementById(){return null}},window:{addEventListener(){}},
    apiFetch:async()=>({ok:true,status:200,json:async()=>({})}),loadEnt:async()=>{},_money:String});
  c.load=name=>vm.runInContext(fs.readFileSync(path.join(root,name),'utf8'),c,{filename:name});
  c.run=code=>vm.runInContext(code,c);return c;
}
test('필수서류 객체·면제·응답 오류를 구분한다',()=>{
 const c=harness();c.load('ent-apply-more.js');
 c.RS.entaCheck={checklist:[{required:true,applicable:true,satisfied:false,doc_type_ko:'NIB'},{required:true,applicable:false,satisfied:false}]};
 assert.match(c.run('ENTA_checklistPanel()'),/필수 0\/1 제출/);
 c.RS.entaCheck={unexpected:true};assert.match(c.run('ENTA_checklistPanel()'),/확인하지 못했습니다/);
 assert.doesNotMatch(c.run('ENTA_checklistPanel()'),/0\/0/);
});
test('프로필 실패 시 save-draft를 호출하지 않고 입력을 유지한다',async()=>{
 const c=harness();c.load('ent-profile.js');c.load('ent-apply-more.js');
 c.run("ENTP_DRAFTS.set(ENTP_key('c1'),{company_name:'새 회사',email:'invalid'})");
 const calls=[];c.apiFetch=async(p,o)=>{calls.push([p,o]);return{ok:false,status:422,json:async()=>({detail:{message:'입력 오류'}})}};
 await c.App.entaSaveDraft();assert.equal(calls.length,1);assert.match(calls[0][0],/profile$/);
 assert.equal(c.run("ENTP_currentVal('company_name')"),'새 회사');assert.match(c.lastToast,/저장하지 못했습니다/);
});
test('닫힌 폼도 초안을 저장하며 저장 중 새 입력은 지우지 않는다',async()=>{
 const c=harness();c.load('ent-profile.js');c.load('ent-apply-more.js');
 c.run("ENTP_DRAFTS.set(ENTP_key('c1'),{company_name:'새 회사'})");const calls=[];
 c.apiFetch=async(p,o)=>{calls.push([p,o]);if(o?.method==='PATCH')c.run("ENTP_DRAFTS.set(ENTP_key('c1'),{company_name:'저장 중 추가 입력'})");return{ok:true,json:async()=>({case_id:'c1',company_name:'새 회사',profile_ext:{}})}};
 await c.App.entaSaveDraft();
 assert.equal(JSON.parse(calls[0][1].body).company_name,'새 회사');
 assert.ok(calls.some(([p])=>p.endsWith('/save-draft')));
 assert.equal(c.run("ENTP_currentVal('company_name')"),'저장 중 추가 입력');
 c.RS.cases=[{case_id:'c2'}];c.RS.entpCase={case_id:'c2',company_name:'다른 회사'};
 assert.equal(c.run("ENTP_currentVal('company_name')"),'다른 회사');
});
test('언어 전환은 회사명·제품명·자유 입력을 보존한다',()=>{
 const c=harness();c.load('i18n-dom.js');c.S.lang='en';
 c.run("I18D_remember({company_name:'UX검증 식품',products:[{name:'검증 식품'}],note:'식품 기업'})");
 assert.equal(c.run("I18D_tr('UX검증 식품')"),'UX검증 식품');
 assert.equal(c.run("I18D_tr('기업: UX검증 식품')"),'Company: UX검증 식품');
 assert.equal(c.run("I18D_tr('검증 식품')"),'검증 식품');
 assert.equal(c.run("I18D_tr('식품 기업')"),'식품 기업');
 assert.equal(c.run("I18D_tr('기업')"),'Company');
});
test('미평가와 완료 준비도, ready 표기를 구분한다',()=>{
 const c=harness();c.S.selCompany=null;c.load('case-insight.js');
 c.RS.cin['rd:c1']={readiness:0,band:'risk',evaluation_complete:false,breakdown:{documents:0,sjph:0,materials:null,audit:null},counts:{}};
 let html=c.run("CIN_readiness('c1')");assert.match(html,/평가 진행 중/);assert.match(html,/미평가/);assert.doesNotMatch(html,/null/);
 c.RS.cin['rd:c1']={readiness:100,band:'ready',evaluation_complete:true,breakdown:{documents:100,sjph:100,materials:100,audit:100},counts:{}};
 html=c.run("CIN_readiness('c1')");assert.match(html,/양호/);assert.match(html,/100점/);
});
test('빈 계약에 0원 및 기본 입금 기한을 표시하지 않는다',()=>{
 const c=harness();c.load('billing-ent.js');
 assert.doesNotMatch(c.run("BLE_contractPanel('c1',{contract:{exists:false}})"),/0 원/);
 assert.equal(c.run("BLE_payPanel('c1',{invoices:[],settings:{due_days:7}})"),'');
});
