/* ===== 2인 확인(two-person) 프런트 접목 ===== */
const TP_origRow = APRV_row;

APRV_row = function(it, i){
  if(!it || !it.two_person) return TP_origRow(it, i);
  const label = it.label || it.action_type || '(알 수 없는 작업)';
  const target = it.company_name || it.case_id || '';
  const who = it.requester_name || it.requester_role || it.requested_by || '';
  const when = APRV_fmtTime(it.created_at);
  const mine = APRV_isMine(it);
  const meta = [target, '요청: '+who, when].filter(x=>x&&String(x).length).join(' · ');
  const pl = it.payload || {};
  const verdict = (pl.verdict!==undefined && pl.verdict!==null) ? pl.verdict : pl.result;
  const hasVerdict = verdict!==undefined && verdict!==null && String(verdict).length;
  const acts = mine
    ? `<span class="muted" style="font-size:11px">서브 오디터 확인 대기</span>
      <button class="btn btn-sm" onclick="App.tpCancel(${i})">요청 취소</button>`
    : APRV_canDecide(it)
    ? `<button class="btn btn-sm btn-primary" onclick="App.aprvApprove(${i})">점검 확인 · 승인</button>
      <button class="btn btn-sm" onclick="App.tpReturn(${i})">의견 달아 되돌리기</button>`
    : `<span class="muted" style="font-size:11px">결정 권한 없음 — 서브 오디터만 확인할 수 있습니다</span>`;
  return `<li class="row">
    <div style="min-width:0">
      <div style="font-weight:600;margin-bottom:2px">${esc(label)} <span class="badge">2인 확인</span></div>
      <div class="muted" style="font-size:12px">${esc(meta)}</div>
      ${hasVerdict ? `<div class="muted" style="font-size:12px;margin-top:4px">판정: ${esc(String(verdict))}</div>` : ''}
      ${it.reason ? `<div class="note" style="margin-top:6px">사유: ${esc(it.reason)}</div>` : ''}
    </div>
    <div class="row-act">${acts}</div>
  </li>`;
};

App.tpReturn = function(i){
  const it = APRV_IDX(i);
  if(!it) return;
  const label = it.label || it.action_type || '2인 확인 요청';
  const body = `<div class="field">
      <label>의견 <span class="req">*</span></label>
      <textarea class="in" id="tp-opinion" rows="3" placeholder="메인 오디터에게 전달할 의견을 입력하세요"></textarea>
      <div class="inline-msg" id="tp-opinion-msg"></div>
    </div>
    <div class="note" style="margin-top:10px">메인 오디터에게 의견이 전달되고 처리는 실행되지 않습니다.</div>
    <div class="note" style="margin-top:6px">${esc(label)} — ${esc(it.company_name || it.case_id || '')}</div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
    <button class="btn btn-primary" onclick="App.tpReturnDo(${i})">되돌리기</button>`;
  openModal('의견 달아 되돌리기', body, foot);
};

App.tpReturnDo = async function(i){
  const it = APRV_IDX(i);
  if(!it) return App.closeModal();
  const reason = (($('#tp-opinion')||{}).value||'').trim();
  if(!reason){
    const m = $('#tp-opinion-msg');
    if(m) m.textContent = '의견을 입력하세요.';
    return;
  }
  let ok = false;
  try{
    const r = await apiFetch('/approvals/'+it.approval_id+'/reject', {method:'POST', body:JSON.stringify({reason})});
    if(!r.ok){
      let msg = '되돌리기에 실패했습니다.';
      let code = '';
      try{ const d = await r.json(); code = d && d.detail && d.detail.code ? d.detail.code : (d && d.code ? d.code : ''); }catch(e){}
      if(r.status === 422 && code === 'OPINION_REQUIRED') msg = '의견을 입력하세요.';
      else if(r.status === 403) msg = '권한이 없습니다.';
      else if(r.status === 409) msg = '이미 처리된 요청입니다.';
      else if(code) msg = msg + ' (' + code + ')';
      toast(msg);
    } else {
      ok = true;
    }
  }catch(e){
    toast('되돌리기에 실패했습니다.');
  }
  if(!ok) return;
  App.closeModal();
  RS.aprv = undefined;
  await APRV_load();
  try{ await loadAud(); }catch(e){}
  render();
  toast('되돌렸습니다 — 메인 오디터에게 의견이 전달됩니다.');
};

App.tpCancel = async function(i){
  const it = APRV_IDX(i);
  if(!it) return;
  let ok = false;
  try{
    const r = await apiFetch('/approvals/'+it.approval_id+'/cancel', {method:'POST', body:JSON.stringify({})});
    if(!r.ok){
      let msg = '취소에 실패했습니다.';
      let code = '';
      try{ const d = await r.json(); code = d && d.detail && d.detail.code ? d.detail.code : (d && d.code ? d.code : ''); }catch(e){}
      if(r.status === 403) msg = '요청자만 취소할 수 있습니다.';
      else if(r.status === 409) msg = '이미 처리된 요청입니다.';
      else if(code) msg = msg + ' (' + code + ')';
      toast(msg);
    } else {
      ok = true;
    }
  }catch(e){
    toast('취소에 실패했습니다.');
  }
  if(!ok) return;
  RS.aprv = undefined;
  await APRV_load();
  try{ await loadAud(); }catch(e){}
  render();
  toast('요청을 취소했습니다.');
};

{ const g = App.aprvApprove; App.aprvApprove = async function(i){ const ret = await g.call(App, i); try{ if(S.role==='aud'){ await loadAud(); render(); } }catch(e){} return ret; }; }

['aud-home'].forEach(v=>{
  if(typeof VIEWS[v] === 'function'){
    const f = VIEWS[v];
    VIEWS[v] = () => APRV_panel() + f();
  }
});

if(NAVC){
  NAVC['aud-home'] = APRV_count;
}
