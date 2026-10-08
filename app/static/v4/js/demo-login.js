/* ===== 데모 빠른 로그인 ===== */
const DL_ACCOUNTS = {enterprise:['applicant1','pw'], consultant:['consultant1','pw'], auditor:['auditor1','pw'], sharia:['fatwa1','pw'], admin:['admin','admin']};
S.dlDemo = false;
S.dlAccounts = null;
function DL_find(id){
  const a = S.dlAccounts;
  if(a){
    if(id==='enterprise'){
      const list = a.enterprise||[];
      if(list.length){ const sel = (S.dlEnt && list.find(e=>e.username===S.dlEnt)) ? S.dlEnt : list[0].username; S.dlEnt = sel; return [sel, 'pw']; }
    } else if(a.staff && a.staff[id]){
      return [a.staff[id], id==='admin' ? 'admin' : 'pw'];
    }
  }
  return (DL_ACCOUNTS[id]||DL_ACCOUNTS.enterprise);
}
function DL_isDemoValue(id, v){
  const acc = DL_find(id);
  return !v || v===acc[0] || v===acc[1] || Object.keys(DL_ACCOUNTS).some(k=>v===DL_ACCOUNTS[k][0]||v===DL_ACCOUNTS[k][1]);
}
function DL_apply(){
  if(!S.dlDemo) return;
  const em = $('#lg-email'), pw = $('#lg-pw');
  if(!em || !pw) return;
  const id = S.loginRole || 'enterprise';
  const acc = DL_find(id);
  if(DL_isDemoValue(id, (em.value||'').trim())) em.value = acc[0];
  if(DL_isDemoValue(id, (pw.value||'').trim())) pw.value = acc[1];
  const form = document.querySelector('form.login-card');
  if(!form) return;
  let hintHtml;
  if(id==='enterprise' && S.dlAccounts && Array.isArray(S.dlAccounts.enterprise) && S.dlAccounts.enterprise.length){
    const opts = S.dlAccounts.enterprise.map(e=>`<option value="${esc(e.username)}" ${e.username===S.dlEnt?'selected':''}>${esc(e.company)} · ${e.scheme==='logistics'?'물류':'제품'} · ${esc(e.step_label||'')} (${esc(e.username)})</option>`).join('');
    hintHtml = `<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">`
      + `<select id="dl-ent" class="in" onchange="App.dlPickEnt(this.value)">${opts}</select>`
      + `<button type="button" class="btn btn-sm btn-primary" onclick="App.dlQuick()">데모 계정으로 바로 로그인</button></div>`
      + `<div class="muted" style="margin-top:6px;font-size:12px">실제 신청 업체 계정입니다 — 선택하면 자동 입력</div>`;
  } else {
    hintHtml = `데모 모드 — 역할을 고르면 시연 계정이 자동 입력됩니다 (${esc(acc[0])} / ${esc(acc[1])}). <button type="button" class="btn btn-sm btn-primary" onclick="App.dlQuick()">데모 계정으로 바로 로그인</button>`;
  }
  let hint = document.getElementById('dl-hint');
  if(!hint){
    hint = document.createElement('div');
    hint.id = 'dl-hint';
    hint.className = 'note';
    hint.style.marginTop = '8px';
    const sub = form.querySelector('button[type=submit]') || form.querySelector('.btn-primary') || form.lastElementChild;
    if(sub && sub.parentNode) sub.parentNode.insertBefore(hint, sub.nextSibling);
    else form.appendChild(hint);
  }
  hint.innerHTML = hintHtml;
}
const DL_origRender = (typeof render === 'function') ? render : null;
if(DL_origRender){
  render = function(){ const r = DL_origRender.apply(this, arguments); try{ DL_apply(); }catch(e){} return r; };
}
if(typeof App.loginRole === 'function'){
  const DL_origLoginRole = App.loginRole;
  App.loginRole = function(){ const r = DL_origLoginRole.apply(this, arguments); try{ DL_apply(); }catch(e){} return r; };
}
App.dlPickEnt = function(u){
  S.dlEnt = u;
  const em = $('#lg-email'), pw = $('#lg-pw');
  if(em) em.value = u;
  if(pw) pw.value = 'pw';
};
App.dlQuick = function(){
  const id = S.loginRole || 'enterprise';
  const acc = DL_find(id);
  const em = $('#lg-email'), pw = $('#lg-pw');
  if(em) em.value = acc[0];
  if(pw) pw.value = acc[1];
  const form = document.querySelector('form.login-card');
  if(form && form.requestSubmit) form.requestSubmit();
};
DL_origFetchDemo = (function(){
  return function(){
    try{
      fetch(API_BASE + '/health').then(r=>r.ok?r.json():null).then(j=>{
        S.dlDemo = !!(j && j.demo === true);
        if(!S.dlDemo){ if(!S.role && document.querySelector('#lg-email') && typeof render === 'function') render(); return; }
        fetch(API_BASE + '/auth/demo-accounts').then(r=>r.ok?r.json():null).then(a=>{
          S.dlAccounts = a || null;
          if(!S.role && document.querySelector('#lg-email') && typeof render === 'function') render();
        }).catch(()=>{ S.dlAccounts = null; if(!S.role && document.querySelector('#lg-email') && typeof render === 'function') render(); });
      }).catch(()=>{ S.dlDemo = false; });
    }catch(e){ S.dlDemo = false; }
  };
})();
if(typeof API_BASE !== 'undefined') DL_origFetchDemo();
