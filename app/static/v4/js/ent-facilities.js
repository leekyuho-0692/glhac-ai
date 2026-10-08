/* ===== 인증기업 공장·시설 + 할랄 관리 조직 ===== */
const ENTF_S8 = (c) => (typeof _s8label === 'function' ? _s8label(c) : '');

const ENTF_FIELDS = ['name', 'address', 'city', 'country', 'zip', 'reg_no'];
const ENTF_PEF = ['label', 'phone', 'email', 'pic_name', 'pic_title', 'cp_name', 'cp_title', 'manufacturer_name'];
const ENTF_MEMBER_ROLES = ['coordinator', 'liaison', 'member'];
const ENTF_DIVISIONS = ['management', 'production', 'qc', 'sanitation', 'warehouse', 'purchasing', 'hr', 'other'];

async function ENTF_req(path, opts, cacheKey) {
  try {
    const r = await apiFetch(path, opts || {});
    if (!r.ok) {
      let code = '';
      try { const j = await r.json(); code = (j && j.detail && j.detail.code) || ''; } catch (e) {}
      return { error: true, status: r.status, code };
    }
    const data = await r.json().catch(() => ({}));
    if (cacheKey) RS.ENTF[cacheKey] = data;
    return { ok: true, data };
  } catch (e) {
    return { error: true };
  }
}

async function ENTF_loadFac(cid) {
  RS.ENTF.fac = RS.ENTF.fac || {};
  const res = await ENTF_req('/cases/' + cid + '/factories', null, null);
  RS.ENTF.fac[cid] = res.error ? { error: true, status: res.status } : (res.data.factories || []);
}

async function ENTF_loadOrg(cid) {
  RS.ENTF.org = RS.ENTF.org || {};
  const res = await ENTF_req('/cases/' + cid + '/halal-org', null, null);
  RS.ENTF.org[cid] = res.error ? { error: true, status: res.status } : res.data;
}

function ENTF_errBox(d) {
  if (d && d.status === 403) return '<p class="empty">이 패널을 볼 권한이 없습니다.</p>';
  return '<p class="empty">불러오지 못했습니다.</p>';
}

