/* ===== 인증기업 보강(서류 체크리스트·임시저장/ZIP 일괄업로드·환불 요청) ===== */

const ENTA_LANG = () => (typeof S !== 'undefined' && S.lang) ? S.lang : 'ko';
const ENTA_CASE = () => (RS.cases && RS.cases[0] && RS.cases[0].case_id) || null;

async function ENTA_loadChecklist(cid){
  try{
    const r = await apiFetch(`/cases/${cid}/doc-checklist?lang=${encodeURIComponent(ENTA_LANG())}`);
    RS.entaCheck = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.entaCheck = {error:true}; }
}
async function ENTA_loadInvoices(cid){
  try{
    const r = await apiFetch(`/cases/${cid}/invoices`);
    RS.entaInv = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.entaInv = {error:true}; }
}

function ENTA_checklistPanel(){
  const cid = ENTA_CASE();
  if(!cid) return '';
  const d = RS.entaCheck;
  if(!d){
    if(RS._entaChkLoading !== cid){ RS._entaChkLoading = cid; ENTA_loadChecklist(cid).then(()=>{ RS._entaChkLoading=null; render(); }); }
    return `<section class="panel"><div class="panel-head"><h2>필요 서류 현황</h2></div><p class="empty">불러오는 중…</p></section>`;
  }
  if(d.error){
    return `<section class="panel"><div class="panel-head"><h2>필요 서류 현황</h2></div>
      <p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  }
  const rows = Array.isArray(d) ? d : (Array.isArray(d.checklist) ? d.checklist : null);
  if(!rows) return `<section class="panel"><h2>필요 서류 현황</h2><p class="empty">서류 정보를 확인하지 못했습니다. 다시 불러와 주세요.</p></section>`;
  const req = rows.filter(r=>r.required && r.applicable !== false);
  const reqOk = req.filter(r=>r.satisfied).length;
  const missing = req.filter(r=>!r.satisfied);
  const stTag = (r) => r.status==='ok' ? '<span class="badge strong">제출</span>'
    : r.status==='rejected' ? '<span class="badge attn">반려</span>'
    : r.status==='not_applicable' ? '<span class="badge">해당 없음</span>'
    : '<span class="badge attn">미제출</span>';
  const body = rows.length ? `<div class="tbl-wrap"><table><thead><tr>
      <th>서류</th><th>필수</th><th>제출</th><th>사유·비고</th></tr></thead><tbody>
      ${rows.map(r=>`<tr>
        <td>${esc(r.doc_type_ko||r.doc_type||'')}${r.source?`<div class="muted" style="font-size:11px">${esc(r.source)}</div>`:''}</td>
        <td>${r.required?'<span class="tag req">필수</span>':'<span class="muted">선택</span>'}</td>
        <td>${stTag(r)} <span class="muted mono">${r.file_count||0}</span>${r.cert_no_on_file?` <span class="count">${r.cert_no_on_file}</span>`:''}</td>
        <td class="muted" style="font-size:12px">${esc(r.na_reason||r.note||r.requirement||'')}</td>
      </tr>`).join('')}</tbody></table></div>` : '<p class="empty">서류 정보가 없습니다.</p>';
  return `<section class="panel"><div class="panel-head"><h2>필요 서류 현황</h2>
      <span class="muted">필수 ${reqOk}/${req.length} 제출</span></div>
    ${missing.length?`<p class="note attn">미제출 필수 서류 ${missing.length}건: ${missing.map(m=>esc(m.doc_type_ko||m.doc_type)).join(', ')}</p>`:''}
    ${body}</section>`;
}

function ENTA_applyPanel(){
  const cid = ENTA_CASE();
  if(!cid) return '';
  // 임시저장·ZIP 일괄 업로드 — 신청기업·컨설턴트 전용(main.py:6159·5794). 할랄감독자(penyelia)는 403
  const canDraft = canCall('POST','/cases/{}/save-draft'), canZip = canCall('POST','/cases/{}/intake-zip-stream');
  const canBulk = canCall('POST','/cases/{}/documents');
  if(!canDraft && !canZip && !canBulk) return '';
  const prog = (S.entaProg||'');
  const bulk = S.entaBulk && S.entaBulk.cid===cid && S.entaBulk.owner===AUTH.username ? S.entaBulk : null;
  return `<section class="panel"><div class="panel-head"><h2>임시저장 · 일괄 업로드</h2></div>
    <div class="inline" style="flex-wrap:wrap;gap:8px">
      ${canDraft?`<button class="btn" onclick="once(this, ()=>App.entaSaveDraft())">기업 정보 임시저장</button>`:''}
      ${canZip?`<button class="btn btn-primary" onclick="const z=$('#enta-zip'); if(z) z.click()">ZIP 파일로 서류 일괄 올리기</button>`:''}
      <input type="file" id="enta-zip" accept=".zip,.rar,.7z" hidden onchange="App.entaZip(this.files[0])">
    </div>
    <p class="muted" style="font-size:12px;margin-top:6px">ZIP·RAR·7z 를 압축 해제해 서류를 자동 분류·채웁니다. 처리 중에는 창을 닫지 마세요.</p>
    ${prog?`<p class="note">${esc(prog)}</p>`:''}
    ${canBulk?`<div class="field"><label for="enta-files">여러 서류 올리기</label>
      <input class="in" type="file" id="enta-files" multiple accept=".pdf,.jpg,.jpeg,.png,.xlsx,.xls,.docx,.doc,.csv,.txt,.hwp" ${bulk&&bulk.busy?'disabled':''} onchange="App.entaUpload(this.files)"></div>
      <div class="dropzone" ondragover="event.preventDefault();this.classList.add('over')" ondragleave="this.classList.remove('over')" ondrop="App.entaDrop(event)">
        <b>서류 여러 개를 여기로 끌어다 놓으세요</b><span class="muted">파일당 최대 20MB · 한 번에 하나씩 올립니다.</span></div>`:''}
    ${bulk?`<div aria-live="polite">${bulk.note?`<p class="note">${esc(bulk.note)}</p>`:''}
      <div class="tbl-wrap"><table><thead><tr><th>파일명</th><th>상태</th><th>결과</th></tr></thead><tbody>
      ${bulk.items.map((it,i)=>`<tr><td>${esc(it.filename)}</td><td>${esc(it.status)}</td><td>${esc(it.doc_type_ko||'')}
        ${it.verdict?`<span class="badge ${it.verdict==='ok'?'strong':'attn'}">${esc(({ok:'일치',warn:'확인 필요',fail:'불일치'})[it.verdict]||'확인 필요')}</span>`:''}
        ${it.reason?`<span>${esc(it.reason)}</span>`:''}
        ${canBulk&&it.status==='실패'&&it.file?`<button class="btn btn-sm" ${bulk.busy?'disabled':''} onclick="App.entaRetry(${i})">다시 올리기</button>`:''}</td></tr>`).join('')}</tbody></table></div></div>`:''}
  </section>`;
}

function ENTA_refundPanel(){
  const cid = ENTA_CASE();
  if(!cid) return '';
  // 환불 요청은 운영자 전용(main.py:13869) — 인증기업 화면에선 403 이라 패널 자체를 숨긴다
  if(!canCall('POST','/invoices/{}/refund/request')) return '';
  const d = RS.entaInv;
  if(!d){
    if(RS._entaInvLoading !== cid){ RS._entaInvLoading = cid; ENTA_loadInvoices(cid).then(()=>{ RS._entaInvLoading=null; render(); }); }
    return `<section class="panel"><div class="panel-head"><h2>환불 요청</h2></div><p class="empty">불러오는 중…</p></section>`;
  }
  if(d.error){
    return `<section class="panel"><div class="panel-head"><h2>환불 요청</h2></div>
      <p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  }
  const rows = (Array.isArray(d)?d:[]).filter(i=>i.status==='paid');
  const body = rows.length ? `<ul class="rows">${rows.map(i=>`<li class="row">
      <span>${esc(i.invoice_no||i.invoice_id)} <span class="tag">${esc(i.service_type||'')}</span></span>
      <span class="num mono">${esc(_money(i.total, 'IDR'))}</span>
      <span class="row-act"><button class="btn btn-sm" onclick="App.entaRefund('${i.invoice_id}')">환불 요청</button></span>
    </li>`).join('')}</ul>` : '<p class="empty">환불 가능한(결제 완료) 인보이스가 없습니다.</p>';
  return `<section class="panel"><div class="panel-head"><h2>환불 요청</h2></div>${body}</section>`;
}

