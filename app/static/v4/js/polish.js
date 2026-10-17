/* ===== 모듈 polish: 메뉴 정리·불가 판정·회원 정지 ===== */
(function(){
  const PL_ENT_ORDER = ['ent-home','ent-apply','ent-pre','ent-formal','ent-contract','ent-prep','ent-audit','ent-flow','ent-docs','ent-consultant'];
  if (typeof NAV !== 'undefined' && NAV.ent) {
    const set = new Set(NAV.ent);
    const head = PL_ENT_ORDER.filter(id => set.has(id));
    const tail = NAV.ent.filter(id => !PL_ENT_ORDER.includes(id));
    NAV.ent = head.concat(tail);
  }
  const PL_KO = {'nav.ent-apply':'① AI 사전심사 신청','nav.ent-pre':'② 사전심사 결과','nav.ent-contract':'④ 계약','nav.ent-flow':'인증 문서함','nav.ent-docs':'서류 보관함'};
  const PL_EN = {'nav.ent-apply':'① AI Pre-assessment','nav.ent-pre':'② Pre-assessment Result','nav.ent-contract':'④ Contract','nav.ent-flow':'Document Flow','nav.ent-docs':'Document Vault'};
  const PL_ID = {'nav.ent-apply':'① Pengajuan Pra-penilaian AI','nav.ent-pre':'② Hasil Pra-penilaian','nav.ent-contract':'④ Kontrak','nav.ent-flow':'Arsip Dokumen','nav.ent-docs':'Penyimpanan Dokumen'};
  if (typeof I18N !== 'undefined' && I18N.ko) Object.assign(I18N.ko, PL_KO);
  if (typeof I18N !== 'undefined' && I18N.en) Object.assign(I18N.en, PL_EN);
  if (typeof I18N !== 'undefined' && I18N.id) Object.assign(I18N.id, PL_ID);
})();

/* B. 사전심사 불가 판정 */
var PL_origPreReview = App.preReview;
App.preReview = function(cid){
  const r = PL_origPreReview.apply(this, arguments);
  setTimeout(() => {
    const note = $('#pr-note');
    if (!note) return;
    const modal = note.closest('.modal');
    if (!modal) return;
    const foot = modal.querySelector('.modal-foot') || modal.querySelector('.modal-body');
    if (!foot || foot.querySelector('[data-pl-reject]')) return;
    const b = document.createElement('button');
    b.className = 'btn btn-ghost';
    b.setAttribute('data-pl-reject','1');
    b.textContent = '불가';
    b.onclick = () => App.plReject(cid);
    foot.appendChild(b);
  }, 0);
  return r;
};

App.plReject = async function(cid){
  const noteEl = $('#pr-note');
  const note = ((noteEl || {}).value || '').trim();
  if (!note) {
    const msg = noteEl && noteEl.closest('.modal') && noteEl.closest('.modal').querySelector('.inline-msg');
    if (msg) msg.textContent = '불가 사유를 입력해 주세요';
    else toast('불가 사유를 입력해 주세요');
    return;
  }
  try {
    const r = await apiFetch('/cases/' + cid + '/preassess/review', {method:'POST', body: JSON.stringify({
      verdict: 'reject',
      sections: {documents:{ok:false,note:''}, materials:{ok:false,note:''}, process:{ok:false,note:''}},
      note: note
    })});
    if (!r.ok) {
      const detail = await r.json().catch(()=>({}));
      const code = detail && detail.detail && detail.detail.code ? ' (' + detail.detail.code + ')' : '';
      toast('판정에 실패했습니다.' + code);
      return;
    }
    const data = await r.json().catch(()=>({}));
    if (data && data.status === 'pending') {
      closeModal();
      toast('서브 오디터 확인 대기');
      return;
    }
    closeModal();
    if (RS.pre) delete RS.pre[cid];
    await loadAud();
    render();
    toast('불가로 판정했습니다 — 기업에 사유가 전달됩니다');
  } catch(e) {
    toast('판정에 실패했습니다.');
  }
};

/* C. 회원 정지·해제 */
RS.plUsers = RS.plUsers;
async function PL_loadUsers(){
  try {
    const r = await apiFetch('/admin/users');
    if(r.ok){ const j = await r.json(); RS.plUsers = Array.isArray(j) ? {items:j} : (j && j.items ? j : {items:[]}); } else RS.plUsers = {error:true, status:r.status};
  } catch(e) { RS.plUsers = {error:true}; }
}

