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
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.usDoApprove('${esc(id)}')">승인</button>`);
};
App.usDoApprove = async function(id){
  const role = (($('#ap-role')||{}).value)||null;
  const r = await apiFetch('/admin/staff-signups/'+id+'/approve', {method:'POST', body:JSON.stringify({role})});
  if(!r.ok) return toast(await apiErr(r, '승인에 실패했습니다.'));
  const d = await r.json().catch(()=>({}));
  closeModal(); await loadAdmMembers(); render(); toast('가입을 승인했습니다. ('+esc(d.username||'')+' · '+esc(d.role||role||'')+')');
};

// ── 사용자 관리 패널 ──
function US_panel(){
  const m = RS.admMembers; if(!m || m.forbidden || !US_isAdmin()) return '';
  const q = (S.usQ||'').toLowerCase();
  const L = (m.users||[]).filter(u=>!q || (u.username||'').toLowerCase().includes(q) || (u.role||'').includes(q) || (u.org_id||'').toLowerCase().includes(q));
  return `<section class="panel"><div class="panel-head"><h2>사용자 관리</h2>
      <div class="row-act"><input class="in" style="max-width:220px" placeholder="아이디·역할·org 검색" value="${esc(S.usQ||'')}" onchange="App.usSearch(this.value)"><button class="btn btn-primary" onclick="App.usNew()">＋ 사용자 생성</button></div></div>
    <div class="tbl-wrap"><table><thead><tr><th style="min-width:170px">아이디</th><th>org</th><th style="min-width:230px">역할</th><th></th></tr></thead><tbody>
    ${L.slice(0,100).map(u=>`<tr><td><b>${esc(u.username)}</b></td><td class="mono muted">${esc(u.org_id||'-')}</td>
      <td><select class="in" id="us-r-${esc(u.user_id)}">${US_opts(US_ALL_ROLES, u.role)}${US_ALL_ROLES.some(r=>r[0]===u.role)?'':`<option selected value="${esc(u.role)}">${esc(u.role)}</option>`}</select></td>
      <td><div class="row-act"><button class="btn btn-sm" onclick="App.usRole('${esc(u.user_id)}')">역할 변경</button>
        <button class="btn btn-sm" onclick="App.usPwReset('${esc(u.user_id)}','${esc(u.username)}')">비밀번호 초기화</button>
        ${u.username==='admin'?'':`<button class="btn btn-sm btn-ghost" onclick="App.usDel('${esc(u.user_id)}','${esc(u.username)}')">삭제</button>`}</div></td></tr>`).join('')||'<tr><td colspan="4" class="empty">사용자 없음</td></tr>'}
    </tbody></table></div>${L.length>100?`<p class="muted">상위 100명만 표시 — 검색으로 좁혀 주세요.</p>`:''}</section>`;
}
App.usSearch = function(v){ S.usQ = v; render(); };
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
  closeModal(); await loadAdmMembers(); render(); toast('사용자를 생성했습니다. ('+esc(body.username)+' · '+body.role+')');
};
App.usRole = async function(uid){
  const role = (($('#us-r-'+uid)||{}).value)||'';
  const r = await apiFetch('/admin/users/'+uid, {method:'PATCH', body:JSON.stringify({role})});
  if(!r.ok) return toast(await apiErr(r, '역할 변경에 실패했습니다.'));
  await loadAdmMembers(); render(); toast('역할을 변경했습니다. ('+role+')');
};
App.usDel = function(uid, name){
  openModal('사용자 삭제', `<p><b>${esc(name)}</b> 계정을 삭제합니다. 되돌릴 수 없습니다(비활성화 기능 없음).</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.usDoDel('${esc(uid)}')">삭제</button>`);
};
App.usDoDel = async function(uid){
  const r = await apiFetch('/admin/users/'+uid, {method:'DELETE'});
  if(!r.ok) return toast(await apiErr(r, '삭제에 실패했습니다.'));
  closeModal(); await loadAdmMembers(); render(); toast('사용자를 삭제했습니다.');
};

