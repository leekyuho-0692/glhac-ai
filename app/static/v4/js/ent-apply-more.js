/* ===== 인증기업 보강(서류 체크리스트·임시저장/ZIP 일괄업로드·환불 요청) ===== */

const ENTA_LANG = () => (typeof S !== 'undefined' && S.lang) ? S.lang : 'ko';
const ENTA_CASE = () => (RS.cases && RS.cases[0] && RS.cases[0].case_id) || null;

async function ENTA_loadChecklist(cid){
  try{
    const r = await apiFetch(`/cases/${cid}/doc-checklist?lang=ko`);
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
  const rows = Array.isArray(d) ? d : [];
  const req = rows.filter(r=>r.required);
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
        <td class="muted" style="font-size:12px">${esc(r.na_reason||r.note||'')}</td>
      </tr>`).join('')}</tbody></table></div>` : '<p class="empty">서류 정보가 없습니다.</p>';
  return `<section class="panel"><div class="panel-head"><h2>필요 서류 현황</h2>
      <span class="muted">필수 ${reqOk}/${req.length} 제출</span></div>
    ${missing.length?`<p class="note attn">미제출 필수 서류 ${missing.length}건: ${missing.map(m=>esc(m.doc_type_ko||m.doc_type)).join(', ')}</p>`:''}
    ${body}</section>`;
}

function ENTA_applyPanel(){
  const cid = ENTA_CASE();
  if(!cid) return '';
  const prog = (S.entaProg||'');
  return `<section class="panel"><div class="panel-head"><h2>임시저장 · 일괄 업로드</h2></div>
    <div class="inline" style="flex-wrap:wrap;gap:8px">
      <button class="btn" onclick="App.entaSaveDraft()">임시저장</button>
      <button class="btn btn-primary" onclick="(($('#enta-zip')||{}).click||(()=>{}))()">ZIP 파일로 서류 일괄 올리기</button>
      <input type="file" id="enta-zip" accept=".zip" hidden onchange="App.entaZip(this.files[0])">
    </div>
    <p class="muted" style="font-size:12px;margin-top:6px">ZIP·RAR·7z 를 압축 해제해 서류를 자동 분류·채웁니다. 처리 중에는 창을 닫지 마세요.</p>
    ${prog?`<p class="note">${esc(prog)}</p>`:''}
  </section>`;
}

function ENTA_refundPanel(){
  const cid = ENTA_CASE();
  if(!cid) return '';
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
App.entaSaveDraft = async function(){
  const cid = ENTA_CASE();
  if(!cid) return toast('대상 업체가 없습니다.');
  try{
    const r = await apiFetch(`/cases/${cid}/save-draft`, {method:'POST', body:JSON.stringify({})});
    if(!r.ok){
      if(r.status===403) return toast('이 작업은 권한이 없습니다.');
      let code=''; try{ code=(await r.json()).code; }catch(e){}
      return toast('임시저장에 실패했습니다.'+(code?` (${code})`:''));
    }
    await loadEnt();
    render(); toast('임시저장했습니다.');
  }catch(e){ toast('임시저장에 실패했습니다.'); }
};

App.entaZip = async function(file){
  const cid = ENTA_CASE();
  if(!cid) return toast('대상 업체가 없습니다.');
  if(!file) return;
  S.entaProg = 'ZIP 파일을 올리는 중…'; render();
  let b64;
  try{
    const buf = await file.arrayBuffer();
    let bin=''; const bytes=new Uint8Array(buf);
    const CH=0x8000;
    for(let i=0;i<bytes.length;i+=CH){ bin+=String.fromCharCode.apply(null, bytes.subarray(i,i+CH)); }
    b64 = btoa(bin);
  }catch(e){ S.entaProg=null; render(); return toast('파일을 읽을 수 없습니다.'); }

  let r;
  try{
    r = await apiFetch(`/cases/${cid}/intake-zip-stream`, {method:'POST', body:JSON.stringify({zip_b64:b64})});
  }catch(e){ S.entaProg=null; render(); return toast('업로드에 실패했습니다.'); }

  if(!r.ok){
    S.entaProg=null; render();
    if(r.status===403) return toast('이 작업은 권한이 없습니다.');
    if(r.status===422) return toast('압축 파일이 아닙니다 (ZIP·RAR·7z 만 지원합니다)');
    let code=''; try{ code=(await r.json()).code; }catch(e){}
    return toast('업로드에 실패했습니다.'+(code?` (${code})`:''));
  }

  // SSE (text/event-stream) 스트림 — "data: {json}\n\n" 프레이밍
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf='', errMsg=null;
  const handle = (obj) => {
    if(obj.type==='start'){ S.entaProg=`처리 시작… (${obj.file_count||obj.total||0}건)`; render(); }
    else if(obj.type==='progress'){ S.entaProg=`처리 중 ${obj.index||obj.done||0}/${obj.total||obj.file_count||0} — ${obj.filename||''}`; render(); }
    else if(obj.type==='error'){ errMsg = obj.message||'처리 실패'; }
    else if(obj.type==='result'){ S.entaProg='마무리 중…'; render(); }
  };
  try{
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

  S.entaProg = null;
  if(errMsg){ render(); return toast(errMsg); }
  await loadEnt();
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
