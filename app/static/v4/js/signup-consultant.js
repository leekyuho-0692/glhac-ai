/* Signup picker keeps the underlying form intact while the dialog is open. */
let signupConsultantResults = [];
let signupConsultantSearchVersion = 0;
App.signupConsultantFind = function() {
  signupConsultantResults = [];
  signupConsultantSearchVersion++;
  openModal(t('signup.find_consultant'), `<form onsubmit="event.preventDefault();App.signupConsultantSearch()"><div class="field"><label for="signup-cons-query">${t('signup.query')}</label><div class="row-act"><input class="in" id="signup-cons-query" minlength="2" maxlength="80" required placeholder="${t('signup.query_hint')}"><button class="btn btn-primary" type="submit">${t('ui.text_001')}</button></div></div></form><div id="signup-cons-results" aria-live="polite">${t('signup.search_prompt')}</div>`, `<button class="btn" onclick="App.closeModal()">${t('ui.text_002')}</button>`);
  const input = $('#signup-cons-query'); if(input) input.focus();
};
App.signupConsultantSearch = async function() {
  const query = (($('#signup-cons-query') || {}).value || '').trim();
  const box = $('#signup-cons-results');
  if(!box) return;
  if(query.length < 2) { box.textContent = t('signup.query_required'); return; }
  const version = ++signupConsultantSearchVersion;
  box.textContent = t('signup.searching');
  try {
    const r = await fetch(API_BASE + '/auth/consultants/search?q=' + encodeURIComponent(query));
    if(version !== signupConsultantSearchVersion || $('#signup-cons-results') !== box) return;
    if(!r.ok) { box.textContent = t('signup.search_error'); return; }
    signupConsultantResults = (await r.json()).items || [];
    box.innerHTML = signupConsultantResults.length ? `<ul class="rows">${signupConsultantResults.map((c,i) => `<li class="row"><div><b>${esc(c.display_name)}</b><div class="muted">${esc(c.username)}</div></div><button type="button" class="btn btn-sm" onclick="App.signupConsultantChoose(${i})">${t('upload.text_008')}</button></li>`).join('')}</ul><p class="muted">${t('signup.search_limit')}</p>` : t('signup.no_results');
  } catch(e) { if(version===signupConsultantSearchVersion) box.textContent = t('signup.search_error'); }
};
App.signupConsultantChoose = function(index) {
  if(S.urlRef || !signupConsultantResults[index]) return;
  S.signupConsultant = signupConsultantResults[index];
  const field = $('#es-consultant');
  if(field) field.value = S.signupConsultant.display_name + ' (' + S.signupConsultant.username + ')';
  signupConsultantSearchVersion++; App.closeModal();
};
App.signupConsultantClear = function() {
  if(S.urlRef) return;
  S.signupConsultant = null;
  const field = $('#es-consultant'); if(field) field.value = t('signup.no_consultant');
};
App.signupConsultantQr = async function(code) {
  try {
    const r = await fetch(API_BASE + '/invites/' + encodeURIComponent(code) + '/check');
    if(!r.ok) return;
    const d = await r.json(), field = $('#es-consultant');
    if(field && S.urlRef===code) {
      S.qrConsultant = d.valid && d.kind==='consultant' ? {consultant_id:d.consultant_id, username:d.username, display_name:d.consultant} : null;
      field.value = d.valid ? (d.consultant ? d.consultant + (d.username ? ' (' + d.username + ')' : '') : t('signup.official_invite')) : t('signup.invalid_invite');
    }
  } catch(e) {}
};
// 첫 가입 화면은 이 모듈보다 먼저 렌더링되므로 로드 직후에도 QR 담당자를 조회한다.
if(S.urlRef) App.signupConsultantQr(S.urlRef);
