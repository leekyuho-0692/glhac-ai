/* ===== 사용자·스태프 역할 관리 (admin 전용) =====
   POST   /admin/users            — rbac admin.user.manage(admin 전용). body {username, password, role(8종), org_id}
   PATCH  /admin/users/{id}       — require_roles()=admin 전용. body {role?, password?}
   DELETE /admin/users/{id}       — admin 전용(비활성화 API 없음 → 삭제만). 'admin' 계정 삭제 불가
   POST   /admin/staff-signups/{id}/approve — admin 전용. body {role?} (미지정=신청 역할)
   POST   /auth/staff-signup      — 공개. requested_role ∈ _STAFF_SIGNUP_ROLES(consultant·auditor·fatwa_liaison·admin) */

const US_ALL_ROLES = [['applicant','인증기업(신청자)'],['consultant','컨설턴트'],['penyelia_halal','할랄감독자'],['pendamping_pph','PPH 동반자'],
  ['auditor','오디터'],['fatwa_liaison','샤리아 위원'],['operator','운영관리자'],['admin','시스템 관리자']];
const US_STAFF_ROLES = [['consultant','컨설턴트'],['auditor','오디터(심사원)'],['fatwa_liaison','샤리아 위원'],['admin','시스템 관리자']];
const US_isAdmin = () => (typeof CAN==='function') ? !!CAN('admin.user.manage') : AUTH.role==='admin';
const US_opts = (list, cur) => list.map(([v,l])=>`<option value="${v}" ${v===cur?'selected':''}>${l} (${v})</option>`).join('');

// ── 스태프 가입 화면: 신청 역할 선택 ──
{ const f = consSignup;
  consSignup = function(){
    const html = f();
    const sel = `<fieldset class="fieldset"><legend><span class="eyebrow">00</span>신청 역할</legend>
      <div class="field"><label for="cs-role">역할<span class="req">필수</span></label><select class="in" id="cs-role" onchange="S.staffRole=this.value">${US_opts(US_STAFF_ROLES, S.staffRole||'consultant')}</select>
      <span class="muted" style="font-size:12.5px">관리자 승인 후 계정이 만들어집니다. 승인 시 관리자가 역할을 조정할 수 있습니다.</span></div></fieldset>`;
    const at = html.indexOf('<fieldset');
    return at<0 ? html : html.slice(0,at) + sel + html.slice(at);
  };
}

// ── 가입 승인: 역할 지정 ──
App.admApproveSignup = function(id){
  const x = ((RS.admMembers||{}).signups||[]).find(s=>s.request_id===id) || {};
  openModal('스태프 가입 승인', `<p><b>${esc(x.display_name||x.username||'')}</b> <span class="muted">${esc(x.username||'')}</span></p>
    <div class="field"><label for="ap-role">부여할 역할</label><select class="in" id="ap-role">${US_opts(US_STAFF_ROLES, x.requested_role||'consultant')}</select>
    <span class="muted" style="font-size:12.5px">신청 역할: ${esc(x.requested_role||'-')}</span></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.usDoApprove('${jsq(id)}')">승인</button>`);
};
App.usDoApprove = async function(id){
  const role = (($('#ap-role')||{}).value)||null;
  const r = await apiFetch('/admin/staff-signups/'+id+'/approve', {method:'POST', body:JSON.stringify({role})});
  if(!r.ok) return toast(await apiErr(r, '승인에 실패했습니다.'));
  const d = await r.json().catch(()=>({}));
  closeModal(); RS.usKey = null; await loadAdmMembers(); render(); toast('가입을 승인했습니다. ('+esc(d.username||'')+' · '+esc(d.role||role||'')+')');
};

