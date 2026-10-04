/* ===== 새 인증 신청(POST /cases) + 기업 신청 전환(케이스 스위처) =====
   서버: POST /cases — require_roles("applicant","consultant") (+admin). body {company_name, scheme, logistics_scope, is_msme}.
   org_id 지정은 admin만 반영 → 컨설턴트가 만든 신청은 컨설턴트 계정의 조직으로 생성된다(고객사 org 지정 미지원).
   기업 화면들은 RS.cases[0]만 본다 → loadEnt 후 선택 케이스(S.entCaseId)를 맨 앞으로 옮겨 전환한다. */

const CN_JASA = [['penyimpanan','보관 (penyimpanan)'],['pengemasan','포장 (pengemasan)'],['pendistribusian','유통 (pendistribusian)']];
const CN_SCHEME_KO = {product:'제품', logistics:'물류'};
const CN_can = () => ['applicant','consultant','admin'].includes(AUTH.role);

// 선택 케이스를 RS.cases 맨 앞으로(기업 화면 공통 기준)
function CN_applySel(){
  const L = RS.cases || []; if(!S.entCaseId) return;
  const i = L.findIndex(c=>c.case_id===S.entCaseId);
  if(i>0){ const c = L[i]; RS.cases = [c].concat(L.slice(0,i), L.slice(i+1)); }
  else if(i<0) S.entCaseId = null;
}
{ const f = loadEnt; loadEnt = async function(){ await f(); CN_applySel(); }; }

App.cnSelEnt = async function(id){
  S.entCaseId = id;
  // 케이스별 키가 없는 기업 모듈 캐시는 비운다(다음 렌더에서 새 케이스로 재조회)
  ['entaCheck','entaInv','entpCase','entpPen','entl','sdcStatus'].forEach(k=>{ RS[k] = null; });
  await loadEnt(); render();
};

function CN_bar(withNew, withSel){
  const L = RS.cases || [];
  const sel = (withSel && L.length>1) ? `<label for="cn-sel" class="muted" style="white-space:nowrap">신청 선택</label>
    <select class="in" id="cn-sel" style="max-width:420px" onchange="App.cnSelEnt(this.value)">${L.map(c=>`<option value="${esc(c.case_id)}" ${c===L[0]?'selected':''}>${esc(c.company_name||'-')} · ${esc(CN_SCHEME_KO[c.scheme]||c.scheme||'-')} · ${esc(c.status||'')} · ${esc(String(c.case_id).slice(0,8))}</option>`).join('')}</select>` : '';
  const btn = (withNew && CN_can()) ? `<button class="btn btn-primary" style="margin-left:auto" onclick="App.cnNew()">＋ 새 인증 신청</button>` : '';
  if(!sel && !btn) return '';
  return `<section class="panel" style="padding:12px 18px"><div class="inline" style="align-items:center;flex-wrap:wrap">${sel}${btn}</div></section>`;
}

App.cnNew = function(){
  const ent = S.role==='ent';
  const pre = ent ? (((RS.cases||[])[0]||{}).company_name||'') : '';
  openModal('새 인증 신청', `
    <div class="field"><label for="cn-co">기업명<span class="req">필수</span></label><input class="in" id="cn-co" value="${esc(pre)}" placeholder="PT 가나다식품"></div>
    <div class="field"><label for="cn-sch">인증 종류</label><select class="in" id="cn-sch" onchange="document.getElementById('cn-jasa').style.display=this.value==='logistics'?'':'none'">
      <option value="product">제품 인증 (product)</option><option value="logistics">물류 서비스 인증 (logistics)</option></select></div>
    <div class="field" id="cn-jasa" style="display:none"><span class="lbl">물류 서비스 범위 · Jasa logistik (1개 이상)</span>
      <div class="inline" style="flex-wrap:wrap;gap:14px">${CN_JASA.map(([k,l],i)=>`<label style="display:inline-flex;gap:6px;align-items:center"><input type="checkbox" name="cn-jasa" value="${k}" ${i===0?'checked':''}> ${l}</label>`).join('')}</div></div>
    <label style="display:inline-flex;gap:6px;align-items:center"><input type="checkbox" id="cn-msme" checked> 중소·영세(UMK/MSME) 사업자</label>
    ${ent?'':'<p class="muted" style="font-size:12.5px">컨설턴트가 만든 신청은 컨설턴트 계정 조직으로 생성됩니다. 고객사가 직접 신청하려면 [신규 인증기업] 초대 링크를 이용하세요.</p>'}
    <div class="inline-msg" id="cn-e"></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" id="cn-go" onclick="App.cnCreate()">신청 생성</button>`);
};

App.cnCreate = async function(){
  const err = m => { const e=$('#cn-e'); if(e) e.textContent=m; };
  const company_name = (($('#cn-co')||{}).value||'').trim();
  if(!company_name) return err('기업명을 입력해 주세요.');
  const scheme = (($('#cn-sch')||{}).value)||'product';
  const body = { company_name, scheme, is_msme: !!(($('#cn-msme')||{}).checked) };
  if(scheme==='logistics'){
    const scope = Array.from(document.querySelectorAll('input[name=cn-jasa]:checked')).map(x=>x.value);
    if(!scope.length) return err('물류 서비스를 최소 1개 선택해 주세요.');
    body.logistics_scope = scope;
  }
  const b = $('#cn-go'); if(b) b.disabled = true;
  try{
    const r = await apiFetch('/cases', {method:'POST', body:JSON.stringify(body)});
    if(!r.ok){ if(b) b.disabled=false; return err(await apiErr(r, '신청 생성에 실패했습니다.')); }
    const d = await r.json();
    closeModal();
    if(S.role==='ent'){ S.entCaseId = d.case_id; await App.cnSelEnt(d.case_id); }
    else { S.selCompany = d.case_id; S.selContract = d.case_id; S.selDocCo = d.case_id; await loadCons(); render(); }
    toast('새 인증 신청을 만들었습니다. ('+String(d.case_id).slice(0,8)+')');
  }catch(e){ if(b) b.disabled=false; err('서버에 연결할 수 없습니다.'); }
};

// ── 화면 확장: 기업 화면 전체에 신청 전환 바, ent-home·cons-home·cons-status 에 [새 인증 신청] ──
(NAV.ent||[]).forEach(v=>{
  const f = VIEWS[v]; if(typeof f!=='function') return;
  VIEWS[v] = () => CN_bar(v==='ent-home', true) + f();
});
['cons-home','cons-status'].forEach(v=>{
  const f = VIEWS[v]; if(typeof f!=='function') return;
  VIEWS[v] = () => CN_bar(true, false) + f();
});
