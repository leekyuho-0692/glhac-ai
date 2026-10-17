/* ===== 기업/컨설턴트 청구·계약 ===== */
const BLE_ST = {draft:'대기', waiting_payment:'청구·입금 대기', need_verification:'입금확인중', paid:'입금완료', expired:'기한 만료'};
const BLE_SECTOR = {food:'식품', cosmetics:'화장품', household:'생활용품', warehouse:'창고', transport:'운송'};
const BLE_NOTICE_KIND = {signature_request:'서명 요청', contract_signed:'계약 체결', payment_due:'입금 안내', payment_reminder:'입금 독촉', payment_confirmed:'입금 확인'};
RS.bleCt = RS.bleCt || {};

const BLE_money = (n) => (Number(n)||0).toLocaleString()+' 원';
const BLE_signedOk = (contract, party) => !!(contract && contract.signatures && contract.signatures.some(s=>s && s.party===party && s.signed_at));

async function BLE_load(cid){
  const g = async (p) => { try{ const r = await apiFetch(p); return r.ok ? await r.json() : null; }catch(e){ return null; } };
  let data;
  try{
    const [quote, contract, invoices, settings, notices] = await Promise.all([
      g('/cases/'+cid+'/quote'),
      g('/cases/'+cid+'/contract'),
      g('/cases/'+cid+'/invoices'),
      g('/billing/settings'),
      g('/cases/'+cid+'/billing/notices')
    ]);
    data = {quote, contract, invoices, settings, notices};
  }catch(e){ data = {error:true}; }
  RS.bleCt[cid] = data;
  return data;
}

function BLE_quotePanel(cid, d){
  const q = d.quote && d.quote.quote;
  if(!q) return `<section class="panel"><div class="panel-head"><h2>견적서 (D-07)</h2></div><p class="empty">견적서가 아직 발송되지 않았습니다.</p></section>`;
  const lines = q.lines || [];
  const rows = lines.map(l=>`<tr><td>${esc(l.label||'')}</td><td class="num">${Number(l.qty)||0}</td><td class="num mono">${BLE_money(l.unit)}</td><td class="num mono">${BLE_money(l.amount)}</td></tr>`).join('');
  return `<section class="panel">
    <div class="panel-head"><h2>견적서 (D-07)</h2><span class="badge">v${Number(q.version)||1}</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>항목</th><th class="num">수량</th><th class="num">단가</th><th class="num">금액</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="4" class="empty">항목이 없습니다.</td></tr>'}</tbody>
    </table></div>
    <dl class="dl">
      <div><dt>소계</dt><dd class="mono">${BLE_money(q.subtotal)}</dd></div>
      <div><dt>부가세</dt><dd class="mono">${BLE_money(q.ppn)}</dd></div>
      <div><dt>총액</dt><dd class="mono"><strong>${BLE_money(q.total)}</strong></dd></div>
    </dl>
    ${q.gen_doc_id?`<div class="row-act"><button class="btn btn-sm" onclick="App.genPdf('${esc(q.gen_doc_id)}','견적서.pdf')">PDF</button></div>`:''}
  </section>`;
}

function BLE_contractPanel(cid, d){
  const ct = d.contract;
  if(!ct) return `<section class="panel"><div class="panel-head"><h2>계약서 서명 (갑: 인증기업)</h2></div><p class="empty">계약서 발송 대기</p></section>`;
  const aOk = BLE_signedOk(ct,'A'), bOk = BLE_signedOk(ct,'B');
  const done = aOk && bOk;
  return `<section class="panel">
    <div class="panel-head"><h2>계약서 서명 (갑: 인증기업)</h2><span class="badge ${done?'strong':''}">${esc(ct.status||'')}</span></div>
    <dl class="dl"><div><dt>계약 금액</dt><dd class="mono">${BLE_money(ct.fee)}</dd></div></dl>
    <div class="chipbar">
      <span class="chip ${aOk?'on':''}">갑 ${aOk?'✔':'대기'}</span>
      <span class="chip ${bOk?'on':''}">을 ${bOk?'✔':'대기'}</span>
    </div>
    ${done?`<p class="note">계약 체결 완료 — 전자서명 확인서(D-09) 발급</p>`:(!aOk&&ct.contract_id?`<div class="row-act"><button class="btn btn-primary" onclick="App.bleSignA('${esc(cid)}','${esc(ct.contract_id)}')">계약서 서명</button></div>`:'')}
  </section>`;
}