// ── 사용자 관리 패널 ──
/* 서버 검색·필터·정렬·페이지(GET /admin/users?page=&size=&q=&role=&status=&org_id=&sort=&dir=) — 연락처는 서버가 가려서 준다 */
const US_SIZE = 20;
function US_key(){ return JSON.stringify([S.usQ||'', S.usRole||'', S.usStatus||'', S.usOrg||'', S.usSort||'username', S.usDir||'asc', S.usPage||1]); }
async function US_load(){
  const key = US_key();
  RS.usKey = key;
  const qs = new URLSearchParams({page:String(S.usPage||1), size:String(US_SIZE), sort:S.usSort||'username', dir:S.usDir||'asc'});
  if(S.usQ) qs.set('q', S.usQ);
  if(S.usRole) qs.set('role', S.usRole);
  if(S.usStatus) qs.set('status', S.usStatus);
  if(S.usOrg) qs.set('org_id', S.usOrg);
  try{
    const r = await apiFetch('/admin/users?' + qs.toString());
    RS.usList = r.ok ? await r.json() : {items:[], total:0, page:1, pages:1, error:true};
  }catch(e){ RS.usList = {items:[], total:0, page:1, pages:1, error:true}; }
  RS._usLoading = false;
  render();
}
const US_dt = (v) => v ? esc(String(v).slice(0,10)) : '-';
function US_panel(){
  const m = RS.admMembers; if(!m || m.forbidden || !US_isAdmin()) return '';
  if(RS.usKey !== US_key() && !RS._usLoading){ RS._usLoading = true; US_load(); }
  const d = RS.usList || {items:[], total:0, page:1, pages:1};
  const L = d.items || [];
  const sortTh = (k, label) => `<th><a href="javascript:void(0)" onclick="App.usSort('${k}')">${label}${(S.usSort||'username')===k ? ((S.usDir||'asc')==='desc' ? ' ▼' : ' ▲') : ''}</a></th>`;
  return `<section class="panel"><div class="panel-head"><h2>사용자 관리</h2>
      <div class="row-act"><button class="btn btn-primary" onclick="App.usNew()">＋ 사용자 생성</button></div></div>
    <div class="row-act" style="flex-wrap:wrap;gap:8px;margin-bottom:8px">
      <input class="in" style="max-width:220px" placeholder="아이디·표기명·회사명 검색" value="${esc(S.usQ||'')}" onchange="App.usSearch(this.value)">
      <select class="in" style="max-width:190px" onchange="App.usFilter('usRole',this.value)"><option value="">역할 전체</option>${US_opts(US_ALL_ROLES, S.usRole||'')}</select>
      <select class="in" style="max-width:130px" onchange="App.usFilter('usStatus',this.value)"><option value="">상태 전체</option><option value="active" ${S.usStatus==='active'?'selected':''}>정상</option><option value="suspended" ${S.usStatus==='suspended'?'selected':''}>정지됨</option></select>
      <input class="in" style="max-width:160px" placeholder="소속(org_id)" value="${esc(S.usOrg||'')}" onchange="App.usFilter('usOrg',this.value)"></div>
    <div class="tbl-wrap"><table><thead><tr>${sortTh('username','아이디')}<th>표기명</th>${sortTh('role','역할')}<th>회사</th><th>상태</th>${sortTh('created_at','가입일')}${sortTh('last_login_at','최근 로그인')}<th>연락처</th><th style="min-width:230px">역할 변경</th><th></th></tr></thead><tbody>
    ${L.map(u=>`<tr><td><a href="javascript:void(0)" onclick="App.usOpen('${jsq(u.user_id)}')"><b>${esc(u.username)}</b></a></td><td>${esc(u.display_name||'-')}</td>
      <td><span class="badge">${esc(ROLE_LABEL[u.role]||u.role)}</span></td><td class="muted">${esc(u.company_name||u.org_id||'-')}</td>
      <td><span class="badge ${u.status==='suspended'?'attn':''}">${u.status==='suspended'?'정지됨':'정상'}</span></td>
      <td class="mono muted">${US_dt(u.created_at)}</td><td class="mono muted">${US_dt(u.last_login_at)}</td>
      <td class="mono muted">${esc(u.contact_email||'')}${u.contact_email&&u.contact_phone?'<br>':''}${esc(u.contact_phone||'')}${!u.contact_email&&!u.contact_phone?'-':''}</td>
      <td><select class="in" id="us-r-${esc(u.user_id)}">${US_opts(US_ALL_ROLES, u.role)}${US_ALL_ROLES.some(r=>r[0]===u.role)?'':`<option selected value="${esc(u.role)}">${esc(u.role)}</option>`}</select></td>
      <td><div class="row-act"><button class="btn btn-sm" onclick="App.usOpen('${jsq(u.user_id)}')">상세</button><button class="btn btn-sm" onclick="App.usRole('${jsq(u.user_id)}')">역할 변경</button>
        <button class="btn btn-sm" onclick="App.usPwReset('${jsq(u.user_id)}','${jsq(u.username)}')">비밀번호 초기화</button>
        ${u.username==='admin'?'':`<button class="btn btn-sm btn-ghost" onclick="App.usDel('${jsq(u.user_id)}','${jsq(u.username)}')">삭제</button>`}</div></td></tr>`).join('')||'<tr><td colspan="10" class="empty">사용자 없음</td></tr>'}
    </tbody></table></div>
    <div class="row-act" style="justify-content:space-between;margin-top:8px"><span class="muted">${d.total||0}명 · ${d.page||1} / ${d.pages||1}</span>
      <span><button class="btn btn-sm" ${(d.page||1)<=1?'disabled':''} onclick="App.usPage(-1)">이전</button> <button class="btn btn-sm" ${(d.page||1)>=(d.pages||1)?'disabled':''} onclick="App.usPage(1)">다음</button></span></div></section>`;
}
App.usSearch = function(v){ S.usQ = v; S.usPage = 1; render(); };
App.usFilter = function(k, v){ S[k] = v; S.usPage = 1; render(); };
App.usSort = function(k){ if((S.usSort||'username')===k) S.usDir = (S.usDir==='desc'?'asc':'desc'); else { S.usSort = k; S.usDir = 'asc'; } S.usPage = 1; render(); };
App.usPage = function(d){ S.usPage = Math.max(1, (S.usPage||1) + d); render(); };
App.usNew = function(){
  openModal('사용자 생성', `
    <div class="field"><label for="un-u">아이디<span class="req">필수</span></label><input class="in" id="un-u" autocomplete="off"></div>
    <div class="field"><label for="un-p">초기 비밀번호<span class="req">필수</span></label><input class="in" id="un-p" type="password" minlength="8" autocomplete="new-password"></div>
    <div class="field"><label for="un-r">역할</label><select class="in" id="un-r">${US_opts(US_ALL_ROLES,'applicant')}</select></div>
    <div class="field"><label for="un-o">소속 org_id</label><input class="in" id="un-o" value="org_demo"></div>
    <div class="inline-msg" id="un-e"></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.usCreate()">생성</button>`);
};
App.usCreate = async function(){
  const v = id => ((($('#'+id)||{}).value)||'').trim();
  const err = m => { const e=$('#un-e'); if(e) e.textContent=m; };
  const body = { username:v('un-u'), password:(($('#un-p')||{}).value||''), role:v('un-r'), org_id:v('un-o')||'org_demo' };
  if(!body.username || !body.password) return err('아이디와 비밀번호를 입력해 주세요.');
  const r = await apiFetch('/admin/users', {method:'POST', body:JSON.stringify(body)});
  if(r.status===409) return err('이미 있는 아이디입니다.');
  if(!r.ok) return err(await apiErr(r, '사용자 생성에 실패했습니다.'));
  closeModal(); RS.usKey = null; await loadAdmMembers(); render(); toast('사용자를 생성했습니다. ('+esc(body.username)+' · '+body.role+')');
};
App.usRole = async function(uid){
  const role = (($('#us-r-'+uid)||{}).value)||'';
  const r = await apiFetch('/admin/users/'+uid, {method:'PATCH', body:JSON.stringify({role})});
  if(!r.ok) return toast(await apiErr(r, '역할 변경에 실패했습니다.'));
  RS.usKey = null; await loadAdmMembers(); render(); toast('역할을 변경했습니다. ('+role+')');
};
App.usDel = function(uid, name){
  openModal('사용자 삭제', `<p><b>${esc(name)}</b> 계정을 삭제합니다. 되돌릴 수 없습니다(비활성화 기능 없음).</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.usDoDel('${jsq(uid)}')">삭제</button>`);
};
App.usDoDel = async function(uid){
  const r = await apiFetch('/admin/users/'+uid, {method:'DELETE'});
  if(!r.ok) return toast(await apiErr(r, '삭제에 실패했습니다.'));
  closeModal(); RS.usKey = null; await loadAdmMembers(); render(); toast('사용자를 삭제했습니다.');
};

