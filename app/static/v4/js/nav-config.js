/* ===== v4 메뉴 설정(구 화면 메뉴 설정 이식 — 역할별·개인별 숨김/순서) ===== */
NAV.adm.push('adm-menus');
Object.assign(I18N.ko, {'nav.adm-menus':'메뉴 설정'});
if(I18N.en) Object.assign(I18N.en, {'nav.adm-menus':'Menu settings'});
if(I18N.id) Object.assign(I18N.id, {'nav.adm-menus':'Pengaturan menu'});

const NVC_DEFAULT = {};
Object.keys(NAV).forEach(r => NVC_DEFAULT[r] = NAV[r].slice());

const NVC_ROLES = [['ent','인증기업'],['cons','컨설턴트'],['aud','오디터'],['sha','샤리아'],['adm','관리자'],['pen','할랄 동반자']];

const NVC_locked = (r) => {
  const rk = Object.keys(ROLES).find(k => ROLES[k].code === r);
  const home = rk ? ROLES[rk].home : null;
  const arr = [];
  if(home) arr.push(home);
  if(r === 'adm') arr.push('adm-menus');
  return arr;
};

const NVC_label = (v) => {
  const k = 'nav.' + v;
  const s = (typeof t === 'function') ? t(k) : k;
  return (s && s !== k) ? s : v;
};

const NVC_orderList = (base, ord, keepHidden) => {
  const o = ord || [];
  const inOrd = o.filter(v => base.includes(v));
  const rest = base.filter(v => !inOrd.includes(v));
  return inOrd.concat(rest);
};

const NVC_effective = (r, cfg) => {
  const base = (NVC_DEFAULT[r] || []).slice();
  const locked = NVC_locked(r);
  const rh = (cfg && cfg.role && cfg.role.hidden) || [];
  const uh = (cfg && cfg.user && cfg.user.hidden) || [];
  const list = base.filter(v => locked.includes(v) || (!rh.includes(v) && !uh.includes(v)));
  const uo = (cfg && cfg.user && cfg.user.order) || [];
  const ro = (cfg && cfg.role && cfg.role.order) || [];
  const ord = (uo && uo.length) ? uo : ro;
  return NVC_orderList(list, ord, false);
};

const NVC_apply = () => {
  const r = S.role;
  if(!r || !NAV[r] || !RS.nvc) return;
  // 권한(rbac canView)으로 숨겨진 화면은 메뉴 설정이 되살리지 않는다
  const eff = NVC_effective(r, RS.nvc).filter(v => typeof canView !== 'function' || canView(v));
  NAV[r].splice(0, NAV[r].length, ...eff);
};

const NVC_load = async () => {
  try{
    const res = await apiFetch('/me/v4-nav');
    RS.nvc = res.ok ? await res.json() : {role:{hidden:[],order:[]}, user:{hidden:[],order:[]}};
  }catch(e){
    RS.nvc = {role:{hidden:[],order:[]}, user:{hidden:[],order:[]}};
  }
  RS.nvcFor = S.role;
  NVC_apply();
  render();
};

RS.nvc = RS.nvc || null;
RS.nvcAdm = RS.nvcAdm || null;
S.nvcAdmDraft = S.nvcAdmDraft || {};

SHELL_EXT.sideFoot.push(() => {
  if(S.role && RS.nvcFor !== S.role && !RS._nvcLoading){
    RS._nvcLoading = true;
    NVC_load().finally(() => { RS._nvcLoading = false; });
  }
  return `<a class="link" style="font-size:12px" href="javascript:void(0)" onclick="App.nvcMine()">내 메뉴 설정</a>`;
});

/* --- 개인 설정 모달 --- */
const NVC_mineBody = () => {
  const r = S.role;
  const locked = NVC_locked(r);
  const d = S.nvcDraft;
  const len = d.list.length;
  const rows = d.list.map((v, i) => {
    const isHidden = d.hidden.includes(v);
    const isLocked = locked.includes(v);
    return `<div class="row"><label><input type="checkbox" ${isHidden?'':'checked'} ${isLocked?'disabled':''} onchange="App.nvcToggle(${i},this.checked)"> ${esc(NVC_label(v))}</label> <span class="row-act"><button class="btn btn-sm btn-ghost" onclick="App.nvcMove(${i},-1)" ${i===0?'disabled':''}>▲</button><button class="btn btn-sm btn-ghost" onclick="App.nvcMove(${i},1)" ${i===len-1?'disabled':''}>▼</button></span></div>`;
  }).join('');
  return `<div id="nvc-body"><p class="muted" style="font-size:12px">체크를 끄면 내 사이드바에서 숨겨집니다. 홈은 숨길 수 없습니다. 관리자가 숨긴 메뉴는 여기 나오지 않습니다.</p>${rows}</div>`;
};

