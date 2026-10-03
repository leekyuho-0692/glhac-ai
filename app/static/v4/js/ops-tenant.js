/* ===== 운영 도구: 테넌트·도메인·브랜딩·정산 ===== */
async function OPST_req(path, opts){
  try{
    const r = await apiFetch(path, opts);
    if(r.ok) return {ok:true, data: await r.json()};
    let d = null; try{ d = await r.json(); }catch(_){}
    return {ok:false, status:r.status, code:(d&&d.detail&&d.detail.code)||(d&&d.code)||null, data:d};
  }catch(e){ return {ok:false, status:0, code:null}; }
}
function OPST_fail(r, msg){
  if(r.status===403) return toast('이 작업은 권한이 없습니다.');
  toast(msg + (r.code ? ' ('+r.code+')' : ''));
}
function OPST_val(sel){ const el = $(sel); return ((el||{}).value||'').trim(); }
function OPST_esc(v){ return esc(v==null?'':String(v)); }

async function OPST_loadOrgs(){
  const r = await OPST_req('/admin/orgs');
  if(r.ok){
    const d = r.data;
    const list = Array.isArray(d) ? d : ((d && Array.isArray(d.items)) ? d.items : []);
    RS.opstOrgs = {list:list};
  } else {
    RS.opstOrgs = {error:true, status:r.status};
  }
}
async function OPST_loadTenant(orgId){
  if(!orgId) return;
  RS.opstTenant = RS.opstTenant || {};
  const r = await OPST_req('/admin/orgs/'+encodeURIComponent(orgId)+'/tenant');
  RS.opstTenant[orgId] = r.ok ? r.data : {error:true, status:r.status};
}
async function OPST_loadSettle(){
  const r = await OPST_req('/admin/settlement');
  RS.opstSettle = r.ok ? r.data : {error:true, status:r.status};
}

const OPST_STATUS_LABEL = {draft:'초안', domain_pending:'검증 대기', active:'활성', suspended:'정지'};
function OPST_status(st){ return OPST_STATUS_LABEL[st] || OPST_str(st); }
function OPST_str(s){ return s==null?'':String(s); }
function OPST_chip(st){
  const on = st==='active';
  return `<span class="badge ${on?'strong':'attn'}">${OPST_esc(OPST_status(st))}</span>`;
}