/* ── 비밀번호 초기화(관리자 → 각 계정) — 정책(8자 이상)을 지키는 임시 비밀번호를 만들어 한 번 보여 준다 ── */
function US_tempPw(){
  const a = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789', b = new Uint32Array(10);
  (window.crypto||window.msCrypto).getRandomValues(b);
  return 'Gh-' + Array.from(b, x => a[x % a.length]).join('');
}
App.usPwReset = function(uid, name){
  openModal('비밀번호 초기화', `<p><b>${esc(name)}</b> 계정의 비밀번호를 임시 비밀번호로 바꿉니다. 이 계정의 다른 로그인은 유지되니, 전달 후 본인이 바로 변경하도록 안내하세요.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.usPwResetDo('${esc(uid)}','${esc(name)}')">초기화</button>`);
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
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.oaIssueDo('${esc(org)}','${esc(company||'')}')">발급</button>`);
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
    `<button class="btn" onclick="(navigator.clipboard&&navigator.clipboard.writeText('아이디: ${esc(d.username)}\\n임시 비밀번호: '+$('#oa-pw').value)).then(()=>toast('복사했습니다.'))">복사</button><button class="btn btn-primary" onclick="App.closeModal()">닫기</button>`);
};
// 컨설턴트 업체별 현황 — 선택 업체에 발급 버튼
{ const f = VIEWS['cons-status']; if(typeof f==='function') VIEWS['cons-status'] = () => {
    const c = (RS.cases||[]).find(x => x.case_id === S.selCompany);
    const btn = c && OA_canIssue() && OA_isCompanyOrg(c.org_id)
      ? `<section class="panel"><div class="panel-head"><h2>기업 계정</h2><button class="btn btn-sm" onclick="App.oaIssue('${esc(c.org_id)}','${esc(c.company_name||'')}')">＋ 기업 계정 발급</button></div><p class="muted">업체가 직접 로그인해 서류·계약을 처리하는 신청인 계정입니다.</p></section>`
      : (c && !OA_isCompanyOrg(c.org_id) ? `<section class="panel"><p class="muted">이 신청은 업체 전용 조직이 아닌 공용 조직에 있어 기업 계정을 발급할 수 없습니다. 관리자에게 업체 분리를 요청하세요.</p></section>` : '');
    return f() + btn; }; }
// 업체별 기업 계정 발급 목록 — 관리자는 회원 관리, 운영자는 배정 관리에서(회원 관리는 admin 전용 화면, rbac.js VIEW_ROLES)
function OA_panel(){
    const seen = {}, rows = [];
    (RS.cases||[]).forEach(c => { if(OA_isCompanyOrg(c.org_id) && !seen[c.org_id]){ seen[c.org_id]=1; rows.push(c); } });
    return `<section class="panel"><div class="panel-head"><h2>업체별 기업 계정 발급</h2></div>
      <div class="tbl-wrap"><table><thead><tr><th>업체</th><th>org</th><th></th></tr></thead><tbody>
      ${rows.map(c=>`<tr><td><b>${esc(c.company_name||'-')}</b></td><td class="mono muted">${esc(c.org_id)}</td>
        <td><button class="btn btn-sm" onclick="App.oaIssue('${esc(c.org_id)}','${esc(c.company_name||'')}')">＋ 기업 계정 발급</button></td></tr>`).join('') || '<tr><td colspan="3" class="empty">업체 전용 조직이 없습니다</td></tr>'}
      </tbody></table></div></section>`;
}
{ const f = VIEWS['adm-members']; if(typeof f==='function') VIEWS['adm-members'] = () => f() + (OA_canIssue() ? OA_panel() : ''); }
{ const f = VIEWS['adm-assign']; if(typeof f==='function') VIEWS['adm-assign'] = () => f() + (AUTH.role === 'operator' ? OA_panel() : ''); }