function ENTF_facTable(cid, list) {
  if (!list.length) return '<p class="empty">등록된 공장이 없습니다.</p>';
  const rows = list.map((f, i) => `<tr>
    <td>${esc(f.label || f.name || '공장')}</td>
    <td>${esc(f.name || '')}</td>
    <td>${esc([f.address, f.city, f.country, f.zip].filter(Boolean).join(' · '))}</td>
    <td class="mono">${esc(f.reg_no || '')}</td>
    <td><div class="row-act">
      <button class="btn btn-sm" onclick="App.entfFacEdit(${i})">수정</button>
      <button class="btn btn-sm btn-ghost" onclick="App.entfFacDel(${i})">삭제</button>
      <button class="btn btn-sm btn-ghost" onclick="App.entfFacPdf('${esc(cid)}','${esc(f.facility_id)}')">공장 프로필 PDF</button>
    </div></td>
  </tr>`).join('');
  return `<div class="tbl-wrap"><table><thead><tr><th>라벨</th><th>이름</th><th>주소</th><th>등록번호</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function ENTF_facPanel(cid) {
  RS.ENTF = RS.ENTF || {};
  RS.ENTF.fac = RS.ENTF.fac || {};
  const d = RS.ENTF.fac[cid];
  let body;
  if (d === undefined) {
    if (RS.ENTF._facLoading !== cid) { RS.ENTF._facLoading = cid; ENTF_loadFac(cid).then(() => { RS.ENTF._facLoading = null; render(); }); }
    body = '<p class="empty">불러오는 중…</p>';
  } else if (d.error) { body = ENTF_errBox(d); }
  else { body = ENTF_facTable(cid, d); }
  return `<section class="panel">
    <div class="panel-head"><h2>공장·시설</h2>
      <button class="btn btn-sm btn-primary" onclick="App.entfFacAdd()">공장 추가</button></div>
    ${body}
  </section>`;
}

function ENTF_orgMembersTable(rows, editable) {
  const list = rows || [];
  const body = list.map((m, i) => `<tr>
    <td><input class="in" data-org-member="${i}" data-k="name" value="${esc(m.name || '')}"></td>
    <td><input class="in" data-org-member="${i}" data-k="title" value="${esc(m.title || '')}"></td>
    <td><select class="in" data-org-member="${i}" data-k="division">${
      ['', ...ENTF_DIVISIONS].map(v => `<option value="${v}" ${((m.division || '') === v) ? 'selected' : ''}>${esc(v || '—')}</option>`).join('')
    }</select></td>
    <td><select class="in" data-org-member="${i}" data-k="role">${
      ENTF_MEMBER_ROLES.map(v => `<option value="${v}" ${((m.role || 'member') === v) ? 'selected' : ''}>${esc(v)}</option>`).join('')
    }</select></td>
    <td>${editable ? `<button class="btn btn-sm btn-ghost" onclick="App.entfOrgDelMember(${i})">삭제</button>` : ''}</td>
  </tr>`).join('');
  return `<div class="tbl-wrap"><table><thead><tr><th>이름</th><th>직함</th><th>부서</th><th>역할</th><th></th></tr></thead><tbody>${
    body || '<tr><td colspan="5" class="muted">없음</td></tr>'
  }</tbody></table></div>`;
}

function ENTF_orgPanel(cid) {
  RS.ENTF = RS.ENTF || {};
  RS.ENTF.org = RS.ENTF.org || {};
  const d = RS.ENTF.org[cid];
  let body;
  if (d === undefined) {
    if (RS.ENTF._orgLoading !== cid) { RS.ENTF._orgLoading = cid; ENTF_loadOrg(cid).then(() => { RS.ENTF._orgLoading = null; render(); }); }
    body = '<p class="empty">불러오는 중…</p>';
  } else if (d.error) { body = ENTF_errBox(d); }
  else {
    const top = d.top_mgmt || {};
    const peny = d.penyelia || [];
    const mem = d.members || [];
    body = `
      <fieldset><legend>대표자 (Top Management)</legend>
        <div class="dl"><div><dt>이름</dt><dd>${esc(top.name || '')}</dd></div>
        <div><dt>직함</dt><dd>${esc(top.title || '')}</dd></div></div>
      </fieldset>
      <fieldset><legend>Penyelia Halal 자격</legend>
        ${peny.length ? `<div class="tbl-wrap"><table><thead><tr><th>이름</th><th>무슬림</th><th>SK</th><th>교육증명</th></tr></thead><tbody>${
          peny.map((p, i) => `<tr><td>${esc(p.name || '')}</td>
            <td><input type="checkbox" data-org-pq="${i}" data-k="is_muslim" ${p.is_muslim ? 'checked' : ''}></td>
            <td><input type="checkbox" data-org-pq="${i}" data-k="sk" ${p.sk ? 'checked' : ''}></td>
            <td><input type="checkbox" data-org-pq="${i}" data-k="training_cert" ${p.training_cert ? 'checked' : ''}></td>
          </tr>`).join('')
        }</tbody></table></div>` : '<p class="muted">Penyelia 정보가 없습니다.</p>'}
      </fieldset>
      <fieldset><legend>부서 대표 (members)</legend>
        ${ENTF_orgMembersTable(mem, true)}
        <div style="margin-top:8px"><button class="btn btn-sm" onclick="App.entfOrgAddMember()">행 추가</button></div>
      </fieldset>
      ${canCall('PUT','/cases/{}/halal-org')?`<div style="margin-top:10px"><button class="btn btn-primary" onclick="App.entfOrgSave('${esc(cid)}')">저장</button></div>`:''}
    `;
  }
  return `<section class="panel"><div class="panel-head"><h2>할랄 관리 조직</h2></div>${body}</section>`;
}

(function ENTF_wrap(){
  const prev = VIEWS['ent-apply'];
  VIEWS['ent-apply'] = () => {
    const base = (typeof prev === 'function') ? prev() : '';
    const cid = (RS.cases && RS.cases[0] && RS.cases[0].case_id) || null;
    if (!cid) return base + '<section class="panel"><p class="empty">케이스 정보가 없습니다.</p></section>';
    return base + ENTF_facPanel(cid) + ENTF_orgPanel(cid);
  };
})();

function ENTF_curCid() { return (RS.cases && RS.cases[0] && RS.cases[0].case_id) || null; }

App.entfFacAdd = function(){
  const fields = ENTF_FIELDS.map(k => `<div class="field"><label>${k}</label><input class="in" id="entf-f-${k}"></div>`).join('');
  const peFields = ENTF_PEF.map(k => `<div class="field"><label>${k}</label><input class="in" id="entf-pe-${k}"></div>`).join('');
  openModal('공장 추가', `<div class="grid-2">${fields}</div><fieldset><legend>추가 정보</legend><div class="grid-2">${peFields}</div></fieldset>`,
    '<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.entfFacAddSave()">저장</button>');
};

App.entfFacAddSave = async function(){
  const cid = ENTF_curCid(); if (!cid) return;
  const body = {}, pe = {};
  ENTF_FIELDS.forEach(k => { const v = (($('#entf-f-' + k) || {}).value || '').trim(); if (v) body[k] = v; });
  ENTF_PEF.forEach(k => { const v = (($('#entf-pe-' + k) || {}).value || '').trim(); if (v) pe[k] = v; });
  if (Object.keys(pe).length) body.profile_ext = pe;
  const res = await ENTF_req('/cases/' + cid + '/factories', { method: 'POST', body: JSON.stringify(body) }, null);
  if (res.error) { toast('공장 추가에 실패했습니다.' + (res.code ? ' (' + res.code + ')' : '')); return; }
  App.closeModal();
  delete RS.ENTF.fac[cid];
  await ENTF_loadFac(cid);
  render();
  toast('공장을 추가했습니다.');
};

App.entfFacEdit = function(idx){
  const cid = ENTF_curCid(); if (!cid) return;
  const list = RS.ENTF.fac[cid] || [];
  const f = list[idx]; if (!f) return;
  const fields = ENTF_FIELDS.map(k => `<div class="field"><label>${k}</label><input class="in" id="entf-f-${k}" value="${esc(f[k] || '')}"></div>`).join('');
  const pe = f.profile_ext || {};
  const peFields = ENTF_PEF.map(k => `<div class="field"><label>${k}</label><input class="in" id="entf-pe-${k}" value="${esc(pe[k] || '')}"></div>`).join('');
  openModal('공장 수정', `<div class="grid-2">${fields}</div><fieldset><legend>추가 정보</legend><div class="grid-2">${peFields}</div></fieldset>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.entfFacEditSave('${esc(f.facility_id)}')">저장</button>`);
};

