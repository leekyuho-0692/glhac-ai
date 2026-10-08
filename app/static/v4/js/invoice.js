/* ===== 청구서 발행 · 결제 기록 · 결제 내역 =====
   POST  /cases/{id}/invoices      — consultant (+admin). 계약 confirmed/invoiced/paid 후에만. body {service_type, amount, line_items[{name,qty,unit_price,amount}]}
   POST  /invoices/{id}/payment    — applicant·consultant·operator (+admin). body {method: bank_transfer|va|card|manual, amount, depositor_name, reference}
                                     va/card + 금액 일치 → 자동 paid(→ _on_invoice_paid 모의심사 진입 게이트), 그 외 need_verification
   PATCH /invoices/{id}/pay        — applicant·consultant (+admin). 간이 결제표시(paid)
   PATCH /invoices/{id}/status     — operator (+admin). need_verification → paid 확정
   GET   /cases/{id}/payments      — 케이스 접근 가능자 */

const IV_PAY_METHODS = [['bank_transfer','계좌이체'],['va','가상계좌(VA)'],['card','카드'],['manual','수기']];
const IV_ST = {waiting_payment:'입금 대기', need_verification:'검증 필요', payment_processing:'처리중', paid:'입금 완료', rejected:'거절', refunded:'환불', expired:'기한만료', draft:'작성중', unpaid:'입금 대기'};
const IV_role = (...roles) => roles.concat('admin').includes(AUTH.role);
RS.ivPay = RS.ivPay || {};

// 결제 관련 캐시 무효화(기존 모듈 캐시 포함)
function IV_drop(cid){
  if(cid){ delete RS.ivPay[cid]; if(RS.entCt) delete RS.entCt[cid]; delete RS['payd_inv_'+cid]; }
  RS.entaInv = null;
}
async function IV_load(cid){
  try{
    const [ri, rp] = await Promise.all([apiFetch('/cases/'+cid+'/invoices'), apiFetch('/cases/'+cid+'/payments')]);
    RS.ivPay[cid] = { invoices: ri.ok?await ri.json():[], payments: rp.ok?await rp.json():[] };
  }catch(e){ RS.ivPay[cid] = {invoices:[], payments:[]}; }
}

