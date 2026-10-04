/* ===== BPJPH 제출 정보 (ent-apply 확장) ===== */
RS.entpCase = RS.entpCase || null;
RS.entpPen = RS.entpPen || null;
S.entpOpen = S.entpOpen || false;

const ENTP_PROFILE_FIELDS = [
  ["company_name", "기업명", "text"],
  ["nib", "사업자 식별번호(NIB)", "text"],
  ["responsible_person", "책임자", "text"],
  ["halal_supervisor", "할랄 감독자", "text"],
  ["email", "이메일", "email"],
  ["phone", "전화", "text"],
  ["address", "회사 주소", "textarea"],
  ["factory_reg_no", "공장 등록번호", "text"],
  ["factory_address", "공장 주소", "textarea"],
  ["due_date", "처리 목표 기한", "date"],
];

const ENTP_EXT_FIELDS = [
  ["office_phone", "회사 전화", "text"],
  ["business_type", "업종", "text"],
  ["total_employee", "총 직원 수", "number"],
  ["employee_count", "직원 수", "number"],
  ["annual_revenue", "연매출", "number"],
  ["outlet_count", "매장 수", "number"],
  ["establishment_date", "설립일", "date"],
  ["corporate_reg_no", "법인등록번호", "text"],
  ["company_name_ko", "기업명(국문)", "text"],
  ["company_name_en", "기업명(영문)", "text"],
  ["responsible_person_en", "책임자(영문)", "text"],
  ["city", "도시", "text"],
  ["country", "국가", "text"],
  ["zip", "우편번호", "text"],
];

async function ENTP_loadCase(cid){
  try {
    const r = await apiFetch('/cases/' + encodeURIComponent(cid));
    if (!r.ok) { RS.entpCase = { error: true, status: r.status }; return; }
    RS.entpCase = await r.json();
  } catch (e) { RS.entpCase = { error: true }; }
}
async function ENTP_loadPen(orgId){
  if (!orgId) { RS.entpPen = { error: true, status: 400 }; return; }
  try {
    const r = await apiFetch('/orgs/' + encodeURIComponent(orgId) + '/penyelia');
    if (!r.ok) { RS.entpPen = { error: true, status: r.status }; return; }
    RS.entpPen = await r.json();
  } catch (e) { RS.entpPen = { error: true }; }
}

const ENTP_ic = (k) => 'entp-' + k + '-' + (Date.now().toString(36));

function ENTP_fieldHtml(f){
  const [k, label, type] = f;
  const v = ENTP_currentVal(k);
  const id = 'entp-f-' + k;
  const val = v == null ? '' : String(v);
  if (type === 'textarea')
    return `<div class="field"><label for="${id}">${esc(label)}</label><textarea id="${id}" class="in" rows="2">${esc(val)}</textarea></div>`;
  return `<div class="field"><label for="${id}">${esc(label)}</label><input id="${id}" class="in" type="${type}" value="${esc(val)}"${type==='number'?' min="0"':''}></div>`;
}

function ENTP_currentVal(k){
  const c = RS.entpCase;
  if (!c || c.error) return '';
  if (k in c) return c[k];
  const pe = c.profile_ext || {};
  return pe[k] != null ? pe[k] : '';
}

function ENTP_collectProfile(){
  const out = {}; const ext = {};
  const base = new Set(ENTP_PROFILE_FIELDS.map(f=>f[0]));
  ENTP_PROFILE_FIELDS.forEach(([k])=>{
    const el = $('#entp-f-' + k); if (!el) return;
    const v = (el.value || '').trim();
    const cur = RS.entpCase ? (RS.entpCase[k] == null ? '' : String(RS.entpCase[k])) : '';
    if (v !== cur) out[k] = v === '' ? null : v;
  });
  ENTP_EXT_FIELDS.forEach(([k,,type])=>{
    const el = $('#entp-f-' + k); if (!el) return;
    const raw = (el.value || '').trim();
    const curPe = (RS.entpCase && RS.entpCase.profile_ext) || {};
    const cur = curPe[k] == null ? '' : String(curPe[k]);
    if (raw === cur) return;
    if (raw === '') { ext[k] = null; return; }
    if (type === 'number') { const n = Number(raw); ext[k] = isNaN(n) ? raw : n; }
    else ext[k] = raw;
  });
  if (Object.keys(ext).length) {
    const pe = Object.assign({}, (RS.entpCase && RS.entpCase.profile_ext) || {});
    Object.keys(ext).forEach(k=>{ if (ext[k] === null) delete pe[k]; else pe[k] = ext[k]; });
    out.profile_ext = pe;
  }
  return out;
}

