/* ===== 운영 도구: 환불 승인 · 결제 리포트 · 청구 상태 변경 ===== */
(function(){
  const OPSD_INV_STATES = ['waiting_payment','unpaid','need_verification','payment_processing','paid','rejected','expired','refunded'];
  const OPSD_REQ_STATES = ['requested','approved','rejected'];
  const OPSD_MONTHS = 12;

  RS.opsd = RS.opsd || {};
  S.opsdReqFilter = S.opsdReqFilter || '';
  S.opsdReportGroup = S.opsdReportGroup || 'month';

  function OPSD_loadRefunds(){
    const st = S.opsdReqFilter;
    const path = '/admin/refunds' + (st ? ('?status=' + encodeURIComponent(st)) : '');
    RS.opsd.refunds = {loading:true};
    apiFetch(path).then(async r=>{
      if(!r.ok){ RS.opsd.refunds = {error:true, status:r.status}; }
      else { const d = await r.json(); RS.opsd.refunds = d; }
      render();
    }).catch(()=>{ RS.opsd.refunds = {error:true}; render(); });
  }

  function OPSD_loadReport(group){
    RS.opsd.report = {loading:true};
    apiFetch('/admin/payments/report?group=' + encodeURIComponent(group)).then(async r=>{
      if(!r.ok){ RS.opsd.report = {error:true, status:r.status}; }
      else { RS.opsd.report = await r.json(); }
      render();
    }).catch(()=>{ RS.opsd.report = {error:true}; render(); });
  }

  App.opsdRefFilter = function(st){
    S.opsdReqFilter = st || '';
    RS.opsd.refunds = null;
    render();
  };

  App.opsdReportGroup = function(g){
    if(g === S.opsdReportGroup) return;
    S.opsdReportGroup = g;
    RS.opsd.report = null;
    render();
  };

  App.opsdReportPdf = async function(){
    try{
      const r = await apiFetch('/admin/payments/report.pdf');
      if(!r.ok) return toast('파일을 불러올 수 없습니다.');
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'payments-report.pdf';
      document.body.appendChild(a);
      a.click();
      setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
    }catch(e){ toast('파일을 불러올 수 없습니다.'); }
  };

  // 환불 반려/승인 모달
  App.opsdDecide = function(refundId, decision){
    const label = decision === 'approved' ? '승인' : '반려';
    openModal('환불 ' + label,
      `<div class="field"><label>사유/메모 (선택)</label>
        <textarea class="in" id="opsd-note" rows="3" placeholder="결정 사유"></textarea></div>
       <p class="note attn">2인 승인 규칙: 본인이 요청한 건은 승인할 수 없습니다.</p>`,
      `<button class="btn" onclick="App.closeModal()">취소</button>
       <button class="btn btn-primary" onclick="App.opsdDecideGo('${refundId}','${decision}')">${label}</button>`);
  };

  App.opsdDecideGo = async function(refundId, decision){
    const note = (($('#opsd-note')||{}).value||'').trim();
    try{
      const r = await apiFetch('/refunds/' + refundId + '/decide', {
        method:'POST', body: JSON.stringify({decision:decision, note:note || null})
      });
      if(!r.ok){
        let code = '';
        try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || (j && j.code) || ''; }catch(_){}
        if(r.status === 403){
          toast('권한이 없습니다' + (code ? ' (' + code + ')' : '') + ' — 본인 요청 건은 승인 불가');
        } else if(r.status === 409){
          toast('이미 처리된 환불입니다' + (code ? ' (' + code + ')' : ''));
        } else {
          toast('환불 ' + (decision==='approved'?'승인':'반려') + '에 실패했습니다.' + (code ? ' (' + code + ')' : ''));
        }
        return;
      }
      App.closeModal();
      RS.opsd.refunds = null;
      RS.opsd.report = null;
      toast('환불을 ' + (decision==='approved'?'승인':'반려') + '했습니다.');
      render();
    }catch(e){
      toast('환불 처리에 실패했습니다.');
    }
  };

  App.opsdSetInvoice = async function(){
    const iid = (($('#opsd-inv')||{}).value||'').trim();
    const st  = (($('#opsd-inv-st')||{}).value||'').trim();
    const rsn = (($('#opsd-inv-rsn')||{}).value||'').trim();
    if(!iid){ toast('청구 ID를 입력하세요.'); return; }
    if(OPSD_INV_STATES.indexOf(st) < 0){ toast('상태를 선택하세요.'); return; }
    try{
      const r = await apiFetch('/invoices/' + encodeURIComponent(iid) + '/status', {
        method:'PATCH', body: JSON.stringify({status:st, reason: rsn || null})
      });
      if(!r.ok){
        let code = '';
        try{ const j = await r.json(); code = (j && j.detail && j.detail.code) || (j && j.code) || ''; }catch(_){}
        if(r.status === 403) toast('권한이 없습니다 (operator 필요)' + (code?' ('+code+')':''));
        else if(r.status === 404) toast('청구를 찾을 수 없습니다' + (code?' ('+code+')':''));
        else toast('상태 변경에 실패했습니다.' + (code?' ('+code+')':''));
        return;
      }
      toast('청구 상태를 변경했습니다.');
      RS.opsd.report = null;
      const inv = $('#opsd-inv'); if(inv) inv.value = '';
      const rsnEl = $('#opsd-inv-rsn'); if(rsnEl) rsnEl.value = '';
      render();
    }catch(e){ toast('상태 변경에 실패했습니다.'); }
  };

  function OPSD_refundSection(){
    const d = RS.opsd.refunds;
    if(!d || d.loading){
      if(!d){ OPSD_loadRefunds(); }
      return `<section class="panel"><div class="panel-head"><h2>환불 승인</h2></div><p class="empty">불러오는 중…</p></section>`;
    }
    if(d.error){
      return `<section class="panel"><div class="panel-head"><h2>환불 승인</h2></div>
        <p class="empty">${d.status===403?'이 기능은 권한이 없습니다 (fatwa_liaison/operator 필요)':'불러오지 못했습니다.'}</p></section>`;
    }
    const items = d.items || [];
    const chips = [['','전체'],['requested','요청됨'],['approved','승인'],['rejected','반려']]
      .map(([k,l])=>`<button class="chip ${S.opsdReqFilter===k?'on':''}" onclick="App.opsdRefFilter('${k}')">${l}</button>`).join('');
    const shown = items.slice(0, 100);
    const rows = shown.map(it=>{
      const canDecide = it.status === 'requested';
      const act = canDecide
        ? `<div class="row-act">
            <button class="btn btn-sm btn-primary" onclick="App.opsdDecide('${it.id}','approved')">승인</button>
            <button class="btn btn-sm" onclick="App.opsdDecide('${it.id}','rejected')">반려</button>
           </div>`
        : `<span class="muted">${it.decided_by?'처리됨':'—'}</span>`;
      const stBadge = it.status==='approved' ? 'badge strong' : (it.status==='rejected' ? 'badge attn' : 'badge');
      return `<tr>
        <td><div>${esc(it.invoice_no||'')}</div><div class="muted mono" style="font-size:11px">${esc(it.invoice_id||'')}</div></td>
        <td>${esc(it.company||'')}</td>
        <td class="num mono">${esc(it.amount_idr||'')}</td>
        <td><span class="${stBadge}">${esc(it.status||'')}</span></td>
        <td class="muted">${esc(it.reason||'')}</td>
        <td class="muted mono" style="font-size:11px">${esc(it.requested_by||'')}</td>
        <td class="muted mono" style="font-size:11px">${esc((it.created_at||'').slice(0,19))}</td>
        <td>${act}</td>
      </tr>`;
    }).join('');
    const more = items.length > 100 ? `<p class="muted" style="margin-top:6px">외 ${items.length-100}건</p>` : '';
    const body = items.length
      ? `<div class="tbl-wrap"><table><thead><tr>
          <th>인보이스</th><th>업체</th><th class="num">금액</th><th>상태</th><th>사유</th><th>요청자</th><th>요청시각</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table></div>${more}`
      : `<p class="empty">환불 요청이 없습니다.</p>`;
    return `<section class="panel"><div class="panel-head"><h2>환불 승인 <span class="count">${items.length}</span></h2>
      <div class="chipbar">${chips}</div></div>${body}</section>`;
  }

  function OPSD_reportSection(){
    const d = RS.opsd.report;
    if(!d || d.loading){
      if(!d){ OPSD_loadReport(S.opsdReportGroup); }
      return `<section class="panel"><div class="panel-head"><h2>결제 리포트</h2></div><p class="empty">불러오는 중…</p></section>`;
    }
    if(d.error){
      return `<section class="panel"><div class="panel-head"><h2>결제 리포트</h2></div>
        <p class="empty">${d.status===403?'이 기능은 권한이 없습니다 (operator 필요)':'불러오지 못했습니다.'}</p></section>`;
    }
    const isMonth = (d.group||'month') === 'month';
    const tabs = [['month','월별'],['org','기관별']]
      .map(([k,l])=>`<button class="chip ${S.opsdReportGroup===k?'on':''}" onclick="App.opsdReportGroup('${k}')">${l}</button>`).join('');
    const t = d.totals || {};
    const sum = `<div class="summary">
      <div><span class="k">청구 합계</span><span class="v mono">${esc(_money(t.invoiced||0,'IDR'))}</span></div>
      <div><span class="k">결제 합계</span><span class="v mono">${esc(_money(t.paid||0,'IDR'))}</span></div>
      <div><span class="k">환불 합계</span><span class="v mono">${esc(_money(t.refunded||0,'IDR'))}</span></div>
      <div><span class="k">순정산</span><span class="v mono">${esc(_money(t.net_settled||0,'IDR'))}</span></div>
      <div><span class="k">미수</span><span class="v mono">${esc(_money(t.outstanding||0,'IDR'))}</span></div>
      <div><span class="k">인보이스 수</span><span class="v mono">${esc(String(t.invoice_count||0))}</span></div>
    </div>`;
    const rows = (d.rows||[]).map(r=>`<tr>
      <td class="mono">${esc(String(r.key||''))}</td>
      <td class="num mono">${esc(String(r.invoiced||0))}</td>
      <td class="num mono">${esc(String(r.paid||0))}</td>
      <td class="num mono">${esc(String(r.refunded||0))}</td>
      <td class="num mono">${esc(String(r.net||0))}</td>
      <td class="num">${r.count||0}</td>
    </tr>`).join('');
    const table = (d.rows||[]).length
      ? `<div class="tbl-wrap"><table><thead><tr>
          <th>${isMonth?'월':'기관'}</th><th class="num">청구</th><th class="num">결제</th><th class="num">환불</th><th class="num">순정산</th><th class="num">건수</th>
        </tr></thead><tbody>${rows}</tbody></table></div>`
      : `<p class="empty">집계 데이터가 없습니다.</p>`;
    return `<section class="panel"><div class="panel-head"><h2>결제 리포트</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <div class="chipbar">${tabs}</div>
        <button class="btn btn-sm" onclick="App.opsdReportPdf()">리포트 PDF</button>
      </div></div>${sum}${table}</section>`;
  }

  function OPSD_invoiceSection(){
    const opts = OPSD_INV_STATES.map(s=>`<option value="${s}">${esc(s)}</option>`).join('');
    return `<section class="panel"><div class="panel-head"><h2>청구 상태 변경</h2></div>
      <div class="inline">
        <input class="in" id="opsd-inv" placeholder="invoice_id" />
        <select class="in" id="opsd-inv-st">${opts}</select>
      </div>
      <div class="inline" style="margin-top:8px">
        <input class="in" id="opsd-inv-rsn" placeholder="사유(선택)" />
        <button class="btn btn-primary" onclick="App.opsdSetInvoice()">변경</button>
      </div>
      <p class="note">상태를 <span class="mono">paid</span> 로 변경하면 결제 확정 및 모의심사 진입 게이트가 트리거됩니다. maker-checker 감사로그가 기록됩니다.</p>
    </section>`;
  }

  const OPSD_render = function(){
    return OPSD_refundSection() + OPSD_reportSection() + OPSD_invoiceSection();
  };

  if(typeof OPS_TABS !== 'undefined' && OPS_TABS && OPS_TABS.push){
    OPS_TABS.push({key:'refund', label:'환불·리포트', render: OPSD_render});
  }
})();