const PL_wrapMembers = () => {
  const orig = VIEWS['adm-members'];
  VIEWS['adm-members'] = () => (RS.admMembers === undefined ? '<section class="panel"><p class="empty">불러오는 중…</p></section>' : orig() + PL_membersPanel());
};
if (typeof VIEWS !== 'undefined' && VIEWS['adm-members']) PL_wrapMembers();
else if (typeof VIEWS !== 'undefined') VIEWS['adm-members'] = () => PL_membersPanel();

function PL_membersPanel(){
  let body;
  const d = RS.plUsers;
  if (!d) { if (RS._plUsersLoading !== true) { RS._plUsersLoading = true; PL_loadUsers().then(()=>{ RS._plUsersLoading = false; render(); }); } body = '<p class="empty">불러오는 중…</p>'; }
  else if (d.error) { body = '<p class="empty">' + (d.status === 403 ? '이 화면을 볼 권한이 없습니다.' : '불러오지 못했습니다.') + '</p>'; }
  else {
    const items = (d.items || []);
    const myName = (typeof AUTH !== 'undefined' && AUTH) ? AUTH.username : '';
    if (!items.length) body = '<p class="empty">회원이 없습니다.</p>';
    else body = '<div class="tbl-wrap"><table><thead><tr><th>사용자</th><th>역할</th><th>상태</th><th class="num"></th></tr></thead><tbody>' +
      items.map((u, i) => {
        const self = esc(u.username) === esc(myName);
        const st = u.suspended ? '<span class="badge attn">정지</span>' : '<span class="badge strong">활성</span>';
        const btn = self ? '<span class="muted">본인</span>' : (u.suspended
          ? '<button class="btn btn-sm" onclick="App.plUnsuspend(' + i + ')">해제</button>'
          : '<button class="btn btn-sm btn-ghost" onclick="App.plSuspend(' + i + ')">정지</button>');
        const role = (typeof ROLE_LABEL !== 'undefined' && ROLE_LABEL[u.role]) ? ROLE_LABEL[u.role] : (u.role || '');
        return '<tr><td class="mono">' + esc(u.username) + '</td><td>' + esc(role) + '</td><td>' + st + '</td><td class="num">' + btn + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  }
  return '<section class="panel"><div class="panel-head"><h2>회원 정지·해제</h2></div>' + body + '</section>';
}

App.plSuspend = function(i){
  const items = (RS.plUsers && RS.plUsers.items) || [];
  const u = items[i]; if (!u) return;
  openModal('회원 정지', '<div class="field"><label>사유</label><textarea class="in" id="pl-reason"></textarea></div><div class="inline-msg"></div>',
    '<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.plSuspendDo(' + i + ')">정지</button>');
};
App.plSuspendDo = async function(i){
  const items = (RS.plUsers && RS.plUsers.items) || [];
  const u = items[i]; if (!u) return;
  const reason = ((($('#pl-reason') || {}).value) || '').trim();
  if (!reason) { const m = $('.inline-msg'); if (m) m.textContent = '사유를 입력해 주세요'; return; }
  try {
    const r = await apiFetch('/admin/users/' + u.user_id + '/suspend', {method:'POST', body: JSON.stringify({reason})});
    if (!r.ok) {
      if (r.status === 403) { toast('권한이 없습니다'); return; }
      const dd = await r.json().catch(()=>({}));
      const code = dd && dd.detail && dd.detail.code ? ' (' + dd.detail.code + ')' : '';
      toast('정지에 실패했습니다.' + code); return;
    }
    closeModal(); RS.plUsers = undefined; render(); toast('정지했습니다');
  } catch(e) { toast('정지에 실패했습니다.'); }
};
App.plUnsuspend = async function(i){
  const items = (RS.plUsers && RS.plUsers.items) || [];
  const u = items[i]; if (!u) return;
  try {
    const r = await apiFetch('/admin/users/' + u.user_id + '/unsuspend', {method:'POST', body: JSON.stringify({})});
    if (!r.ok) {
      if (r.status === 403) { toast('권한이 없습니다'); return; }
      const dd = await r.json().catch(()=>({}));
      const code = dd && dd.detail && dd.detail.code ? ' (' + dd.detail.code + ')' : '';
      toast('해제에 실패했습니다.' + code); return;
    }
    RS.plUsers = undefined; render(); toast('해제했습니다');
  } catch(e) { toast('해제에 실패했습니다.'); }
};

/* App.go 래핑 — 이동 시 캐시 무효화 */
var PL_origGo = App.go;
App.go = function(){
  RS.plUsers = undefined;
  return PL_origGo.apply(this, arguments);
};