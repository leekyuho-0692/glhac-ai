/* ===== 원재료 정리 도구(구 화면 이식) ===== */
RS.mtlLast = RS.mtlLast || {};
RS.mtlCat = RS.mtlCat || [];

const MTL_ROLES = ['applicant', 'consultant'];

const MTL_panel = (cid) => {
  if (!cid) return '';
  if (!AUTH || MTL_ROLES.indexOf(AUTH.role) < 0) return '';
  const last = RS.mtlLast[cid];
  return `<section class="panel">
    <div class="panel-head"><h2>원재료 정리 도구</h2></div>
    <p class="muted" style="font-size:12px">중복 정리·다른 신청 건의 원재료 재사용·제품 라벨 사진으로 원재료 자동 등록</p>
    <div class="row-act">
      <button class="btn" onclick="App.mtlDedup('${cid}')">중복 정리</button>
      <button class="btn" onclick="App.mtlReuseOpen('${cid}')">기존 원재료 재사용</button>
      <button class="btn" onclick="App.mtlLabel('${cid}')">라벨 사진으로 등록</button>
    </div>
    ${last ? `<div class="note">${esc(last)}</div>` : ''}
  </section>`;
};

const MTL_after = (cid) => {
  if (RS.mats) delete RS.mats[cid];
  if (RS.pkg) delete RS.pkg[cid];
  render();
};

const MTL_err = async (r, what) => {
  if (r.status === 403) { toast('권한이 없습니다.'); return; }
  let code = '';
  try { const j = await r.json(); if (j && j.detail && typeof j.detail === 'object' && j.detail.code) code = j.detail.code; } catch (e) {}
  toast(`${what}에 실패했습니다.${code ? ' (' + code + ')' : ''}`);
};

const MTL_rows = (q) => {
  const cat = RS.mtlCat || [];
  const list = cat
    .map((m, i) => ({ m, i }))
    .filter(({ m }) => !q || (m.name || '').toLowerCase().indexOf(q) >= 0);
  if (!list.length) return '<p class="empty">해당하는 원재료가 없습니다.</p>';
  return list.map(({ m, i }) => `<label class="row" style="cursor:pointer">
    <input type="checkbox" class="mtl-chk" value="${i}">
    <b>${esc(m.name)}</b>
    <span class="muted">${esc(m.supplier || '-')} · ${esc(m.screen_status || '미판정')}</span>${m.evidence_provided ? ' <span class="badge strong">증빙 보유</span>' : ''}${m.cert_no ? ` <span class="badge">인증 ${esc(m.cert_no)}</span>` : ''}
  </label>`).join('');
};

App.mtlDedup = async function (cid) {
  if (!confirm('이름이 같은(괄호·공백·원산지 표기만 다른) 원재료를 하나만 남기고 정리합니다. 증빙이 붙은 항목은 지우지 않습니다. 진행할까요?')) return;
  try {
    const r = await apiFetch(`/cases/${cid}/materials/dedup`, { method: 'POST', body: JSON.stringify({}) });
    if (!r.ok) return MTL_err(r, '중복 정리');
    const d = await r.json();
    const msg = `중복 정리: ${d.removed}건 삭제 · ${d.remaining}건 남음`;
    RS.mtlLast[cid] = msg;
    MTL_after(cid);
    toast(msg);
  } catch (e) { toast('네트워크 오류가 발생했습니다.'); }
};

