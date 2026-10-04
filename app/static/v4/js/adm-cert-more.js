/* ===== 관리자 인증서 보강: 변경 영향 분석 + SIHALAL 동일성 확인 ===== */
const ADMCM_IMPACT_TYPES = [
  ['supplier_changed','공급사 변경'],
  ['material_added','원재료 추가'],
  ['process_changed','공정 변경'],
  ['material_source_changed','원재료 공급원 변경'],
];
const ADMCM_LOOKUP_TYPES = [
  ['penyelia','사업자 등록 확인'],
  ['lph','LPH 인가 확인'],
  ['lhln','LHLN 인정 확인'],
];

const ADMCM_kindLabel = (k) => (ADMCM_IMPACT_TYPES.find(t=>t[0]===k)||[null,k])[1];
const ADMCM_riskLabel = (lv) => ({high:'높음', medium:'중간', low:'낮음'}[lv] || lv || '-');

async function ADMCM_json(path, opts){
  try{ const r = await apiFetch(path, opts);
    if(!r.ok){ let det=null; try{ det = (await r.json()).detail; }catch(e){}
      return {error:true, status:r.status, code:(det&&det.code)||null}; }
    return await r.json();
  }catch(e){ return {error:true}; }
}
function ADMCM_fail(d, ctx){
  if(d.status===403) return toast('이 작업은 권한이 없습니다.');
  toast((ctx||'요청')+'에 실패했습니다.' + (d.code?' ('+d.code+')':''));
}
function ADMCM_rows(arr, cols, empty){
  if(!arr || !arr.length) return `<p class="empty">${empty}</p>`;
  return `<div class="tbl-wrap"><table><thead><tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${arr.join('')}</tbody></table></div>`;
}

const ADMCM_loadHistory = async function(cid){
  const d = await ADMCM_json('/cases/'+cid+'/certificate/change-impact-history');
  RS.admcmHist[cid] = Array.isArray(d) ? d : (d.error ? {error:true, status:d.status} : []);
};

function ADMCM_historyPanel(cid){
  const h = RS.admcmHist[cid];
  if(h===undefined){ if(RS._admcmH!==cid){ RS._admcmH=cid; ADMCM_loadHistory(cid).then(()=>{ RS._admcmH=null; render(); }); }
    return `<section class="panel"><p class="empty">불러오는 중…</p></section>`; }
  if(h.error) return `<section class="panel"><p class="empty">${h.status===403?'이 화면을 볼 권한이 없습니다.':'이력을 불러오지 못했습니다.'}</p></section>`;
  const rows = (h||[]).map(r=>`<tr>
    <td><span class="tag">${esc(ADMCM_kindLabel(r.change_type))}</span></td>
    <td class="num mono">${esc(String(r.impact_score))}</td>
    <td><span class="badge ${r.risk_level==='high'?'attn':(r.risk_level==='low'?'strong':'')}">${esc(ADMCM_riskLabel(r.risk_level))}</span></td>
    <td>${esc((r.affected_products||[]).join(', ')||'-')}</td>
    <td>${esc((r.required_actions||[]).join(' · ')||'-')}</td>
    <td class="muted mono" style="font-size:11px">${esc(r.created_at||'')}</td>
  </tr>`);
  return `<section class="panel"><div class="panel-head"><h2>변경 영향 분석 이력</h2></div>
    ${ADMCM_rows(rows, ['유형','점수','위험','영향 제품','필요 조치','시각'], '분석 이력이 없습니다.')}
  </section>`;
}