/* ── 비밀번호 초기화(관리자 → 각 계정) — 정책(8자 이상)을 지키는 임시 비밀번호를 만들어 한 번 보여 준다 ── */
function US_tempPw(){
  const a = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789', b = new Uint32Array(10);
  (window.crypto||window.msCrypto).getRandomValues(b);
  return 'Gh-' + Array.from(b, x => a[x % a.length]).join('');
}
App.usPwReset = function(uid, name){
  openModal('비밀번호 초기화', `<p><b>${esc(name)}</b> 계정의 비밀번호를 임시 비밀번호로 바꿉니다. 이 계정의 다른 로그인은 유지되니, 전달 후 본인이 바로 변경하도록 안내하세요.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.usPwResetDo('${jsq(uid)}','${jsq(name)}')">초기화</button>`);
};
App.usPwResetDo = async function(uid, name){
  const pw = US_tempPw();
  const r = await apiFetch('/admin/users/'+uid, {method:'PATCH', body:JSON.stringify({password:pw})});
  if(!r.ok) return toast(await apiErr(r, '비밀번호 초기화에 실패했습니다.'));
  openModal('임시 비밀번호', `<p><b>${esc(name)}</b> 계정의 임시 비밀번호입니다. 이 창을 닫으면 다시 볼 수 없습니다.</p>
    <div class="field"><input class="in mono" id="us-tmp" readonly value="${esc(pw)}" onclick="this.select()"></div>`,
    `<button class="btn" onclick="(navigator.clipboard&&navigator.clipboard.writeText($('#us-tmp').value)).then(()=>toast('복사했습니다.'))">복사</button><button class="btn btn-primary" onclick="App.closeModal()">닫기</button>`);
};