// ── 청구서 발행(컨설턴트) ──
function IV_issuePanel(cid){
  if(!cid || !IV_role('consultant')) return '';
  return `<section class="panel"><div class="panel-head"><h2>청구서 발행</h2><span class="muted">계약 최종 확인 후 발행 가능</span></div>
    <p class="muted">서비스 유형과 청구 항목(항목명·수량·단가)을 입력하면 DPP 합계에 PPN 11%가 더해져 청구됩니다.</p>
    <div class="row-act" style="justify-content:flex-end"><button class="btn btn-primary" onclick="App.ivNew('${esc(cid)}')">＋ 청구서 발행</button></div></section>`;
}
App.ivNew = async function(cid){
  let rates = [{service_type:'pre_audit', label:'사전심사', amount:0}, {service_type:'onsite', label:'현장심사', amount:0}];
  try{ const r = await apiFetch('/billing/defaults'); if(r.ok){ const d = await r.json(); if((d.rates||[]).length) rates = d.rates; } }catch(e){}
  S.ivRates = rates; S.ivCase = cid; S.ivType = rates[0].service_type;
  S.ivItems = [{name:'기본 심사료', qty:1, unit_price:Number(rates[0].amount)||0}];
  openModal('청구서 발행', `
    <div class="field"><label for="iv-type">서비스 유형</label><select class="in" id="iv-type" onchange="App.ivType(this.value)">${rates.map(o=>`<option value="${esc(o.service_type)}">${esc(o.label||o.service_type)}${o.amount?(' — '+_money(o.amount,'IDR')):''}</option>`).join('')}</select></div>
    <div class="tbl-wrap"><table><thead><tr><th style="min-width:160px">항목명</th><th style="width:70px">수량</th><th style="width:130px">단가</th><th style="width:130px">금액</th><th></th></tr></thead><tbody id="iv-rows"></tbody></table></div>
    <div class="row-act"><button class="btn btn-sm" onclick="App.ivAdd()">＋ 행 추가</button></div>
    <dl class="dl"><div><dt>소계(DPP)</dt><dd class="mono" id="iv-dpp">-</dd></div><div><dt>부가세(PPN 11%)</dt><dd class="mono" id="iv-ppn">-</dd></div><div><dt>합계</dt><dd class="mono" id="iv-tot">-</dd></div></dl>
    <div class="inline-msg" id="iv-e"></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" id="iv-go" onclick="App.ivSubmit()">청구서 발행</button>`, true);
  IV_rows();
};
function IV_rows(){
  const tb = $('#iv-rows'); if(!tb) return;
  tb.innerHTML = (S.ivItems||[]).map((it,i)=>`<tr>
    <td><input class="in" value="${esc(it.name)}" oninput="App.ivEdit(${i},'name',this.value)"></td>
    <td><input class="in" type="number" min="0" value="${esc(it.qty)}" oninput="App.ivEdit(${i},'qty',this.value)"></td>
    <td><input class="in" type="number" min="0" value="${esc(it.unit_price)}" oninput="App.ivEdit(${i},'unit_price',this.value)"></td>
    <td class="mono" id="iv-amt${i}">${_money((Number(it.qty)||0)*(Number(it.unit_price)||0),'IDR')}</td>
    <td><button class="btn btn-sm btn-ghost" onclick="App.ivDel(${i})" aria-label="행 삭제">✕</button></td></tr>`).join('');
  IV_recalc();
}
function IV_recalc(){
  const dpp = (S.ivItems||[]).reduce((a,it)=>a+(Number(it.qty)||0)*(Number(it.unit_price)||0),0);
  const ppn = Math.round(dpp*0.11*100)/100;
  const set = (id,v) => { const e=$('#'+id); if(e) e.textContent=_money(v,'IDR'); };
  set('iv-dpp',dpp); set('iv-ppn',ppn); set('iv-tot',dpp+ppn);
}
App.ivType = function(v){
  S.ivType = v; const it = (S.ivItems||[])[0];
  const rt = (S.ivRates||[]).find(r=>r.service_type===v);
  if(it && !it.touched && rt){ it.unit_price = Number(rt.amount)||0; IV_rows(); }   // 손대지 않은 기본행만 요금표로 갱신
};
App.ivEdit = function(i,k,v){
  const it = (S.ivItems||[])[i]; if(!it) return;
  it[k] = k==='name' ? v : (Number(v)||0); if(i===0) it.touched = true;
  const c = $('#iv-amt'+i); if(c) c.textContent = _money((Number(it.qty)||0)*(Number(it.unit_price)||0),'IDR');
  IV_recalc();
};
App.ivAdd = function(){ S.ivItems.push({name:'', qty:1, unit_price:0}); IV_rows(); };
App.ivDel = function(i){ S.ivItems.splice(i,1); if(!S.ivItems.length) S.ivItems.push({name:'', qty:1, unit_price:0}); IV_rows(); };
App.ivSubmit = async function(){
  const err = m => { const e=$('#iv-e'); if(e) e.textContent=m; };
  const items = (S.ivItems||[]).map(it=>({name:(it.name||'').trim(), qty:Number(it.qty)||0, unit_price:Number(it.unit_price)||0,
    amount:Math.round((Number(it.qty)||0)*(Number(it.unit_price)||0)*100)/100})).filter(it=>it.name||it.amount);
  if(!items.length) return err('청구 항목을 입력해 주세요.');
  const amount = items.reduce((a,it)=>a+it.amount,0);
  const cid = S.ivCase; const b = $('#iv-go'); if(b) b.disabled = true;
  const r = await apiFetch('/cases/'+cid+'/invoices', {method:'POST', body:JSON.stringify({service_type:S.ivType, amount, line_items:items})});
  if(!r.ok){ if(b) b.disabled=false; return err(await apiErr(r, '청구서 발행에 실패했습니다.')); }
  const d = await r.json();
  closeModal(); IV_drop(cid); render(); toast('청구서를 발행했습니다. '+(d.invoice_no||'')+' · '+_money(d.total,'IDR'));
};