function ADMCM_impactPanel(cid){
  const opts = ADMCM_IMPACT_TYPES.map(([k,l])=>`<option value="${k}">${esc(l)}</option>`).join('');
  const last = RS.admcmLast && RS.admcmLast.case_id===cid ? RS.admcmLast : null;
  const res = last ? `<div class="note ${last.risk_level==='high'?'attn':''}">
    <div class="summary">
      <div><span class="k">영향 점수</span><span class="v mono">${esc(String(last.impact_score))}</span></div>
      <div><span class="k">위험 등급</span><span class="v">${esc(ADMCM_riskLabel(last.risk_level))}</span></div>
      <div><span class="k">재심 필요</span><span class="v">${last.risk_level==='low'?'검토':'재심 검토 필요'}</span></div>
    </div>
    <div class="muted" style="margin-top:8px">영향 범위: ${esc((last.affected_products||[]).join(', ')||'-')}</div>
    <div class="muted">필요 조치: ${esc((last.required_actions||[]).join(' · ')||'-')}</div>
    <div class="muted">근거: ${esc((last.reason_chain||[]).join(' · '))}</div>
  </div>` : '';
  return `<section class="panel"><div class="panel-head"><h2>변경 영향 분석</h2></div>
    <div class="fieldset"><legend>변경 내용</legend>
      <div class="grid-2">
        <div class="field"><label>변경 유형</label>
          <select class="in" id="admcm-type">${opts}</select></div>
        <div class="field"><label>비고</label>
          <input class="in" id="admcm-note" placeholder="변경 설명(선택)"/></div>
      </div>
      <div class="inline" style="margin-top:10px">
        <button class="btn btn-primary" onclick="App.admcmImpact('${cid}')">영향 분석 실행</button>
      </div>
    </div>
    ${res}
  </section>`;
}

function ADMCM_sihalalPanel(cid){
  // 연결(applicant·consultant main.py:16989)·검증(consultant 17004) — 운영자에겐 403 이라 조회만 남긴다
  const canLink = canCall('POST','/cases/{}/sihalal/identity/link'), canVerify = canCall('POST','/sihalal/identity/{}/verify');
  const lk = RS.admcmLookup;
  let result = '';
  if(lk){
    if(lk.error){ result = `<div class="note attn">SIHALAL 조회 실패: ${esc(lk.error)}</div>`; }
    else if(lk.available===false){ result = `<div class="note attn">${esc(lk.note||'SIHALAL 연동이 설정되지 않았습니다')}</div>`; }
    else {
      const rows = (lk.matches||[]).map((m,i)=>`<tr>
        <td>${esc(m.name||'-')}</td>
        <td class="mono">${esc(m.sihalal_id||'-')}</td>
        <td>${canLink?`<button class="btn btn-sm" onclick="App.admcmLink('${cid}',${i},'${esc(lk.type||'penyelia')}')">이 업체로 연결</button>`:''}</td>
      </tr>`);
      const dup = lk.duplicate ? `<div class="note attn">중복 등록 감지: ${esc(lk.duplicate.name)} (유사도 ${esc(String(lk.duplicate.similarity))})</div>` : '';
      result = `${dup}
        <div class="muted">${esc(lk.source||'')} · 총 ${esc(String(lk.total||0))}건</div>
        ${ADMCM_rows(rows, ['업체명','SIHALAL ID',''], '일치하는 등록이 없습니다.')}`;
    }
  }
  const id = RS.admcmId[cid];
  let identity = '';
  if(id && id.external_identity_id){
    identity = `<div class="fieldset"><legend>연결된 SIHALAL 신원</legend>
      <div class="summary">
        <div><span class="k">아이디</span><span class="v mono">${esc(id.external_username||'-')}</span></div>
        <div><span class="k">이메일</span><span class="v mono">${esc(id.external_email||'-')}</span></div>
        <div><span class="k">신청번호</span><span class="v mono">${esc(id.external_application_no||'-')}</span></div>
        <div><span class="k">검증 상태</span><span class="v">${esc(id.verification_status||'-')}</span></div>
      </div>
      ${id.verification_status==='verified'?'<p class="note">식별자 검증 완료.</p>':!canVerify?'':
        `<div class="inline" style="margin-top:10px">
          <input class="in" id="admcm-expect" placeholder="GL-HAC 식별자(이메일/NIB)"/>
          <button class="btn" onclick="App.admcmVerify('${esc(id.external_identity_id)}')">동일성 검증</button>
        </div>`}
    </div>`;
  }
  return `<section class="panel"><div class="panel-head"><h2>SIHALAL 동일성 확인</h2></div>
    ${canLink?'':'<p class="muted" style="font-size:12px">신원 연결·검증은 컨설턴트(업체별 현황 화면)가 합니다.</p>'}
    <div class="fieldset"><legend>조회</legend>
      <div class="inline">
        <input class="in" id="admcm-q" placeholder="업체명/SIHALAL ID" value="${esc(lk&&lk.query||'')}"/>
        <select class="in" id="admcm-ltype">${ADMCM_LOOKUP_TYPES.map(([k,l])=>`<option value="${k}">${esc(l)}</option>`).join('')}</select>
        <button class="btn" onclick="App.admcmLookup('${cid}')">SIHALAL 조회</button>
      </div>
    </div>
    ${result}
    ${identity}
  </section>`;
}