/* ── 내 비밀번호 변경(모든 계정) — 사이드바 로그아웃 위에 버튼 ── */
App.pwMine = function(){
  openModal('비밀번호 변경', `
    <div class="field"><label for="pw-cur">현재 비밀번호</label><input class="in" id="pw-cur" type="password" autocomplete="current-password"></div>
    <div class="field"><label for="pw-new">새 비밀번호 <span class="req">8자 이상</span></label><input class="in" id="pw-new" type="password" minlength="8" autocomplete="new-password"></div>
    <div class="field"><label for="pw-new2">새 비밀번호 확인</label><input class="in" id="pw-new2" type="password" autocomplete="new-password"></div>
    <div class="inline-msg" id="pw-e"></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.pwMineDo()">변경</button>`);
};
App.pwMineDo = async function(){
  const v = id => (($('#'+id)||{}).value)||'';
  const err = m => { const e=$('#pw-e'); if(e) e.textContent=m; };
  if(!v('pw-cur') || !v('pw-new')) return err('현재 비밀번호와 새 비밀번호를 입력해 주세요.');
  if(v('pw-new') !== v('pw-new2')) return err('새 비밀번호가 서로 다릅니다.');
  const r = await apiFetch('/auth/change-password', {method:'POST', body:JSON.stringify({current_password:v('pw-cur'), new_password:v('pw-new')})});
  if(!r.ok) return err(await apiErr(r, '비밀번호를 바꾸지 못했습니다.'));
  const d = await r.json();
  AUTH.access = d.token; AUTH.refresh = d.refresh_token; authSave();   // 서버가 다른 세션을 끊었으니 새 토큰으로 갈아끼운다
  closeModal(); toast('비밀번호를 변경했습니다. 다른 기기의 로그인은 해제됩니다.');
};
{ const _rsh = renderShell;
  renderShell = function(){ return _rsh().replace('<button class="btn btn-block" onclick="App.logout()">',
    '<button class="btn btn-block btn-ghost" onclick="App.pwMine()">비밀번호 변경</button><button class="btn btn-block" onclick="App.logout()">'); }; }

