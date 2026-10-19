/* Signup picker keeps the underlying form intact while the dialog is open. */
let signupConsultantResults = [];
let signupConsultantSearchVersion = 0;
App.signupConsultantFind = function() {
  signupConsultantResults = [];
  signupConsultantSearchVersion++;
  openModal('컨설턴트 찾기', `<form onsubmit="event.preventDefault();App.signupConsultantSearch()"><div class="field"><label for="signup-cons-query">이름 또는 아이디</label><div class="row-act"><input class="in" id="signup-cons-query" minlength="2" maxlength="80" required placeholder="두 글자 이상 입력하세요"><button class="btn btn-primary" type="submit">검색</button></div></div></form><div id="signup-cons-results" aria-live="polite">이름 또는 아이디를 입력해 검색하세요.</div>`, `<button class="btn" onclick="App.closeModal()">닫기</button>`);
  const input = $('#signup-cons-query'); if(input) input.focus();
};
App.signupConsultantSearch = async function() {
  const query = (($('#signup-cons-query') || {}).value || '').trim();
  const box = $('#signup-cons-results');
  if(!box) return;
  if(query.length < 2) { box.textContent = '두 글자 이상 입력하세요.'; return; }
  const version = ++signupConsultantSearchVersion;
  box.textContent = '검색 중…';
  try {
    const r = await fetch(API_BASE + '/auth/consultants/search?q=' + encodeURIComponent(query));
    if(version !== signupConsultantSearchVersion || $('#signup-cons-results') !== box) return;
    if(!r.ok) { box.textContent = '검색에 실패했습니다. 다시 시도해 주세요.'; return; }
    signupConsultantResults = (await r.json()).items || [];
    box.innerHTML = signupConsultantResults.length ? `<ul class="rows">${signupConsultantResults.map((c,i) => `<li class="row"><div><b>${esc(c.display_name)}</b><div class="muted">${esc(c.username)}</div></div><button type="button" class="btn btn-sm" onclick="App.signupConsultantChoose(${i})">선택</button></li>`).join('')}</ul><p class="muted">최대 20명 표시 · 결과가 많으면 이름이나 아이디를 더 입력하세요.</p>` : '검색 결과가 없습니다.';
  } catch(e) { if(version===signupConsultantSearchVersion) box.textContent = '검색에 실패했습니다. 다시 시도해 주세요.'; }
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
  const field = $('#es-consultant'); if(field) field.value = '선택하지 않음';
};
App.signupConsultantQr = async function(code) {
  try {
    const r = await fetch(API_BASE + '/invites/' + encodeURIComponent(code) + '/check');
    if(!r.ok) return;
    const d = await r.json(), field = $('#es-consultant');
    if(field && S.urlRef===code) {
      S.qrConsultant = d.valid && d.kind==='consultant' ? {consultant_id:d.consultant_id, username:d.username, display_name:d.consultant} : null;
      field.value = d.valid ? (d.consultant ? d.consultant + (d.username ? ' (' + d.username + ')' : '') : '공식 가입 초대 · 담당 컨설턴트 미지정') : '유효하지 않은 초대 코드 · 담당자 확인 필요';
    }
  } catch(e) {}
};
// 첫 가입 화면은 이 모듈보다 먼저 렌더링되므로 로드 직후에도 QR 담당자를 조회한다.
if(S.urlRef) App.signupConsultantQr(S.urlRef);