App.entfFacEditSave = async function(fid){
  const cid = ENTF_curCid(); if (!cid) return;
  const body = {}, pe = {};
  ENTF_FIELDS.forEach(k => { const el = $('#entf-f-' + k); if (el) body[k] = (el.value || '').trim(); });
  ENTF_PEF.forEach(k => { const el = $('#entf-pe-' + k); if (el) pe[k] = (el.value || '').trim(); });
  body.profile_ext = pe;
  const res = await ENTF_req('/facilities/' + fid, { method: 'PATCH', body: JSON.stringify(body) }, null);
  if (res.error) { toast('수정에 실패했습니다.' + (res.code ? ' (' + res.code + ')' : '')); return; }
  App.closeModal();
  delete RS.ENTF.fac[cid];
  await ENTF_loadFac(cid);
  render();
  toast('공장 정보를 저장했습니다.');
};

App.entfFacDel = function(idx){
  const cid = ENTF_curCid(); if (!cid) return;
  const list = RS.ENTF.fac[cid] || [];
  const f = list[idx]; if (!f) return;
  openModal('공장 삭제', `<p>케이스에서 <b>${esc(f.label || f.name || '공장')}</b> 연결을 해제합니다. 다른 케이스에서도 사용 중이면 레코드는 유지됩니다.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.entfFacDelDo('${esc(f.facility_id)}')">삭제</button>`);
};

