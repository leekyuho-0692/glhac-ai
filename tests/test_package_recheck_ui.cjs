const test=require('node:test'), assert=require('node:assert/strict');
const {harness}=require('./fixtures/uiv4_frontend.cjs');
function setup(lang){
  const h=harness(lang); h.c.load('package-check.js');
  h.c.AUTH.role='auditor'; h.c.RS.pkg={c1:complete()};
  return h;
}
function complete(){ return {complete:true,items:{company:{ok:true,missing:[]},product:{ok:true,count:1},material:{ok:true,count:2},factory:{ok:true}},gaps:[],unreadable:[],unprocessed:[]}; }
function response(data,ok=true){return {ok,status:ok?200:503,json:async()=>data,clone(){return this}};}
for(const lang of ['ko','en','id']){
  test(`recheck disables button, ignores double click and reports unchanged completion: ${lang}`,async()=>{
    const {c,notices}=setup(lang); let finish,calls=0;
    c.apiFetch=()=>{calls++;return new Promise(resolve=>finish=resolve)};
    const job=c.App.pkgRefresh('c1');
    let button=c.PKG_panel('c1').match(/<button[^>]*App.pkgRefresh[^>]*>[^<]*<\/button>/)[0];
    assert.match(button,/disabled/); assert.ok(button.includes(c.run("t('pkg.checking')")));
    await c.App.pkgRefresh('c1'); assert.equal(calls,1);
    const reordered=complete(); reordered.items={factory:reordered.items.factory,...reordered.items};
    finish(response(reordered)); await job;
    assert.equal(notices.length,1);
    assert.equal(notices[0],c.run("t('pkg.complete')+' '+t('pkg.unchanged')"));
    button=c.PKG_panel('c1').match(/<button[^>]*App.pkgRefresh[^>]*>[^<]*<\/button>/)[0];
    assert.doesNotMatch(button,/disabled/); assert.ok(button.includes(c.run("t('pkg.refresh')")));
  });
  test(`recheck lists gaps, company fields and unreadable/unprocessed groups: ${lang}`,async()=>{
    const {c,notices}=setup(lang),d=complete();
    d.complete=false; d.gaps=['company','material','factory']; d.items.company={ok:false,missing:['nib','address']};
    d.unreadable=[{document_id:'d1'}];d.unprocessed=[{document_id:'d2'}];
    c.apiFetch=async()=>response(d); await c.App.pkgRefresh('c1');
    for(const key of ['pkg.item.company','pkg.item.material','pkg.item.factory','pkg.company.nib','pkg.company.address','pkg.unreadable','pkg.unprocessed']) assert.ok(notices[0].includes(c.run(`t('${key}')`)),key);
    assert.ok(notices[0].includes('5')); assert.ok(!notices[0].includes(c.run("t('pkg.unchanged')")));
    if(lang!=='ko')assert.doesNotMatch(notices[0],/[가-힣]/);
  });
  for(const failure of ['http','payload','network','json'])test(`failed recheck retains previous panel and releases button (${failure}): ${lang}`,async()=>{
    const {c,notices}=setup(lang),old=c.RS.pkg.c1;let errors=0;
    c.apiErr=async(r,fallback)=>{errors++;return fallback+' (failure)'};
    c.apiFetch=async()=>{
      if(failure==='network')throw Error('offline');
      if(failure==='json')return {...response({}),json:async()=>{throw Error('bad JSON')}};
      return response(failure==='payload'?{error:'FAILED'}:{detail:{message:'failure'}},failure!=='http');
    };
    await c.App.pkgRefresh('c1');
    assert.equal(c.RS.pkg.c1,old); assert.ok(c.PKG_panel('c1'));
    assert.ok(notices[0].includes(c.run("t('pkg.error')")));
    if(['http','payload'].includes(failure))assert.equal(errors,1);
    assert.doesNotMatch(c.PKG_panel('c1').match(/<button[^>]*App.pkgRefresh[^>]*>/)[0],/disabled/);
  });
  test(`logistics gaps and possible product duplicates are localized: ${lang}`,async()=>{
    const {c,notices}=setup(lang),d={...complete(),scheme:'logistics',complete:false,gaps:['scope','vehicle','facility','cleaning']};
    c.apiFetch=async()=>response(d);await c.App.pkgRefresh('c1');
    for(const key of d.gaps)assert.ok(notices[0].includes(c.run(`t('pkg.item.${key}')`)));
    c.s={cid:'c1',pending:[{value:'Face product',name:'Face product',reason:'possible_same_product'}]};
    c.INTAKE_conflicts=()=>'';
    assert.ok(c.run("PKG_pendingTable(s,'product')").includes(c.run("t('intake.reason.possible_same_product')")));
  });
}
