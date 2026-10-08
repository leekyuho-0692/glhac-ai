/* ===== 심사원 배치통보서(구 화면 이식) ===== */
RS.dep = RS.dep || {};

const DEP_LABEL = {
  id_no: '사업자 식별번호(NIB)',
  company_name: '업체명',
  office_address: '사무소 주소',
  factory_address: '공장 주소',
  product_service: '제품·서비스',
  trademark: '상표',
  audit_date: '심사일',
  lph_name: 'LPH 기관'
};

const DEP_ROW_ORDER = ['id_no', 'company_name', 'office_address', 'factory_address', 'product_service', 'trademark', 'audit_date', 'lph_name'];

async function DEP_load(cid){
  try{
    const r = await apiFetch('/cases/' + cid + '/audit-deployment');
    RS.dep[cid] = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){
    RS.dep[cid] = {error:true};
  }
}

function DEP_allowed(){
  return ['auditor', 'fatwa_liaison', 'operator', 'admin', 'consultant'].includes(AUTH && AUTH.role);
}

function DEP_panel(cid){
  if(!cid) return '';
  if(!DEP_allowed()) return '';

  const d = RS.dep[cid];
  let body;

  if(!d){
    if(RS._depLoading !== cid){
      RS._depLoading = cid;
      DEP_load(cid).then(()=>{ RS._depLoading = null; render(); });
    }
    body = '<section class="panel"><p class="empty">불러오는 중…</p></section>';
    return body;
  }
  if(d.error){
    const msg = (d.status === 403) ? '권한이 없습니다.' : '배치통보서 정보를 불러오지 못했습니다.';
    return `<section class="panel"><p class="empty">${esc(msg)}</p></section>`;
  }

  let inner = '';

  if(Array.isArray(d.missing) && d.missing.length){
    const labels = d.missing.map(k => DEP_LABEL[k] || k).join(', ');
    inner += `<div class="note attn"><b>빈칸 ${d.missing.length}개</b><p>${esc(labels)}. 업체 정보·현장 일정·배정에서 채우면 양식에 반영됩니다.</p></div>`;
  }

  const rows = DEP_ROW_ORDER.map(k => {
    const v = d[k];
    const val = (v === null || v === undefined || v === '') ? '<span class="muted">—</span>' : esc(String(v));
    return `<tr><th>${esc(DEP_LABEL[k] || k)}</th><td>${val}</td></tr>`;
  }).join('');
  inner += `<div class="tbl-wrap"><table><tbody>${rows}</tbody></table></div>`;

  inner += '<h3>심사팀</h3>';
  const team = Array.isArray(d.team) ? d.team : [];
  if(!team.length){
    inner += '<p class="empty">배정된 심사원이 없습니다.</p>';
  }else{
    const trows = team.map(m => `<tr><td class="num mono">${esc(String(m.no == null ? '' : m.no))}</td><td>${esc(m.name || '')}</td><td>${esc(m.position || '')}</td><td class="mono">${esc(m.cert_no || '')}</td></tr>`).join('');
    inner += `<div class="tbl-wrap"><table><thead><tr><th>No</th><th>성명</th><th>직위</th><th>자격번호</th></tr></thead><tbody>${trows}</tbody></table></div>`;
  }

  inner += '<h3>서명</h3>';
  const signers = Array.isArray(d.signers) ? d.signers : [];
  inner += `<ul class="rows">${signers.map(s => `<li class="row"><div><b>${esc(s.title || '')}</b><span class="muted">${esc(s.name || '')}</span></div></li>`).join('')}</ul>`;

  if(d.notice){
    inner += `<p class="muted" style="font-size:12px;margin-top:8px">${esc(d.notice)}</p>`;
  }

  return `<section class="panel"><div class="panel-head"><h2>심사원 배치통보서</h2><div class="row-act">
   <button class="btn btn-sm" onclick="App.depPdf('${cid}')">PDF 내려받기</button>
   <button class="btn btn-sm btn-ghost" onclick="App.depReload('${cid}')">새로고침</button></div></div>${inner}</section>`;
}

App.depPdf = async function(cid){
  try{
    const r = await apiFetch('/cases/' + cid + '/audit-deployment.pdf');
    if(!r.ok) return toast('PDF 를 만들지 못했습니다.');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'audit_deployment.pdf';
    document.body.appendChild(a);
    a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
    toast('배치통보서를 내려받았습니다.');
  }catch(e){
    toast('PDF 를 만들지 못했습니다.');
  }
};

App.depReload = function(cid){
  delete RS.dep[cid];
  render();
};

(function(){
  const wrap = (v, pick) => {
    const f = VIEWS[v];
    if(typeof f === 'function'){
      VIEWS[v] = () => { const h = f(); return h + DEP_panel(pick()); };
    }
  };
  wrap('aud-schedule', () => S.selSched);
  wrap('aud-report', () => S.selOnsite);
})();