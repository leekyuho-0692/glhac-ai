/* 실제 화면 함수를 실행해 선택 반영·중복 전송·폴링 중단을 확인한다. node --test 로 실행. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..', 'app', 'static', 'v4');
const reply = (body, status=200) => new Response(JSON.stringify(body), {status});
const plain = value => JSON.parse(JSON.stringify(value));

function screen(){
  let now=0;
  const dom={'#pk-material':{}, '#pk-product':{}}, notices=[];
  const c={App:{},S:{view:'ent-apply'},AUTH:{role:'applicant',username:'user'},RS:{cases:[{case_id:'c1'}]},
    VIEWS:{'ent-home':()=>'', 'ent-apply':()=>'', 'ent-contract':()=>''},
    AbortController,AbortSignal,Uint8Array,btoa,
    Date:class extends Date{ static now(){ return now; } },
    setTimeout:(fn,ms)=>setImmediate(()=>{now+=ms;fn();}),clearTimeout:clearImmediate,
    $:selector=>dom[selector]||null,esc:String,jsq:String,canCall:()=>true,
    openModal:()=>{},closeModal:()=>{},toast:value=>notices.push(value),
    apiErr:async r=>(await r.json()).detail?.code||'실패',
    apiFetch:async()=>reply({items:[]}),render:()=>{
      if(c.S.entaBulk) c.ENTA_bulkActive(c.S.entaBulk);
    }};
  vm.createContext(c);
  for(const file of ['ent-apply-more.js','package-check.js']){
    vm.runInContext(fs.readFileSync(path.join(root,'js',file),'utf8'),c,{filename:file});
  }
  c.PKG_load=async()=>{};
  return {c,dom,notices};
}

for(const kind of ['material','product']){
  test(kind+' 확인 대기는 미선택이며 수정·제외를 한 요청으로 처리한다',async()=>{
    const {c,dom}=screen(), posts=[];
    let items=['Keep','Rename','Dismiss','Untouched'].map(value=>({kind:kind+'_names',value,filename:'source.pdf'}));
    c.apiFetch=async(url,opts={})=>{
      if(opts.method==='POST'){
        posts.push({url,body:JSON.parse(opts.body)});
        items=items.filter(it=>it.value==='Untouched');
        return reply({count:3});
      }
      return reply({items});
    };
    if(kind==='material') await c.App.pkgMaterialOpen('c1');
    else{ c.App.pkgEdit('c1','product'); await new Promise(setImmediate); }
    const s=c.S[kind==='material'?'pkgMaterial':'pkgProduct'];
    assert.equal(s.pending.length,4);
    assert.ok(s.pending.every(it=>!it.selected&&!it.dismiss));
    assert.match(dom['#pk-'+kind].innerHTML,/등록 안 함/);
    const apply=kind==='material'?c.App.pkgMaterialApply:c.App.pkgProductApply;
    await apply();
    assert.equal(posts.length,0);
    s.pending[0].selected=true;
    s.pending[1].selected=true; s.pending[1].name='New Name';
    s.pending[2].dismiss=true;
    await apply();
    assert.deepEqual(posts,[{url:'/cases/c1/pending-extractions/apply',body:{values:['Keep'],renames:{Rename:'New Name'},dismiss:['Dismiss']}}]);
    assert.deepEqual(plain(s.pending.map(it=>it.value)),['Untouched']);
  });
}

test('수기 원재료는 공백·대소문자 중복을 한 번만 보내고 서버 409를 성공으로 표시한다',async()=>{
  const {c}=screen(), posts=[];
  c.S.pkgMaterial={cid:'c1',pending:[],rows:['Raw Water','RAWWATER','Salt'].map(name=>({name,supplier:'',origin:''}))};
  c.apiFetch=async(url,opts={})=>{
    if(opts.method==='POST'){
      posts.push(JSON.parse(opts.body).name);
      return posts.length===1?reply({material_id:'m1'}):reply({detail:{code:'DUPLICATE_MATERIAL',material_id:'m2'}},409);
    }
    return reply({items:[]});
  };
  await c.App.pkgMaterialSave();
  assert.deepEqual(posts,['Raw Water','Salt']);
  assert.ok(c.S.pkgMaterial.rows.every(it=>it.name===''));
  assert.match(c.S.pkgMaterial.rows[1].result,/이미 등록됨/);
  assert.match(c.S.pkgMaterial.rows[2].result,/이미 등록됨/);
  assert.match(c.S.pkgMaterial.notice,/0건 실패/);
});

test('편집 역할과 API 권한이 모두 있어야 등록 버튼을 표시한다',()=>{
  const {c,dom}=screen(), s={pending:[{value:'A',name:'A'}]};
  c.AUTH.role='operator';
  assert.doesNotMatch(c.PKG_pendingTable(s,'material'),/선택 항목 처리/);
  c.AUTH.role='applicant'; c.canCall=()=>false;
  assert.doesNotMatch(c.PKG_pendingTable(s,'product'),/선택 항목 처리/);
  c.canCall=()=>true;
  c.S.pkgMaterial={...s,cid:'c1',rows:[],failed:[{}]};
  c.PKG_materialRender(c.S.pkgMaterial);
  assert.doesNotMatch(dom['#pk-material'].innerHTML,/onclick="App.pkgReproc/);
});

function file(name,size=3){
  return {name,size,reads:0,async arrayBuffer(){this.reads++;return new Uint8Array(size).buffer;}};
}

test('검증 배지 없이 완료하고 실패 파일만 재전송하며 성공 파일 재선택은 건너뛴다',async()=>{
  const {c}=screen(), good=file('good.pdf'), bad=file('bad.pdf'), posts=[], docs=[];
  let fail=true, polls=0;
  c.apiFetch=async(url,opts={})=>{
    if(opts.method==='POST'){
      const body=JSON.parse(opts.body); posts.push(body.filename);
      if(body.filename==='bad.pdf'&&fail) return reply({detail:{code:'FORBIDDEN'}},403);
      const document_id='d'+posts.length;
      docs.push({document_id,confidence:0,doc_type:'product_label',fields:{}});
      return reply({document_id,autoparse_queued:true});
    }
    if(url.endsWith('/documents')){polls++;return reply(docs);}
    return reply({items:[]});
  };
  await c.App.entaUpload([good,bad]);
  assert.equal(polls,1);
  assert.equal(c.S.entaBulk.busy,false);
  assert.equal(c.S.entaBulk.items[0].status,'분석 완료');
  assert.equal(c.S.entaBulk.items[1].reason,'권한 없음');
  assert.match(c.ENTA_applyPanel(),/다시 올리기/);
  fail=false;
  await c.App.entaRetry(1);
  assert.deepEqual(posts,['good.pdf','bad.pdf','bad.pdf']);
  assert.equal(good.reads,1); assert.equal(bad.reads,2);
  await c.App.entaUpload([file('good.pdf')]);
  assert.equal(posts.length,3);
  assert.equal(c.S.entaBulk.items.at(-1).status,'이미 올림');
  await c.App.entaUpload([file('good.pdf',4)]);
  assert.equal(posts.length,4);
});

for(const change of ['view','case']){
  test(change+' 변경 시 진행 중 조회를 취소하고 폴링을 중단한다',async()=>{
    const {c}=screen(), urls=[];
    let signal;
    c.apiFetch=async(url,opts={})=>{
      urls.push(url);
      if(opts.method==='POST') return reply({document_id:'d1',autoparse_queued:true});
      signal=opts.signal;
      if(change==='view') c.S.view='ent-docs';
      else c.RS.cases=[{case_id:'c2'}];
      c.render();
      return reply([{document_id:'d1',confidence:1,verify:{verdict:'ok'}}]);
    };
    await c.App.entaUpload([file('scan.pdf')]);
    assert.equal(c.S.entaBulk.cancelled,true);
    assert.equal(c.S.entaBulk.busy,false);
    assert.equal(signal.aborted,true);
    assert.equal(urls.length,2);
    assert.equal(c.S.entaBulk.items[0].status,'분석 대기');
  });
}

test('분석 대기 폴링은 3분을 넘지 않고 있는 검증 배지는 표시한다',async()=>{
  const {c}=screen();
  let polls=0;
  c.apiFetch=async(url,opts={})=>{
    if(opts.method==='POST') return reply({document_id:'d1',autoparse_queued:true});
    polls++;
    return reply([{document_id:'d1',confidence:null,fields:{},verify:{verdict:'warn'}}]);
  };
  await c.App.entaUpload([file('scan.pdf')]);
  assert.equal(polls,59);
  assert.equal(c.S.entaBulk.busy,false);
  assert.match(c.S.entaBulk.note,/3분/);
  assert.match(c.ENTA_applyPanel(),/확인 필요/);
});

test('업로드 오류 코드를 사용자 문구로 표시한다',()=>{
  const {c}=screen();
  for(const code of ['NOT_AUTHORIZED','FORBIDDEN']) assert.equal(c.ENTA_errorText(code),'권한 없음');
  for(const code of ['CASE_CLOSED','SCOPE_FROZEN','CASE_LOCKED']) assert.equal(c.ENTA_errorText(code),'잠긴 신청서');
  assert.equal(c.ENTA_errorText('UNKNOWN_CODE'),'업로드 실패(UNKNOWN_CODE)');
});

test('가입 전 사업자번호를 검사하고 서버 422의 담당자 칸을 표시한다',async()=>{
  const {c,dom,notices}=screen();
  const html=fs.readFileSync(path.join(root,'index.html'),'utf8');
  const start=html.indexOf('  async submitEnt(e){'), end=html.indexOf('  dupReset()',start);
  vm.runInContext('App.submitEnt=({'+html.slice(start,end)+'}).submitEnt;',c);
  c.API_BASE='';
  for(const [id,value] of Object.entries({'es-pw':'Passw0rd!23','es-pw2':'Passw0rd!23','es-id':'a@b.com','es-biz':'1234567890'})) dom['#'+id]={value};
  dom['#es-company-email']={value:'company@example.com'};
  dom['#es-email-consent']={checked:true};
  let calls=0;
  c.fetch=async()=>{calls++;return reply({detail:{code:'VALIDATION_ERROR',errors:[{field:'contacts.cp.phone',message:'전화 형식 오류'}]}},422);};
  const event={preventDefault(){},target:{querySelector:()=>({})}};
  await c.App.submitEnt(event);
  assert.equal(calls,0); assert.match(notices.at(-1),/000-00-00000/);
  dom['#es-biz'].value='123-45-67890';
  await c.App.submitEnt(event);
  assert.equal(calls,1); assert.match(notices.at(-1),/담당자 2 · 연락처: 전화 형식 오류/);
});