/* --- 뷰 확장 --- */
{
  const f = VIEWS['ent-home'];
  VIEWS['ent-home'] = () => f() + ENTA_checklistPanel();
}
{
  const f = VIEWS['ent-apply'];
  VIEWS['ent-apply'] = () => ENTA_applyPanel() + f();
}
{
  const f = VIEWS['ent-contract'];
  VIEWS['ent-contract'] = () => f() + ENTA_refundPanel();
}

/* --- 액션 --- */
async function ENTA_fileB64(file){
  const buf = await file.arrayBuffer(), bytes = new Uint8Array(buf);
  let bin = '';
  for(let i=0;i<bytes.length;i+=0x8000) bin+=String.fromCharCode.apply(null, bytes.subarray(i,i+0x8000));
  return btoa(bin);
}
function ENTA_errorText(code){
  code=String(code||'UNKNOWN');
  if(/(?:CLOSED|FROZEN|LOCKED)/.test(code)) return '잠긴 신청서';
  return ({UNSUPPORTED_TYPE:'지원하지 않는 형식', CONTENT_MISMATCH:'확장자와 내용이 다름',
    UPLOAD_TOO_LARGE:'용량 초과', NOT_AUTHORIZED:'권한 없음', FORBIDDEN:'권한 없음',
    ORG_FORBIDDEN:'권한 없음', '401':'권한 없음', '403':'권한 없음'})[code] || `업로드 실패(${code})`;
}
async function ENTA_uploadError(r){
  const d = await r.json().catch(()=>({}));
  return ENTA_errorText((d.detail&&d.detail.code)||d.code||r.status);
}
function ENTA_cancelBulk(bulk){
  if(!bulk || bulk.cancelled) return;
  bulk.cancelled=true; bulk.busy=false;
  if(bulk.controller) bulk.controller.abort();
  if(bulk.wake) bulk.wake();
  for(const it of bulk.items){
    if(['대기','올리는 중'].includes(it.status)){ it.status='실패'; it.reason='업로드 중단'; }
  }
}
function ENTA_bulkActive(bulk){
  if(S.entaBulk!==bulk || AUTH.username!==bulk.owner || S.view!=='ent-apply' || ENTA_CASE()!==bulk.cid) ENTA_cancelBulk(bulk);
  return !bulk.cancelled;
}
App.entaDrop = function(e){
  e.preventDefault(); e.currentTarget.classList.remove('over');
  return App.entaUpload(e.dataTransfer.files);
};
App.entaUpload = async function(files){
  const cid = ENTA_CASE();
  if(!cid || S.view!=='ent-apply' || !canCall('POST','/cases/{}/documents')) return;
  const previous=S.entaBulk;
  if(previous && ENTA_bulkActive(previous) && previous.busy) return toast('진행 중인 업로드·분석을 기다려 주세요.');
  const selected = Array.from(files||[]);
  if(!selected.length) return;
  const items=selected.map(file=>({file,filename:file.name,status:'대기'}));
  const old=previous && previous.cid===cid && previous.owner===AUTH.username ? previous.items : [];
  ENTA_cancelBulk(previous);
  const bulk={cid,owner:AUTH.username,busy:true,cancelled:false,items:[...old,...items],note:'',controller:new AbortController()};
  S.entaBulk = bulk; render();
  await ENTA_runBulk(bulk,items);
};
App.entaRetry = async function(i){
  const previous=S.entaBulk;
  if(!previous || previous.busy || S.view!=='ent-apply' || previous.cid!==ENTA_CASE() || previous.owner!==AUTH.username || !canCall('POST','/cases/{}/documents')) return;
  const it=previous.items[i];
  if(!it || it.status!=='실패' || !it.file) return;
  ENTA_cancelBulk(previous);
  const bulk={...previous,busy:true,cancelled:false,note:'',controller:new AbortController(),wake:null};
  it.status='대기'; it.reason=''; it.document_id=null; it.verdict=null; it.doc_type_ko='';
  S.entaBulk=bulk; render();
  await ENTA_runBulk(bulk,[it],true);
};
async function ENTA_runBulk(bulk, items, retry=false){
  const active=()=>ENTA_bulkActive(bulk);
  try{
    // 파일 읽기까지 순차 실행해 여러 개의 base64를 동시에 메모리에 올리지 않는다.
    for(const it of items){
      if(!active()) break;
      const file=it.file;
      const uploaded=!retry && bulk.items.find(x=>x!==it && x.document_id && ['분석 대기','분석 완료'].includes(x.status) && x.filename===file.name && x.file.size===file.size);
      if(uploaded){
        Object.assign(it,{status:'이미 올림',document_id:uploaded.document_id,doc_type_ko:uploaded.doc_type_ko,verdict:uploaded.verdict,reason:''});
        render(); continue;
      }
      if(file.size>20*1024*1024){ it.status='실패'; it.reason='용량 초과'; render(); continue; }
      it.status='올리는 중'; render();
      try{
        const b64=await ENTA_fileB64(file);
        if(!active()) break;
        const r=await apiFetch(`/cases/${bulk.cid}/documents`, {method:'POST',body:JSON.stringify({filename:file.name,file_b64:b64}),signal:bulk.controller.signal});
        if(!active()) break;
        if(!r.ok){
          const reason=await ENTA_uploadError(r);
          if(!active()) break;
          it.status='실패'; it.reason=reason;
        }
        else{
          const d=await r.json();
          if(!active()) break;
          it.document_id=d.document_id; it.status='분석 대기'; it.reason='';
          if(!d.autoparse_queued) it.reason='자동 분석 대기 — 재처리가 필요할 수 있습니다.';
        }
      }catch(e){ if(active()){ it.status='실패'; it.reason='파일 읽기 또는 서버 연결 실패'; } }
      if(active()) render();
    }
    if(active()) await ENTA_pollBulk(bulk, active);
  }finally{
    bulk.busy=false;
    if(active()){
      if(typeof PKG_load==='function') await PKG_load(bulk.cid);
      if(active()) render();
    }
  }
}
async function ENTA_pollBulk(bulk, active){
  const end=Date.now()+180000;
  while(active() && Date.now()<end && bulk.items.some(it=>it.document_id && it.status==='분석 대기')){
    await new Promise(resolve=>{
      const timer=setTimeout(done, Math.min(3000,end-Date.now()));
      function done(){ clearTimeout(timer); bulk.wake=null; resolve(); }
      bulk.wake=done;
    });
    if(!active() || Date.now()>=end) break;
    try{
      const r=await apiFetch(`/cases/${bulk.cid}/documents`, {signal:AbortSignal.any([bulk.controller.signal,AbortSignal.timeout(end-Date.now())])});
      if(!active()) return;
      if(!r.ok){ bulk.note='진행 상황 조회 실패: '+await ENTA_uploadError(r); if(r.status===403) break; }
      else{
        const docs=await r.json();
        if(!active()) return;
        bulk.note='';
        for(const it of bulk.items){
          if(it.status==='이미 올림') continue;
          const d=docs.find(x=>x.document_id===it.document_id);
          if(!d) continue;
          it.doc_type_ko=d.doc_type_ko||d.doc_type; it.verdict=(d.verify||{}).verdict;
          if((d.fields||{})._llm_error){ it.status='실패'; it.reason='AI 분석 실패 — 재처리 필요'; }
          else if(d.confidence!=null){ it.status='분석 완료'; it.reason=''; }
        }
      }
    }catch(e){ bulk.note='진행 상황을 불러오지 못했습니다. 문서함에서 다시 확인해 주세요.'; }
    if(active()) render();
  }
  if(!active()) return;
  if(bulk.items.some(it=>it.status==='분석 대기')) bulk.note=bulk.note||'3분간 확인했습니다. 분석 대기 서류는 문서함에서 다시 확인해 주세요.';
  if(Date.now()>=end) return;
  try{
    const r=await apiFetch(`/cases/${bulk.cid}/pending-extractions`, {signal:bulk.controller.signal});
    if(!active()) return;
    if(r.ok){
      const d=await r.json(), ids=new Set(bulk.items.map(it=>it.document_id));
      if(!active()) return;
      const n=(d.items||[]).filter(it=>it.kind==='material_names' && ids.has(it.document_id)).length;
      if(n) bulk.note+=(bulk.note?' ':'')+`원재료 ${n}건이 확인 대기에 있습니다(원재료 등록 창에서 확인).`;
    }
  }catch(e){ /* 업로드 결과는 유지하고 다음 점검에서 확인 대기를 다시 조회한다. */ }
}