// ── 결제 등록 / 간이 결제표시 / 결제 내역 ──
App.ivPay = function(iid, total, cid, ctx){
  openModal('결제 기록', `
    <div class="field"><label for="ip-m">결제 방식</label><select class="in" id="ip-m">${IV_PAY_METHODS.map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select></div>
    <div class="field"><label for="ip-a">입금액(IDR)</label><input class="in" id="ip-a" type="number" min="0" value="${esc(total==null?'':total)}" placeholder="비우면 청구 합계"></div>
    <div class="field"><label for="ip-p">입금자명</label><input class="in" id="ip-p" placeholder="(선택)"></div>
    <div class="field"><label for="ip-r">참조번호</label><input class="in" id="ip-r" placeholder="TRX-..."></div>
    <p class="muted" style="font-size:12.5px">가상계좌·카드로 청구 합계와 같은 금액을 기록하면 즉시 입금 완료 처리됩니다. 계좌이체·수기 또는 금액 불일치는 관리자 확인이 필요합니다.</p>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.ivDoPay('${esc(iid)}','${esc(cid||'')}','${esc(ctx||'')}')">기록</button>`);
};
App.ivDoPay = async function(iid, cid, ctx){
  const v = id => ((($('#'+id)||{}).value)||'').trim();
  const body = { method: v('ip-m')||'bank_transfer', amount: v('ip-a')===''?null:Number(v('ip-a')),
                 depositor_name: v('ip-p')||null, reference: v('ip-r')||null };
  const r = await apiFetch('/invoices/'+iid+'/payment', {method:'POST', body:JSON.stringify(body)});
  if(!r.ok) return toast(await apiErr(r, '결제 기록에 실패했습니다.'));
  const d = await r.json(); closeModal();
  await IV_after(cid, ctx);
  toast(d.invoice_status==='paid' ? '결제가 확인되었습니다(입금 완료).' : '결제를 기록했습니다. 관리자 확인 후 입금 완료됩니다.');
};
App.ivMarkPaid = async function(iid, cid, ctx){
  const r = await apiFetch('/invoices/'+iid+'/pay', {method:'PATCH', body:'{}'});
  if(!r.ok) return toast(await apiErr(r, '결제 표시에 실패했습니다.'));
  await IV_after(cid, ctx); toast('입금 완료로 표시했습니다.');
};
App.ivConfirm = async function(iid, cid){
  const r = await apiFetch('/invoices/'+iid+'/status', {method:'PATCH', body:JSON.stringify({status:'paid', reason:'입금 확인'})});
  if(!r.ok) return toast(await apiErr(r, '입금 확인에 실패했습니다.'));
  await IV_after(cid, 'adm'); toast('입금을 확인했습니다(paid 확정).');
};
async function IV_after(cid, ctx){
  IV_drop(cid);
  if(ctx==='adm'){ if(typeof loadAdmPay==='function') await loadAdmPay(); }
  else if(S.role==='ent') await loadEnt();
  else if(S.role==='cons') await loadCons();
  render();
}