App.mtlReuseOpen = async function (cid) {
  try {
    const r = await apiFetch('/orgs/materials/catalog?limit=100');
    if (!r.ok) return MTL_err(r, '기존 원재료 불러오기');
    const d = await r.json();
    RS.mtlCat = (d && d.items) || [];
    if (!RS.mtlCat.length) { toast('재사용할 기존 원재료가 없습니다.'); return; }
    const body = `<p class="muted">같은 회사의 다른 신청 건에 등록된 원재료를 복사합니다. 이미 있는 원재료는 건너뛰고, 증빙은 이 신청 건에서 다시 확인합니다.</p>
      <div class="field"><input class="in" id="mtl-q" placeholder="원재료명 검색" oninput="App.mtlFilter()"></div>
      <div id="mtl-list" style="max-height:50vh;overflow:auto">${MTL_rows('')}</div>`;
    const foot = `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.mtlReuseDo('${cid}')">선택 항목 추가</button>`;
    openModal('기존 원재료 재사용', body, foot, true);
  } catch (e) { toast('네트워크 오류가 발생했습니다.'); }
};

App.mtlFilter = function () {
  const el = $('#mtl-list'); if (!el) return;
  const q = (($('#mtl-q') || {}).value || '').trim().toLowerCase();
  el.innerHTML = MTL_rows(q);
};

App.mtlReuseDo = async function (cid) {
  const chks = Array.from(document.querySelectorAll('.mtl-chk')).filter((c) => c.checked);
  const names = chks.map((c) => { const i = parseInt(c.value, 10); const m = (RS.mtlCat || [])[i]; return m ? m.name : null; }).filter(Boolean);
  if (!names.length) { toast('항목을 선택해 주세요.'); return; }
  try {
    const r = await apiFetch(`/cases/${cid}/materials/reuse`, { method: 'POST', body: JSON.stringify({ names }) });
    if (!r.ok) return MTL_err(r, '원재료 재사용');
    const d = await r.json();
    closeModal();
    const msg = `재사용: ${d.added_count}건 추가` + ((d.skipped && d.skipped.length) ? ` · 중복 제외 ${d.skipped.length}건` : '');
    RS.mtlLast[cid] = msg;
    MTL_after(cid);
    toast(msg);
  } catch (e) { toast('네트워크 오류가 발생했습니다.'); }
};

App.mtlLabel = function (cid) {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = 'image/*';
  inp.style.display = 'none';
  inp.onchange = () => {
    const f = inp.files && inp.files[0];
    if (f) App.mtlLabelSend(cid, f);
    inp.remove();
  };
  document.body.appendChild(inp);
  inp.click();
};

App.mtlLabelSend = function (cid, file) {
  toast('라벨을 읽는 중입니다… (최대 1분)');
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const dataUrl = String(reader.result || '');
      const b64 = dataUrl.indexOf(',') >= 0 ? dataUrl.slice(dataUrl.indexOf(',') + 1) : '';
      if (!b64) { toast('이미지를 읽지 못했습니다.'); return; }
      const r = await apiFetch(`/cases/${cid}/materials/from-label-b64`, { method: 'POST', body: JSON.stringify({ image_b64: b64, filename: file.name }) });
      if (r.status === 422) { toast('라벨에서 원재료를 읽지 못했습니다. 더 선명한 사진으로 다시 시도해 주세요.'); return; }
      if (!r.ok) return MTL_err(r, '라벨 인식');
      const d = await r.json();
      const msg = `라벨 인식: 원재료 ${d.count}건 등록` + (d.critical_count ? ` · 주의 원재료 ${d.critical_count}건` : '');
      RS.mtlLast[cid] = msg;
      MTL_after(cid);
      toast(msg);
    } catch (e) { toast('네트워크 오류가 발생했습니다.'); }
  };
  reader.onerror = () => toast('이미지를 읽지 못했습니다.');
  reader.readAsDataURL(file);
};

const MTL_pickFirst = () => ((RS.cases || [])[0] || {}).case_id;
const MTL_pickCompany = () => S.selCompany;

const MTL_wrap = (viewId, pick) => {
  const f = VIEWS[viewId];
  if (typeof f === 'function') VIEWS[viewId] = () => f() + MTL_panel(pick());
};

MTL_wrap('ent-home', MTL_pickFirst);
MTL_wrap('ent-apply', MTL_pickFirst);
MTL_wrap('cons-status', MTL_pickCompany);
