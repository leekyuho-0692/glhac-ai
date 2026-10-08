/* ===== 데모 빠른 로그인 ===== */
const DL_ACCOUNTS = {enterprise:['applicant1','pw'], consultant:['consultant1','pw'], auditor:['auditor1','pw'], sharia:['fatwa1','pw'], admin:['admin','admin']};
S.dlDemo = false;
function DL_find(id){ return (DL_ACCOUNTS[id]||DL_ACCOUNTS.enterprise); }
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
  const hintHtml = `데모 모드 — 역할을 고르면 시연 계정이 자동 입력됩니다 (${esc(acc[0])} / ${esc(acc[1])}). <button type="button" class="btn btn-sm btn-primary" onclick="App.dlQuick()">데모 계정으로 바로 로그인</button>`;
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
        if(!S.role && document.querySelector('#lg-email') && typeof render === 'function') render();
      }).catch(()=>{ S.dlDemo = false; });
    }catch(e){ S.dlDemo = false; }
  };
})();
if(typeof API_BASE !== 'undefined') DL_origFetchDemo();
