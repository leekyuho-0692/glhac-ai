const test=require('node:test');
const assert=require('node:assert/strict');
const {harness}=require('./fixtures/uiv4_frontend.cjs');

const reply=data=>new Response(JSON.stringify(data));
function screen(count,lang='ko'){
  const h=harness(lang),c=h.c;
  let now=0;
  const timers=[],calls=[],frames=[];
  c.Date=class extends Date{static now(){return now;}};
  c.setTimeout=(fn,ms)=>{
    timers.push({at:now,ms});
    return setImmediate(()=>{now+=ms;fn();});
  };
  c.clearTimeout=clearImmediate;
  c.render=()=>frames.push({at:now,note:c.S.entaBulk?.note});
  c.load('ent-apply-more.js');
  const bulk={cid:'c1',owner:c.AUTH.username,busy:true,cancelled:false,note:'',controller:new AbortController(),
    items:Array.from({length:count},(_,i)=>({document_id:'d'+i,filename:'scan'+i+'.pdf',status:'pending'}))};
  c.S.entaBulk=bulk;
  h.documents=()=>bulk.items.filter(it=>it.document_id).map(it=>({document_id:it.document_id,confidence:null,fields:{}}));
  h.extractions=[];
  c.apiFetch=async(url,opts)=>{
    calls.push({at:now,url,signal:opts.signal});
    return reply(url.includes('/pending-extractions')?{items:h.extractions}:h.documents());
  };
  return Object.assign(h,{bulk,timers,calls,frames,now:()=>now,poll:()=>c.ENTA_pollBulk(bulk,()=>c.ENTA_bulkActive(bulk))});
}

for(const [count,minutes] of [[1,3],[23,23],[50,30]]){
  test(`${count} pending documents wait ${minutes} minutes with a slower interval after minute three`,async()=>{
    const h=screen(count);
    await h.poll();
    assert.equal(h.now(),minutes*60000);
    assert.equal(h.bulk.note,'서버에서 계속 분석 중입니다. 완료되면 문서함에 반영되고, 읽을 수 없는 파일은 알림으로 안내됩니다. 이 창을 닫아도 됩니다.');
    assert.ok(h.bulk.items.every(it=>it.status==='pending'));
    assert.equal(h.timers.filter(t=>t.at<180000).length,60);
    for(const timer of h.timers) assert.equal(timer.ms,timer.at<180000?3000:10000);
    assert.equal(h.calls[0].at,3000);
    assert.ok(h.calls.every(call=>call.at<minutes*60000 && call.url.includes('/documents?')));
    if(count>1){
      assert.ok(h.calls.some(call=>call.at===180000));
      assert.ok(h.calls.some(call=>call.at===190000));
    }
    assert.ok(h.calls.every(call=>!call.signal.aborted));
    assert.equal(h.bulk.wake,null);
  });
}

test('only initially pending documents determine the window, even when most finish early',async()=>{
  const h=screen(23);
  h.bulk.items.push(...Array.from({length:30},()=>({status:'complete'})));
  h.documents=()=>h.bulk.items.slice(0,23).map((it,i)=>({document_id:it.document_id,
    confidence:i===22?null:1,fields:i===0?{_ocr_error:'CORRUPT'}:{}}));
  await h.poll();
  assert.equal(h.now(),23*60000);
  assert.equal(h.bulk.items.filter(it=>it.status==='pending').length,1);
  assert.equal(h.bulk.items[0].status,'failed');
  assert.equal(h.bulk.note,h.c.ENTA_t('upload.timeout')+' 판독 불가 1건 — 목록의 사유를 확인해 주세요');
});

for(const lang of ['ko','en','id']){
  test(`all completed documents keep the analysis completion note: ${lang}`,async()=>{
    const h=screen(2,lang);
    h.documents=()=>h.bulk.items.map(it=>({document_id:it.document_id,confidence:1,fields:{}}));
    await h.poll();
    assert.ok(h.bulk.items.every(it=>it.status==='complete'));
    assert.equal(h.bulk.note,{
      ko:'AI 분석 완료 — 2/2',
      en:'AI analysis complete — 2/2',
      id:'Analisis AI selesai — 2/2'
    }[lang]);
  });

  test(`progress counts every non-pending item and completion retains unreadable and review summaries: ${lang}`,async()=>{
    const h=screen(4,lang),c=h.c;
    h.bulk.items.push({status:'complete'},{status:'duplicate'},{status:'failed',reason:'Upload failed'});
    h.documents=()=>h.bulk.items.slice(0,4).map((it,i)=>({document_id:it.document_id,
      confidence:i<2||h.now()>=190000?0:null,fields:i===1?{_ocr_error:'CORRUPT'}:{}}));
    h.extractions=[{document_id:'d0',kind:'material_names'},{document_id:'unrelated',kind:'material_names'}];
    await h.poll();
    assert.equal(h.now(),190000);
    assert.equal(h.frames[0].note,c.ENTA_t('upload.analysis_progress',{done:3,total:7,m:0}));
    assert.ok(h.frames.some(f=>f.at===3000 && f.note===c.ENTA_t('upload.analysis_progress',{done:5,total:7,m:0})));
    assert.ok(h.frames.some(f=>f.at===180000 && f.note===c.ENTA_t('upload.analysis_progress',{done:5,total:7,m:3})));
    assert.equal(h.bulk.note,c.ENTA_t('upload.analysis_done',{done:7,total:7})+' '+c.ENTA_t('upload.unreadable_count',{n:1})+' '+c.ENTA_t('upload.materials_pending',{count:1}));
    assert.equal(h.calls.filter(call=>call.url.includes('/pending-extractions')).length,1);
    if(lang==='ko') assert.equal(h.frames[0].note,'AI 분석 중 — 완료 3/7, 경과 0분');
    else assert.doesNotMatch(h.frames.map(f=>f.note).join(' ')+h.bulk.note,/[가-힣]/);
    assert.doesNotMatch(c.ENTA_t('upload.timeout'),/3분|three minutes|tiga menit/);
  });
}