App.entfFacDelDo = async function(fid){
  const cid = ENTF_curCid(); if (!cid) return;
  const res = await ENTF_req('/cases/' + cid + '/factories/' + fid, { method: 'DELETE' }, null);
  if (res.error) { toast('삭제에 실패했습니다.' + (res.code ? ' (' + res.code + ')' : '')); return; }
  App.closeModal();
  delete RS.ENTF.fac[cid];
  await ENTF_loadFac(cid);
  render();
  toast(res.data && res.data.record_deleted ? '공장을 삭제했습니다.' : '케이스에서 연결을 해제했습니다.');
};

App.entfFacPdf = function(cid, fid){
  if (!cid || !fid) return;
  App.entfDownload('/cases/' + encodeURIComponent(cid) + '/facilities/' + encodeURIComponent(fid) + '/docs/factory-profile.pdf', 'factory-profile-' + fid + '.pdf', '아직 생성되지 않았습니다.');
};

App.entfDownload = async function(path, filename, failMsg){
  try {
    const r = await apiFetch(path);
    if (!r.ok) { toast(failMsg || '파일을 불러올 수 없습니다.'); return; }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  } catch (e) { toast(failMsg || '파일을 불러올 수 없습니다.'); }
};

App.entfOrgAddMember = function(){
  const cid = ENTF_curCid(); if (!cid) return;
  const d = RS.ENTF.org[cid]; if (!d || d.error) return;
  if (!d._draft) d._draft = JSON.parse(JSON.stringify(d));
  d._draft.members = d._draft.members || [];
  d._draft.members.push({ name: '', title: '', division: '', role: 'member' });
  render();
};

App.entfOrgDelMember = function(i){
  const cid = ENTF_curCid(); if (!cid) return;
  const d = RS.ENTF.org[cid]; if (!d) return;
  if (!d._draft) d._draft = JSON.parse(JSON.stringify(d));
  d._draft.members = d._draft.members || [];
  d._draft.members.splice(i, 1);
  render();
};

App.entfOrgSave = async function(cid){
  const d = RS.ENTF.org[cid]; if (!d || d.error) return;
  const src = d._draft || d;
  const members = {};
  document.querySelectorAll('[data-org-member]').forEach(el => {
    const i = +el.getAttribute('data-org-member'); const k = el.getAttribute('data-k');
    members[i] = members[i] || {}; members[i][k] = el.value;
  });
  const memberList = Object.keys(members).sort((a, b) => a - b).map(i => members[i])
    .filter(m => (m.name || '').trim());
  const quals = {};
  document.querySelectorAll('[data-org-pq]').forEach(el => {
    const i = +el.getAttribute('data-org-pq'); const k = el.getAttribute('data-k');
    const name = (src.penyelia && src.penyelia[i] && src.penyelia[i].name) || '';
    if (!name) return;
    quals[name] = quals[name] || { is_muslim: false, sk: false, training_cert: false };
    quals[name][k] = !!el.checked;
  });
  const body = { members: memberList };
  if (Object.keys(quals).length) body.penyelia_quals = quals;
  const res = await ENTF_req('/cases/' + cid + '/halal-org', { method: 'PUT', body: JSON.stringify(body) }, null);
  if (res.error) { toast('저장에 실패했습니다.' + (res.code ? ' (' + res.code + ')' : '')); return; }
  RS.ENTF.org[cid] = res.data;
  render();
  toast('할랄 관리 조직을 저장했습니다.');
};