function IV_payPanel(cid, ctx){
  if(!cid) return '';
  const d = RS.ivPay[cid];
  if(!d){ if(RS._ivLoading!==cid){ RS._ivLoading=cid; IV_load(cid).then(()=>{ RS._ivLoading=null; render(); }); }
    return `<section class="panel"><div class="panel-head"><h2>결제</h2></div><p class="empty">불러오는 중…</p></section>`; }
  const canRec = IV_role('applicant','consultant','operator'), canMark = IV_role('applicant','consultant');
  const open = (d.invoices||[]).filter(i=>i.status!=='paid' && i.status!=='refunded');
  const pays = d.payments||[];
  return `<section class="panel"><div class="panel-head"><h2>결제 등록</h2><span class="muted">미결제 ${open.length}건</span></div>
    ${open.length?`<div class="tbl-wrap"><table><thead><tr><th>청구번호</th><th>구분</th><th>청구액</th><th>상태</th><th></th></tr></thead><tbody>
    ${open.map(i=>`<tr><td class="mono">${esc(i.invoice_no||String(i.invoice_id).slice(0,8))}</td><td>${esc(i.service_type||'-')}</td><td class="mono">${_money(i.total!=null?i.total:i.amount,'IDR')}</td>
      <td><span class="badge attn">${IV_ST[i.status]||esc(i.status)}</span></td>
      <td><div class="row-act">${canRec?`<button class="btn btn-sm btn-primary" onclick="App.ivPay('${esc(i.invoice_id)}',${Number(i.total)||0},'${esc(cid)}','${ctx}')">결제 기록</button>`:''}${canMark&&i.status!=='need_verification'?`<button class="btn btn-sm btn-ghost" onclick="App.ivMarkPaid('${esc(i.invoice_id)}','${esc(cid)}','${ctx}')">간이 결제표시</button>`:''}</div></td></tr>`).join('')}
    </tbody></table></div>`:'<p class="empty">결제할 청구가 없습니다.</p>'}
    ${pays.length?`<b style="font-size:13px">결제 내역 ${pays.length}건</b><div class="tbl-wrap"><table><thead><tr><th>방식</th><th>금액</th><th>참조</th><th>상태</th><th>일시</th></tr></thead><tbody>
    ${pays.map(p=>`<tr><td>${esc((IV_PAY_METHODS.find(m=>m[0]===p.method)||[0,p.method])[1])}</td><td class="mono">${_money(p.amount,'IDR')}</td><td class="muted">${esc(p.reference||'-')}</td>
      <td><span class="badge ${p.status==='confirmed'?'strong':''}">${p.status==='confirmed'?'확인':(p.status==='pending'?'확인 대기':esc(p.status))}</span></td><td class="mono">${esc(String(p.paid_at||'').slice(0,16))}</td></tr>`).join('')}
    </tbody></table></div>`:''}</section>`;
}

// 관리자 청구 목록(RS.admPay) 기반 결제 처리
function IV_admPanel(){
  if(!IV_role('operator')) return '';
  const open = (RS.admPay||[]).filter(i=>i.status!=='paid' && i.status!=='refunded');
  if(!open.length) return '';
  return `<section class="panel"><div class="panel-head"><h2>결제 처리</h2><span class="muted">미결제 ${open.length}건</span></div>
    <div class="tbl-wrap"><table><thead><tr><th>청구번호</th><th style="min-width:150px">기업</th><th>금액</th><th>상태</th><th></th></tr></thead><tbody>
    ${open.map(i=>`<tr><td class="mono">${esc(i.invoice_no||'-')}</td><td><b>${esc(i.company||'-')}</b></td><td class="mono">${_money(i.total,'IDR')}</td>
      <td><span class="badge attn">${IV_ST[i.status]||esc(i.status)}</span>${i.payments?` <span class="muted">결제 ${i.payments}</span>`:''}</td>
      <td><div class="row-act"><button class="btn btn-sm" onclick="App.ivPay('${esc(i.invoice_id)}',${Number(i.total)||0},'${esc(i.case_id||'')}','adm')">결제 기록</button>
        <button class="btn btn-sm btn-primary" onclick="App.ivConfirm('${esc(i.invoice_id)}','${esc(i.case_id||'')}')">입금 확인</button></div></td></tr>`).join('')}
    </tbody></table></div>
    <p class="muted" style="font-size:12.5px">입금 확인은 청구를 paid로 확정하고, 대기 중 결제를 확인 처리합니다(감사로그 기록). 상담검토 단계 케이스는 모의심사 요청 단계로 진행됩니다.</p></section>`;
}

// ── 화면 확장 ──
{ const f = VIEWS['cons-contract']; if(typeof f==='function') VIEWS['cons-contract'] = () => {
    const L = RS.cases||[]; const cid = S.selContract || (L[0]&&L[0].case_id);
    return f() + IV_issuePanel(cid) + IV_payPanel(cid, 'cons');
  }; }
{ const f = VIEWS['ent-contract']; if(typeof f==='function') VIEWS['ent-contract'] = () => {
    const c = (RS.cases||[])[0]; return f() + (c ? IV_payPanel(c.case_id, 'ent') : '');
  }; }
{ const f = VIEWS['adm-contract']; if(typeof f==='function') VIEWS['adm-contract'] = () => f() + (RS.admPay ? IV_admPanel() : ''); }