App.nvcMine = function(){
  const r = S.role;
  const cfg = RS.nvc || {role:{hidden:[],order:[]}, user:{hidden:[],order:[]}};
  const roleHidden = (cfg.role && cfg.role.hidden) || [];
  const candidates = (NVC_DEFAULT[r] || []).filter(v => !roleHidden.includes(v));
  const cur = (NAV[r] || []).slice();
  const list = cur.concat(candidates.filter(v => !cur.includes(v)));
  S.nvcDraft = { list: list, hidden: ((cfg.user && cfg.user.hidden) || []).slice() };
  const body = NVC_mineBody();
  const foot = `<button class="btn" onclick="App.nvcMineReset()">기본값으로</button><button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.nvcMineSave()">저장</button>`;
  openModal('내 메뉴 설정', body, foot, false);
};

App.nvcToggle = function(i, on){
  if(!S.nvcDraft) return;
  const v = S.nvcDraft.list[i];
  if(!v) return;
  if(on){
    S.nvcDraft.hidden = S.nvcDraft.hidden.filter(x => x !== v);
  }else{
    if(!S.nvcDraft.hidden.includes(v)) S.nvcDraft.hidden.push(v);
  }
};

App.nvcMove = function(i, d){
  if(!S.nvcDraft) return;
  const list = S.nvcDraft.list;
  const j = i + d;
  if(j < 0 || j >= list.length) return;
  const tmp = list[i]; list[i] = list[j]; list[j] = tmp;
  App.nvcMine();
};

App.nvcMineSave = async function(){
  if(!S.nvcDraft) return;
  const payload = { hidden: S.nvcDraft.hidden, order: S.nvcDraft.list };
  try{
    const r = await apiFetch('/me/v4-nav', {method:'PUT', body:JSON.stringify(payload)});
    if(!r.ok){
      let msg = '저장에 실패했습니다.';
      try{ const d = await r.json(); if(d && d.detail && d.detail.code) msg += ` (${d.detail.code})`; }catch(e){}
      return toast(msg);
    }
  }catch(e){ return toast('저장에 실패했습니다.'); }
  closeModal();
  RS.nvcFor = null;
  toast('메뉴 설정을 저장했습니다.');
  render();
};

App.nvcMineReset = async function(){
  if(!confirm('내 메뉴를 기본값으로 되돌릴까요?')) return;
  try{
    const r = await apiFetch('/me/v4-nav/reset', {method:'POST'});
    if(!r.ok) return toast('기본값으로 되돌리지 못했습니다.');
  }catch(e){ return toast('기본값으로 되돌리지 못했습니다.'); }
  closeModal();
  RS.nvcFor = null;
  toast('기본 메뉴로 되돌렸습니다.');
  render();
};

/* --- 관리자 화면 --- */
const NVC_admDraft = (r) => {
  if(S.nvcAdmDraft[r]) return S.nvcAdmDraft[r];
  const saved = (RS.nvcAdm && RS.nvcAdm.roles && RS.nvcAdm.roles[r]) || {hidden:[], order:[]};
  const base = (NVC_DEFAULT[r] || []).slice();
  const list = NVC_orderList(base, saved.order || [], false);
  S.nvcAdmDraft[r] = { list: list, hidden: (saved.hidden || []).slice() };
  return S.nvcAdmDraft[r];
};

const NVC_admBody = (r) => {
  const locked = NVC_locked(r);
  const d = S.nvcAdmDraft[r];
  const len = d.list.length;
  const rows = d.list.map((v, i) => {
    const isHidden = d.hidden.includes(v);
    const isLocked = locked.includes(v);
    return `<div class="row"><label><input type="checkbox" ${isHidden?'':'checked'} ${isLocked?'disabled':''} onchange="App.nvcAdmToggle(${i},this.checked)"> ${esc(NVC_label(v))}</label> <span class="row-act"><button class="btn btn-sm btn-ghost" onclick="App.nvcAdmMove(${i},-1)" ${i===0?'disabled':''}>▲</button><button class="btn btn-sm btn-ghost" onclick="App.nvcAdmMove(${i},1)" ${i===len-1?'disabled':''}>▼</button></span></div>`;
  }).join('');
  return rows;
};

