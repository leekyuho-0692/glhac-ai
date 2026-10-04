/* ===== 인증서 갱신: 신청(기업·컨설턴트) ↔ 승인(운영관리자) 분리 =====
   POST /cases/{id}/renew/request  — rbac certificate.renew_request = applicant·consultant (+admin). 서버는 certificate_issued 상태만 허용.
   POST /cases/{id}/renew          — rbac certificate.renew = operator (+admin). 발급·사후관리·변경영향·갱신준비 상태 허용 → 갱신 케이스 파생. */

const RN_REQ_ST = ['certificate_issued'];
const RN_OK_ST = ['certificate_issued','post_certification_monitoring','change_impact','renewal_preparation'];
const RN_can = (id, roles) => (typeof CAN==='function') ? !!CAN(id) : roles.includes(AUTH.role);

function RN_reqPanel(c){
  if(!c || !RN_REQ_ST.includes(c.status)) return '';
  if(!RN_can('certificate.renew_request', ['applicant','consultant','admin'])) return '';
  return `<section class="panel"><div class="panel-head"><h2>인증서 갱신</h2><span class="badge">발급 완료</span></div>
    <p class="muted">유효기간 만료 전에 갱신을 신청하세요. 운영관리자가 승인하면 원료·제품을 이어받은 갱신 신청이 생성됩니다.</p>
    <div class="row-act" style="justify-content:flex-end"><button class="btn btn-primary" onclick="App.rnReq('${esc(c.case_id)}')">갱신 신청</button></div></section>`;
}
App.rnReq = function(cid){
  openModal('인증서 갱신 신청', `<p>갱신 의사를 등록합니다. 사유·메모(선택).</p><div class="field"><label for="rn-r">사유</label><textarea class="in" id="rn-r" placeholder="예) 유효기간 만료 3개월 전 갱신"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.rnDoReq('${esc(cid)}')">신청</button>`);
};
App.rnDoReq = async function(cid){
  const reason = (($('#rn-r')||{}).value||'').trim() || null;
  const r = await apiFetch('/cases/'+cid+'/renew/request', {method:'POST', body:JSON.stringify({reason})});
  if(!r.ok) return toast(await apiErr(r, '갱신 신청에 실패했습니다.'));
  const d = await r.json().catch(()=>({}));
  closeModal(); toast(d.message || '갱신 신청이 접수되었습니다.');
};

function RN_okPanel(cid){
  const c = (RS.cases||[]).find(x=>x.case_id===cid);
  if(!c || !RN_OK_ST.includes(c.status)) return '';
  if(!RN_can('certificate.renew', ['operator','admin'])) return '';
  return `<section class="panel"><div class="panel-head"><h2>인증서 갱신 승인</h2><span class="muted mono">${esc(c.status)}</span></div>
    <p class="muted">기업·컨설턴트의 갱신 신청을 확인한 뒤 승인하면 갱신 케이스(onboarding)가 생성되고, 원 케이스는 갱신 준비 단계로 이동합니다.</p>
    <div class="row-act" style="justify-content:flex-end"><button class="btn btn-primary" onclick="App.rnOk('${esc(cid)}')">갱신 승인 · 갱신 케이스 생성</button></div></section>`;
}
App.rnOk = function(cid){
  openModal('갱신 승인', `<p>갱신 케이스를 생성합니다. 원료·제품 목록이 복사되며 원 케이스는 <b>renewal_preparation</b>으로 이동합니다.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.rnDoOk('${esc(cid)}')">승인</button>`);
};
App.rnDoOk = async function(cid){
  const r = await apiFetch('/cases/'+cid+'/renew', {method:'POST'});
  if(!r.ok) return toast(await apiErr(r, '갱신 승인에 실패했습니다.'));
  const d = await r.json();
  closeModal(); if(RS.admCert) delete RS.admCert[cid];
  await loadAdm(); render();
  toast('갱신 케이스를 생성했습니다. ('+String(d.new_case_id||'').slice(0,8)+' · 원 케이스 '+(d.parent_status||'')+')');
};

// ── 화면 확장 ──
{ const f = VIEWS['ent-home']; if(typeof f==='function') VIEWS['ent-home'] = () => f() + RN_reqPanel((RS.cases||[])[0]); }
{ const f = VIEWS['cons-status']; if(typeof f==='function') VIEWS['cons-status'] = () => {
    const L = RS.cases||[]; const c = L.find(x=>x.case_id===S.selCompany) || L[0];
    return f() + RN_reqPanel(c);
  }; }
{ const f = VIEWS['adm-cert']; if(typeof f==='function') VIEWS['adm-cert'] = () => f() + (S.selCert ? RN_okPanel(S.selCert) : ''); }
