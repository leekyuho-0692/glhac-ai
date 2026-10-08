/* ===== v4 연결 5: 단계별 전용 전이 버튼 =====
   컨설턴트 자유입력 전이(직무분리 우회)를 없애고, 역할이 실제로 할 수 있는 다음 전이만 버튼으로 둔다.
   모두 기존 POST /cases/{id}/transition — 권한(TRANSITION_ROLES)·가드는 서버가 판정, 막히면 사유 코드 표시.
   큐 조건은 index.html AUDQ(nav8 기준)와 같다. */

// ── 공통: 전이 호출 → 실패 시 403/가드 사유, 성공 시 캐시 무효화·재조회 ──
async function FA_go(cid, to, action, okMsg, reload){
  let r;
  try{ r = await apiFetch('/cases/'+cid+'/transition', {method:'POST', body:JSON.stringify({to_state:to, action})}); }
  catch(e){ return toast('단계 진행에 실패했습니다.'); }
  if(!r.ok){
    let d = null; try{ d = await r.clone().json(); }catch(e){}
    const bl = ((d && d.detail && d.detail.blockers) || []).map(b=>b.code + (b.count!=null?'×'+b.count:'')).filter(Boolean);
    const base = await apiErr(r, r.status===403 ? '이 단계 진행 권한이 없습니다.' : '단계를 진행할 수 없습니다.');
    return toast(base + (bl.length ? ' — ' + bl.join(', ') : ''));
  }
  ['onsite','final','mock','pre'].forEach(k=>{ if(RS[k]) delete RS[k][cid]; });
  try{ await reload(); }catch(e){}
  render(); toast(okMsg);
}
function FA_btn(label, onclick, primary){
  return `<button class="btn ${primary?'btn-primary':''}" onclick="${onclick}">${esc(label)}</button>`;
}
function FA_panel(title, note, btns){
  return `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2></div>
    ${note?`<p class="muted" style="font-size:13px">${note}</p>`:''}
    <div class="row-act" style="justify-content:flex-end;flex-wrap:wrap">${btns}</div></section>`;
}
const FA_case = cid => (RS.cases||[]).find(c=>c.case_id===cid);

// ── 오디터: 현장심사(aud-report) — 시작 / 종료 / 시정조치 요구 ──
App.faAud = function(cid, to, action, msg){ return FA_go(cid, to, action, msg, loadAud); };
function FA_audReport(cid){
  const c = FA_case(cid); if(!c || !AUDQ.report(c)) return '';
  if(c.status==='onsite_audit_scheduled')
    return FA_panel('현장심사 진행', '체크리스트를 저장하면 자동으로 진행 상태가 됩니다. 바로 시작할 수도 있습니다.',
      FA_btn('현장심사 시작', `App.faAud('${cid}','onsite_audit_in_progress','onsite.start','현장심사를 시작했습니다.')`, true));
  const car = c.status==='corrective_action_submitted';
  return FA_panel('현장심사 판정',
    car ? '기업이 시정조치를 제출했습니다. 보완·재전송 센터에서 시정조치를 수용한 뒤 종료하거나, 다시 요구할 수 있습니다.'
        : '미해결 지적·시정조치가 없으면 현장심사를 종료합니다. 지적이 남아 있으면 시정조치를 요구합니다.',
    FA_btn('시정조치 요구', `App.faAud('${cid}','corrective_action_required','onsite.car_required','시정조치를 요구했습니다. 기업에 전달됩니다.')`)
    + FA_btn('현장심사 종료', `App.faAud('${cid}','audit_closed','onsite.complete','현장심사를 종료했습니다.')`, true));
}
{ const f = VIEWS['aud-report']; if(f) VIEWS['aud-report'] = () => { const h = f(); return h + FA_audReport(S.selOnsite); }; }

