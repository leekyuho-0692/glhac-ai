/* ===== 운영 도구 · 조직·초대 탭 ===== */
(function(){
  const OPSO_ORGS = 'opso.orgs';
  const OPSO_INV  = 'opso.invites';
  const OPSO_STF  = 'opso.staff';
  const OPSO_OV   = 'opso.overview';

  const OPSO_TYPE_LABEL = '조직';
  const OPSO_403 = (need) => `<section class="panel"><p class="empty">이 기능은 권한이 없습니다${need?'('+need+')':''}.</p></section>`;
  const OPSO_ERR = (m) => `<section class="panel"><p class="empty">${esc(m||'불러오지 못했습니다.')}</p></section>`;
  const OPSO_BADGE = (ok, txtOk, txtNo) => ok
    ? `<span class="badge strong">${esc(txtOk)}</span>`
    : `<span class="badge attn">${esc(txtNo)}</span>`;

  // ── 조직 목록 ────────────────────────────────────────────
  async function OPSO_loadOrgs(){
    try{
      const r = await apiFetch('/admin/orgs');
      if(!r.ok){ RS[OPSO_ORGS] = {error:true, status:r.status}; return; }
      const arr = await r.json();
      RS[OPSO_ORGS] = {items: Array.isArray(arr) ? arr : []};
    }catch(e){ RS[OPSO_ORGS] = {error:true}; }
  }
  async function OPSO_loadOverview(org_id){
    try{
      const r = await apiFetch('/admin/orgs/'+encodeURIComponent(org_id)+'/overview');
      if(!r.ok){ RS[OPSO_OV][org_id] = {error:true, status:r.status}; return; }
      RS[OPSO_OV][org_id] = await r.json();
    }catch(e){ RS[OPSO_OV][org_id] = {error:true}; }
  }

  function OPSO_orgType(o){
    // 원문에 명시적 유형 필드는 없다. 이름 유무로만 표기(추정 금지, 라벨만).
    return o.name ? OPSO_TYPE_LABEL : '이름 없음';
  }

  function OPSO_orgTable(list){
    if(!list.length) return '<p class="empty">조직이 없습니다.</p>';
    const head = `<tr><th>조직</th><th>이름</th><th class="num">사용자</th><th class="num">케이스</th><th>담당 컨설턴트</th><th></th></tr>`;
    const rows = list.map((o, i) => {
      const on = S.opsoOrgIdx === i;
      return `<tr>
        <td class="mono">${esc(o.org_id)}</td>
        <td>${esc(o.name||'—')}</td>
        <td class="num">${o.users||0}</td>
        <td class="num">${o.cases||0}</td>
        <td>${o.consultant_id
          ? `<span class="tag" title="${esc(o.consultant_id)}">${esc(o.consultant_name||o.consultant_id)}</span>`
          : '<span class="muted">미지정</span>'}</td>
        <td class="row-act"><button class="btn btn-sm ${on?'btn-primary':''}" onclick="App.opsoPick(${i})">${on?'선택됨':'상세'}</button></td>
      </tr>`;
    }).join('');
    return `<div class="tbl-wrap"><table>${head}${rows}</table></div>`;
  }

  function OPSO_detail(o){
    const org_id = o.org_id;
    const ov = RS[OPSO_OV][org_id];
    let ovHtml;
    if(!ov){
      if(RS._opsoOvLoading !== org_id){
        RS._opsoOvLoading = org_id;
        OPSO_loadOverview(org_id).then(()=>{ RS._opsoOvLoading = null; render(); });
      }
      ovHtml = '<p class="empty">불러오는 중…</p>';
    } else if(ov.error){
      ovHtml = ov.status === 403 ? '<p class="empty">개요를 볼 권한이 없습니다.</p>' : '<p class="empty">개요를 불러오지 못했습니다.</p>';
    } else {
      const byStatus = ov.by_status || {};
      const stRows = Object.keys(byStatus).length
        ? Object.keys(byStatus).map(k=>`<div><dt>${esc(k)}</dt><dd class="mono">${byStatus[k]}</dd></div>`).join('')
        : '<div><dt>상태</dt><dd class="muted">없음</dd></div>';
      ovHtml = `<div class="summary">
          <div><span class="k">케이스</span><span class="v mono">${ov.cases||0}</span></div>
          <div><span class="k">사용자</span><span class="v mono">${ov.users||0}</span></div>
          <div><span class="k">활성 인증서</span><span class="v mono">${ov.certificates||0}</span></div>
          <div><span class="k">정산 완료</span><span class="v mono">${esc(_money(ov.revenue_paid||0, 'IDR'))}</span></div>
        </div>
        <div class="dl" style="margin-top:10px">${stRows}</div>`;
    }
    return `<section class="panel">
      <div class="panel-head"><h2>${esc(o.org_id)} <span class="muted" style="font-weight:400">${esc(o.name||'')}</span></h2>
        <div class="row-act">
          <button class="btn btn-sm" onclick="App.opsoRename()">정보 수정</button>
        </div>
      </div>
      ${ovHtml}
      <fieldset style="margin-top:14px">
        <legend>담당 컨설턴트</legend>
        <p class="muted">현재: ${o.consultant_id ? esc(o.consultant_name||o.consultant_id)+' <span class="mono">('+esc(o.consultant_id)+')</span>' : '미지정'}${o.consultant_linked_at?' · 연결 '+esc(o.consultant_linked_at):''}</p>
        <div class="inline">
          <input class="in" id="opso-cons" placeholder="컨설턴트 user_id 또는 username" value="">
          <input class="in" id="opso-cons-reason" placeholder="사유(선택)">
          <button class="btn btn-primary" onclick="App.opsoSetCons()">지정</button>
          ${o.consultant_id ? '<button class="btn btn-ghost" onclick="App.opsoClearCons()">지정 해제</button>' : ''}
        </div>
      </fieldset>
    </section>`;
  }

  function OPSO_orgSection(){
    if(!canCall('GET','/admin/orgs')) return '';   // 조직 목록·생성은 admin 전용(main.py:1682·1706) — operator 화면에선 숨김
    const c = RS[OPSO_ORGS];
    if(!c){
      if(!RS._opsoOrgsLoading){ RS._opsoOrgsLoading = true; OPSO_loadOrgs().then(()=>{ RS._opsoOrgsLoading = false; render(); }); }
      return '<section class="panel"><div class="panel-head"><h2>조직</h2></div><p class="empty">불러오는 중…</p></section>';
    }
    if(c.error){
      return c.status === 403 ? OPSO_403('admin/operator') : OPSO_ERR('조직 목록을 불러오지 못했습니다.');
    }
    const list = c.items;
    let pick = null;
    if(list.length){
      const idx = (typeof S.opsoOrgIdx === 'number' && S.opsoOrgIdx >= 0 && S.opsoOrgIdx < list.length) ? S.opsoOrgIdx : 0;
      S.opsoOrgIdx = idx;
      pick = list[idx];
    }
    const add = `<fieldset style="margin-top:14px">
      <legend>조직 추가</legend>
      <div class="inline">
        <input class="in" id="opso-new-id" placeholder="org_id">
        <input class="in" id="opso-new-name" placeholder="이름(선택)">
        <button class="btn btn-primary" onclick="App.opsoCreate()">추가</button>
      </div>
    </fieldset>`;
    const detail = pick ? OPSO_detail(pick) : '';
    return `<section class="panel"><div class="panel-head"><h2>조직 <span class="count">${list.length}</span></h2></div>
      ${OPSO_orgTable(list)}
      ${add}
    </section>${detail}`;
  }

  // ── 클라이언트 초대 ──────────────────────────────────────
  async function OPSO_loadInvites(){
    try{
      const r = await apiFetch('/admin/client-invites');
      if(!r.ok){ RS[OPSO_INV] = {error:true, status:r.status}; return; }
      const j = await r.json();
      RS[OPSO_INV] = {items: (j && j.items) || []};
    }catch(e){ RS[OPSO_INV] = {error:true}; }
  }

  function OPSO_invTable(items){
    if(!items.length) return '<p class="empty">초대가 없습니다.</p>';
    const LIMIT = 100;
    const show = items.slice(0, LIMIT);
    const rest = items.length - show.length;
    const rows = show.map((it, i) => {
      const st = it.revoked ? ['attn','회수됨']
        : (it.usable ? ['strong','사용 가능'] : ['attn','만료/소진']);
      return `<tr>
        <td class="mono">${esc(it.code)}${it.is_primary?' <span class="tag">대표</span>':''}</td>
        <td>${esc(it.company_name||'—')}${it.note?`<div class="muted" style="font-size:11px">${esc(it.note)}</div>`:''}</td>
        <td><span class="badge ${st[0]}">${st[1]}</span></td>
        <td class="num mono">${it.used_count||0}/${it.max_uses||0}</td>
        <td class="muted" style="font-size:11px">${esc(it.expires_at||'무기한')}</td>
        <td class="row-act">
          <button class="btn btn-sm" onclick="App.opsoQr(${i})">QR</button>
          ${it.is_primary?'':`<button class="btn btn-sm btn-ghost" onclick="App.opsoRevoke(${i})">회수</button>`}
        </td>
      </tr>`;
    }).join('');
    const head = `<tr><th>코드</th><th>메모·대상</th><th>상태</th><th class="num">사용</th><th>만료</th><th></th></tr>`;
    return `<div class="tbl-wrap"><table>${head}${rows}</table></div>${rest>0?`<p class="muted" style="margin-top:6px">외 ${rest}건</p>`:''}`;
  }

  function OPSO_invSection(){
    const c = RS[OPSO_INV];
    let body;
    if(!c){
      if(!RS._opsoInvLoading){ RS._opsoInvLoading = true; OPSO_loadInvites().then(()=>{ RS._opsoInvLoading = false; render(); }); }
      body = '<p class="empty">불러오는 중…</p>';
    } else if(c.error){
      body = c.status === 403 ? '<p class="empty">이 기능은 권한이 없습니다(admin/operator).</p>' : '<p class="empty">초대 목록을 불러오지 못했습니다.</p>';
    } else {
      body = OPSO_invTable(c.items);
    }
    const form = `<fieldset style="margin-top:14px">
      <legend>초대 생성</legend>
      <div class="inline">
        <input class="in" id="opso-inv-company" placeholder="대상 업체명(선택)">
        <input class="in" id="opso-inv-note" placeholder="메모(선택)">
        <input class="in" id="opso-inv-uses" type="number" min="1" value="1" style="max-width:90px" placeholder="사용 횟수">
        <input class="in" id="opso-inv-days" type="number" min="1" max="365" value="30" style="max-width:90px" placeholder="만료(일)">
        <button class="btn btn-primary" onclick="App.opsoCreateInvite()">생성</button>
      </div>
    </fieldset>`;
    return `<section class="panel"><div class="panel-head"><h2>클라이언트 초대 QR</h2></div>${body}${form}</section>`;
  }

  // ── 스태프 가입 링크 ─────────────────────────────────────
  async function OPSO_loadStaff(){
    try{
      const r = await apiFetch('/admin/staff-signup-link');
      if(!r.ok){ RS[OPSO_STF] = {error:true, status:r.status}; return; }
      RS[OPSO_STF] = await r.json();
    }catch(e){ RS[OPSO_STF] = {error:true}; }
  }

  function OPSO_staffSection(){
    const c = RS[OPSO_STF];
    let body;
    if(!c){
      if(!RS._opsoStfLoading){ RS._opsoStfLoading = true; OPSO_loadStaff().then(()=>{ RS._opsoStfLoading = false; render(); }); }
      body = '<p class="empty">불러오는 중…</p>';
    } else if(c.error){
      body = c.status === 403 ? '<p class="empty">이 기능은 시스템 관리자(admin) 전용입니다.</p>' : '<p class="empty">가입 링크를 불러오지 못했습니다.</p>';
    } else {
      body = `<div class="summary">
          <div><span class="k">가입 링크</span><span class="v mono">${esc(c.url||'')}</span></div>
        </div>
        <div class="row-act" style="margin-top:10px">
          <button class="btn" onclick="App.opsoStaffQr('png')">QR 내려받기(PNG)</button>
        </div>`;
    }
    return `<section class="panel"><div class="panel-head"><h2>스태프 가입 링크</h2></div>${body}</section>`;
  }

  // ── 탭 렌더 ──────────────────────────────────────────────
  function OPSO_render(){
    RS[OPSO_OV] = RS[OPSO_OV] || {};
    return OPSO_orgSection() + OPSO_invSection() + OPSO_staffSection();
  }

  // ── 액션 ────────────────────────────────────────────────
  App.opsoPick = function(i){
    S.opsoOrgIdx = i;
    const c = RS[OPSO_ORGS];
    if(c && c.items && c.items[i]){
      const id = c.items[i].org_id;
      if(!RS[OPSO_OV][id] && RS._opsoOvLoading !== id){
        RS._opsoOvLoading = id;
        OPSO_loadOverview(id).then(()=>{ RS._opsoOvLoading = null; render(); });
      }
    }
    render();
  };

  App.opsoCreate = async function(){
    const org_id = (($('#opso-new-id')||{}).value||'').trim();
    const name = (($('#opso-new-name')||{}).value||'').trim();
    if(!org_id) return toast('org_id 를 입력하세요.');
    try{
      const r = await apiFetch('/admin/orgs', {method:'POST', body: JSON.stringify({org_id, name: name||null})});
      if(!r.ok){
        let code = '';
        try{ const j = await r.json(); code = j && j.detail && j.detail.code ? ' ('+j.detail.code+')' : ''; }catch(e){}
        return toast('조직 생성에 실패했습니다.'+code);
      }
      RS[OPSO_ORGS] = null;
      await OPSO_loadOrgs();
      render();
      toast('조직을 추가했습니다.');
    }catch(e){ toast('조직 생성에 실패했습니다.'); }
  };

  App.opsoRename = function(){
    const c = RS[OPSO_ORGS];
    const o = c && c.items && c.items[S.opsoOrgIdx];
    if(!o) return;
    const body = `<div class="field"><label>이름</label>
      <input class="in" id="opso-rename-name" value="${esc(o.name||'')}"></div>
      <p class="muted" style="margin-top:6px">표기만 바뀝니다. org_id·소속·케이스는 그대로입니다.</p>`;
    const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
      <button class="btn btn-primary" onclick="App.opsoRenameDo()">저장</button>`;
    openModal('조직 정보 수정', body, foot);
  };

  App.opsoRenameDo = async function(){
    const c = RS[OPSO_ORGS];
    const o = c && c.items && c.items[S.opsoOrgIdx];
    if(!o){ App.closeModal(); return; }
    const name = (($('#opso-rename-name')||{}).value||'').trim();
    if(!name) return toast('이름을 입력하세요.');
    try{
      const r = await apiFetch('/admin/orgs/'+encodeURIComponent(o.org_id), {method:'PATCH', body: JSON.stringify({name})});
      if(!r.ok){
        if(r.status === 403) return toast('이 작업을 할 권한이 없습니다.');
        let code = '';
        try{ const j = await r.json(); code = j && j.detail && j.detail.code ? ' ('+j.detail.code+')' : ''; }catch(e){}
        return toast('이름 변경에 실패했습니다.'+code);
      }
      App.closeModal();
      RS[OPSO_ORGS] = null;
      await OPSO_loadOrgs();
      render();
      toast('이름을 변경했습니다.');
    }catch(e){ toast('이름 변경에 실패했습니다.'); }
  };

  App.opsoSetCons = async function(){
    const c = RS[OPSO_ORGS];
    const o = c && c.items && c.items[S.opsoOrgIdx];
    if(!o) return;
    const consultant_id = (($('#opso-cons')||{}).value||'').trim();
    const reason = (($('#opso-cons-reason')||{}).value||'').trim();
    if(!consultant_id) return toast('컨설턴트 id 를 입력하세요.');
    try{
      const r = await apiFetch('/admin/orgs/'+encodeURIComponent(o.org_id)+'/consultant',
        {method:'PUT', body: JSON.stringify({consultant_id, reason: reason||null})});
      if(!r.ok){
        if(r.status === 403) return toast('이 작업을 할 권한이 없습니다.');
        let code = '';
        try{ const j = await r.json(); code = j && j.detail && j.detail.code ? ' ('+j.detail.code+')' : ''; }catch(e){}
        return toast('담당 지정에 실패했습니다.'+code);
      }
      RS[OPSO_ORGS] = null;
      await OPSO_loadOrgs();
      const cur = RS[OPSO_ORGS] && RS[OPSO_ORGS].items && RS[OPSO_ORGS].items[S.opsoOrgIdx];
      if(cur && !RS[OPSO_OV][cur.org_id]) await OPSO_loadOverview(cur.org_id);
      render();
      toast('담당 컨설턴트를 지정했습니다.');
    }catch(e){ toast('담당 지정에 실패했습니다.'); }
  };

  App.opsoClearCons = async function(){
    const c = RS[OPSO_ORGS];
    const o = c && c.items && c.items[S.opsoOrgIdx];
    if(!o) return;
    try{
      const r = await apiFetch('/admin/orgs/'+encodeURIComponent(o.org_id)+'/consultant', {method:'DELETE'});
      if(!r.ok){
        if(r.status === 403) return toast('이 작업을 할 권한이 없습니다.');
        let code = '';
        try{ const j = await r.json(); code = j && j.detail && j.detail.code ? ' ('+j.detail.code+')' : ''; }catch(e){}
        return toast('지정 해제에 실패했습니다.'+code);
      }
      RS[OPSO_ORGS] = null;
      await OPSO_loadOrgs();
      render();
      toast('담당 지정을 해제했습니다.');
    }catch(e){ toast('지정 해제에 실패했습니다.'); }
  };

  App.opsoCreateInvite = async function(){
    const company_name = (($('#opso-inv-company')||{}).value||'').trim();
    const note = (($('#opso-inv-note')||{}).value||'').trim();
    const max_uses = parseInt((($('#opso-inv-uses')||{}).value||'1'), 10) || 1;
    const expires_days = parseInt((($('#opso-inv-days')||{}).value||'30'), 10) || 30;
    try{
      const r = await apiFetch('/admin/client-invites', {
        method:'POST',
        body: JSON.stringify({company_name: company_name||null, note: note||null, max_uses, expires_days})
      });
      if(!r.ok){
        if(r.status === 403) return toast('이 작업을 할 권한이 없습니다.');
        let code = '';
        try{ const j = await r.json(); code = j && j.detail && j.detail.code ? ' ('+j.detail.code+')' : ''; }catch(e){}
        return toast('초대 생성에 실패했습니다.'+code);
      }
      RS[OPSO_INV] = null;
      await OPSO_loadInvites();
      render();
      toast('초대를 생성했습니다.');
    }catch(e){ toast('초대 생성에 실패했습니다.'); }
  };

  App.opsoQr = function(i){
    const c = RS[OPSO_INV];
    const it = c && c.items && c.items[i];
    if(!it) return;
    App.opsoDownloadBlob('/admin/client-invites/'+encodeURIComponent(it.invite_id)+'/qr?fmt=png', 'invite_qr_'+it.code+'.png');
  };

  App.opsoRevoke = async function(i){
    const c = RS[OPSO_INV];
    const it = c && c.items && c.items[i];
    if(!it) return;
    try{
      const r = await apiFetch('/admin/client-invites/'+encodeURIComponent(it.invite_id)+'/revoke', {method:'POST'});
      if(!r.ok){
        if(r.status === 403) return toast('이 작업을 할 권한이 없습니다.');
        let code = '';
        try{ const j = await r.json(); code = j && j.detail && j.detail.code ? ' ('+j.detail.code+')' : ''; }catch(e){}
        return toast('회수에 실패했습니다.'+code);
      }
      RS[OPSO_INV] = null;
      await OPSO_loadInvites();
      render();
      toast('초대를 회수했습니다.');
    }catch(e){ toast('회수에 실패했습니다.'); }
  };

  App.opsoStaffQr = function(){
    App.opsoDownloadBlob('/admin/staff-signup-link/qr?fmt=png', 'staff_signup_qr.png');
  };

  App.opsoDownloadBlob = async function(path, filename){
    try{
      const r = await apiFetch(path);
      if(!r.ok){
        if(r.status === 403) return toast('이 파일을 받을 권한이 없습니다.');
        return toast('파일을 불러올 수 없습니다.');
      }
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
    }catch(e){ toast('파일을 불러올 수 없습니다.'); }
  };

  // ── 탭 등록 ─────────────────────────────────────────────
  if(typeof OPS_TABS !== 'undefined' && Array.isArray(OPS_TABS)){
    OPS_TABS.push({key:'org', label:'조직·초대', render: OPSO_render});
  }
})();