{ const f = VIEWS['adm-members']; if(typeof f==='function') VIEWS['adm-members'] = () => f() + US_panel(); }
// ?signup=staff 로 들어오면 인라인 스크립트가 모듈 로드 전에 이미 그렸다 → 역할 선택이 보이도록 다시 그린다
if(!S.role && S.auth==='signup-cons') render();

/* ── 기업 계정 발급 — 업체마다 자기 조직·자기 계정을 갖는다(POST /orgs/{org}/accounts).
   컨설턴트: 담당 업체(업체별 현황) / 관리자·운영자: 회원 관리. 임시 비밀번호는 한 번만 보인다. ── */
function OA_canIssue(){ return ['consultant','operator','admin'].includes(AUTH.role); }
function OA_isCompanyOrg(org){ return org && org !== 'org_demo' && org !== '*'; }
App.oaIssue = function(org, company){
  openModal('기업 계정 발급', `<p><b>${esc(company||org)}</b> 전용 신청인 계정을 만듭니다. 임시 비밀번호는 발급 직후 한 번만 보입니다.</p>
    <div class="field"><label for="oa-u">아이디 <span class="req">영문·숫자 3~32자 또는 이메일</span></label><input class="in" id="oa-u" autocomplete="off"></div>
    <div class="inline-msg" id="oa-e"></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.oaIssueDo('${jsq(org)}','${jsq(company||'')}')">발급</button>`);
};
App.oaIssueDo = async function(org, company){
  const username = ((($('#oa-u')||{}).value)||'').trim();
  const err = m => { const e=$('#oa-e'); if(e) e.textContent=m; };
  if(!username) return err('아이디를 입력해 주세요.');
  const r = await apiFetch('/orgs/'+encodeURIComponent(org)+'/accounts', {method:'POST', body:JSON.stringify({username})});
  if(r.status===409) return err('이미 있는 아이디입니다.');
  if(!r.ok) return err(await apiErr(r, '계정 발급에 실패했습니다.'));
  const d = await r.json();
  openModal('기업 계정 발급 완료', `<p><b>${esc(d.company||company)}</b> 계정을 발급했습니다. 이 창을 닫으면 비밀번호를 다시 볼 수 없습니다.</p>
    <div class="field"><label>아이디</label><input class="in mono" readonly value="${esc(d.username)}"></div>
    <div class="field"><label>임시 비밀번호</label><input class="in mono" id="oa-pw" readonly value="${esc(d.temp_password)}" onclick="this.select()"></div>
    <p class="muted">첫 로그인 후 사이드바 '비밀번호 변경'으로 바꾸도록 안내하세요.</p>`,
    `<button class="btn" onclick="(navigator.clipboard&&navigator.clipboard.writeText('아이디: ${jsq(d.username)}\\n임시 비밀번호: '+$('#oa-pw').value)).then(()=>toast('복사했습니다.'))">복사</button><button class="btn btn-primary" onclick="App.closeModal()">닫기</button>`);
};
// 컨설턴트 업체별 현황 — 선택 업체에 발급 버튼
{ const f = VIEWS['cons-status']; if(typeof f==='function') VIEWS['cons-status'] = () => {
    const c = (RS.cases||[]).find(x => x.case_id === S.selCompany);
    const btn = c && OA_canIssue() && OA_isCompanyOrg(c.org_id)
      ? `<section class="panel"><div class="panel-head"><h2>기업 계정</h2><button class="btn btn-sm" onclick="App.oaIssue('${jsq(c.org_id)}','${jsq(c.company_name||'')}')">＋ 기업 계정 발급</button></div><p class="muted">업체가 직접 로그인해 서류·계약을 처리하는 신청인 계정입니다.</p></section>`
      : (c && !OA_isCompanyOrg(c.org_id) ? `<section class="panel"><p class="muted">이 신청은 업체 전용 조직이 아닌 공용 조직에 있어 기업 계정을 발급할 수 없습니다. 관리자에게 업체 분리를 요청하세요.</p></section>` : '');
    return f() + btn; }; }