App.admcmImpact = async function(cid){
  const ct = (($('#admcm-type')||{}).value||'').trim();
  if(!ct) return;
  const d = await ADMCM_json('/cases/'+cid+'/certificate/change-impact',
    {method:'POST', body:JSON.stringify({change_type:ct})});
  if(d.error) return ADMCM_fail(d, '영향 분석');
  RS.admcmLast = Object.assign({case_id:cid}, d);
  delete RS.admcmHist[cid];
  render(); toast('영향 분석을 완료했습니다.');
};
App.admcmLookup = async function(cid){
  const q = (($('#admcm-q')||{}).value||'').trim();
  const ty = (($('#admcm-ltype')||{}).value||'penyelia').trim();
  if(q.length<2) return toast('조회어를 2자 이상 입력하세요.');
  RS.admcmLookup = {loading:true, query:q, type:ty}; render();
  const d = await ADMCM_json('/sihalal/lookup?q='+encodeURIComponent(q)+'&type='+encodeURIComponent(ty));
  RS.admcmLookup = d;
  if(d.error && d.status===403) toast('이 화면을 볼 권한이 없습니다.');
  render();
};
App.admcmLink = async function(cid, idx, ltype){
  const lk = RS.admcmLookup || {};
  const m = (lk.matches||[])[idx];
  if(!m) return;
  const body = {external_username: (m.sihalal_id||m.name||null)};
  if(m.name && !m.sihalal_id) body.external_email = null;
  const d = await ADMCM_json('/cases/'+cid+'/sihalal/identity/link',
    {method:'POST', body:JSON.stringify(body)});
  if(d.error) return ADMCM_fail(d, '연결');
  RS.admcmId[cid] = Object.assign({}, RS.admcmId[cid], d, {external_username: body.external_username});
  render(); toast('SIHALAL 신원을 연결했습니다.');
};
App.admcmVerify = async function(eid){
  const exp = (($('#admcm-expect')||{}).value||'').trim();
  if(!exp) return toast('식별자를 입력하세요.');
  const d = await ADMCM_json('/sihalal/identity/'+eid+'/verify',
    {method:'POST', body:JSON.stringify({expected_identifier:exp})});
  if(d.error) return ADMCM_fail(d, '검증');
  Object.keys(RS.admcmId).forEach(cid=>{
    if(RS.admcmId[cid] && RS.admcmId[cid].external_identity_id===eid)
      Object.assign(RS.admcmId[cid], d);
  });
  render();
  toast(d.identifier_match ? '식별자가 일치합니다.' : '식별자 불일치('+ (d.verification_status||'') +')');
};

{
  const prev = VIEWS['adm-cert'];
  if(typeof prev === 'function'){
    if(!RS.admcmHist) RS.admcmHist = {};
    if(!RS.admcmId) RS.admcmId = {};
    VIEWS['adm-cert'] = () => {
      const cid = S.selCert;
      if(!cid) return prev();
      return prev() + ADMCM_impactPanel(cid) + ADMCM_historyPanel(cid) + ADMCM_sihalalPanel(cid);
    };
  }
}
/* 컨설턴트 업체별 현황(cons-status) — SIHALAL 신원 연결·검증의 실제 담당 화면 */
{
  const prev = VIEWS['cons-status'];
  if(typeof prev === 'function'){
    VIEWS['cons-status'] = () => {
      if(!RS.admcmId) RS.admcmId = {};
      const base = prev(); const cid = S.selCompany;   // prev() 가 기본 선택을 정한다
      return base + (cid ? ADMCM_sihalalPanel(cid) : '');
    };
  }
}