function BLE_payPanel(cid, d){
  const s = d.settings || {};
  const ba = s.bank_account || {};
  const inv = (d.invoices||[]).slice().sort((x,y)=>(Number(x.round)||0)-(Number(y.round)||0));
  const rows = inv.map(iv=>{
    const od = iv.status==='waiting_payment' && iv.due_date && new Date(iv.due_date).getTime() < Date.now();
    return `<tr>
      <td class="mono">${esc(iv.invoice_no||'')}</td>
      <td class="num mono">${BLE_money(iv.total)}</td>
      <td>${od?`<span class="badge attn">기한 경과</span>`:`<span class="badge">${esc(BLE_ST[iv.status]||iv.status||'')}</span>`}</td>
      <td class="mono">${esc(iv.due_date||'')}</td>
      <td class="row-act">${iv.status==='waiting_payment'?`<button class="btn btn-sm" onclick="App.blePaidNotice('${esc(cid)}','${esc(iv.invoice_id)}')">입금 완료 알림</button>`:''}</td>
    </tr>`;
  }).join('');
  return `<section class="panel">
    <div class="panel-head"><h2>결제 일정 · 입금 안내</h2></div>
    <dl class="dl">
      <div><dt>은행</dt><dd>${esc(ba.bank||'-')}</dd></div>
      <div><dt>계좌번호</dt><dd class="mono">${esc(ba.number||'-')}</dd></div>
      <div><dt>예금주</dt><dd>${esc(ba.holder||'-')}</dd></div>
      <div><dt>입금 기한</dt><dd>${esc(s.due_days!=null?String(s.due_days):'-')}일</dd></div>
    </dl>
    <div class="tbl-wrap"><table>
      <thead><tr><th>회차</th><th class="num">금액</th><th>상태</th><th>기한</th><th></th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="empty">청구 내역이 없습니다.</td></tr>'}</tbody>
    </table></div>
  </section>`;
}

function BLE_noticePanel(d){
  const items = ((d.notices&&d.notices.items)||[]).slice(0,10);
  const rows = items.map(n=>`<li class="row"><span class="mono muted" style="min-width:130px">${esc(n.at||'')}</span><span class="tag">${esc(n.kind_label||BLE_NOTICE_KIND[n.kind]||n.kind||'')}</span><span>${esc(n.message||'')}</span></li>`).join('');
  return `<section class="panel"><div class="panel-head"><h2>받은 고지·알림</h2></div>${rows?`<ul class="rows">${rows}</ul>`:'<p class="empty">받은 고지가 없습니다.</p>'}</section>`;
}

