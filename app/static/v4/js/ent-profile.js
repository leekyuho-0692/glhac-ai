/* ===== BPJPH 제출 정보 (ent-apply 확장) ===== */
RS.entpCase = RS.entpCase || null;
RS.entpPen = RS.entpPen || null;
S.entpOpen = S.entpOpen !== false;
// 이 탭에서만 보관. 계정/신청별로 분리하고 서버 저장 성공 전에는 지우지 않는다.
const ENTP_DRAFTS = new Map();
const ENTP_PRODUCTS = new Map();
const ENTP_key = cid => [AUTH.org_id, AUTH.username, cid].join(':');
const ENTP_cid = () => ((RS.cases || [])[0] || {}).case_id || '';
function ENTP_draft(cid=ENTP_cid()) { return ENTP_DRAFTS.get(ENTP_key(cid)) || {}; }
function ENTP_dirty(){ return [...ENTP_DRAFTS.values()].some(d=>Object.keys(d).length) || [...ENTP_PRODUCTS.values()].some(d=>Object.values(d).some(Boolean)) || Object.values((RS.ENTF||{}).org||{}).some(d=>d && d._draft); }
document.addEventListener('input', e => {
  const el=e.target;
  if(S.view==='ent-apply' && ['pr-name','pr-cat'].includes(el.id)){
    const key=ENTP_key(ENTP_cid()); ENTP_PRODUCTS.set(key,{...(ENTP_PRODUCTS.get(key)||{}),[el.id]:el.value}); return;
  }
  const panel=el.closest && el.closest('[data-entp-case]');
  if(!panel || !el.id.startsWith('entp-f-')) return;
  const cid=panel.dataset.entpCase, key=ENTP_key(cid), values={...ENTP_draft(cid)};
  const field=el.id.slice(7), c=RS.entpCase || {};
  const baseline=field in c ? c[field] : (c.profile_ext || {})[field];
  if(el.value === String(baseline ?? '')) delete values[field]; else values[field]=el.value;
  ENTP_DRAFTS.set(key,values);
  const status=$('#entp-save-state');
  if(status) status.textContent=Object.keys(values).length ? t('profile.dirty') : t('profile.saved');
});
window.addEventListener('beforeunload', e => { if(ENTP_dirty()){ e.preventDefault(); e.returnValue=''; } });
{ const logout=App.logout; App.logout=function(){
  if(ENTP_dirty() && !confirm(t('profile.discard'))) return;
  ENTP_DRAFTS.clear(); ENTP_PRODUCTS.clear(); if(RS.ENTF) RS.ENTF.org={}; RS.entpCase=null; RS.entpPen=null; return logout.apply(this,arguments);
}; }

const ENTP_PROFILE_FIELDS = [
  ["company_name", "signup.company_name", "text"],
  ["nib", "profile.field.nib", "text"],
  ["responsible_person", "profile.field.responsible_person", "text"],
  ["halal_supervisor", "role.penyelia_halal", "text"],
  ["email", "ui.text_037", "email"],
  ["phone", "profile.field.phone", "text"],
  ["address", "profile.field.address", "textarea"],
  ["factory_reg_no", "profile.field.factory_reg_no", "text"],
  ["factory_address", "profile.field.factory_address", "textarea"],
  ["due_date", "profile.field.due_date", "date"],
];

const ENTP_EXT_FIELDS = [
  ["office_phone", "profile.field.office_phone", "text"],
  ["business_type", "profile.field.business_type", "text"],
  ["total_employee", "profile.field.total_employee", "number"],
  ["employee_count", "profile.field.employee_count", "number"],
  ["annual_revenue", "profile.field.annual_revenue", "number"],
  ["outlet_count", "profile.field.outlet_count", "number"],
  ["establishment_date", "profile.field.establishment_date", "date"],
  ["corporate_reg_no", "profile.field.corporate_reg_no", "text"],
  ["company_name_ko", "ui.text_104", "text"],
  ["company_name_en", "ui.text_105", "text"],
  ["responsible_person_en", "profile.field.responsible_person_en", "text"],
  ["city", "profile.field.city", "text"],
  ["country", "profile.field.country", "text"],
  ["zip", "profile.field.zip", "text"],
];