function ENTP_errHtml(detail){
  if (!detail) return '';
  if (Array.isArray(detail)) {
    return detail.map(e=>{
      const loc = Array.isArray(e.loc) ? e.loc[e.loc.length-1] : '';
      return `<div>${esc(String(loc))}: ${esc(e.msg || '')}</div>`;
    }).join('');
  }
  return `<div>${esc(detail.message || detail.code || String(detail))}</div>`;
}

function ENTP_setMsg(html){
  const el = $('#entp-msg'); if (el) el.innerHTML = html || '';
}

function ENTP_profileHtml(){
  const c = RS.entpCase;
  if (!c) {
    if (RS._entpLoading !== 'case') {
      RS._entpLoading = 'case';
      const cid = (RS.cases && RS.cases[0] && RS.cases[0].case_id) || '';
      ENTP_loadCase(cid).then(()=>{ RS._entpLoading = null; render(); });
    }
    return `<section class="panel"><p class="empty">불러오는 중…</p></section>`;
  }
  if (c.error) {
    return `<section class="panel"><p class="empty">${c.status === 403 ? '이 화면을 볼 권한이 없습니다.' : '케이스를 불러오지 못했습니다.'}</p></section>`;
  }
  const rows = ENTP_PROFILE_FIELDS.map(ENTP_fieldHtml).join('');
  const ext = ENTP_EXT_FIELDS.map(ENTP_fieldHtml).join('');
  return `<section class="panel">
    <div class="panel-head"><h2>기업 프로필(Form.1)</h2></div>
    <div class="inline" style="margin-bottom:10px">
      ${canCall('POST','/cases/{}/profile/autofill')?`<button class="btn btn-sm" onclick="App.entpAutofill()">서류에서 자동 채우기</button>`:''}
      ${canCall('PATCH','/cases/{}/profile')?`<button class="btn btn-sm btn-primary" onclick="App.entpSaveProfile()">프로필 저장</button>`:'<span class="muted">프로필 수정은 신청기업·컨설턴트 계정만 할 수 있습니다.</span>'}
    </div>
    <div id="entp-msg" class="inline-msg"></div>
    <div class="grid-2">${rows}</div>
    <fieldset style="margin-top:14px"><legend>확장 정보 (profile_ext)</legend>
      <div class="grid-3">${ext}</div>
    </fieldset>
  </section>`;
}

function ENTP_penHtml(){
  const p = RS.entpPen;
  if (!p) {
    if (RS._entpLoading !== 'pen') {
      RS._entpLoading = 'pen';
      ENTP_loadPen(AUTH && AUTH.org_id).then(()=>{ RS._entpLoading = null; render(); });
    }
    return `<section class="panel"><p class="empty">불러오는 중…</p></section>`;
  }
  if (p.error) {
    return `<section class="panel"><p class="empty">${p.status === 403 ? '이 조직 정보를 볼 권한이 없습니다.' : '감독자 목록을 불러오지 못했습니다.'}</p></section>`;
  }
  const items = p.items || [];
  const trs = items.map(x=>`<tr>
    <td>${esc(x.name || '')}</td>
    <td>${esc(x.training_cert || '')}</td>
    <td>${esc(x.cert_expiry || '')}</td>
    <td><span class="badge ${x.status === 'active' ? 'strong' : 'attn'}">${esc(x.status || '')}</span></td>
    <td><div class="row-act">
      <button class="btn btn-sm" onclick="App.entpPenEdit('${x.penyelia_id}','${esc(x.status||'')}')">수정</button>
    </div></td>
  </tr>`).join('');
  return `<section class="panel">
    <div class="panel-head"><h2>할랄 감독자(Penyelia Halal)</h2>
      <button class="btn btn-sm btn-primary" onclick="App.entpPenAdd()">감독자 추가</button>
    </div>
    <p class="muted">활성 ${p.active_count || 0}명 / 전체 ${items.length}명</p>
    ${items.length ? `<div class="tbl-wrap"><table>
      <thead><tr><th>이름</th><th>교육 이수</th><th>만료일</th><th>상태</th><th></th></tr></thead>
      <tbody>${trs}</tbody>
    </table></div>` : `<p class="empty">등록된 감독자가 없습니다.</p>`}
  </section>`;
}