const reasons={
  CORRUPT:'파일이 손상되어 읽을 수 없습니다. 원본을 다시 받아 올려 주세요.',
  ENCRYPTED:'암호가 걸린 파일입니다. 암호를 풀어 다시 올려 주세요.',
  TIMEOUT:'판독 시간이 초과되었습니다. 자동 재처리 대상입니다.',
  ERROR:'파일을 읽지 못했습니다. 다시 올려 주세요.'
};
for(const lang of ['ko','en','id']){
  test(`OCR failures take precedence over LLM and confidence results: ${lang}`,()=>{
    const {c}=screen(0,lang);
    for(const code of ['CORRUPT','ENCRYPTED','TIMEOUT','BOMB','PIXEL_LIMIT','ERROR','FUTURE_ERROR']){
      const it={status:'pending'};
      c.ENTA_applyParsedStatus(it,{confidence:1,fields:{_ocr_error:code,_llm_error:'LLM_UNAVAILABLE'},verify:{verdict:'ok'}});
      assert.equal(it.status,'failed',code);
      assert.equal(it.verdict,'unknown',code);
      assert.equal(it.ocrError,code);
      if(lang==='ko') assert.equal(it.reason,reasons[code]||reasons.ERROR);
      else{
        assert.ok(it.reason.length>20);
        assert.doesNotMatch(it.reason,/[가-힣]|upload\.ocr/);
      }
      c.ENTA_applyParsedStatus(it,{confidence:0,fields:{}});
      assert.equal(it.status,'complete');
      assert.equal(it.reason,'');
      assert.equal(it.ocrError,null);
    }
    const unavailable={status:'pending'};
    c.ENTA_applyParsedStatus(unavailable,{fields:{_llm_error:'LLM_UNAVAILABLE'}});
    assert.equal(unavailable.status,'complete');
    assert.equal(unavailable.reason,c.ENTA_t('docs.ai_unavailable'));
  });

  test(`general LLM failures clear previous verdict badges: ${lang}`,()=>{
    const {c}=screen(0,lang),it={status:'pending',verdict:'ok'};
    c.ENTA_applyParsedStatus(it,{confidence:1,fields:{_llm_error:'MODEL_ERROR'},verify:{verdict:'ok'}});
    assert.equal(it.status,'failed');
    assert.equal(it.verdict,'unknown');
    assert.equal(it.reason,c.ENTA_t('upload.analysis_failed'));
  });
}

test('a recovered unreadable document is included even when no polling is needed',async()=>{
  const h=screen(1);
  h.c.ENTA_applyParsedStatus(h.bulk.items[0],{fields:{_ocr_error:'CORRUPT'}});
  await h.poll();
  assert.equal(h.timers.length,0);
  assert.equal(h.bulk.note,'AI 분석 완료 — 1/1 판독 불가 1건 — 목록의 사유를 확인해 주세요');
});

for(const change of ['view','case','owner','cancel']){
  test(`${change} during a polling wait wakes and cancels without a request or stale render`,async()=>{
    const h=screen(23),c=h.c;
    const job=h.poll(),frames=h.frames.length;
    if(change==='view') c.S.view='ent-docs';
    else if(change==='case') c.RS.cases=[{case_id:'c2'}];
    else if(change==='owner') c.AUTH.username='another_user';
    else c.ENTA_cancelBulk(h.bulk);
    c.ENTA_bulkActive(h.bulk);
    await job;
    assert.equal(h.bulk.cancelled,true);
    assert.equal(h.bulk.busy,false);
    assert.equal(h.bulk.controller.signal.aborted,true);
    assert.equal(h.bulk.wake,null);
    assert.equal(h.calls.length,0);
    assert.equal(h.frames.length,frames);
    assert.equal(h.bulk.items[0].status,'pending');
  });
}

test('transient polling errors cannot replace the server-continuation notice at the deadline',async()=>{
  const h=screen(1);
  h.c.apiFetch=async()=>{throw Error('offline');};
  await h.poll();
  assert.equal(h.now(),180000);
  assert.equal(h.bulk.note,h.c.ENTA_t('upload.timeout'));
});

test('the continuation notice is consistent before and after loading the upload module',()=>{
  for(const lang of ['ko','en','id']){
    const {c}=harness(lang),initial=c.run("t('upload.timeout')");
    c.load('ent-apply-more.js');
    assert.equal(initial,c.ENTA_t('upload.timeout'));
    assert.doesNotMatch(initial,/3분|three minutes|tiga menit/);
  }
});