async function ENTP_loadCase(cid){
  if(!cid) return;
  let data;
  try {
    const r=await apiFetch('/cases/'+encodeURIComponent(cid));
    data=r.ok ? await r.json() : {case_id:cid,error:true,status:r.status};
  } catch(e){ data={case_id:cid,error:true}; }
  if(ENTP_cid()===cid) RS.entpCase=data;
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
    return `<div class="field"><label for="${id}">${esc(t(label))}</label><textarea id="${id}" class="in" rows="2">${esc(val)}</textarea></div>`;
  return `<div class="field"><label for="${id}">${esc(t(label))}</label><input id="${id}" class="in" type="${type}" value="${esc(val)}"${type==='number'?' min="0"':''}></div>`;
}

function ENTP_currentVal(k){
  const draft=ENTP_draft();
  if(Object.prototype.hasOwnProperty.call(draft,k)) return draft[k];
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
    const el = $('#entp-f-' + k), draft=ENTP_draft();
    if(!el && !(k in draft)) return;
    const v = String(k in draft ? draft[k] : el.value || '').trim();
    const cur = RS.entpCase ? (RS.entpCase[k] == null ? '' : String(RS.entpCase[k])) : '';
    if (v !== cur) out[k] = v === '' ? null : v;
  });
  ENTP_EXT_FIELDS.forEach(([k,,type])=>{
    const el = $('#entp-f-' + k), draft=ENTP_draft();
    if(!el && !(k in draft)) return;
    const raw = String(k in draft ? draft[k] : el.value || '').trim();
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
  if(!ENTP_cid()) return '';
  const c = RS.entpCase;
  if (!c || c.case_id!==ENTP_cid()) {
    if (RS._entpLoading !== 'case') {
      RS._entpLoading = 'case';
      const cid = (RS.cases && RS.cases[0] && RS.cases[0].case_id) || '';
      ENTP_loadCase(cid).then(()=>{ RS._entpLoading = null; render(); });
    }
    return `<section class="panel"><p class="empty">${t('members.loading')}</p></section>`;
  }
  if (c.error) {
    return `<section class="panel"><p class="empty">${c.status === 403 ? t('upload.text_038') : t('profile.case_failed')}</p></section>`;
  }
  const rows = ENTP_PROFILE_FIELDS.map(ENTP_fieldHtml).join('');
  const ext = ENTP_EXT_FIELDS.map(ENTP_fieldHtml).join('');
  return `<section class="panel" data-entp-case="${esc(ENTP_cid())}">
    <div class="panel-head"><h2>${t('profile.basic')}</h2><span id="entp-save-state" role="status">${Object.keys(ENTP_draft()).length ? t('profile.dirty') : t('profile.saved')}</span></div>
    <div class="inline" style="margin-bottom:10px">
      ${canCall('POST','/cases/{}/profile/autofill')?`<button class="btn btn-sm" onclick="App.entpAutofill()">${t('profile.autofill')}</button>`:''}
      ${canCall('PATCH','/cases/{}/profile')?`<button class="btn btn-sm btn-primary" onclick="App.entpSaveProfile()">${t('profile.save')}</button>`:`<span class="muted">${t('profile.edit_hint')}</span>`}
    </div>
    <div id="entp-msg" class="inline-msg"></div>
    <div class="grid-2">${rows}</div>
    <details style="margin-top:14px"><summary>${t('profile.extra')}</summary>
      <div class="grid-3">${ext}</div>
    </details>
  </section>`;
}

function ENTP_penHtml(){
  const p = RS.entpPen;
  if (!p) {
    if (RS._entpLoading !== 'pen') {
      RS._entpLoading = 'pen';
      ENTP_loadPen(AUTH && AUTH.org_id).then(()=>{ RS._entpLoading = null; render(); });
    }
    return `<section class="panel"><p class="empty">${t('members.loading')}</p></section>`;
  }
  if (p.error) {
    return `<section class="panel"><p class="empty">${p.status === 403 ? t('profile.org_forbidden') : t('profile.supervisors_failed')}</p></section>`;
  }
  const items = p.items || [];
  const trs = items.map(x=>`<tr>
    <td>${esc(x.name || '')}</td>
    <td>${esc(x.training_cert || '')}</td>
    <td>${esc(x.cert_expiry || '')}</td>
    <td><span class="badge ${x.status === 'active' ? 'strong' : 'attn'}">${esc(x.status || '')}</span></td>
    <td><div class="row-act">
      <button class="btn btn-sm" onclick="App.entpPenEdit('${x.penyelia_id}','${jsq(x.status||'')}')">${t('profile.edit')}</button>
    </div></td>
  </tr>`).join('');
  return `<section class="panel">
    <div class="panel-head"><h2>${t('profile.supervisors')}</h2>
      <button class="btn btn-sm btn-primary" onclick="App.entpPenAdd()">${t('profile.add_supervisor')}</button>
    </div>
    <p class="muted">${esc(tf('profile.supervisor_count',{active:p.active_count||0,total:items.length}))}</p>
    ${items.length ? `<div class="tbl-wrap"><table>
      <thead><tr><th>${t('profile.name')}</th><th>${t('profile.training')}</th><th>${t('profile.expiry')}</th><th>${t('upload.text_013')}</th><th></th></tr></thead>
      <tbody>${trs}</tbody>
    </table></div>` : `<p class="empty">${t('profile.no_supervisors')}</p>`}
  </section>`;
}

function ENTP_section(){
  if(!ENTP_cid()) return '';
  const open = !!S.entpOpen;
  const head = `<div class="panel-head">
    <h2>${t('profile.contacts')}</h2>
    <button class="btn btn-sm btn-ghost" onclick="App.entpToggle()">${open ? t('profile.collapse') : t('profile.expand')}</button>
  </div>`;
  if (!open) return `<section class="panel">${head}<p class="muted">${t('profile.expand_hint')}</p></section>`;
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

async function ENTP_saveProfile({quiet=false}={}){
  const cid=ENTP_cid();
  if(!cid){ toast(t('profile.no_case')); return false; }
  if(!RS.entpCase || RS.entpCase.error || RS.entpCase.case_id !== cid) await ENTP_loadCase(cid);
  if(ENTP_cid()!==cid) return false;
  if(!RS.entpCase || RS.entpCase.error){ toast(t('profile.load_failed')); return false; }
  const snapshot={...ENTP_draft(cid)}, key=ENTP_key(cid), body=ENTP_collectProfile();
  if(!Object.keys(body).length){ if(!quiet) ENTP_setMsg(t('profile.unchanged')); return true; }
  try {
    const r=await apiFetch('/cases/'+encodeURIComponent(cid)+'/profile', {method:'PATCH',body:JSON.stringify(body)});
    if(!r.ok){
      const d=await r.json().catch(()=>({}));
      ENTP_setMsg(ENTP_errHtml(d.detail) || t('profile.check_access'));
      toast(t('profile.save_failed'));
      return false;
    }
    const remaining={...(ENTP_DRAFTS.get(key)||{})};
    Object.keys(snapshot).forEach(k=>{ if(remaining[k]===snapshot[k]) delete remaining[k]; });
    ENTP_DRAFTS.set(key,remaining);
    if(ENTP_cid()===cid){
      await ENTP_loadCase(cid);
      const listed=(RS.cases||[]).find(c=>c.case_id===cid);
      if(listed && RS.entpCase && !RS.entpCase.error) listed.company_name=RS.entpCase.company_name;
      if(!quiet){ render(); toast(t('profile.saved_notice')); }
    }
    return true;
  } catch(e){ toast(t('profile.save_failed')); return false; }
}
App.entpSaveProfile = () => ENTP_saveProfile();

App.entpAutofill = async function(){
  const cid = (RS.cases && RS.cases[0] && RS.cases[0].case_id) || '';
  if (!cid) return toast(t('profile.no_case'));
  const btn = $('#entp-msg'); ENTP_setMsg('');
  try {
    const r = await apiFetch('/cases/' + encodeURIComponent(cid) + '/profile/autofill',
      { method: 'POST', body: JSON.stringify({}) });
    if (!r.ok) {
      let d = null; try { d = await r.json(); } catch(e){}
      if (r.status === 403) { ENTP_setMsg(`<div>${t('profile.forbidden')}</div>`); return toast(t('profile.autofill_forbidden')); }
      ENTP_setMsg(ENTP_errHtml(d && d.detail));
      return toast(t('profile.autofill_failed'));
    }
    const j = await r.json();
    RS.entpCase = null;
    await ENTP_loadCase(cid);
    render();
    toast(tf('profile.autofilled',{count:(j.applied||[]).length}));
  } catch(e){ toast(t('profile.autofill_failed')); }
};

App.entpPenAdd = function(){
  const id1 = ENTP_ic('name'), id2 = ENTP_ic('cert'), id3 = ENTP_ic('exp');
  openModal(t('profile.add_supervisor_title'),
    `<div class="field"><label for="${id1}">${t('profile.name')}<span class="req">*</span></label><input id="${id1}" class="in" type="text"></div>
     <div class="field"><label for="${id2}">${t('profile.training_info')}</label><input id="${id2}" class="in" type="text"></div>
     <div class="field"><label for="${id3}">${t('profile.qualification_expiry')}</label><input id="${id3}" class="in" type="date"></div>
     <div id="entp-modal-msg" class="inline-msg"></div>`,
    `<button class="btn" onclick="App.closeModal()">${t('upload.text_020')}</button>
     <button class="btn btn-primary" onclick="App.entpPenAddSubmit('${id1}','${id2}','${id3}')">${t('profile.add')}</button>`);
};

App.entpPenAddSubmit = async function(id1, id2, id3){
  const name = (($('#'+id1)||{}).value||'').trim();
  const training_cert = (($('#'+id2)||{}).value||'').trim();
  const cert_expiry = (($('#'+id3)||{}).value||'').trim();
  const body = { name };
  if (training_cert) body.training_cert = training_cert;
  if (cert_expiry) body.cert_expiry = cert_expiry;
  const orgId = AUTH && AUTH.org_id;
  if (!orgId) return toast(t('profile.no_org'));
  try {
    const r = await apiFetch('/orgs/' + encodeURIComponent(orgId) + '/penyelia',
      { method: 'POST', body: JSON.stringify(body) });
    if (!r.ok) {
      let d = null; try { d = await r.json(); } catch(e){}
      if (r.status === 403) { toast(t('profile.add_forbidden')); return; }
      if (r.status === 422) {
        const el = $('#entp-modal-msg'); if (el) el.innerHTML = ENTP_errHtml(d && d.detail) || t('profile.check_input');
        return toast(t('profile.supervisor_invalid'));
      }
      toast(t('profile.supervisor_add_failed'));
      return;
    }
    App.closeModal();
    RS.entpPen = null;
    await ENTP_loadPen(orgId);
    render();
    toast(t('profile.supervisor_added'));
  } catch(e){ toast(t('profile.supervisor_add_failed')); }
};

App.entpPenEdit = function(pid, curStatus){
  const id1 = ENTP_ic('st');
  openModal(t('profile.edit_supervisor'),
    `<div class="field"><label for="${id1}">${t('upload.text_013')}</label>
      <select id="${id1}" class="in">
        <option value="active"${curStatus==='active'?' selected':''}>active</option>
        <option value="inactive"${curStatus==='inactive'?' selected':''}>inactive</option>
      </select></div>
     <div id="entp-modal-msg" class="inline-msg"></div>`,
    `<button class="btn" onclick="App.closeModal()">${t('upload.text_020')}</button>
     <button class="btn btn-primary" onclick="App.entpPenEditSubmit('${pid}','${id1}')">${t('ui.text_011')}</button>`);
};

App.entpPenEditSubmit = async function(pid, id1){
  const status = (($('#'+id1)||{}).value||'').trim();
  const orgId = AUTH && AUTH.org_id;
  if (!orgId) return toast(t('profile.no_org'));
  try {
    const r = await apiFetch('/orgs/' + encodeURIComponent(orgId) + '/penyelia/' + encodeURIComponent(pid),
      { method: 'PATCH', body: JSON.stringify({ status }) });
    if (!r.ok) {
      let d = null; try { d = await r.json(); } catch(e){}
      if (r.status === 403) { toast(t('profile.edit_forbidden')); return; }
      if (r.status === 422) {
        const el = $('#entp-modal-msg');
        if (el) el.innerHTML = ENTP_errHtml(d && d.detail) || t('profile.invalid_status');
        return toast(t('profile.supervisor_edit_invalid'));
      }
      toast(t('profile.supervisor_edit_failed'));
      return;
    }
    App.closeModal();
    RS.entpPen = null;
    await ENTP_loadPen(orgId);
    render();
    toast(t('profile.supervisor_saved'));
  } catch(e){ toast(t('profile.supervisor_edit_failed')); }
};