function BLE_entPanel(){
  const c = (RS.cases||[])[0];
  if(!c) return '<section class="panel"><p class="empty">대상 업체가 없습니다.</p></section>';
  const cid = c.case_id, d = RS.bleCt[cid];
  if(!d){
    if(RS._bleLoading!==cid){ RS._bleLoading=cid; BLE_load(cid).then(()=>{ RS._bleLoading=null; render(); }); }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(d.error) return '<section class="panel"><p class="empty">불러오지 못했습니다.</p></section>';
  return `<div style="display:flex;flex-direction:column;gap:22px">${BLE_quotePanel(cid,d)}${BLE_contractPanel(cid,d)}${BLE_payPanel(cid,d)}${BLE_noticePanel(d)}</div>`;
}

function BLE_consPanel(){
  const list = RS.cases||[];
  const c = (S.selCase && list.find(x=>x.case_id===S.selCase)) || list[0];
  if(!c) return '<section class="panel"><p class="empty">대상 업체가 없습니다.</p></section>';
  const cid = c.case_id, d = RS.bleCt[cid];
  if(!d){
    if(RS._bleLoading!==cid){ RS._bleLoading=cid; BLE_load(cid).then(()=>{ RS._bleLoading=null; render(); }); }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if(d.error) return '<section class="panel"><p class="empty">불러오지 못했습니다.</p></section>';
  const q = d.quote||{};
  const quote = q.quote;
  const pending = q.pending_change;
  const ct = d.contract;
  const aOk = BLE_signedOk(ct,'A'), bOk = BLE_signedOk(ct,'B');
  return `<div style="display:flex;flex-direction:column;gap:22px">
    <section class="panel">
      <div class="panel-head"><h2>견적서</h2>${quote?`<span class="badge">v${Number(quote.version)||1}</span>`:''}</div>
      ${quote?
        `<dl class="dl"><div><dt>총액</dt><dd class="mono">${BLE_money(quote.total)}</dd></div></dl>`:
        '<p class="empty">견적서가 발급되지 않았습니다.</p>'}
      ${pending?'<p class="note attn">견적 변경 요청 대기 중입니다.</p>':''}
      <div class="row-act"><button class="btn" onclick="App.bleChangeReq('${esc(cid)}')">견적 변경 요청</button></div>
    </section>
    <section class="panel">
      <div class="panel-head"><h2>계약 서명 진행</h2></div>
      <div class="chipbar"><span class="chip ${aOk?'on':''}">갑 ${aOk?'✔':'대기'}</span><span class="chip ${bOk?'on':''}">을 ${bOk?'✔':'대기'}</span></div>
      <p class="note">컨설턴트는 계약 당사자가 아니라 서명하지 않습니다.</p>
    </section>
  </div>`;
}

if(typeof VIEWS!=='undefined' && VIEWS['ent-contract']){ const f=VIEWS['ent-contract']; VIEWS['ent-contract']=()=>BLE_entPanel()+f(); }
if(typeof VIEWS!=='undefined' && VIEWS['cons-contract']){ const f=VIEWS['cons-contract']; VIEWS['cons-contract']=()=>BLE_consPanel()+f(); }

if(typeof App.go==='function'){ const g=App.go; App.go=function(v){ RS.bleCt={}; RS._bleLoading=null; return g.apply(this,arguments); }; }

App.bleSignA = function(cid, contractId){
  openModal('계약서 서명', `<div class="field"><label>서명자 이름</label><input id="ble-signer" class="in"></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.bleSignASubmit('${esc(cid)}','${esc(contractId)}')">서명</button>`);
};
App.bleSignASubmit = async function(cid, contractId){
  const name = (($('#ble-signer')||{}).value||'').trim();
  if(!name) return toast('서명자 이름을 입력하세요.');
  try{
    const r = await apiFetch('/contracts/'+encodeURIComponent(contractId)+'/sign?party=A&name='+encodeURIComponent(name), {method:'POST', body:JSON.stringify({})});
    if(!r.ok){
      let code='';
      try{ const j=await r.json(); code=(j&&j.detail&&j.detail.code)||''; }catch(e){}
      if(r.status===409 && code==='ALREADY_SIGNED') return toast('이미 서명되었습니다.');
      return toast('서명에 실패했습니다.'+(code?` (${code})`:''));
    }
  }catch(e){ return toast('서명에 실패했습니다.'); }
  App.closeModal(); RS.bleCt={}; await BLE_load(cid); render(); toast('서명이 완료되었습니다.');
};

App.blePaidNotice = function(cid, invoiceId){
  openModal('입금 완료 알림', `<div class="field"><label>메모 (선택)</label><textarea id="ble-note" class="in"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.blePaidNoticeSubmit('${esc(cid)}','${esc(invoiceId)}')">알림</button>`);
};
App.blePaidNoticeSubmit = async function(cid, invoiceId){
  const note = (($('#ble-note')||{}).value||'').trim();
  const body = note ? {note} : {};
  try{
    const r = await apiFetch('/invoices/'+encodeURIComponent(invoiceId)+'/paid-notice', {method:'POST', body:JSON.stringify(body)});
    if(!r.ok){
      let code='';
      try{ const j=await r.json(); code=(j&&j.detail&&j.detail.code)||''; }catch(e){}
      return toast('입금 완료 알림에 실패했습니다.'+(code?` (${code})`:''));
    }
  }catch(e){ return toast('입금 완료 알림에 실패했습니다.'); }
  App.closeModal(); RS.bleCt={}; await BLE_load(cid); render(); toast('입금 완료를 알렸습니다. 관리자가 확인합니다.');
};

App.bleChangeReq = function(cid){
  openModal('견적 변경 요청', `<div class="field"><label class="req">사유</label><textarea id="ble-reason" class="in"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.bleChangeReqSubmit('${esc(cid)}')">요청</button>`);
};
App.bleChangeReqSubmit = async function(cid){
  const reason = (($('#ble-reason')||{}).value||'').trim();
  if(!reason) return toast('사유를 입력하세요.');
  try{
    const r = await apiFetch('/cases/'+encodeURIComponent(cid)+'/quote/change-request', {method:'POST', body:JSON.stringify({reason})});
    if(!r.ok){
      let code='';
      try{ const j=await r.json(); code=(j&&j.detail&&j.detail.code)||''; }catch(e){}
      if(r.status===409 && code==='QUOTE_NOT_ISSUED') return toast('견적서가 발급되지 않아 변경 요청을 할 수 없습니다.');
      return toast('변경 요청에 실패했습니다.'+(code?` (${code})`:''));
    }
  }catch(e){ return toast('변경 요청에 실패했습니다.'); }
  App.closeModal(); RS.bleCt={}; await BLE_load(cid); render(); toast('견적 변경을 요청했습니다.');
};