App.entaSaveDraft = async function(){
  const cid = ENTA_CASE();
  if(!cid) return toast('대상 업체가 없습니다.');
  if(!await ENTP_saveProfile({quiet:true})) return;
  try{
    const r = await apiFetch(`/cases/${cid}/save-draft`, {method:'POST', body:JSON.stringify({})});
    if(!r.ok){
      if(r.status===403) return toast('이 작업은 권한이 없습니다.');
      let code=''; try{ code=(await r.json()).code; }catch(e){}
      return toast('임시저장에 실패했습니다.'+(code?` (${code})`:''));
    }
    await loadEnt();
    render(); toast('기업 정보를 임시저장했습니다. 품목·공장·조직은 각 등록 버튼으로 저장해 주세요.');
  }catch(e){ toast('임시저장에 실패했습니다.'); }
};

App.entaZip = async function(file){
  const cid = ENTA_CASE();
  if(!cid) return toast('대상 업체가 없습니다.');
  if(!file || !canCall('POST','/cases/{}/intake-zip-stream')) return;
  const fail = msg => { S.entaProg='ZIP 업로드 실패: '+msg; render(); toast(msg); };
  S.entaProg = 'ZIP 파일을 올리는 중…'; render();
  let b64;
  try{
    const buf = await file.arrayBuffer();
    let bin=''; const bytes=new Uint8Array(buf);
    const CH=0x8000;
    for(let i=0;i<bytes.length;i+=CH){ bin+=String.fromCharCode.apply(null, bytes.subarray(i,i+CH)); }
    b64 = btoa(bin);
  }catch(e){ return fail('파일을 읽을 수 없습니다.'); }

  let r;
  try{
    r = await apiFetch(`/cases/${cid}/intake-zip-stream`, {method:'POST', body:JSON.stringify({zip_b64:b64})});
  }catch(e){ return fail('업로드에 실패했습니다.'); }

  if(!r.ok){
    return fail(await ENTA_uploadError(r));
  }

  // SSE (text/event-stream) 스트림 — "data: {json}\n\n" 프레이밍
  const dec = new TextDecoder();
  let buf='', errMsg=null, result=null;
  const handle = (obj) => {
    if(obj.type==='start'){ S.entaProg=`처리 시작… (${obj.file_count||obj.total||0}건)`; render(); }
    else if(obj.type==='progress'){ S.entaProg=`처리 중 ${obj.index||obj.done||0}/${obj.total||obj.file_count||0} — ${obj.filename||''}`; render(); }
    else if(obj.type==='error'){ errMsg = ENTA_errorText(obj.code||obj.message||obj.detail||'처리 실패'); }
    else if(obj.type==='result'){ result=obj; S.entaProg='마무리 중…'; render(); }
  };
  try{
    const reader = r.body.getReader();
    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      buf += dec.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf('\n\n')) >= 0){
        const chunk = buf.slice(0, idx); buf = buf.slice(idx+2);
        for(const line of chunk.split('\n')){
          const t = line.trim();
          if(!t.startsWith('data:')) continue;
          try{ handle(JSON.parse(t.slice(5).trim())); }catch(e){}
        }
      }
    }
  }catch(e){ errMsg = errMsg || '처리 중 연결이 끊겼습니다.'; }

  if(errMsg || !result) return fail(errMsg||'완료 결과를 받지 못했습니다. 문서함을 확인해 주세요.');
  S.entaProg = '서류를 처리했습니다.';
  try{
    const pending=await apiFetch(`/cases/${cid}/pending-extractions`);
    if(pending.ok){
      const n=((await pending.json()).items||[]).filter(it=>it.kind==='material_names').length;
      if(n) S.entaProg+=` 원재료 ${n}건이 확인 대기에 있습니다(원재료 등록 창에서 확인).`;
    }
  }catch(e){ /* 처리 결과는 유지한다. */ }
  await loadEnt();
  if(typeof PKG_load==='function') await PKG_load(cid);
  render(); toast('서류를 처리했습니다.');
};

