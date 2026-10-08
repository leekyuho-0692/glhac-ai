/* ===== 인증 현황 · 보고서 ===== */
const RP_GRANS = [['daily','일일'],['weekly','주간'],['monthly','월간'],['quarterly','분기'],['half','반기'],['yearly','연간']];
const RP_METRICS = [['new_cases','신규 신청'],['eligible','인증 가능 판정'],['contracts_signed','계약 체결'],['contract_amount','계약 금액'],['paid_amount','입금액'],['onsite_audits','현장심사'],['fatwa_submitted','샤리아 상정'],['certificates_issued','인증서 발급']];

Object.assign(I18N.ko, {'nav.adm-report':'인증 현황·보고서'});
if(I18N.en) Object.assign(I18N.en, {'nav.adm-report':'Status Report'});
if(I18N.id) Object.assign(I18N.id, {'nav.adm-report':'Laporan Status'});
if(NAV.adm && NAV.adm.indexOf('adm-flow')>=0) NAV.adm.splice(NAV.adm.indexOf('adm-flow')+1, 0, 'adm-report');

const RP_today = () => { const d=new Date(); return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate()); };
S.rpGran = S.rpGran || 'monthly';
S.rpDate = S.rpDate || RP_today();

const RP_mon = n => (n<0?'−':'+')+Math.abs(n);
const RP_fmt = (v,key) => { v = (v==null?0:v); if(key.indexOf('amount')>=0) return _money(v); return String(v); };
const RP_deltaTag = (cur, prev) => { const d=(cur||0)-(prev||0); const cls = d>=0?'strong':'attn'; return `<span class="badge ${cls}" style="margin-left:4px">${RP_mon(d)}</span>`; };
const RP_bucketLabel = (b) => {
  const s = String(b);
  if(/^\d{4}-\d{2}-\d{2}$/.test(s)){
    const d = new Date(s+'T00:00:00');
    if(isNaN(d.getTime())) return s;
    const wd = ['일','월','화','수','목','금','토'][d.getDay()];
    return s.slice(5)+'('+wd+')';
  }
  if(/^\d{4}-\d{2}$/.test(s)) return s;
  return s;
};
const RP_bars = (trend, key, max) => `<div style="display:flex;align-items:flex-end;gap:2px;height:80px">${(trend||[]).map(t=>{ const v=(t[key]||0); const h = max>0 ? Math.max(2, Math.round(v/max*80)) : 2; return `<div title="${esc(RP_bucketLabel(t.bucket))}: ${esc(RP_fmt(v,key))}" style="flex:1;min-width:6px;height:${h}px;background:var(--c, #2b6cb0);border-radius:2px 2px 0 0"></div>`; }).join('')}</div>`;
const RP_barRow = (trend, key) => { const max = Math.max.apply(null, [0].concat((trend||[]).map(t=>(t[key]||0)))); return RP_bars(trend, key, max); };

async function RP_load(){
  const g = S.rpGran, d = S.rpDate;
  try{ const r = await apiFetch('/admin/reports/period?granularity='+encodeURIComponent(g)+'&date='+encodeURIComponent(d)); RS.rp = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.rp = {error:true}; }
  RS.rpKey = g+'|'+d;
}
async function RP_loadList(){
  try{ const r = await apiFetch('/admin/reports'); RS.rpList = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.rpList = {error:true}; }
}

App.rpGran = function(g){ S.rpGran = g; RS.rp = undefined; render(); };
App.rpDate = function(v){ S.rpDate = v||RP_today(); RS.rp = undefined; render(); };
App.rpShift = function(dir){
  const d = new Date((S.rpDate||RP_today())+'T00:00:00');
  if(isNaN(d.getTime())) return;
  if(dir===0){ S.rpDate = RP_today(); RS.rp = undefined; return render(); }
  const g = S.rpGran;
  if(g==='daily') d.setDate(d.getDate()+dir);
  else if(g==='weekly') d.setDate(d.getDate()+7*dir);
  else if(g==='monthly'){ d.setDate(1); d.setMonth(d.getMonth()+dir); }
  else if(g==='quarterly'){ d.setDate(1); d.setMonth(d.getMonth()+3*dir); }
  else if(g==='half'){ d.setDate(1); d.setMonth(d.getMonth()+6*dir); }
  else if(g==='yearly'){ d.setDate(1); d.setFullYear(d.getFullYear()+dir); }
  S.rpDate = d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
  RS.rp = undefined; render();
};
App.rpPdf = function(id, no){ App.pfxDownload('/admin/reports/'+id+'/pdf', (no||'report')+'.pdf'); };
App.rpCreate = async function(){
  try{
    const r = await apiFetch('/admin/reports', {method:'POST', body:JSON.stringify({granularity:S.rpGran, date:S.rpDate})});
    if(!r.ok){ let code=''; try{ const d=await r.json(); code=(d&&d.detail&&d.detail.code)?('('+d.detail.code+')'):''; }catch(e){} toast('보고서 생성에 실패했습니다.'+code); return; }
    const j = await r.json();
    RS.rpList = undefined;
    await RP_loadList();
    render();
    toast('보고서 '+(j.report_no||'')+' 를 생성했습니다');
  }catch(e){ toast('보고서 생성에 실패했습니다.'); }
};

