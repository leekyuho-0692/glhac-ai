/* ===== 관리자 신청접수·운영도구 ===== */
const ADMR_ERR = (r, d) => { const code = (d && d.detail && d.detail.code) || (d && d.code); const pfx = r.status===403 ? '이 작업은 권한이 없습니다' : '실패했습니다'; toast(code ? `${pfx} (${code})` : pfx); };

async function ADMR_postAction(path, body){
  try{
    const r = await apiFetch(path, {method:'POST', body:JSON.stringify(body||{})});
    let d = null; try{ d = await r.json(); }catch(e){}
    if(!r.ok){ ADMR_ERR(r, d); return false; }
    return true;
  }catch(e){ toast('요청에 실패했습니다.'); return false; }
}

async function ADMR_after(okMsg){
  try{ await loadAdm(); }catch(e){}
  render(); toast(okMsg);
}

App.admrApprove = async function(caseId){
  if(await ADMR_postAction('/ops/companies/'+caseId+'/approve', {})) await ADMR_after('승인되었습니다.');
};

App.admrReject = function(caseId){
  openModal('신규 업체 거절',
    `<div class="field"><label>사유 <span class="req">*</span></label><textarea class="in" id="admr-reason" rows="4"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button>
     <button class="btn btn-primary" onclick="App.admrRejectDo('${caseId}')">거절</button>`);
};
App.admrRejectDo = async function(caseId){
  const reason = (($('#admr-reason')||{}).value||'').trim();
  if(!reason) return toast('사유를 입력하세요.');
  if(await ADMR_postAction('/ops/companies/'+caseId+'/reject', {reason})){
    App.closeModal(); await ADMR_after('거절 처리되었습니다.');
  }
};

App.admrReturn = function(caseId){
  openModal('신청서 반려(보완 요청)',
    `<div class="field"><label>반려 사유 <span class="req">*</span></label><textarea class="in" id="admr-reason" rows="4"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button>
     <button class="btn btn-primary" onclick="App.admrReturnDo('${caseId}')">반려</button>`);
};
App.admrReturnDo = async function(caseId){
  const reason = (($('#admr-reason')||{}).value||'').trim();
  if(!reason) return toast('사유를 입력하세요.');
  if(await ADMR_postAction('/cases/'+caseId+'/return-application', {reason})){
    App.closeModal(); await ADMR_after('반려되었습니다.');
  }
};

const ADMR_intakeHtml = () => {
  const list = (RS.cases||[]).filter(c => !c.done && c.step8===0);
  if(!list.length) return '';
  const rows = list.map(c => `<li class="row">
    <div style="flex:1;min-width:0">
      <div><strong>${esc(c.company_name)}</strong> <span class="badge attn">${esc(_s8label(c))}</span></div>
      <div class="muted mono" style="font-size:11px">${esc(c.status||'')}</div>
    </div>
    <div class="row-act">
      ${canCall('POST','/ops/companies/{}/approve')?`<button class="btn btn-sm btn-primary" onclick="App.admrApprove('${c.case_id}')">승인</button>`:''}
      ${canCall('POST','/ops/companies/{}/reject')?`<button class="btn btn-sm" onclick="App.admrReject('${c.case_id}')">거절</button>`:''}
      <button class="btn btn-sm btn-ghost" onclick="App.admrReturn('${c.case_id}')">신청서 반려(보완 요청)</button>
    </div>
  </li>`).join('');
  return `<section class="panel">
    <div class="panel-head"><h2>신청 접수 처리</h2><span class="count">${list.length}건</span></div>
    <ul class="rows">${rows}</ul>
  </section>`;
};
{ const f = VIEWS['adm-home']; VIEWS['adm-home'] = () => ADMR_intakeHtml() + (f ? f() : ''); }