App.entaRefund = function(invoiceId){
  openModal('환불 요청', `
    <div class="field"><label>환불 사유 <span class="req">*</span></label>
      <textarea class="in" id="enta-refund-reason" rows="3" placeholder="환불 사유를 입력하세요"></textarea></div>
    <div class="field"><label>금액 (비우면 전액)</label>
      <input class="in" id="enta-refund-amount" inputmode="numeric" placeholder="예: 1500000"></div>
    <p class="muted" style="font-size:12px">관리자 승인(maker-checker) 후 처리됩니다.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button>
     <button class="btn btn-primary" onclick="App.entaRefundSubmit('${invoiceId}')">요청</button>`);
};

App.entaRefundSubmit = async function(invoiceId){
  const reason = (($('#enta-refund-reason')||{}).value||'').trim();
  const amtRaw = (($('#enta-refund-amount')||{}).value||'').trim();
  if(!reason){ const el=$('#enta-refund-reason'); if(el) el.focus(); return toast('환불 사유를 입력하세요.'); }
  const body = {reason};
  if(amtRaw){ const n = Number(amtRaw.replace(/[^0-9.]/g,'')); if(!isNaN(n)) body.amount = n; }
  try{
    const r = await apiFetch(`/invoices/${invoiceId}/refund/request`, {method:'POST', body:JSON.stringify(body)});
    if(!r.ok){
      if(r.status===403) return toast('이 작업은 권한이 없습니다. (operator 전용)');
      let code=''; try{ code=(await r.json()).code; }catch(e){}
      return toast('환불 요청에 실패했습니다.'+(code?` (${code})`:''));
    }
    App.closeModal();
    await loadEnt();
    render(); toast('환불을 요청했습니다. 관리자 승인 후 처리됩니다.');
  }catch(e){ toast('환불 요청에 실패했습니다.'); }
};