VIEWS['adm-report'] = () => {
  const key = S.rpGran+'|'+S.rpDate;
  if(RS.rpKey && RS.rpKey!==key) RS.rp = undefined;
  if(!RS.rp){ if(RS._rpLoading!==key){ RS._rpLoading=key; RP_load().then(()=>{ RS._rpLoading=null; render(); }); } }
  if(!RS.rpList && RS._rpLoadingList!==1){ RS._rpLoadingList=1; RP_loadList().then(()=>{ RS._rpLoadingList=null; render(); }); }
  const granBtns = RP_GRANS.map(([k,l])=>`<button class="btn btn-sm ${S.rpGran===k?'btn-primary':''}" onclick="App.rpGran('${k}')">${l}</button>`).join('');
  const ctrl = `<section class="panel"><div class="panel-head"><h2>인증 현황·보고서</h2></div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">${granBtns}</div>
    <div class="inline" style="margin-top:10px"><button class="btn btn-sm" onclick="App.rpShift(-1)">◀ 이전</button><button class="btn btn-sm" onclick="App.rpShift(0)">오늘</button><button class="btn btn-sm" onclick="App.rpShift(1)">다음 ▶</button><input class="in" type="date" id="rp-date" value="${esc(S.rpDate)}" onchange="App.rpDate((($('#rp-date')||{}).value||'').trim())" style="max-width:160px"></div></section>`;
  const d = RS.rp;
  if(!d) return `<div style="display:flex;flex-direction:column;gap:22px">${ctrl}<section class="panel"><p class="empty">불러오는 중…</p></section>${RP_listPanel()}</div>`;
  if(d.error){
    const msg = d.status===403 ? '이 화면을 볼 권한이 없습니다.' : '불러오지 못했습니다.';
    return `<div style="display:flex;flex-direction:column;gap:22px">${ctrl}<section class="panel"><p class="empty">${msg}</p></section>${RP_listPanel()}</div>`;
  }
  const m = d.metrics||{}, pm = d.previous_metrics||{}, p = d.period||{};
  const head = `<div class="panel-head"><h2>${esc((p.label||'')+' · '+S.rpGran)}</h2><div style="display:flex;gap:8px;align-items:center">${p.in_progress?'<span class="badge attn">진행 중 기간(오늘까지 집계)</span>':''}<span class="muted">전기: ${esc((d.previous&&d.previous.label)||'')}</span></div></div>`;
  const cards = `<section class="summary">${RP_METRICS.map(([k,l])=>`<div><span class="k">${l}</span><span class="v">${esc(RP_fmt(m[k],k))}</span><span class="muted" style="font-size:11px">전기 ${esc(RP_fmt(pm[k],k))} ${RP_deltaTag(m[k]||0, pm[k]||0)}</span></div>`).join('')}</section>`;
  const trend = d.trend||[];
  const trendHtml = trend.length ? `
    <div style="margin-top:12px"><div class="muted" style="font-size:11px">신규 신청 / 계약 체결 / 인증서 발급</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:6px">${['new_cases','contracts_signed','certificates_issued'].map(k=>RP_barRow(trend,k)).join('')}</div>
    </div>
    <div style="margin-top:12px"><div class="muted" style="font-size:11px">입금액</div>${RP_barRow(trend,'paid_amount')}</div>
    <div class="muted mono" style="display:flex;gap:8px;font-size:10px;margin-top:4px">${trend.map(t=>`<span style="flex:1;text-align:center">${esc(RP_bucketLabel(t.bucket))}</span>`).join('')}</div>` : '<p class="empty">추이 데이터가 없습니다.</p>';
  const steps = `<fieldset><legend>현재 진행(8단계)</legend><div class="tbl-wrap"><table><thead><tr><th>단계</th><th class="num">건수</th></tr></thead><tbody>${(d.in_progress_steps||[]).map(s=>`<tr><td>${esc(s.label||('단계 '+s.step))}</td><td class="num">${s.count||0}</td></tr>`).join('')||'<tr><td colspan="2" class="empty">없음</td></tr>'}</tbody></table></div></fieldset>`;
  return `<div style="display:flex;flex-direction:column;gap:22px">${ctrl}
    <section class="panel">${head}${cards}
      <fieldset style="margin-top:14px"><legend>추이</legend>${trendHtml}</fieldset>
      <div class="grid-2" style="margin-top:14px">
        ${steps}
        <fieldset><legend>조치 필요</legend><ul class="rows">${RP_actions(d.actions)}</ul></fieldset>
      </div>
      <div class="grid-2" style="margin-top:14px">
        <fieldset><legend>단계 처리 실적</legend><div class="tbl-wrap"><table><thead><tr><th>상태</th><th class="num">건수</th></tr></thead><tbody>${(d.stages||[]).map(s=>`<tr><td>${esc(s.status||'')}</td><td class="num">${s.count||0}</td></tr>`).join('')||'<tr><td colspan="2" class="empty">없음</td></tr>'}</tbody></table></div></fieldset>
        <fieldset><legend>심사원별</legend><div class="tbl-wrap"><table><thead><tr><th>심사원</th><th class="num">건수</th></tr></thead><tbody>${(d.auditors||[]).map(a=>`<tr><td>${esc(a.auditor||'')}</td><td class="num">${a.count||0}</td></tr>`).join('')||'<tr><td colspan="2" class="empty">없음</td></tr>'}</tbody></table></div></fieldset>
      </div>
      <fieldset style="margin-top:14px"><legend>분야별</legend><div class="tbl-wrap"><table><thead><tr><th>분야</th><th class="num">신청</th><th class="num">발급</th></tr></thead><tbody>${(d.sectors||[]).map(s=>`<tr><td>${esc(s.label||s.sector||'')}</td><td class="num">${s.applied||0}</td><td class="num">${s.issued||0}</td></tr>`).join('')||'<tr><td colspan="3" class="empty">없음</td></tr>'}</tbody></table></div></fieldset>
    </section>
    ${RP_listPanel()}
  </div>`;
};

