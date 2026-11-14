const {test}=require('node:test');
const assert=require('node:assert/strict');
const {harness,between}=require('./fixtures/uiv4_frontend.cjs');

function deletionHarness(lang='ko', products=[
  {product_id:'drop',name:'English duplicate'}, {product_id:'keep&/id',name:'Keep <product>'},
]){
  const h=harness(lang), {c,dom,calls}=h;
  c.RS.entProd={c1:products};
  c.openModal=(title,body,foot)=>{h.modal={title,body,foot};};
  c.closeModal=()=>{h.closed=true;delete dom['pr-merge-into'];};
  c.App.closeModal=c.closeModal;
  c.loadEntProd=async cid=>{h.reloaded=cid;};
  c.apiErr=async(r,fallback)=>fallback;
  c.apiFetch=async(url,options)=>{calls.push({url,options});return {ok:true};};
  c.run(between('App.entDelProduct =','const BLOCK_KO ='));
  return h;
}

for(const lang of ['ko','en','id'])test(`product deletion confirms merge selection in ${lang}`,async()=>{
  const h=deletionHarness(lang),{c,dom,calls}=h;
  await c.App.entDelProduct('c1','drop');
  assert.equal(calls.length,0,'Opening confirmation must not delete');
  assert.match(h.modal.body,/<select[^>]*id="pr-merge-into"/);
  assert.match(h.modal.body,/<option value="">/);
  assert.match(h.modal.body,/Keep &lt;product&gt;/);
  assert.doesNotMatch(h.modal.body,/<option value="drop"/);
  if(lang!=='ko') assert.doesNotMatch(h.modal.title+h.modal.body+h.modal.foot,/[가-힣]/);
  assert.match(h.modal.foot,/App.closeModal\(\)/);
  dom['pr-merge-into']={value:'keep&/id'};
  await c.App.entConfirmDelProduct('c1','drop');
  assert.deepEqual(calls.map(x=>[x.url,x.options.method]),[
    ['/cases/c1/products/drop?merge_into=keep%26%2Fid','DELETE'],
  ]);
  assert.equal(h.closed,true);
  assert.equal(h.reloaded,'c1');
});

test('optional selection keeps ordinary deletion and cancel sends no request',async()=>{
  const h=deletionHarness();
  await h.c.App.entDelProduct('c1','drop');
  h.c.App.closeModal();
  assert.equal(h.calls.length,0);
  await h.c.App.entDelProduct('c1','drop');
  h.dom['pr-merge-into']={value:''};
  await h.c.App.entConfirmDelProduct('c1','drop');
  assert.equal(h.calls[0].url,'/cases/c1/products/drop');
});

test('single product has a confirmation without a merge selector',async()=>{
  const h=deletionHarness('ko',[{product_id:'only',name:'Only product'}]);
  await h.c.App.entDelProduct('c1','only');
  assert.doesNotMatch(h.modal.body,/<select/);
  assert.equal(h.calls.length,0);
  await h.c.App.entConfirmDelProduct('c1','only');
  assert.equal(h.calls[0].url,'/cases/c1/products/only');
});

test('server rejection preserves the dialog and selected merge target',async()=>{
  const h=deletionHarness();
  await h.c.App.entDelProduct('c1','drop');
  h.dom['pr-merge-into']={value:'keep&/id'};
  h.c.apiFetch=async()=>({ok:false});
  await h.c.App.entConfirmDelProduct('c1','drop');
  assert.equal(h.closed,undefined);
  assert.equal(h.reloaded,undefined);
  assert.equal(h.dom['pr-merge-into'].value,'keep&/id');
  assert.match(h.notices.at(-1),/삭제에 실패/);
});
