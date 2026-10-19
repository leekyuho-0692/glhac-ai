/* ===== 내 정보(모든 로그인 역할) =====
   GET   /me/profile   — 본인 계정 상태(역할·소속·가입일·연락처·요약). /auth/me(토큰 정보)와 별개.
   PATCH /me/profile   — 연락처·표기명만. role·org_id·status 같은 권한 필드는 서버가 422 로 거부.
   비밀번호 변경은 기존 App.pwMine(POST /auth/change-password)을 재사용한다.
   NAV 에 넣지 않는다(nav-config.js 가 NAV 스냅샷을 찍는다) — 사이드바 하단 링크(SHELL_EXT.sideFoot)로만 진입. */
Object.assign(I18N.ko, {'nav.my-info':'내 정보'});
if(I18N.en) Object.assign(I18N.en, {'nav.my-info':'My account'});
if(I18N.id) Object.assign(I18N.id, {'nav.my-info':'Akun saya'});

RS.myProfile = RS.myProfile || null;
const MI_dt = (v) => v ? esc(String(v).slice(0,16).replace('T',' ')) : '-';

async function MI_load(){
  try{
    const r = await apiFetch('/me/profile');
    RS.myProfile = r.ok ? await r.json() : {error:true};
  }catch(e){ RS.myProfile = {error:true}; }
  render();
}

VIEWS['my-info'] = () => {
  const p = RS.myProfile;
  if(!p || (!p.error && p.username !== AUTH.username)){
    if(!RS._miLoading){ RS._miLoading = true; MI_load().finally(() => { RS._miLoading = false; }); }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(p.error) return '<section class="panel"><p class="empty">내 정보를 불러오지 못했습니다.</p></section>';
  const sm = p.summary || {};
  const sumRows = [];
  if(sm.invite_count !== undefined) sumRows.push(['초대코드', sm.invite_count + '건']);
  if(sm.client_count !== undefined) sumRows.push(['담당 업체', sm.client_count + '개사']);
  if(sm.assigned_case_count !== undefined) sumRows.push(['배정 케이스', sm.assigned_case_count + '건']);
  if(sm.case_count !== undefined) sumRows.push(['소속 케이스', sm.case_count + '건']);
  const kv = (k, v) => `<tr><th style="width:150px;text-align:left">${k}</th><td>${v}</td></tr>`;
  return `
  <section class="panel"><div class="panel-head"><h2>계정 정보</h2></div>
    <div class="tbl-wrap"><table><tbody>
      ${kv('아이디', `<b>${esc(p.username)}</b>`)}
      ${kv('역할', esc(ROLE_LABEL[p.role] || p.role))}
      ${kv('소속', esc(p.company_name || p.org_id || '-'))}
      ${p.company_role && ['client_admin','client_staff'].includes(p.company_role) && ROLE_LABEL[p.role] === '인증기업'
        ? kv('회사 내 역할', esc(p.company_role === 'client_admin' ? '업무 관리자' : '업무자')) : ''}
      ${kv('가입일', MI_dt(p.created_at))}
      ${kv('최근 로그인', MI_dt(p.last_login_at))}
      ${kv('계정 상태', `<span class="badge ${p.status === 'active' ? '' : 'attn'}">${p.status === 'active' ? '정상' : '정지됨'}</span>`)}
      ${p.consultant_name ? kv('담당 컨설턴트', esc(p.consultant_name)) : ''}
      ${sumRows.map(([k, v]) => kv(k, esc(v))).join('')}
    </tbody></table></div></section>
  <section class="panel"><div class="panel-head"><h2>연락처 · 표기명</h2></div>
    <p class="muted" style="font-size:12.5px">알림(이메일·SMS) 수신에 쓰입니다. 비우고 저장하면 삭제됩니다.</p>
    <div class="field"><label for="mi-email">이메일</label><input class="in" id="mi-email" type="email" autocomplete="email" value="${esc(p.contact_email || '')}"></div>
    <div class="field"><label for="mi-phone">전화</label><input class="in" id="mi-phone" type="tel" autocomplete="tel" value="${esc(p.contact_phone || '')}"></div>
    ${p.can_edit_display_name ? `<div class="field"><label for="mi-name">표기명</label><input class="in" id="mi-name" maxlength="80" value="${esc(p.display_name || '')}"></div>` : ''}
    <div class="inline-msg" id="mi-e"></div>
    <div class="row-act"><button class="btn btn-primary" onclick="App.miSave()">저장</button>
      <button class="btn" onclick="App.pwMine()">비밀번호 변경</button></div>
    <p class="muted" style="font-size:12.5px">비밀번호를 바꾸면 다른 기기의 로그인은 해제됩니다. 다시 로그인하라는 안내가 나오면 새 비밀번호로 로그인해 주세요.</p>
  </section>`;
};

App.miSave = async function(){
  const v = id => ((($('#' + id) || {}).value) || '').trim();
  const err = m => { const e = $('#mi-e'); if(e) e.textContent = m; };
  const body = {contact_email: v('mi-email'), contact_phone: v('mi-phone')};
  if($('#mi-name')) body.display_name = v('mi-name');
  const r = await apiFetch('/me/profile', {method:'PATCH', body:JSON.stringify(body)});
  if(!r.ok){
    const d = await r.clone().json().catch(() => ({}));
    const code = ((d.detail || {}).code) || '';
    return err(code === 'BAD_CONTACT_EMAIL' ? '이메일 형식이 올바르지 않습니다.'
      : code === 'BAD_CONTACT_PHONE' ? '전화번호 형식이 올바르지 않습니다.'
      : await apiErr(r, '저장에 실패했습니다.'));
  }
  RS.myProfile = await r.json(); render(); toast('저장했습니다.');
};

SHELL_EXT.sideFoot.push(() => S.role
  ? `<a class="link" style="font-size:12px" href="javascript:void(0)" onclick="App.go('my-info')">내 정보</a>` : '');