const RP_actions = (a) => {
  a = a||{};
  const n = x => Array.isArray(x)?x.length:0;
  const rows = [
    ['접수 대기', n(a.formal_wait), 'adm-contract'],
    ['서명 대기', n(a.signature_wait), 'adm-contract'],
    ['입금 기한 경과', n(a.payment_overdue), 'adm-contract'],
    ['입금 확인', n(a.payment_verify), 'adm-contract'],
    ['서브 확인 대기', (a.two_person_pending||0), 'adm-home'],
    ['오디터 배정 필요', n(a.auditor_unassigned), 'adm-assign']
  ];
  return rows.map(([l,c,v])=>`<li class="row"><span>${l} <span class="badge ${c>0?'attn':'strong'}">${c}</span></span><span class="row-act"><button class="link" onclick="App.go('${v}')">바로 가기</button></span></li>`).join('');
};

const RP_listPanel = () => {
  const L = RS.rpList;
  let body;
  if(!L) body = '<p class="empty">불러오는 중…</p>';
  else if(L.error) body = `<p class="empty">${L.status===403?'권한이 없습니다.':'불러오지 못했습니다.'}</p>`;
  else if(!(L.items||[]).length) body = '<p class="empty">보고서가 없습니다.</p>';
  else body = `<div class="tbl-wrap"><table><thead><tr><th>번호</th><th>기간</th><th>생성일</th><th></th></tr></thead><tbody>${L.items.map(it=>`<tr><td class="mono">${esc(it.report_no||'')}</td><td>${esc((it.period&&it.period.label)||'')}</td><td class="mono">${esc(it.at||'')}</td><td class="row-act"><button class="link" onclick="App.rpPdf('${esc(it.gen_doc_id)}','${esc(it.report_no)}')">PDF</button></td></tr>`).join('')}</tbody></table></div>`;
  return `<section class="panel"><div class="panel-head"><h2>생성된 보고서</h2><button class="btn btn-primary" onclick="App.rpCreate()">보고서 만들기</button></div>${body}</section>`;
};

NAVC['adm-report'] = () => {
  const d = RS.rp;
  if(!d || d.error) return 0;
  const a = d.actions||{};
  const n = x => Array.isArray(x)?x.length:0;
  return n(a.formal_wait)+n(a.signature_wait)+n(a.payment_overdue)+n(a.payment_verify)+(a.two_person_pending||0)+n(a.auditor_unassigned);
};

const RP_goOrig = App.go;
App.go = async function(v){
  RS.rp = undefined; RS.rpList = undefined; RS.rpKey = undefined;
  return RP_goOrig.apply(this, arguments);
};