// ── 오디터: 최종 취합(aud-final) — HPAS 평가 → 최종 패키지 → (기존 버튼) 파트와 상정 ──
function FA_audFinal(cid){
  const c = FA_case(cid); if(!c || !AUDQ.final(c)) return '';
  if(c.status==='audit_closed')
    return FA_panel('다음 단계', '보고서에 전자서명하면 HPAS 평가 단계로 자동 진행됩니다.',
      FA_btn('HPAS 평가 단계로 진행', `App.faAud('${cid}','hpas_evaluation_ready','hpas.ready','HPAS 평가 단계로 진행했습니다.')`));
  if(c.status==='hpas_evaluation_ready')
    return FA_panel('다음 단계', 'HPAS 5요소가 모두 적합이면 자동 진행됩니다. 미해결 중대 부적합이 없으면 바로 진행할 수 있습니다.',
      FA_btn('최종 패키지 준비로 진행', `App.faAud('${cid}','final_package_preparation','evaluation.complete','최종 패키지 준비 단계로 진행했습니다.')`, true));
  return FA_panel('다음 단계', '최종 패키지 준비 중입니다. 상정 요건이 충족되면 <b>샤리아 위원회 상정</b>으로 파트와 심의에 넘깁니다.', '');
}
{ const f = VIEWS['aud-final']; if(f) VIEWS['aud-final'] = () => { const h = f(); return h + FA_audFinal(S.selFinal); }; }

// ── 운영(adm): 운영 전이 대기 — LPH 배정 단계 진행 · 자기선언 위원회 회부 ──
const FA_ADM = {
  document_pre_audit_approved: ['lph_assignment', 'lph.assignment.start', 'LPH 배정 단계로 진행', '모의심사 승인 — 현장심사(LPH 배정) 단계로 넘깁니다.'],
  self_declaration_submitted: ['committee_verification', 'self_declare.committee', 'KFPH 위원회 회부', '자기선언 제출 — KFPH 위원회 검증으로 넘깁니다.'],
};
App.faAdm = function(cid, st){ const a = FA_ADM[st]; if(a) return FA_go(cid, a[0], a[1], a[2]+' 처리했습니다.', loadAdm); };
function FA_admQueue(){
  const L = (RS.cases||[]).filter(c=>!c.done && FA_ADM[c.status]);
  if(!L.length) return '';
  return `<section class="panel"><div class="panel-head"><h2>운영 처리 대기</h2><span class="muted">${L.length}건</span></div>
    <ul class="rows">${L.map(c=>{ const a = FA_ADM[c.status];
      return `<li class="row"><div><b>${esc(c.company_name||'')}</b><span class="muted">${esc(a[3])}</span></div>
        <button class="btn btn-sm btn-primary" onclick="App.faAdm('${c.case_id}','${c.status}')">${esc(a[2])}</button></li>`; }).join('')}</ul></section>`;
}
{ const f = VIEWS['adm-assign']; if(f) VIEWS['adm-assign'] = () => FA_admQueue() + f(); }
{ const g = NAVC['adm-assign']; NAVC['adm-assign'] = () => (g ? g() : 0) + (RS.cases||[]).filter(c=>!c.done && FA_ADM[c.status]).length; }

// ── 컨설턴트(cons-status): 컨설턴트 검토 완료 → 모의심사 요청 (자유입력 전이 대체) ──
App.faCons = function(cid){ return FA_go(cid, 'document_pre_audit_requested', 'mock_audit.request', '모의심사를 요청했습니다.', loadCons); };
function FA_consStatus(cid){
  const c = FA_case(cid); if(!c || c.status!=='consultant_review') return '';
  return FA_panel('다음 단계', '계약·서류 준비가 끝나면 모의심사를 요청합니다. 결제가 정리돼야 오디터가 모의심사를 시작할 수 있습니다.',
    FA_btn('모의심사 요청', `App.faCons('${cid}')`, true));
}
{ const f = VIEWS['cons-status']; if(f) VIEWS['cons-status'] = () => { const h = f(); return h + FA_consStatus(S.selCompany); }; }
