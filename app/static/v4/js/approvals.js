/* ===== 2인 승인(maker-checker) 승인함 ===== */
const APRV_IDX = (i) => (RS.aprv && RS.aprv.items && RS.aprv.items[i]) || null;

async function APRV_load(){
  try{
    const r = await apiFetch('/approvals?status=pending');
    if(r.status === 403){
      RS.aprv = {items:[], forbidden:true};
      return;
    }
    if(!r.ok){
      RS.aprv = {items:[], error:true, status:r.status};
      return;
    }
    const data = await r.json();
    const items = Array.isArray(data) ? data : (data && data.items) || [];
    RS.aprv = {items};
  }catch(e){
    RS.aprv = {items:[], error:true};
  }
}

function APRV_isMine(it){
  if(!it) return false;
  const uname = AUTH && AUTH.username;
  if(it.requested_by && AUTH && AUTH.uid && it.requested_by === AUTH.uid) return true;
  if(it.requester_name && uname && it.requester_name === uname) return true;
  return false;
}

function APRV_fmtTime(iso){
  if(!iso) return '';
  try{
    const d = new Date(iso);
    if(isNaN(d.getTime())) return String(iso);
    return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes());
  }catch(e){ return String(iso); }
}

function APRV_row(it, i){
  const label = it.label || it.action_type || '(알 수 없는 작업)';
  const target = it.company_name || it.case_id || '';
  const who = it.requester_name || it.requester_role || it.requested_by || '';
  const when = APRV_fmtTime(it.created_at);
  const mine = APRV_isMine(it);
  const meta = [target, '요청: '+who, when].filter(x=>x&&String(x).length).join(' · ');
  const acts = mine
    ? `<span class="muted" style="font-size:11px">내가 요청한 건 — 다른 계정이 승인해야 합니다</span>`
    : `<button class="btn btn-sm btn-primary" onclick="App.aprvApprove(${i})">승인</button>
      <button class="btn btn-sm" onclick="App.aprvReject(${i})">반려</button>`;
  return `<li class="row">
    <div style="min-width:0">
      <div style="font-weight:600;margin-bottom:2px">${esc(label)}</div>
      <div class="muted" style="font-size:12px">${esc(meta)}</div>
      ${it.reason ? `<div class="note" style="margin-top:6px">사유: ${esc(it.reason)}</div>` : ''}
    </div>
    <div class="row-act">${acts}</div>
  </li>`;
}

function APRV_panel(){
  if(RS.aprv === undefined){
    if(RS._aprvLoading !== true){
      RS._aprvLoading = true;
      APRV_load().then(()=>{ RS._aprvLoading = false; render(); });
    }
    return '';
  }
  if(RS.aprv.forbidden) return '';
  const items = (RS.aprv.items||[]);
  if(!items.length) return '';
  const n = items.length;
  return `<section class="panel">
    <div class="panel-head">
      <h2>승인 대기</h2>
      <span class="badge attn">${n}건</span>
    </div>
    <ul class="rows">${items.map((it,i)=>APRV_row(it,i)).join('')}</ul>
  </section>`;
}

App.aprvApprove = async function(i){
  const it = APRV_IDX(i);
  if(!it) return;
  try{
    const r = await apiFetch('/approvals/'+it.approval_id+'/approve', {method:'POST', body:JSON.stringify({reason: null})});
    if(!r.ok){
      let msg = '승인에 실패했습니다.';
      let code = '';
      try{ const d = await r.json(); code = d && d.detail && d.detail.code ? d.detail.code : (d && d.code ? d.code : ''); }catch(e){}
      if(r.status === 403 && code === 'SELF_APPROVAL_FORBIDDEN') msg = '본인이 요청한 건은 승인할 수 없습니다.';
      else if(r.status === 403) msg = '승인 권한이 없습니다.';
      else if(r.status === 409) msg = '이미 처리된 요청입니다.';
      else if(code) msg = msg + ' (' + code + ')';
      return toast(msg);
    }
  }catch(e){
    return toast('승인에 실패했습니다.');
  }
  RS.aprv = undefined;
  await APRV_load();
  try{ await (S.role === 'sha' ? loadSha() : loadAdm()); }catch(e){}
  render();
  toast('승인했습니다.');
};

App.aprvReject = function(i){
  const it = APRV_IDX(i);
  if(!it) return;
  const label = it.label || it.action_type || '승인 요청';
  const body = `<div class="field">
      <label>반려 사유 <span class="req">*</span></label>
      <textarea class="in" id="aprv-reason" rows="3" placeholder="반려 사유를 입력하세요"></textarea>
      <div class="inline-msg" id="aprv-reason-msg"></div>
    </div>
    <div class="note" style="margin-top:10px">${esc(label)} — ${esc(it.company_name || it.case_id || '')}</div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
    <button class="btn btn-primary" onclick="App.aprvRejectDo(${i})">반려</button>`;
  openModal('요청 반려', body, foot);
};

App.aprvRejectDo = async function(i){
  const it = APRV_IDX(i);
  if(!it) return App.closeModal();
  const reason = (($('#aprv-reason')||{}).value||'').trim();
  if(!reason){
    const m = $('#aprv-reason-msg');
    if(m) m.textContent = '반려 사유를 입력하세요.';
    return;
  }
  let ok = false;
  try{
    const r = await apiFetch('/approvals/'+it.approval_id+'/reject', {method:'POST', body:JSON.stringify({reason})});
    if(!r.ok){
      let msg = '반려에 실패했습니다.';
      let code = '';
      try{ const d = await r.json(); code = d && d.detail && d.detail.code ? d.detail.code : (d && d.code ? d.code : ''); }catch(e){}
      if(r.status === 403 && code === 'SELF_APPROVAL_FORBIDDEN') msg = '본인이 요청한 건은 반려할 수 없습니다.';
      else if(r.status === 403) msg = '반려 권한이 없습니다.';
      else if(r.status === 409) msg = '이미 처리된 요청입니다.';
      else if(code) msg = msg + ' (' + code + ')';
      toast(msg);
    } else {
      ok = true;
    }
  }catch(e){
    toast('반려에 실패했습니다.');
  }
  if(!ok) return;
  App.closeModal();
  RS.aprv = undefined;
  await APRV_load();
  try{ await (S.role === 'sha' ? loadSha() : loadAdm()); }catch(e){}
  render();
  toast('반려했습니다.');
};

/* 화면 삽입 */
['sha-home','adm-home','adm-cert'].forEach(v=>{
  if(typeof VIEWS[v] === 'function'){
    const f = VIEWS[v];
    VIEWS[v] = () => APRV_panel() + f();
  }
});

/* 사이드바 배지 */
const APRV_count = () => (RS.aprv && RS.aprv.items ? RS.aprv.items.length : 0);
if(NAVC){
  NAVC['sha-home'] = APRV_count;
  NAVC['adm-cert'] = APRV_count;
}

{ const g = App.go; App.go = async function(v){ RS.aprv = undefined; return g.call(App, v); }; }
