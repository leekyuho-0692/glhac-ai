/* Execute legacy renderers; actual geometry is checked by uileg-browser/check.py. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.env.UILEG_HTML || path.join(__dirname, '../app/static/index.html'), 'utf8');
function between(start, end) {
  const a=source.indexOf(start), b=source.indexOf(end,a);
  assert.ok(a>=0 && b>a,start);
  return source.slice(a,b);
}
function translations(c) {
  vm.runInContext(between('var EN_STRINGS={','\nfunction T('),c);
  vm.runInContext(between('const UI_STRINGS={','/* 전 언어 렌더 후처리'),c);
  for(const name of ['_dualSplit','T']) vm.runInContext(source.match(new RegExp('^function '+name+'\\(.*$','m'))[0],c);
}
function materials(actions,role,material={},translate=String) {
  const c=vm.createContext({SCREEN:{},CURCASE:'case1',ROLE:role,RBAC_ACTIONS:actions,MAT_OPEN:new Map(),
    API:async()=>[{material_id:'mat1',name:'Gelatin',status:'mushbooh',evidence:[],...material}],
    ENUMS:async()=>({material_type:[{value:'raw',label:'Raw'}]}),T:translate,
    pageSlice:(_,items)=>({items}),pager:()=>'',matCategoryChecklist:()=>'',scol:()=>'',rcol:()=>'',bTag:(_,s)=>s,statusColor:()=>''});
  for(const name of ['esc','safeClick']) vm.runInContext(source.match(new RegExp('^function '+name+'\\(.*$','m'))[0],c);
  vm.runInContext(source.match(/^function CAN\(.*$/m)[0],c);
  vm.runInContext(between('SCREEN.materials=async()=>{','async function exportMaterialsXlsx'),c);
  return c.SCREEN.materials();
}
function hasControl(html,fn) {
  return html.includes(`data-safe-click="${fn}"`) || html.includes(`onclick="${fn}(`);
}
test('M1 consultant controls follow server actions',async()=>{
  const html=await materials({'material.add':true,'material.delete':true,'material.evidence':true},'consultant');
  assert.ok(hasControl(html,'addMaterial'));
  for(const fn of ['renameMaterial','editMaterial','rescreen','delMaterial','attachEvidence']) assert.ok(html.includes(`data-safe-click="${fn}"`),fn);
  assert.ok(!html.includes('읽기전용'));
});
test('M1 pendamping mapped to client has no write controls',async()=>{
  const html=await materials({},'client');
  for(const fn of ['addMaterial','editMaterial','renameMaterial','rescreen','delMaterial','attachEvidence','dedupMaterials','reuseMaterials','intakeZip','labelOcr']) assert.ok(!hasControl(html,fn),fn);
  assert.match(html,/읽기전용/);
});
test('M1 missing actions fail closed even for frontend admin',async()=>{
  const html=await materials({},'admin');
  for(const fn of ['addMaterial','editMaterial','renameMaterial','rescreen','delMaterial','attachEvidence']) assert.ok(!hasControl(html,fn),fn);
});
const handlers=[['material.add',['renameMaterial','editMaterial','rescreen']],['material.delete',['delMaterial']],['material.evidence',['attachEvidence']]];
for(const [action,fns] of handlers) test('M1 independent action '+action,async()=>{
  const html=await materials({[action]:true},'auditor');
  for(const fn of fns) assert.ok(html.includes(`data-safe-click="${fn}"`),fn);
  for(const [other,others] of handlers) if(other!==action) for(const fn of others) assert.ok(!hasControl(html,fn),fn);
});
test('M1 material row delegates escaped arguments with translations and certificate fallback',async()=>{
  const payload=`a');window.__audit_marker=1;// " <img src=x onerror="window.__audit_marker=1"> &quot;\\\n\u2028\u2029`;
  const mid="mat');window.__audit_marker=1;//";
  const actions={'material.add':true,'material.delete':true,'material.evidence':true};
  const entities={'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"};
  for(const [cert_no,declared_cert_no,expectedCert] of [['',payload,payload],['verified',payload,'verified'],['','','']]) {
    const html=await materials(actions,'consultant',{material_id:mid,name:payload,manufacturer:payload,mat_type_code:payload,
      cert_no,declared_cert_no,evidence:[{}]},key=>'translated:'+key);
    const row=html.match(/<tbody>([\s\S]*?)<\/tbody>/)[1];
    assert.ok(row.includes('text-align:right;white-space:nowrap'));
    assert.ok(!row.includes('<img'));
    const buttons=[...row.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)];
    const expected=[['matEvidenceList',['case1',mid,payload]],['renameMaterial',[mid,payload]],
      ['editMaterial',[mid,{manufacturer:payload,mat_type_code:payload,cert_no:expectedCert}]],
      ['attachEvidence',['case1',mid]],['rescreen',[mid]],['delMaterial',[mid]]];
    assert.equal(buttons.length,expected.length);
    assert.equal(buttons[3][2],'translated:증빙');
    assert.equal(buttons[4][2],'translated:재스크');
    const calls=[];let listener,prevented=0,stopped=0;
    const c=vm.createContext({window:{__audit_marker:0},
      document:{addEventListener(type,fn,capture){assert.equal(type,'click');assert.equal(capture,true);listener=fn;}},
      ...Object.fromEntries(expected.map(([fn])=>[fn,(...args)=>calls.push([fn,args])]))});
    vm.runInContext(between('function safeClick(','/* end safe data events */'),c);
    for(const [index,button] of buttons.entries()) {
      const attrs=Object.fromEntries([...button[1].matchAll(/([\w-]+)="([^"]*)"/g)]
        .map(([,key,value])=>[key,value.replace(/&amp;|&lt;|&gt;|&quot;|&#39;/g,entity=>entities[entity])]));
      assert.ok(Object.keys(attrs).every(key=>!/^on/i.test(key)));
      assert.equal(attrs['data-safe-click'],expected[index][0]);
      assert.deepEqual(JSON.parse(attrs['data-safe-args']),expected[index][1]);
      const target={dataset:{safeClick:attrs['data-safe-click'],safeArgs:attrs['data-safe-args']}};
      listener({target:{closest:()=>target},preventDefault(){prevented++;},stopPropagation(){stopped++;}});
    }
    assert.deepEqual(JSON.parse(JSON.stringify(calls)),expected);
    assert.equal(c.window.__audit_marker,0);
    assert.equal(prevented,expected.length);
    assert.equal(stopped,expected.length);
  }
});
for(const [lang,index] of [['ko',1],['en',0],['id',2]]) test('M7 '+lang+' manual guidance uses message keys',()=>{
  const c=vm.createContext({LANG:index,esc:String});translations(c);
  vm.runInContext(between('let CAPS=null;','/* ── i18n 스캐폴딩'),c);
  vm.runInContext(`CAPS={ai_ready:false,capabilities:{llm:{ok:false},ocr:{ok:false}},
    manual_steps:['doc_classify','field_extract','explain','translate','ocr'].map(key=>({key,feature:'서버 원문은 바뀔 수 있음',instead:'직접 출력하면 실패'})),
    works_without_ai:Object.keys(CAP_WORK_KEYS)};`,c);
  const html=c.capBanner();
  assert.equal((html.match(/data-cap-step=/g)||[]).length,5);
  assert.equal((html.match(/✓ /g)||[]).length,7);
  assert.ok(!html.includes('서버 원문') && !html.includes('직접 출력'));
  assert.ok(!/cap\.(doc|field|work|ocr|explain|translate)/.test(html));
  if(lang!=='ko') assert.ok(!/[가-힣]/.test(html),html);
  else assert.match(html,/파일명 사전으로 판정합니다/);
  vm.runInContext("CAPS.manual_steps=[{key:'future_feature',feature:'서버 한국어'}];CAPS.works_without_ai=['새 업무'];",c);
  const unknown=c.capBanner();
  assert.ok(!unknown.includes('서버 한국어') && !unknown.includes('새 업무'));
  if(lang!=='ko') assert.ok(!/[가-힣]/.test(unknown),unknown);
  vm.runInContext('CAPS.ai_ready=true;',c);assert.equal(c.capBanner(),'');
});
function element(attrs={}) {
  const classes=new Set();
  return {attrs:{...attrs},dataset:{i18n:attrs['data-i18n']},textContent:'',inert:false,
    getAttribute(key){return this.attrs[key]??null;},setAttribute(key,value){this.attrs[key]=value;},removeAttribute(key){delete this.attrs[key];},
    classList:{contains:key=>classes.has(key),toggle(key,on){on??=!classes.has(key);if(on)classes.add(key);else classes.delete(key);return on;}}};
}
function header() {
  const markup=between('<div class="topbar">','<div id="rpanelBody"');
  const els=[...markup.matchAll(/<[^>]+data-i18n[^>]*>/g)].map(m=>element(Object.fromEntries([...m[0].matchAll(/([\w-]+)="([^"]*)"/g)].map(a=>[a[1],a[2]]))));
  const dom=Object.fromEntries(els.filter(el=>el.attrs.id).map(el=>[el.attrs.id,el]));
  for(const id of ['userRoleLabel','caseSel','notifCount']) dom[id]=element();
  dom.caseSel.value='case1';dom.caseSel.onchange=()=>{};dom.notifCount.textContent='7';
  const root=element({'data-theme':'light'});
  const c=vm.createContext({LANG:1,LANGS:['EN','KO','ID'],ROLE:'auditor',ROLE_DEF:{auditor:{ko:'오디터'}},
    localStorage:{setItem(){}},renderLiveBadge(){},loadLabels:async()=>{},loadMenus:async()=>{},buildNav(){},applyView:async()=>{},
    document:{documentElement:root,getElementById:id=>dom[id]||null,
      querySelectorAll:selector=>{const attr=selector.match(/\[([^\]]+)\]/)[1];return els.filter(el=>el.attrs[attr]);}}});
  translations(c);
  vm.runInContext(between('function renderHeader(){','/* 로그인/회원가입 화면 언어 스위칭'),c);
  vm.runInContext(between('function toggleTheme(){','/* ---- 알림 센터'),c);
  return {c,dom,els,root};
}
test('M7 fixed header survives KO → ID → EN → KO, preserving case selection and notifications',async()=>{
  const {c,dom,els,root}=header(), onchange=dom.caseSel.onchange;
  const logout=els.find(el=>el.attrs['data-i18n']==='header.logout');
  c.applyLang();assert.equal(logout.textContent,'⏻ 로그아웃');
  await c.cycleLang();assert.equal(logout.textContent,'⏻ Keluar');assert.equal(dom.themePill.textContent,'🌙 Gelap');
  await c.cycleLang();assert.equal(logout.textContent,'⏻ Logout');assert.equal(dom.themePill.textContent,'🌙 Dark');
  assert.equal(root.lang,'en');assert.equal(logout.attrs['aria-label'],'Logout');
  assert.equal(dom.caseSel.value,'case1');assert.equal(dom.caseSel.onchange,onchange);assert.equal(dom.notifCount.textContent,'7');
  await c.cycleLang();assert.equal(logout.textContent,'⏻ 로그아웃');assert.equal(dom.themePill.textContent,'🌙 다크');
  assert.equal(logout.attrs['data-i18n'],'header.logout');
});
test('M7 theme labels follow both current theme and language',async()=>{
  const {c,dom,root}=header();c.applyLang();c.toggleTheme();assert.equal(dom.themePill.textContent,'☀️ 라이트');
  await c.cycleLang();assert.equal(dom.themePill.textContent,'☀️ Terang');
  await c.cycleLang();assert.equal(dom.themePill.textContent,'☀️ Light');assert.equal(root.getAttribute('data-theme'),'dark');
  c.toggleTheme();assert.equal(dom.themePill.textContent,'🌙 Dark');
});
test('H3 opening the menu closes the case panel and makes it inert',()=>{
  const dom={appView:element(),hamburger:element(),rpanel:element()};let renders=0;
  const c=vm.createContext({document:{getElementById:id=>dom[id]},localStorage:{setItem(){}},renderRightPanel(){renders++;}});
  vm.runInContext(between('function toggleNav(on){','function go(id)'),c);
  vm.runInContext(between('function setRPanel(open){','async function postDiscussion'),c);
  c.toggleRPanel();assert.equal(renders,1);assert.equal(dom.rpanel.inert,false);
  c.toggleNav(true);assert.equal(dom.appView.classList.contains('navopen'),true);
  assert.equal(dom.rpanel.classList.contains('open'),false);assert.equal(dom.rpanel.inert,true);assert.equal(dom.rpanel.attrs['aria-hidden'],'true');
  c.toggleRPanel();assert.equal(dom.appView.classList.contains('navopen'),false);
  assert.equal(dom.hamburger.attrs['aria-expanded'],'false');assert.equal(dom.rpanel.attrs['aria-hidden'],'false');
  c.toggleRPanel();assert.equal(dom.rpanel.inert,true);
});
test('H3/M8 CSS: scoped drawer, hidden panel inside viewport, wrapping header/buttons',()=>{
  const css=between('<style>','</style>');
  assert.ok(!/(?:^|\})\s*(?:\.app\.navopen\s+)?aside\s*\{/m.test(css));
  assert.match(css,/\.app\.navopen #sidebar\s*\{/);
  const closed=css.match(/\.rpanel\{([^}]+)\}/)[1];
  for(const prop of ['left:auto','right:0','visibility:hidden','pointer-events:none','max-width:100vw']) assert.ok(closed.includes(prop),prop);
  assert.ok(!closed.includes('translateX'));
  assert.match(css.match(/\.topbar\{([^}]+)\}/)[1],/flex-wrap:wrap/);
  assert.match(css.match(/\.pill\{([^}]+)\}/)[1],/min-width:0/);
  assert.match(css.match(/\.btn\{([^}]+)\}/)[1],/min-width:0/);
  assert.ok(!/overflow-x:\s*(hidden|clip)/.test(css));
});