function OPST_tenantView(){
  if(!RS.opstOrgs){
    if(!RS._opstOrgsLoading){ RS._opstOrgsLoading=true; OPST_loadOrgs().then(()=>{ RS._opstOrgsLoading=false; render(); }); }
    return '<p class="empty">불러오는 중…</p>';
  }
  if(RS.opstOrgs && RS.opstOrgs.error) return `<p class="empty">${RS.opstOrgs.status===403?'권한이 없습니다.':'조직 목록을 불러오지 못했습니다.'}</p>`;
  const list = (RS.opstOrgs && RS.opstOrgs.list) || [];
  if(!list.length) return '<p class="empty">조직이 없습니다.</p>';
  const cur = (S.opstOrg && list.find(o=>o.org_id===S.opstOrg)) ? S.opstOrg : (S.opstOrg = list[0].org_id);
  const opts = list.map(o=>`<option value="${OPST_esc(o.org_id)}" ${o.org_id===cur?'selected':''}>${OPST_esc(o.name||o.org_id)} (${o.users||0}사용자·${o.cases||0}케이스)</option>`).join('');
  const t = (RS.opstTenant && RS.opstTenant[cur]) || null;
  let body;
  if(!t){ if(!RS.opstTenant) RS.opstTenant={}; if(RS._opstTenantLoading!==cur){ RS._opstTenantLoading=cur; OPST_loadTenant(cur).then(()=>{ RS._opstTenantLoading=null; render(); }); } body='<p class="empty">불러오는 중…</p>'; }
  else if(t.error) body = `<p class="empty">${t.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p>`;
  else body = OPST_tenantDetail(cur, t);
  const header = `<div class="panel-head"><h2>테넌트 도메인·브랜딩</h2>
    <div class="inline"><label class="muted" for="opst-org">조직</label>
    <select id="opst-org" class="in" onchange="App.opstOrgSel(this.value)">${opts}</select>
    <button class="btn btn-sm" onclick="App.opstRefresh()">새로고침</button></div></div>`;
  return `<section class="panel">${header}${body}</section>`;
}
function OPST_tenantDetail(orgId, t){
  const b = t.branding || {};
  const verify = t.verify_txt_record || '';
  const dl = `<div class="dl">
    <div><dt>조직 ID</dt><dd class="mono">${OPST_esc(t.org_id)}</dd></div>
    <div><dt>도메인</dt><dd class="mono">${OPST_esc(t.domain||'—')}</dd></div>
    <div><dt>도메인 상태</dt><dd>${OPST_chip(t.domain_status)}</dd></div>
    <div><dt>검증 시각</dt><dd class="mono">${OPST_esc(t.verified_at||'—')}</dd></div>
    <div><dt>브랜드명</dt><dd>${OPST_esc(b.brand_name||'—')}</dd></div>
    <div><dt>주색상</dt><dd class="mono">${OPST_esc(b.primary_color||'—')}</dd></div>
    <div><dt>로고 URL</dt><dd class="mono" style="word-break:break-all">${OPST_esc(b.logo_url||'—')}</dd></div>
    <div><dt>로케일</dt><dd>${OPST_esc(b.locale||'—')}</dd></div>
    <div><dt>지원 메일</dt><dd class="mono">${OPST_esc(b.support_email||'—')}</dd></div>
  </div>`;
  const domainBox = `<fieldset><legend>도메인</legend>
    <div class="inline"><input id="opst-domain" class="in" placeholder="example.com" value="${OPST_esc(t.domain||'')}">
      <button class="btn" onclick="App.opstSetDomain('${OPST_esc(orgId)}')">등록</button>
      <button class="btn" onclick="App.opstVerifyDomain('${OPST_esc(orgId)}')">도메인 검증</button>
      <button class="btn" onclick="App.opstActivateDomain('${OPST_esc(orgId)}','active')">도메인 활성화</button>
      <button class="btn btn-ghost" onclick="App.opstActivateDomain('${OPST_esc(orgId)}','suspended')">정지</button></div>
    ${verify ? `<div class="note attn">DNS TXT 레코드 값: <code class="mono">${OPST_esc(verify)}</code></div>` : ''}
  </fieldset>`;
  const brandBox = `<fieldset><legend>화이트라벨 브랜딩</legend>
    <div class="grid-2">
      <div class="field"><label>표시 이름</label><input id="opst-b-brand" class="in" maxlength="60" value="${OPST_esc(b.brand_name||'')}"></div>
      <div class="field"><label>주색상 (#RRGGBB)</label><input id="opst-b-color" class="in" placeholder="#1a73e8" value="${OPST_esc(b.primary_color||'')}"></div>
      <div class="field"><label>로고 URL</label><input id="opst-b-logo" class="in" placeholder="https://… 또는 /static/…" value="${OPST_esc(b.logo_url||'')}"></div>
      <div class="field"><label>로케일</label>
        <select id="opst-b-locale" class="in">
          <option value="" ${!b.locale?'selected':''}>—</option>
          <option value="ko" ${b.locale==='ko'?'selected':''}>ko</option>
          <option value="en" ${b.locale==='en'?'selected':''}>en</option>
          <option value="id" ${b.locale==='id'?'selected':''}>id</option>
        </select></div>
      <div class="field"><label>지원 메일</label><input id="opst-b-mail" class="in" maxlength="80" value="${OPST_esc(b.support_email||'')}"></div>
    </div>
    <div style="margin-top:8px"><button class="btn btn-primary" onclick="App.opstSaveBranding('${OPST_esc(orgId)}')">브랜딩 저장</button></div>
  </fieldset>`;
  return dl + domainBox + brandBox;
}

function OPST_settleView(){
  if(!RS.opstSettle){
    if(!RS._opstSettleLoading){ RS._opstSettleLoading=true; OPST_loadSettle().then(()=>{ RS._opstSettleLoading=false; render(); }); }
    return '<p class="empty">불러오는 중…</p>';
  }
  const s = RS.opstSettle;
  if(s.error) return `<p class="empty">${s.status===403?'권한이 없습니다.':'정산 정보를 불러오지 못했습니다.'}</p>`;
  const rows = [
    ['청구 합계', s.invoiced_idr || _money(s.invoiced, 'IDR')],
    ['결제 완료', s.paid_idr || _money(s.paid, 'IDR')],
    ['환불 합계', s.refunded_idr || _money(s.refunded, 'IDR')],
    ['실 순정산', s.net_settled_idr || _money(s.net_settled, 'IDR')],
    ['미결제(대기)', s.outstanding_idr || _money(s.outstanding, 'IDR')],
  ];
  const summary = `<div class="summary">${rows.map(([k,v])=>`<div><span class="k">${k}</span><span class="v mono">${OPST_esc(v)}</span></div>`).join('')}</div>`;
  const tbl = `<div class="tbl-wrap"><table><thead><tr><th>항목</th><th class="num">건수/금액</th></tr></thead><tbody>
    <tr><td>인보이스 건수</td><td class="num mono">${OPST_esc(String(s.invoice_count||0))}</td></tr>
    <tr><td>대기 환불 요청</td><td class="num mono">${OPST_esc(String(s.pending_refunds||0))}</td></tr>
  </tbody></table></div>`;
  return `<section class="panel"><div class="panel-head"><h2>정산 현황</h2>
    <button class="btn btn-sm" onclick="App.opstSettleReload()">새로고침</button></div>
    ${summary}${tbl}</section>`;
}

