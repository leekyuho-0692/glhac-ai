/* ===== 관리자 신청접수·운영도구 ===== */
const ADMR_ERR = (r, d) => { const detail=(d&&d.detail)||d||{}; const blockers=(detail.blockers||[]).map(b=>b.message||b.code).join(', '); toast((detail.message || (r.status===403?'이 작업은 권한이 없습니다':'처리에 실패했습니다'))+(blockers?' · '+blockers:'')); };
const ADMR_busy = new Set();

async function ADMR_postAction(path, body){
  const caseId=path.startsWith('/cases/')?path.split('/')[2]:path.split('/')[3];
  if(ADMR_busy.has(caseId)) return false;
  ADMR_busy.add(caseId); render();
  try{
    const r = await apiFetch(path, {method:'POST', body:JSON.stringify(body||{})});
    let d = null; try{ d = await r.json(); }catch(e){}
    if(!r.ok){ ADMR_ERR(r, d); return false; }
    const item=(RS.cases||[]).find(c=>c.case_id===caseId);
    if(item){
      item.intake_pending=false;
      if(path.endsWith('/approve')) { item.company_decision='approved'; item.status=d.transitioned_to||item.status; }
      if(path.endsWith('/reject')) { item.company_decision='rejected'; item.draft_state='returned'; }
      if(path.endsWith('/return-application')) { item.draft_state='returned'; item.status='application_draft'; }
    }
    return true;
  }catch(e){ toast('요청에 실패했습니다.'); return false; }
  finally { ADMR_busy.delete(caseId); render(); }
}

async function ADMR_after(okMsg){
  let refreshed=false;
  try{ refreshed=(await loadAdm())!==false; }catch(e){}
  RS.admr = {};
  if(!refreshed){ render(); toast(okMsg+' 목록을 새로고침하지 못했습니다. 다시 조회해 주세요.'); return; }
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

const ADMR_pages = {pending:1, processed:1};
const ADMR_pageSize = 10;
App.admrPage = function(group, page){
  if(!(group in ADMR_pages)) return;
  ADMR_pages[group]=Math.max(1, Number(page)||1); render();
};
const ADMR_isPending = c => c.intake_pending === true;
function ADMR_intakeSection(list, group, title){
  const pages=Math.max(1,Math.ceil(list.length/ADMR_pageSize));
  const page=ADMR_pages[group]=Math.min(ADMR_pages[group],pages);
  const start=(page-1)*ADMR_pageSize;
  const rows = list.slice(start,start+ADMR_pageSize).map(c => {
    const pending=ADMR_isPending(c), busy=ADMR_busy.has(c.case_id);
    const label=pending?'승인 대기':c.company_decision==='rejected'?'거절':c.draft_state==='returned'?'보완 요청':c.company_decision==='approved'?'승인 완료':'신청서 작성 중';
    return `<li class="row">
    <div style="flex:1;min-width:0">
      <div><strong>${esc(c.company_name)}</strong> <span class="badge ${pending?'attn':'strong'}">${label}</span></div>
      <div class="muted" style="font-size:12px">${esc(_s8label(c))}</div>
    </div>
    <div class="row-act">
      ${pending&&canCall('POST','/ops/companies/{}/approve')?`<button class="btn btn-sm btn-primary" ${busy?'disabled':''} onclick="App.admrApprove('${c.case_id}')">${busy?'처리 중…':'승인'}</button>`:''}
      ${pending&&canCall('POST','/ops/companies/{}/reject')?`<button class="btn btn-sm" ${busy?'disabled':''} onclick="App.admrReject('${c.case_id}')">거절</button>`:''}
      ${!pending?`<button class="btn btn-sm" disabled>${label}</button>`:''}
      ${!['onboarding','application_draft'].includes(c.status)&&c.draft_state!=='returned'&&c.company_decision!=='rejected'&&canCall('POST','/cases/{}/return-application')?`<button class="btn btn-sm btn-ghost" ${busy?'disabled':''} onclick="App.admrReturn('${c.case_id}')">신청서 반려(보완 요청)</button>`:''}
    </div>
  </li>`; }).join('');
  return `<section class="panel" aria-label="${title}">
    <div class="panel-head"><h2>${title}</h2><span class="count">${list.length}건</span></div>
    ${rows?`<ul class="rows">${rows}</ul>`:'<p class="empty">해당 업체가 없습니다.</p>'}
    <nav class="row-act" aria-label="${title} 페이지" style="margin-top:16px">
      <button class="btn btn-sm" ${page===1?'disabled':''} onclick="App.admrPage('${group}',${page-1})">이전</button>
      <span aria-live="polite">${page} / ${pages} 페이지 · ${list.length?start+1:0}–${Math.min(start+ADMR_pageSize,list.length)} / ${list.length}건</span>
      <button class="btn btn-sm" ${page===pages?'disabled':''} onclick="App.admrPage('${group}',${page+1})">다음</button>
    </nav>
  </section>`;
}
const ADMR_intakeHtml = () => {
  const list=(RS.cases||[]).filter(c=>!c.done&&c.step8===0);
  const pending=list.filter(ADMR_isPending).sort((a,b)=>String(a.created_at||'').localeCompare(String(b.created_at||''))||a.case_id.localeCompare(b.case_id));
  const processed=list.filter(c=>!ADMR_isPending(c));
  return ADMR_intakeSection(pending,'pending','신청 접수 처리 · 승인 대기')+
    ADMR_intakeSection(processed,'processed','작성·보완 중 또는 접수 처리된 신청');
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