function ENTP_section(){
  const open = !!S.entpOpen;
  const head = `<div class="panel-head">
    <h2>BPJPH 제출 정보</h2>
    <button class="btn btn-sm btn-ghost" onclick="App.entpToggle()">${open ? '접기' : '펼치기'}</button>
  </div>`;
  if (!open) return `<section class="panel">${head}<p class="muted">펼치면 기업 프로필과 할랄 감독자를 확인·수정할 수 있습니다.</p></section>`;
  return `<section class="panel">${head}
    <div style="display:flex;flex-direction:column;gap:20px">
      ${ENTP_profileHtml()}
      ${ENTP_penHtml()}
    </div>
  </section>`;
}

const ENTP_f = VIEWS["ent-apply"];
VIEWS["ent-apply"] = () => ENTP_f() + ENTP_section();

App.entpToggle = function(){
  S.entpOpen = !S.entpOpen;
  if (S.entpOpen) { RS.entpCase = null; RS.entpPen = null; }
  render();
};

App.entpSaveProfile = async function(){
  const cid = (RS.cases && RS.cases[0] && RS.cases[0].case_id) || '';
  if (!cid) return toast('대상 케이스가 없습니다.');
  const body = ENTP_collectProfile();
  if (!Object.keys(body).length) { ENTP_setMsg('변경된 항목이 없습니다.'); return; }
  const btn = $('#entp-msg'); ENTP_setMsg('');
  try {
    const r = await apiFetch('/cases/' + encodeURIComponent(cid) + '/profile',
      { method: 'PATCH', body: JSON.stringify(body) });
    if (!r.ok) {
      let d = null; try { d = await r.json(); } catch(e){}
      const det = d && d.detail;
      if (r.status === 403) { ENTP_setMsg('<div>권한이 없습니다.</div>'); return toast('프로필 저장에 실패했습니다. (권한 없음)'); }
      if (r.status === 422) { ENTP_setMsg(ENTP_errHtml(det) || '<div>입력값을 확인하세요.</div>'); return toast('프로필 저장에 실패했습니다. (입력 오류)'); }
      if (r.status === 409) { ENTP_setMsg(ENTP_errHtml(det)); return toast('프로필 저장에 실패했습니다. (' + ((det && det.code) || '충돌') + ')'); }
      ENTP_setMsg(ENTP_errHtml(det));
      return toast('프로필 저장에 실패했습니다.');
    }
    RS.entpCase = null;
    await ENTP_loadCase(cid);
    render();
    toast('프로필을 저장했습니다.');
  } catch(e){ toast('프로필 저장에 실패했습니다.'); }
};

App.entpAutofill = async function(){
  const cid = (RS.cases && RS.cases[0] && RS.cases[0].case_id) || '';
  if (!cid) return toast('대상 케이스가 없습니다.');
  const btn = $('#entp-msg'); ENTP_setMsg('');
  try {
    const r = await apiFetch('/cases/' + encodeURIComponent(cid) + '/profile/autofill',
      { method: 'POST', body: JSON.stringify({}) });
    if (!r.ok) {
      let d = null; try { d = await r.json(); } catch(e){}
      if (r.status === 403) { ENTP_setMsg('<div>권한이 없습니다.</div>'); return toast('자동 채우기에 실패했습니다. (권한 없음)'); }
      ENTP_setMsg(ENTP_errHtml(d && d.detail));
      return toast('자동 채우기에 실패했습니다.');
    }
    const j = await r.json();
    RS.entpCase = null;
    await ENTP_loadCase(cid);
    render();
    toast('서류에서 ' + ((j.applied || []).length) + '개 항목을 채웠습니다.');
  } catch(e){ toast('자동 채우기에 실패했습니다.'); }
};