function OPST_render(){
  const cur = S.opstTab || 'tenant';
  const tabs = `<div class="tabs">
    <button class="${cur==='tenant'?'on':''}" onclick="App.opstTab('tenant')">테넌트·도메인·브랜딩</button>
    <button class="${cur==='settle'?'on':''}" onclick="App.opstTab('settle')">정산</button>
  </div>`;
  const body = cur==='settle' ? OPST_settleView() : OPST_tenantView();
  return `<div style="display:flex;flex-direction:column;gap:16px">${tabs}${body}</div>`;
}

App.opstTab = function(k){ S.opstTab = k; render(); };
App.opstOrgSel = function(id){ S.opstOrg = id; if(!RS.opstTenant || !RS.opstTenant[id]){ OPST_loadTenant(id).then(render); } else render(); };
App.opstRefresh = function(){ RS.opstOrgs=null; RS.opstTenant={}; S.opstOrg=null; RS._opstOrgsLoading=true; OPST_loadOrgs().then(()=>{ RS._opstOrgsLoading=false; render(); }); };
App.opstSettleReload = function(){ RS.opstSettle=null; RS._opstSettleLoading=true; OPST_loadSettle().then(()=>{ RS._opstSettleLoading=false; render(); }); };

App.opstSetDomain = async function(orgId){
  const dom = OPST_val('#opst-domain');
  if(!dom) return toast('도메인을 입력하세요.');
  const r = await OPST_req('/admin/orgs/'+encodeURIComponent(orgId)+'/domain', {method:'POST', body:JSON.stringify({domain:dom})});
  if(!r.ok) return OPST_fail(r, '도메인 등록에 실패했습니다.');
  toast('도메인이 등록되었습니다. DNS TXT 레코드를 설정한 뒤 검증하세요.');
  await OPST_loadTenant(orgId); render();
};
App.opstVerifyDomain = async function(orgId){
  const r = await OPST_req('/admin/orgs/'+encodeURIComponent(orgId)+'/verify-domain', {method:'POST', body:JSON.stringify({})});
  if(!r.ok){
    if(r.code==='DNS_RESOLVER_UNAVAILABLE') return toast('서버에 DNS 조회 기능이 없습니다. 관리자 수동 활성화를 사용하세요.');
    if(r.code==='TXT_RECORD_NOT_FOUND') return toast('TXT 레코드가 아직 조회되지 않습니다.');
    if(r.code==='DNS_LOOKUP_FAILED') return toast('DNS 조회에 실패했습니다.');
    return OPST_fail(r, '도메인 검증에 실패했습니다.');
  }
  toast('도메인 검증 완료 — 활성 상태입니다.');
  await OPST_loadTenant(orgId); render();
};
App.opstActivateDomain = async function(orgId, status){
  const r = await OPST_req('/admin/orgs/'+encodeURIComponent(orgId)+'/activate-domain', {method:'POST', body:JSON.stringify({status:status})});
  if(!r.ok) return OPST_fail(r, '상태 변경에 실패했습니다.');
  toast(status==='active'?'도메인이 활성화되었습니다.':'도메인이 정지되었습니다.');
  await OPST_loadTenant(orgId); render();
};
App.opstSaveBranding = async function(orgId){
  const body = {
    brand_name: OPST_val('#opst-b-brand'),
    primary_color: OPST_val('#opst-b-color'),
    logo_url: OPST_val('#opst-b-logo'),
    locale: OPST_val('#opst-b-locale'),
    support_email: OPST_val('#opst-b-mail'),
  };
  const r = await OPST_req('/admin/orgs/'+encodeURIComponent(orgId)+'/branding', {method:'POST', body:JSON.stringify(body)});
  if(!r.ok) return OPST_fail(r, '브랜딩 저장에 실패했습니다.');
  toast('브랜딩이 저장되었습니다.');
  await OPST_loadTenant(orgId); render();
};

if(typeof OPS_TABS !== "undefined") OPS_TABS.push({key:"tenant", label:"테넌트·정산", render: OPST_render});
