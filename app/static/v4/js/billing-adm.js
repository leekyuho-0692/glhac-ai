/* ===== 관리자 계약·입금 처리 ===== */
const BLA_SECTORS = ['food','cosmetics','household','warehouse','transport'];
const BLA_SECTOR_KO = {food:'식품',cosmetics:'화장품',household:'생활용품',warehouse:'창고',transport:'운송'};
const BLA_FILTERS = [['action','조치 필요'],['signing','서명'],['signed','체결'],['paying','입금'],['overdue','기한 경과'],['settled','완납'],['all','전체']];
const BLA_ISTAT = {draft:'발행 전',waiting_payment:'입금 대기',need_verification:'입금 확인 필요',paid:'입금 완료'};

async function BLA_load(){
  try{ const r = await apiFetch('/admin/billing/board?filter='+(S.blaFilter||'action')); RS.blaBoard = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.blaBoard = {error:true}; }
}
const BLA_it = (i) => (RS.blaBoard && RS.blaBoard.items && RS.blaBoard.items[i]) || null;
function BLA_row(i){
  const inv = (arr, st) => (i.rounds||[]).find(x => x.status === st);
  const done = i.rounds && i.rounds.length && i.rounds.every(x => x.status === 'paid');
  const rounds = (i.rounds||[]).map(x => `R${esc(x.round)} ${esc(Number(x.total||0).toLocaleString())} ${esc(BLA_ISTAT[x.status]||x.status)} ${x.due_date?'기한 '+esc(String(x.due_date).slice(0,10)):''} ${x.overdue?'<span class="badge attn">기한 경과</span>':''}`).join('<br>');
  const na = i.next_action;
  let act = '';
  if(na==='accept_formal') act = '<span class="muted">위 접수 보드에서 접수 확인</span>';
  else if(na==='send_contract') act = `<button class="btn btn-sm" onclick="App.blaQuote(${i})">견적·계약서 발송</button>`;
  else if(na==='request_signature') act = `<button class="btn btn-sm" onclick="App.blaNotice(${i},'signature_request')">서명 요청 발송</button>`;
  else if(na==='sign_b') act = `<button class="btn btn-sm btn-primary" onclick="App.blaSignB(${i})">GL HAC 서명</button>`;
  else if(na==='notice_contract') act = `<button class="btn btn-sm" onclick="App.blaNotice(${i},'contract_signed')">체결 고지</button>`;
  else if(na==='notice_due'){ const w = inv(i.rounds,'waiting_payment'); act = `<button class="btn btn-sm" onclick="App.blaNotice(${i},'payment_due','${w?esc(w.invoice_id):''}')">입금 고지</button>`; }
  else if(na==='notice_reminder'){ const o = inv(i.rounds,'waiting_payment'); act = `<button class="btn btn-sm" onclick="App.blaNotice(${i},'payment_reminder','${o?esc(o.invoice_id):''}')">재고지</button>`; }
  else if(na==='confirm_paid'){ const v = inv(i.rounds,'need_verification'); act = `<button class="btn btn-sm btn-primary" onclick="App.blaConfirmPaid(${i},'${v?esc(v.invoice_id):''}')">입금 확인</button>`; }
  else if(na==='notice_paid'){ const p = (i.rounds||[]).find(x=>x.status==='paid' && !x.confirmed_notice_at); act = `<button class="btn btn-sm" onclick="App.blaNotice(${i},'payment_confirmed','${p?esc(p.invoice_id):''}')">확인 알림</button>`; }
  else if(na==='next_round'){ const d = inv(i.rounds,'draft'); act = `<button class="btn btn-sm" onclick="App.blaBillNow(${i},'${d?esc(d.invoice_id):''}')">미리 청구</button>`; }
  else if(na==='wait_payment') act = '<span class="muted">입금 대기</span>';
  else if(na==='settled') act = '<span class="badge strong">완납</span>';
  return `<tr><td>${esc(i.company_name)}</td><td class="num mono">${esc(Number(i.fee||i.quote_total||0).toLocaleString())}</td><td>${i.signed_a?'✔':'—'} / ${i.signed_b?'✔':'—'}</td><td>${i.signed_notice_sent?'✔':'—'}</td><td style="font-size:12px">${rounds||'—'}</td><td>${i.next_action_label?'<span class="badge attn">'+esc(i.next_action_label)+'</span>':''}</td><td><div class="row-act">${act}<button class="btn btn-sm btn-ghost" onclick="App.blaNotices(${i})">알림 이력</button></div></td></tr>`;
}
function BLA_board(){
  if(S.role!=='adm') return '';
  const d = RS.blaBoard;
  const tabs = `<div class="tabs">${BLA_FILTERS.map(([k,l])=>`<button class="${(S.blaFilter||'action')===k?'on':''}" onclick="App.blaFilter('${k}')">${l}</button>`).join('')}</div>`;
  if(!d){ if(!RS._blaLoading){ RS._blaLoading=true; BLA_load().then(()=>{ RS._blaLoading=false; render(); }); } return `<section class="panel"><div class="panel-head"><h2>계약·입금 보드</h2></div>${tabs}<p class="empty">불러오는 중…</p></section>`; }
  if(d.error) return `<section class="panel"><div class="panel-head"><h2>계약·입금 보드</h2></div>${tabs}<p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'불러오지 못했습니다.'}</p></section>`;
  const items = d.items||[];
  const body = items.length ? `<div class="tbl-wrap"><table><thead><tr><th>업체</th><th class="num">계약 금액</th><th>서명 갑/을</th><th>체결 고지</th><th>회차</th><th>다음 조치</th><th>처리</th></tr></thead><tbody>${items.map(BLA_row).join('')}</tbody></table></div>` : '<p class="empty">해당하는 케이스가 없습니다.</p>';
  return `<section class="panel"><div class="panel-head"><h2>계약·입금 보드</h2><span class="count">${(d.count!=null?d.count:items.length)}건</span></div>${tabs}${body}</section>`;
}
App.blaFilter = function(f){ S.blaFilter = f; RS.blaBoard = undefined; render(); };
App.blaQuote = function(i){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const opts = BLA_SECTORS.map(s=>`<div><span class="k">${BLA_SECTOR_KO[s]}</span></div>`).join('');
  const body = `<div class="field"><label>품목 수</label><input class="in" id="bla-pc" type="number" min="1" value="1"></div>`
    + `<div class="field"><label><input type="checkbox" id="bla-onsite" checked> 현장심사 출장비</label></div>`
    + `<div class="field"><label><input type="checkbox" id="bla-opt-sihalal"> SiHalal 등록 옵션</label></div>`
    + `<div class="inline"><input class="in" id="bla-adj-label" placeholder="조정 항목"><input class="in" id="bla-adj-amount" type="number" placeholder="금액"></div>`
    + `<div class="field"><label>비고</label><textarea class="in" id="bla-note"></textarea></div>`
    + `<div id="bla-quote-result"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn" onclick="App.blaQuoteCalc(${i})">견적 산출</button><button class="btn btn-primary" onclick="App.blaSendContract(${i})">계약서 발송</button>`;
  openModal('견적 산출 · 계약서 발송', body, foot, true);
};
App.blaQuoteCalc = async function(i){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const adj = [];
  const al = ($('#bla-adj-label')||{}).value, aa = ($('#bla-adj-amount')||{}).value;
  if(al && aa) adj.push({label:al.trim(), amount:Number(aa)});
  const body = {product_count:Number(($('#bla-pc')||{}).value||1), onsite:!!($('#bla-onsite')||{}).checked, options:($('#bla-opt-sihalal')||{}).checked?['sihalal_registration']:[], adjustments:adj, note:(($('#bla-note')||{}).value||'').trim()};
  const r = await apiFetch('/cases/'+cid+'/quote', {method:'POST', body:JSON.stringify(body)});
  if(!r.ok){ const e = await r.json().catch(()=>({})); return toast('견적 산출에 실패했습니다.'+(e.detail&&e.detail.code?' ('+e.detail.code+')':'')); }
  const q = await r.json();
  const el = $('#bla-quote-result'); if(el) el.innerHTML = `<div class="tbl-wrap"><table><thead><tr><th>항목</th><th class="num">수량</th><th class="num">단가</th><th class="num">금액</th></tr></thead><tbody>${(q.lines||[]).map(l=>`<tr><td>${esc(l.label)}</td><td class="num">${esc(l.qty)}</td><td class="num mono">${esc(Number(l.unit||0).toLocaleString())}</td><td class="num mono">${esc(Number(l.amount||0).toLocaleString())}</td></tr>`).join('')}</tbody></table></div><div class="summary"><div><span class="k">소계</span><span class="v mono">${esc(Number(q.subtotal||0).toLocaleString())} 원</span></div><div><span class="k">PPN</span><span class="v mono">${esc(Number(q.ppn||0).toLocaleString())} 원</span></div><div><span class="k">합계</span><span class="v mono">${esc(Number(q.total||0).toLocaleString())} 원</span></div></div>`;
};
App.blaSendContract = async function(i){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const r = await apiFetch('/cases/'+cid+'/contract/approve', {method:'POST', body:JSON.stringify({})});
  if(!r.ok){ const e = await r.json().catch(()=>({})); const c = e.detail&&e.detail.code; if(r.status===409 && c==='FORMAL_NOT_ACCEPTED') return toast('정식 신청 접수 확인 후 발송할 수 있습니다'); return toast('계약서 발송에 실패했습니다.'+(c?' ('+c+')':'')); }
  delete RS.blaBoard; await loadAdm(); render(); toast('계약서를 발송했습니다'); closeModal();
};
App.blaNotice = function(i, kind, invoiceId){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const body = `<div class="field"><label>받는 사람</label>`
    + `<label><input type="checkbox" id="bla-r-client" checked> 기업</label> `
    + `<label><input type="checkbox" id="bla-r-consultant" checked> 컨설턴트</label> `
    + `<label><input type="checkbox" id="bla-r-auditor" ${kind==='contract_signed'?'checked':''}> 심사관</label></div>`
    + `<div class="field"><label>방법</label><label><input type="checkbox" id="bla-c-inapp" checked disabled> 인앱</label> <label><input type="checkbox" id="bla-c-email" checked> 이메일</label> <label><input type="checkbox" id="bla-c-kakao"> 카카오</label></div>`
    + (kind==='payment_due'?`<div class="field"><label>입금 기한(일)</label><input class="in" id="bla-due" type="number" value="7"></div>`:'')
    + `<div class="field"><label>내용</label><textarea class="in" id="bla-msg" placeholder="비우면 자동 문안"></textarea></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.blaNoticeDo(${i},'${kind}','${invoiceId||''}')">발송</button>`;
  openModal('알림 발송', body, foot);
};
App.blaNoticeDo = async function(i, kind, invoiceId){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const rec = []; if(($('#bla-r-client')||{}).checked) rec.push('client'); if(($('#bla-r-consultant')||{}).checked) rec.push('consultant'); if(($('#bla-r-auditor')||{}).checked) rec.push('auditor');
  const ch = ['inapp']; if(($('#bla-c-email')||{}).checked) ch.push('email'); if(($('#bla-c-kakao')||{}).checked) ch.push('kakao');
  const body = {kind, recipients:rec, channels:ch, message:(($('#bla-msg')||{}).value||'').trim()};
  if(invoiceId) body.invoice_id = invoiceId;
  const dEl = $('#bla-due'); if(dEl) body.due_days = Number(dEl.value||7);
  const r = await apiFetch('/cases/'+cid+'/billing/notice', {method:'POST', body:JSON.stringify(body)});
  if(!r.ok){ const e = await r.json().catch(()=>({})); return toast('발송에 실패했습니다.'+(e.detail&&e.detail.code?' ('+e.detail.code+')':'')); }
  delete RS.blaBoard; render(); toast('발송했습니다'); closeModal();
};
App.blaNotices = async function(i){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const r = await apiFetch('/cases/'+cid+'/billing/notices');
  if(!r.ok){ const e = await r.json().catch(()=>({})); return toast('알림 이력을 불러오지 못했습니다.'+(e.detail&&e.detail.code?' ('+e.detail.code+')':'')); }
  const d = await r.json();
  const body = (d.items||[]).length ? `<ul class="rows">${d.items.map(n=>`<li class="row"><div>${esc(String(n.at||'').slice(0,16))} · ${esc(n.kind_label||n.kind)} · ${esc((n.recipients||[]).join(','))} · ${esc((n.channels||[]).join(','))}${n.auto?' · 자동':''}</div>${n.message?`<div class="muted">${esc(n.message)}</div>`:''}</li>`).join('')}</ul>` : '<p class="empty">이력이 없습니다.</p>';
  openModal('알림 이력', body, `<button class="btn" onclick="App.closeModal()">닫기</button>`);
};
App.blaSignB = function(i){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const body = `<div class="field"><label>서명자 이름</label><input class="in" id="bla-signer" value="운영관리자"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.blaSignBDo(${i})">서명</button>`;
  openModal('GL HAC 서명', body, foot);
};
App.blaSignBDo = async function(i){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const name = (($('#bla-signer')||{}).value||'').trim() || '운영관리자';
  const ct = it;
  if(!ct || !ct.contract_id) return toast('계약 정보를 찾을 수 없습니다.');
  const r = await apiFetch('/contracts/'+ct.contract_id+'/sign?party=B&name='+encodeURIComponent(name), {method:'POST'});
  if(!r.ok){ const e = await r.json().catch(()=>({})); const c = e.detail&&e.detail.code; if(r.status===403) return toast('권한이 없습니다.'+(c?' ('+c+')':'')); return toast('서명에 실패했습니다.'+(c?' ('+c+')':'')); }
  delete RS.blaBoard; render(); toast('GL HAC 서명을 기록했습니다 — 계약이 체결되었습니다'); closeModal();
};
App.blaConfirmPaid = async function(i, invoiceId){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const r = await apiFetch('/invoices/'+invoiceId+'/status', {method:'PATCH', body:JSON.stringify({status:'paid', reason:''})});
  if(!r.ok){ const e = await r.json().catch(()=>({})); return toast('입금 확인에 실패했습니다.'+(e.detail&&e.detail.code?' ('+e.detail.code+')':'')); }
  delete RS.blaBoard; render(); toast('입금을 확인했습니다');
};
App.blaBillNow = async function(i, invoiceId){
  const it = BLA_it(i); if(!it) return toast('항목을 찾을 수 없습니다.'); const cid = it.case_id;
  const r = await apiFetch('/invoices/'+invoiceId+'/bill-now', {method:'POST', body:JSON.stringify({})});
  if(!r.ok){ const e = await r.json().catch(()=>({})); return toast('청구에 실패했습니다.'+(e.detail&&e.detail.code?' ('+e.detail.code+')':'')); }
  delete RS.blaBoard; render(); toast('청구했습니다');
};
async function BLA_loadSettings(){
  try{ const r = await apiFetch('/billing/settings'); RS.blaSettings = r.ok ? (await r.json()) : {error:true, status:r.status}; }
  catch(e){ RS.blaSettings = {error:true}; }
}
function BLA_settings(){
  if(S.role!=='adm') return '';
  const d = RS.blaSettings;
  if(!d){ if(!RS._blaSetLoading){ RS._blaSetLoading=true; BLA_loadSettings().then(()=>{ RS._blaSetLoading=false; render(); }); } return `<section class="panel"><p class="empty">불러오는 중…</p></section>`; }
  if(d.error) return `<section class="panel"><p class="empty">${d.status===403?'이 화면을 볼 권한이 없습니다.':'설정을 불러오지 못했습니다.'}</p></section>`;
  const sb = d.sector_base||{};
  const p1 = `<div class="fieldset"><legend>분야별 가격 기준표</legend>${BLA_SECTORS.map(s=>`<div class="field"><label>${BLA_SECTOR_KO[s]}</label><input class="in" id="bla-sb-${s}" type="number" value="${esc(sb[s]!=null?sb[s]:0)}"></div>`).join('')}<div class="field"><label>품목 추가비</label><input class="in" id="bla-ppe" type="number" value="${esc(d.per_product_extra||0)}"></div><div class="field"><label>출장비</label><input class="in" id="bla-travel" type="number" value="${esc(d.onsite_travel||0)}"></div><div class="field"><label>SiHalal 옵션</label><input class="in" id="bla-opt-si" type="number" value="${esc((d.options||{}).sihalal_registration||0)}"></div></div>`;
  const opts = d.options||{};
  const bank = d.bank_account||{};
  const p2 = `<div class="fieldset"><legend>계약·입금 설정</legend><div class="field"><label>지급 조건</label><select class="in" id="bla-terms"><option value="lump" ${d.payment_terms==='lump'?'selected':''}>일시불</option><option value="split50" ${d.payment_terms==='split50'?'selected':''}>계약 시 50%·발급 시 50%</option></select></div><div class="field"><label>입금 기한(일)</label><input class="in" id="bla-due-days" type="number" value="${esc(d.due_days||0)}"></div><div class="field"><label><input type="checkbox" id="bla-auto-c" ${d.auto_notice_contract?'checked':''}> 체결 고지 자동</label></div><div class="field"><label><input type="checkbox" id="bla-auto-p" ${d.auto_notice_paid?'checked':''}> 입금 확인 알림 자동</label></div><div class="field"><label>은행</label><input class="in" id="bla-bank" value="${esc(bank.bank||'')}"></div><div class="field"><label>계좌번호</label><input class="in" id="bla-acct" value="${esc(bank.number||'')}"></div><div class="field"><label>예금주</label><input class="in" id="bla-holder" value="${esc(bank.holder||'')}"></div></div>`;
  return `<section class="panel"><div class="panel-head"><h2>알림 기준·계약 설정</h2><button class="btn btn-primary btn-sm" onclick="App.blaSaveSettings()">저장</button></div><div class="grid-2">${p1}${p2}</div></section>`;
}
App.blaSaveSettings = async function(){
  const val = id => { const el=$(id); return el?el.value:undefined; };
  const chk = id => { const el=$(id); return el?!!el.checked:undefined; };
  const body = {sector_base:{}, per_product_extra:Number(val('#bla-ppe')||0), onsite_travel:Number(val('#bla-travel')||0), options:{sihalal_registration:Number(val('#bla-opt-si')||0)}, payment_terms:val('#bla-terms'), due_days:Number(val('#bla-due-days')||0), auto_notice_contract:chk('#bla-auto-c'), auto_notice_paid:chk('#bla-auto-p'), bank_account:{bank:val('#bla-bank'), number:val('#bla-acct'), holder:val('#bla-holder')}};
  BLA_SECTORS.forEach(s=>{ body.sector_base[s]=Number(val('#bla-sb-'+s)||0); });
  const r = await apiFetch('/billing/settings', {method:'POST', body:JSON.stringify(body)});
  if(!r.ok){ const e = await r.json().catch(()=>({})); return toast('저장에 실패했습니다.'+(e.detail&&e.detail.code?' ('+e.detail.code+')':'')); }
  RS.blaSettings = undefined; render(); toast('저장했습니다');
};
(function(){
  const f1 = VIEWS['adm-contract']; VIEWS['adm-contract'] = () => BLA_board() + (f1?f1():'');
  const f2 = VIEWS['adm-price']; VIEWS['adm-price'] = () => (f2?f2():'') + BLA_settings();
  const prevN = NAVC['adm-contract'];
  NAVC['adm-contract'] = () => (typeof prevN==='function'?prevN():0) + ((S.blaFilter||'action')==='action' && RS.blaBoard && RS.blaBoard.items ? RS.blaBoard.items.length : 0);
  const g = App.go; App.go = function(v){ RS.blaBoard = undefined; return g.apply(this, arguments); };
})();