// 업체별 기업 계정 발급 목록 — 관리자는 회원 관리, 운영자는 배정 관리에서(회원 관리는 admin 전용 화면, rbac.js VIEW_ROLES)
function OA_panel(){
    const seen = {}, rows = [];
    (RS.cases||[]).forEach(c => { if(OA_isCompanyOrg(c.org_id) && !seen[c.org_id]){ seen[c.org_id]=1; rows.push(c); } });
    return `<section class="panel"><div class="panel-head"><h2>업체별 기업 계정 발급</h2></div>
      <div class="tbl-wrap"><table><thead><tr><th>업체</th><th>org</th><th></th></tr></thead><tbody>
      ${rows.map(c=>`<tr><td><b>${esc(c.company_name||'-')}</b></td><td class="mono muted">${esc(c.org_id)}</td>
        <td><button class="btn btn-sm" onclick="App.oaIssue('${jsq(c.org_id)}','${jsq(c.company_name||'')}')">＋ 기업 계정 발급</button></td></tr>`).join('') || '<tr><td colspan="3" class="empty">업체 전용 조직이 없습니다</td></tr>'}
      </tbody></table></div></section>`;
}
{ const f = VIEWS['adm-members']; if(typeof f==='function') VIEWS['adm-members'] = () => f() + (OA_canIssue() ? OA_panel() : ''); }
{ const f = VIEWS['adm-assign']; if(typeof f==='function') VIEWS['adm-assign'] = () => f() + (AUTH.role === 'operator' ? OA_panel() : ''); }

/* ── 운영자 사용자 상세(adm-user-detail) — GET /admin/users/{id}
     연락처 원문을 보여 주므로 서버가 열람을 감사로그(user.view)에 남긴다. 화면에서 새로고침/렌더가 반복돼도
     요청은 사용자당 1회(RS.usDetail 캐시)만 나간다. 수정은 기존 PATCH /admin/users/{id}·suspend·unsuspend 를 쓴다. ── */