VIEWS['adm-menus'] = () => {
  if(!RS.nvcAdm){
    if(RS._nvcAdmLoading !== true){
      RS._nvcAdmLoading = true;
      (async () => {
        try{
          const res = await apiFetch('/admin/v4-nav');
          RS.nvcAdm = res.ok ? await res.json() : {error:true, status:res.status};
        }catch(e){ RS.nvcAdm = {error:true}; }
        RS._nvcAdmLoading = false;
        render();
      })();
    }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(RS.nvcAdm.error){
    return `<section class="panel"><p class="empty">${RS.nvcAdm.status===403?'메뉴 설정은 관리자·최고운영자만 할 수 있습니다.':'불러오지 못했습니다.'}</p></section>`;
  }
  const cur = S.nvcRole || 'ent';
  const chips = `<div class="chipbar">${NVC_ROLES.map(([k,l]) => `<button class="chip ${cur===k?'on':''}" onclick="App.nvcRoleTab('${k}')">${esc(l)}</button>`).join('')}</div>`;
  const label = (NVC_ROLES.find(x => x[0] === cur) || [cur,cur])[1];
  const draft = NVC_admDraft(cur);
  const saved = (RS.nvcAdm.roles && RS.nvcAdm.roles[cur]) || {};
  const updated = saved.updated_at ? `<p class="muted">마지막 저장 ${esc(String(saved.updated_at).slice(0,16).replace('T',' '))}</p>` : '';
  const body = `<section class="panel">
    <div class="panel-head"><h2>${esc(label)} 메뉴</h2></div>
    <p class="muted" style="font-size:12px">이 역할의 모든 사용자 사이드바에 적용됩니다. 메뉴를 숨겨도 권한은 바뀌지 않습니다(접근 권한은 서버가 따로 지킵니다). 홈·메뉴 설정은 숨길 수 없습니다.</p>
    ${NVC_admBody(cur)}
    ${updated}
    <div class="row-act" style="margin-top:12px">
      <button class="btn btn-primary" onclick="App.nvcAdmSave()">저장</button>
      <button class="btn" onclick="App.nvcAdmReset()">기본값으로</button>
    </div>
  </section>`;
  return chips + body;
};

App.nvcRoleTab = function(k){
  S.nvcRole = k;
  render();
};

App.nvcAdmToggle = function(i, on){
  const r = S.nvcRole || 'ent';
  const d = S.nvcAdmDraft[r];
  if(!d) return;
  const v = d.list[i];
  if(!v) return;
  if(on){
    d.hidden = d.hidden.filter(x => x !== v);
  }else{
    if(!d.hidden.includes(v)) d.hidden.push(v);
  }
};

App.nvcAdmMove = function(i, d2){
  const r = S.nvcRole || 'ent';
  const d = S.nvcAdmDraft[r];
  if(!d) return;
  const j = i + d2;
  if(j < 0 || j >= d.list.length) return;
  const tmp = d.list[i]; d.list[i] = d.list[j]; d.list[j] = tmp;
  render();
};

App.nvcAdmSave = async function(){
  const r = S.nvcRole || 'ent';
  const d = S.nvcAdmDraft[r];
  if(!d) return;
  const payload = { hidden: d.hidden, order: d.list };
  try{
    const res = await apiFetch('/admin/v4-nav/' + r, {method:'PUT', body:JSON.stringify(payload)});
    if(!res.ok){
      if(res.status === 403) return toast('권한이 없습니다.');
      let msg = '저장에 실패했습니다.';
      try{ const j = await res.json(); if(j && j.detail && j.detail.code) msg += ` (${j.detail.code})`; }catch(e){}
      return toast(msg);
    }
  }catch(e){ return toast('저장에 실패했습니다.'); }
  RS.nvcAdm = null;
  delete S.nvcAdmDraft[r];
  RS.nvcFor = null;
  toast('역할 메뉴를 저장했습니다.');
  render();
};

App.nvcAdmReset = async function(){
  const r = S.nvcRole || 'ent';
  if(!confirm('이 역할 메뉴를 기본값으로 되돌릴까요?')) return;
  try{
    const res = await apiFetch('/admin/v4-nav/' + r, {method:'PUT', body:JSON.stringify({hidden:[], order:[]})});
    if(!res.ok){
      if(res.status === 403) return toast('권한이 없습니다.');
      return toast('기본값으로 되돌리지 못했습니다.');
    }
  }catch(e){ return toast('기본값으로 되돌리지 못했습니다.'); }
  RS.nvcAdm = null;
  delete S.nvcAdmDraft[r];
  RS.nvcFor = null;
  toast('기본 메뉴로 되돌렸습니다.');
  render();
};