App.entpPenAdd = function(){
  const id1 = ENTP_ic('name'), id2 = ENTP_ic('cert'), id3 = ENTP_ic('exp');
  openModal('할랄 감독자 추가',
    `<div class="field"><label for="${id1}">이름<span class="req">*</span></label><input id="${id1}" class="in" type="text"></div>
     <div class="field"><label for="${id2}">교육 이수 정보</label><input id="${id2}" class="in" type="text"></div>
     <div class="field"><label for="${id3}">자격 만료일</label><input id="${id3}" class="in" type="date"></div>
     <div id="entp-modal-msg" class="inline-msg"></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button>
     <button class="btn btn-primary" onclick="App.entpPenAddSubmit('${id1}','${id2}','${id3}')">추가</button>`);
};

App.entpPenAddSubmit = async function(id1, id2, id3){
  const name = (($('#'+id1)||{}).value||'').trim();
  const training_cert = (($('#'+id2)||{}).value||'').trim();
  const cert_expiry = (($('#'+id3)||{}).value||'').trim();
  const body = { name };
  if (training_cert) body.training_cert = training_cert;
  if (cert_expiry) body.cert_expiry = cert_expiry;
  const orgId = AUTH && AUTH.org_id;
  if (!orgId) return toast('조직 정보가 없습니다.');
  try {
    const r = await apiFetch('/orgs/' + encodeURIComponent(orgId) + '/penyelia',
      { method: 'POST', body: JSON.stringify(body) });
    if (!r.ok) {
      let d = null; try { d = await r.json(); } catch(e){}
      if (r.status === 403) { toast('추가 권한이 없습니다.'); return; }
      if (r.status === 422) {
        const el = $('#entp-modal-msg'); if (el) el.innerHTML = ENTP_errHtml(d && d.detail) || '입력값을 확인하세요.';
        return toast('감독자 추가에 실패했습니다. (입력 오류)');
      }
      toast('감독자 추가에 실패했습니다.');
      return;
    }
    App.closeModal();
    RS.entpPen = null;
    await ENTP_loadPen(orgId);
    render();
    toast('감독자를 추가했습니다.');
  } catch(e){ toast('감독자 추가에 실패했습니다.'); }
};

App.entpPenEdit = function(pid, curStatus){
  const id1 = ENTP_ic('st');
  openModal('감독자 상태 수정',
    `<div class="field"><label for="${id1}">상태</label>
      <select id="${id1}" class="in">
        <option value="active"${curStatus==='active'?' selected':''}>active</option>
        <option value="inactive"${curStatus==='inactive'?' selected':''}>inactive</option>
      </select></div>
     <div id="entp-modal-msg" class="inline-msg"></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button>
     <button class="btn btn-primary" onclick="App.entpPenEditSubmit('${pid}','${id1}')">저장</button>`);
};

App.entpPenEditSubmit = async function(pid, id1){
  const status = (($('#'+id1)||{}).value||'').trim();
  const orgId = AUTH && AUTH.org_id;
  if (!orgId) return toast('조직 정보가 없습니다.');
  try {
    const r = await apiFetch('/orgs/' + encodeURIComponent(orgId) + '/penyelia/' + encodeURIComponent(pid),
      { method: 'PATCH', body: JSON.stringify({ status }) });
    if (!r.ok) {
      let d = null; try { d = await r.json(); } catch(e){}
      if (r.status === 403) { toast('수정 권한이 없습니다.'); return; }
      if (r.status === 422) {
        const el = $('#entp-modal-msg');
        if (el) el.innerHTML = ENTP_errHtml(d && d.detail) || '상태값이 올바르지 않습니다.';
        return toast('감독자 수정에 실패했습니다. (입력 오류)');
      }
      toast('감독자 수정에 실패했습니다.');
      return;
    }
    App.closeModal();
    RS.entpPen = null;
    await ENTP_loadPen(orgId);
    render();
    toast('감독자 상태를 저장했습니다.');
  } catch(e){ toast('감독자 수정에 실패했습니다.'); }
};