Object.assign(I18N.ko, {'nav.adm-user-detail':'사용자 상세'});
if(I18N.en) Object.assign(I18N.en, {'nav.adm-user-detail':'User detail'});
if(I18N.id) Object.assign(I18N.id, {'nav.adm-user-detail':'Detail pengguna'});
RS.usDetail = RS.usDetail || null;
App.usOpen = function(uid){ S.usDetailId = uid; RS.usDetail = null; App.go('adm-user-detail'); };
async function US_loadDetail(){
  const uid = S.usDetailId;
  try{
    const r = await apiFetch('/admin/users/' + encodeURIComponent(uid));
    RS.usDetail = r.ok ? await r.json() : {error:true, status:r.status, user_id:uid};
  }catch(e){ RS.usDetail = {error:true, user_id:uid}; }
  RS._usDetailLoading = false;
  render();
}
VIEWS['adm-user-detail'] = () => {
  const back = `<p><button class="btn btn-sm" onclick="App.go('adm-members')">← 목록으로</button></p>`;
  if(!US_isAdmin()) return back + '<section class="panel"><div class="note attn"><b>관리자 전용 화면</b></div></section>';
  const d = RS.usDetail;
  if(!d || d.user_id !== S.usDetailId){
    if(!RS._usDetailLoading){ RS._usDetailLoading = true; US_loadDetail(); }
    return back + '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(d.error) return back + '<section class="panel"><p class="empty">사용자를 불러오지 못했습니다.</p></section>';
  const kv = (k, v) => `<tr><th style="width:150px;text-align:left">${k}</th><td>${v}</td></tr>`;
  const dt = v => v ? esc(String(v).slice(0,16).replace('T',' ')) : '-';
  const uid = esc(d.user_id);
  return back + `
  <section class="panel"><div class="panel-head"><h2>${esc(d.username)}</h2>
      <span class="badge ${d.status==='suspended'?'attn':''}">${d.status==='suspended'?'정지됨':'정상'}</span></div>
    <div class="tbl-wrap"><table><tbody>
      ${kv('아이디', `<b>${esc(d.username)}</b> <span class="mono muted">${uid}</span>`)}
      ${kv('표기명', esc(d.display_name||'-'))}
      ${kv('역할', esc(ROLE_LABEL[d.role]||d.role))}
      ${kv('소속', esc((d.org&&d.org.name)||d.org_id||'-') + ' <span class="mono muted">' + esc(d.org_id||'') + '</span>')}
      ${ROLE_LABEL[d.role]==='인증기업' ? kv('회사 내 역할', esc(d.company_role||'-')) : ''}
      ${kv('가입일', dt(d.created_at))}
      ${kv('최근 로그인', dt(d.last_login_at))}
      ${kv('로그인 제한', d.login&&d.login.rate_limited ? '<span class="badge attn">제한 중</span>' : '없음')}
      ${Object.entries(d.summary||{}).map(([k,v])=>kv(esc(({invite_count:'초대코드',client_count:'담당 업체',assigned_case_count:'배정 케이스',case_count:'소속 케이스'})[k]||k), esc(v))).join('')}
    </tbody></table></div></section>
  <section class="panel"><div class="panel-head"><h2>연락처 · 계정 관리</h2></div>
    <div class="field"><label for="ud-email">이메일</label><input class="in" id="ud-email" value="${esc(d.contact_email||'')}"></div>
    <div class="field"><label for="ud-phone">전화</label><input class="in" id="ud-phone" value="${esc(d.contact_phone||'')}"></div>
    <div class="inline-msg" id="ud-e"></div>
    <div class="row-act"><button class="btn btn-primary" onclick="App.usSaveContact('${jsq(d.user_id)}')">연락처 저장</button></div>
    <div class="field" style="margin-top:12px"><label for="ud-role">역할 변경</label><select class="in" id="ud-role">${US_opts(US_ALL_ROLES, d.role)}</select></div>
    <div class="row-act"><button class="btn" onclick="App.usDetailRole('${jsq(d.user_id)}')">역할 변경</button>
      ${d.status==='suspended'
        ? `<button class="btn" onclick="App.usUnsuspend('${jsq(d.user_id)}')">정지 해제</button>`
        : `<button class="btn btn-ghost" onclick="App.usSuspend('${jsq(d.user_id)}','${jsq(d.username)}')">정지</button>`}</div></section>
  <section class="panel"><div class="panel-head"><h2>상태 이력</h2></div>
    <div class="tbl-wrap"><table><thead><tr><th>시각</th><th>상태</th><th>사유</th></tr></thead><tbody>
    ${(d.status_history||[]).map(h=>`<tr><td class="mono">${dt(h.at)}</td><td>${h.status==='suspended'?'정지됨':'정상'}</td><td>${esc(h.reason||'-')}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">이력 없음</td></tr>'}
    </tbody></table></div>
    <div class="tbl-wrap"><table><thead><tr><th>시각</th><th>관리 조치</th></tr></thead><tbody>
    ${(d.admin_actions||[]).map(a=>`<tr><td class="mono">${dt(a.at)}</td><td class="mono">${esc(a.action)}</td></tr>`).join('')||'<tr><td colspan="2" class="empty">조치 없음</td></tr>'}
    </tbody></table></div></section>
  <section class="panel"><div class="panel-head"><h2>관련 케이스</h2><span class="badge">${(d.related_cases||[]).length}건</span></div>
    <div class="tbl-wrap"><table><thead><tr><th>케이스</th><th>회사</th><th>상태</th></tr></thead><tbody>
    ${(d.related_cases||[]).map(c=>`<tr><td class="mono">${esc(c.case_id)}</td><td>${esc(c.company_name||'-')}</td><td>${esc(c.status||'-')}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">케이스 없음</td></tr>'}
    </tbody></table></div></section>
  <section class="panel"><div class="panel-head"><h2>최근 감사로그</h2><span class="badge">${(d.recent_audit||[]).length}건</span></div>
    <div class="tbl-wrap"><table><thead><tr><th>시각</th><th>행위</th><th>대상</th></tr></thead><tbody>
    ${(d.recent_audit||[]).map(a=>`<tr><td class="mono">${dt(a.at)}</td><td class="mono">${esc(a.action)}</td><td class="mono muted">${esc(a.case_id||a.resource_id||'-')}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">기록 없음</td></tr>'}
    </tbody></table></div></section>`;
};
App.usSaveContact = async function(uid){
  const v = id => ((($('#'+id)||{}).value)||'').trim();
  const r = await apiFetch('/admin/users/'+uid, {method:'PATCH', body:JSON.stringify({contact_email:v('ud-email'), contact_phone:v('ud-phone')})});
  if(!r.ok){ const e=$('#ud-e'); if(e) e.textContent = await apiErr(r, '저장에 실패했습니다.'); return; }
  RS.usDetail = null; RS.usKey = null; render(); toast('연락처를 저장했습니다.');
};
App.usDetailRole = async function(uid){
  const role = (($('#ud-role')||{}).value)||'';
  const r = await apiFetch('/admin/users/'+uid, {method:'PATCH', body:JSON.stringify({role})});
  if(!r.ok) return toast(await apiErr(r, '역할 변경에 실패했습니다.'));
  RS.usDetail = null; RS.usKey = null; render(); toast('역할을 변경했습니다. ('+role+')');
};
App.usSuspend = function(uid, name){
  openModal('계정 정지', `<p><b>${esc(name)}</b> 계정을 정지합니다. 로그인이 막히고 기존 로그인은 즉시 해제됩니다.</p>
    <div class="field"><label for="ud-reason">사유</label><textarea class="in" id="ud-reason"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.usSuspendDo('${jsq(uid)}')">정지</button>`);
};
App.usSuspendDo = async function(uid){
  const reason = (($('#ud-reason')||{}).value||'').trim();
  const r = await apiFetch('/admin/users/'+uid+'/suspend', {method:'POST', body:JSON.stringify({reason})});
  if(!r.ok) return toast(await apiErr(r, '정지에 실패했습니다.'));
  closeModal(); RS.usDetail = null; RS.usKey = null; render(); toast('계정을 정지했습니다.');
};
App.usUnsuspend = async function(uid){
  const r = await apiFetch('/admin/users/'+uid+'/unsuspend', {method:'POST'});
  if(!r.ok) return toast(await apiErr(r, '정지 해제에 실패했습니다.'));
  RS.usDetail = null; RS.usKey = null; render(); toast('정지를 해제했습니다.');
};
