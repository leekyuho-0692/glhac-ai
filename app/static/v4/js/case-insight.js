/* ===== 케이스 진단(구 화면 이식) — 준비도·진행 여정·업체 확인·LPH 배정 ===== */
RS.cin = RS.cin || {};
RS._cinLoading = RS._cinLoading || new Set();

function CIN_get(kind, cid, path){
  if(!cid) return null;
  const key = kind + ':' + cid;
  if(RS.cin[key] !== undefined) return RS.cin[key];
  if(RS._cinLoading.has(key)) return null;
  RS._cinLoading.add(key);
  (async () => {
    try{
      const r = await apiFetch(path);
      if(r.ok) RS.cin[key] = await r.json();
      else RS.cin[key] = {error:true, status:r.status};
    }catch(e){
      RS.cin[key] = {error:true};
    }
    RS._cinLoading.delete(key);
    render();
  })();
  return null;
}

function CIN_panel(h2, inner){
  return `<section class="panel"><div class="panel-head"><h2>${esc(h2)}</h2></div>${inner}</section>`;
}

function CIN_loadMsg(){
  return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
}

function CIN_errMsg(d){
  if(d && d.status === 403) return '';
  return '<section class="panel"><p class="empty">불러오지 못했습니다.</p></section>';
}

function CIN_readiness(cid){
  if(!cid) return '';
  const d = CIN_get('rd', cid, '/cases/' + cid + '/readiness');
  if(d === null) return CIN_loadMsg();
  if(d.error) return CIN_errMsg(d);

  const read = d.readiness;
  const band = d.band;
  let bandLabel, bandCls;
  if(band === 'ok'){ bandLabel = '양호'; bandCls = 'strong'; }
  else if(band === 'warn'){ bandLabel = '보통'; bandCls = ''; }
  else { bandLabel = '위험'; bandCls = 'attn'; }

  const bd = d.breakdown || {};
  const ct = d.counts || {};

  const inner = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <span class="num mono" style="font-size:30px;font-weight:700">${esc(String(read))}점</span>
      <span class="badge ${bandCls}">${esc(bandLabel)}</span>
    </div>
    <div class="summary">
      <div><span class="k">서류</span><span class="v mono">${esc(String(bd.documents))}</span></div>
      <div><span class="k">SJPH</span><span class="v mono">${esc(String(bd.sjph))}</span></div>
      <div><span class="k">원재료</span><span class="v mono">${esc(String(bd.materials))}</span></div>
      <div><span class="k">심사</span><span class="v mono">${esc(String(bd.audit))}</span></div>
    </div>
    <p class="muted" style="font-size:12px">고위험 원재료 ${esc(String(ct.critical_high))}건 · 증빙 필요 ${esc(String(ct.needs_evidence))}건 · 미해결 지적 ${esc(String(ct.open_findings))}건</p>`;
  return CIN_panel('인증 준비도', inner);
}

function CIN_journey(cid){
  if(!cid) return '';
  const d = CIN_get('jn', cid, '/cases/' + cid + '/journey');
  if(d === null) return CIN_loadMsg();
  if(d.error) return CIN_errMsg(d);

  const stages = d.stages || [];
  const rows = stages.map((st, i) => {
    const status = st.status;
    const badgeCls = status === 'done' ? 'strong' : (status === 'current' ? 'attn' : '');
    const badgeLabel = status === 'done' ? '완료' : (status === 'current' ? '진행 중' : '대기');
    return `<li class="row"><div><b>${i + 1}. ${esc(st.label)}</b></div><span class="badge ${badgeCls}">${esc(badgeLabel)}</span></li>`;
  }).join('');

  const inner = stages.length ? `<ol class="rows">${rows}</ol>` : '<p class="empty">진행 정보가 없습니다.</p>';
  return CIN_panel('전체 진행 여정', inner);
}

function CIN_company(cid){
  if(!cid) return '';
  const d = CIN_get('cc', cid, '/cases/' + cid + '/company-check');
  if(d === null) return CIN_loadMsg();
  if(d.error) return CIN_errMsg(d);

  const caseCompany = esc(d.case_company || '');
  let head;
  if(d.mismatch){
    head = `<div class="note attn"><b>업체명 불일치</b><p>신청서: ${caseCompany} · 서류: ${esc(d.suggested_company || '—')} (유사도 ${Math.round((d.best_similarity || 0) * 100)}%) — 다른 회사 서류가 올라왔는지 확인하세요.</p></div>`;
  }else{
    head = `<div class="note"><b>✓ 업체명 일치</b><p>${caseCompany}</p></div>`;
  }

  const docs = d.applicant_docs || [];
  let docsHtml;
  if(docs.length){
    const trs = docs.map(doc => `<tr>
      <td>${esc(doc.filename)}</td>
      <td>${esc(doc.doc_type)}</td>
      <td>${esc(doc.doc_company || '')}</td>
      <td class="num mono">${Math.round((doc.similarity || 0) * 100)}%</td>
    </tr>`).join('');
    docsHtml = `<div class="tbl-wrap"><table>
      <thead><tr><th>파일</th><th>서류 종류</th><th>서류상 업체명</th><th class="num">유사도</th></tr></thead>
      <tbody>${trs}</tbody>
    </table></div>`;
  }else{
    docsHtml = '<p class="muted">대조할 신청기업 서류(사업자등록증·공장등록증)가 아직 없습니다.</p>';
  }

  let extra = '';
  if(d.facility_review_count > 0){
    extra += `<p class="muted">확인이 필요한 공장 ${esc(String(d.facility_review_count))}곳</p>`;
  }
  if(d.note){
    extra += `<p class="muted">${esc(d.note)}</p>`;
  }

  return CIN_panel('업체 서류 확인', head + docsHtml + extra);
}

function CIN_lph(cid){
  if(!cid) return '';
  const d = CIN_get('lph', cid, '/cases/' + cid + '/lph-assignment');
  if(d === null) return CIN_loadMsg();
  if(d.error) return CIN_errMsg(d);

  const list = Array.isArray(d) ? d : [];
  let listHtml;
  if(list.length){
    listHtml = `<ul class="rows">${list.map(a => `<li class="row"><div><b>${esc(a.lph_name)}</b><span class="muted">${esc(a.auditor_ref || '')} · ${esc(a.source || '')}</span></div></li>`).join('')}</ul>`;
  }else{
    listHtml = '<p class="empty">배정된 LPH 가 없습니다.</p>';
  }

  const role = AUTH && AUTH.role;
  let form = '';
  if(role === 'fatwa_liaison' || role === 'operator' || role === 'admin'){
    form = `<div class="inline" style="margin-top:12px">
      <input id="cin-lph-name" class="in" placeholder="LPH 기관명 (예: LPH Sucofindo)">
      <input id="cin-lph-ref" class="in" placeholder="담당 심사원(선택)">
      <button class="btn btn-primary" onclick="App.cinLphAdd('${cid}')">배정 추가</button>
    </div>`;
  }

  return CIN_panel('LPH(검사기관) 배정', listHtml + form);
}

App.cinLphAdd = async function(cid){
  const name = (($('#cin-lph-name') || {}).value || '').trim();
  if(!name){ toast('LPH 기관명을 입력해 주세요.'); return; }
  const ref = (($('#cin-lph-ref') || {}).value || '').trim();
  try{
    const r = await apiFetch('/cases/' + cid + '/lph-assignment', {
      method: 'POST',
      body: JSON.stringify({ lph_name: name, auditor_ref: ref })
    });
    if(!r.ok){
      if(r.status === 403){ toast('권한이 없습니다.'); return; }
      let code = '';
      try{ const j = await r.json(); if(j && j.detail && j.detail.code) code = j.detail.code; }catch(e){}
      toast('LPH 배정에 실패했습니다.' + (code ? ' (' + code + ')' : ''));
      return;
    }
    delete RS.cin['lph:' + cid];
    render();
    toast('LPH 를 배정했습니다.');
  }catch(e){
    toast('네트워크 오류가 발생했습니다.');
  }
};

(function(){
  const wrap = (v, pick, parts) => {
    const f = VIEWS[v];
    if(typeof f === 'function'){
      VIEWS[v] = () => {
        const h = f();
        let cid = null;
        try{ cid = pick(); }catch(e){ cid = null; }
        return h + (cid ? parts(cid) : '');
      };
    }
  };
  wrap('ent-home', () => ((RS.cases || [])[0] || {}).case_id, cid => CIN_journey(cid) + CIN_readiness(cid));
  wrap('cons-status', () => S.selCompany, cid => CIN_readiness(cid));
  wrap('aud-pre', () => S.selPre, cid => CIN_readiness(cid) + CIN_company(cid));
  wrap('aud-report', () => S.selOnsite, cid => CIN_lph(cid));
})();