/* --- 운영 도구 탭 --- */
RS.admr = RS.admr || {};
async function ADMR_load(key, path){
  try{ const r = await apiFetch(path); RS.admr[key] = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.admr[key] = {error:true}; }
}
const ADMR_fetch = (key, path) => { if(!RS.admr[key]) ADMR_load(key, path).then(render); };
const ADMR_panel = (d, title, inner) => {
  if(!d) return `<section class="panel"><div class="panel-head"><h2>${title}</h2></div><p class="empty">불러오는 중…</p></section>`;
  if(d.error) return `<section class="panel"><div class="panel-head"><h2>${title}</h2></div><p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  return `<section class="panel"><div class="panel-head"><h2>${title}</h2></div>${inner(d)}</section>`;
};

function ADMR_dash(){
  const d = RS.admr.dash, cal = RS.admr.cal, ac = RS.admr.allcases;
  const canAll = canCall('GET','/admin/cases');   // 전체 케이스는 admin 전용(main.py:1715)
  ADMR_fetch('dash', '/ops/dashboard'); ADMR_fetch('cal','/ops/calendar'); if(canAll) ADMR_fetch('allcases','/admin/cases?limit=100&meta=1');
  const dashHtml = ADMR_panel(d, '운영 대시보드', x => {
    const cap = x.capacity||{}, bl = cap.backlog||{};
    const sum = `<div class="summary">
      <div><span class="k">전체 케이스</span><span class="v mono">${cap.total??0}</span></div>
      <div><span class="k">신규승인 대기</span><span class="v mono">${bl.pending_company??0}</span></div>
      <div><span class="k">서류심사 대기</span><span class="v mono">${bl.doc_audit_wait??0}</span></div>
      <div><span class="k">현장심사 대기</span><span class="v mono">${bl.onsite_wait??0}</span></div>
      <div><span class="k">파트와 대기</span><span class="v mono">${bl.fatwa_wait??0}</span></div>
      <div><span class="k">보완요구</span><span class="v mono">${bl.corrective_wait??0}</span></div>
    </div>`;
    const st = (cap.by_status||[]).map(r=>`<tr><td>${esc(r.label)}</td><td class="num mono">${r.count}</td></tr>`).join('');
    const reg = (x.regions&&x.regions.regions||[]).map(r=>`<tr><td>${esc(r.province)}</td><td class="num mono">${r.count}</td></tr>`).join('');
    const rej = (x.assignment_rejected||[]).map(r=>`<tr><td>${esc(r.company)}</td><td>${esc(r.auditor_name||'')}</td><td class="muted">${esc(r.reject_reason||'')}</td></tr>`).join('');
    const aw = (x.assignment_awaiting||[]).map(r=>`<tr><td>${esc(r.company)}</td><td>${esc(r.auditor_name||'')}</td><td><span class="badge attn">수락 대기</span></td></tr>`).join('');
    const una = (x.unassigned_clients&&x.unassigned_clients.items||[]).map(r=>`<tr><td>${esc(r.name)}</td><td class="num mono">${r.cases??0}</td></tr>`).join('');
    return `<div class="grid-2" style="align-items:start">
      <div>${sum}
        <div class="fieldset"><legend>상태별</legend><div class="tbl-wrap"><table><tbody>${st||'<tr><td class="muted">없음</td></tr>'}</tbody></table></div></div>
        <div class="fieldset"><legend>지역별</legend><div class="tbl-wrap"><table><tbody>${reg||'<tr><td class="muted">지역 데이터 없음</td></tr>'}</tbody></table></div></div>
      </div>
      <div>
        <div class="fieldset"><legend>배정 거절(재배정 대상)</legend><div class="tbl-wrap"><table><thead><tr><th>업체</th><th>심사원</th><th>사유</th></tr></thead><tbody>${rej||'<tr><td class="muted">없음</td></tr>'}</tbody></table></div></div>
        <div class="fieldset"><legend>배정 수락 대기</legend><div class="tbl-wrap"><table><thead><tr><th>업체</th><th>심사원</th><th></th></tr></thead><tbody>${aw||'<tr><td class="muted">없음</td></tr>'}</tbody></table></div></div>
        <div class="fieldset"><legend>담당 컨설턴트 미지정</legend><div class="tbl-wrap"><table><thead><tr><th>업체</th><th>케이스</th></tr></thead><tbody>${una||'<tr><td class="muted">없음</td></tr>'}</tbody></table></div></div>
      </div>
    </div>`;
  });
  const calHtml = ADMR_panel(cal, '현장심사 일정', x => {
    const ev = (x.events||[]).map(e=>`<tr><td class="mono">${esc(e.date)}</td><td>${esc(e.company_name)}</td><td>${esc(e.lph_name||'')}</td><td><span class="badge ${e.status==='confirmed'?'strong':'attn'}">${esc(e.status)}</span> ${esc(e.kind)}</td></tr>`).join('');
    return `<div class="tbl-wrap"><table><thead><tr><th>일자</th><th>업체</th><th>심사원</th><th>상태</th></tr></thead><tbody>${ev||'<tr><td colspan="4" class="muted">일정이 없습니다.</td></tr>'}</tbody></table></div>`;
  });
  const acHtml = !canAll ? '' : ADMR_panel(ac, '전체 케이스', x => {
    const items = x.items||x;
    const rows = items.map(c=>`<tr><td class="mono">${esc(c.case_id)}</td><td>${esc(c.company_name)}</td><td>${esc(c.status||'')}</td><td>${esc(c.pathway||'')}</td><td>${esc(c.org_id||'')}</td></tr>`).join('');
    const tot = (x.total!=null) ? `<span class="count">총 ${x.total}</span>` : '';
    return `<div class="tbl-wrap"><table><thead><tr><th>ID</th><th>업체</th><th>상태</th><th>경로</th><th>기관</th></tr></thead><tbody>${rows||'<tr><td colspan="5" class="muted">없음</td></tr>'}</tbody></table></div>${tot}`;
  });
  return `<div style="display:flex;flex-direction:column;gap:22px">${dashHtml}${calHtml}${acHtml}</div>`;
}

if(typeof OPS_TABS !== 'undefined'){
  OPS_TABS.push({key:'dash', label:'운영 현황', render: ADMR_dash});
}
