/* ===== 결제 문서 · 영업 정보 (pay-docs) ===== */
const PAYD_ = {
  mime: {'application/pdf':'pdf','image/png':'png','image/svg+xml':'svg'},
  err: (r)=> r.status===403 ? t('upload.text_038') : (r.status===404 ? t('ui.text_032') : t('upload.text_024')),
};

async function PAYD_fetch(key, path){
  if(RS[key] && !RS[key].loading) return RS[key];
  RS[key] = {loading:true};
  try{
    const r = await apiFetch(path);
    RS[key] = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS[key] = {error:true}; }
  return RS[key];
}

function PAYD_money(v, cur){
  if(v===null||v===undefined||v==='') return '—';
  return _money(Number(v)||0, cur||'IDR');
}

function PAYD_invTable(invs, withDocs){
  if(!Array.isArray(invs) || !invs.length) return `<p class="empty">${t('ui.text_003')}</p>`;
  return `<div class="tbl-wrap"><table><thead><tr><th>${t('ui.text_004')}</th><th class="num">${t('ui.text_005')}</th><th>${t('upload.text_013')}</th>${withDocs?`<th>${t('ui.text_006')}</th>`:''}</tr></thead><tbody>${
    invs.map((iv,i)=>`<tr>
      <td>${esc(iv.service_type||'—')}<div class="muted mono" style="font-size:11px">${esc(iv.invoice_no||'')}</div></td>
      <td class="num mono">${esc(PAYD_money(iv.total))}<div class="muted" style="font-size:11px">공급 ${esc(PAYD_money(iv.amount))} + PPN ${esc(PAYD_money(iv.ppn))}</div></td>
      <td><span class="badge ${iv.status==='paid'?'strong':'attn'}">${esc(iv.status||'')}</span></td>
      ${withDocs?`<td class="row-act">
        <button class="link" onclick="App.paydDoc(${i},'quotation.pdf')">${t('ui.text_007')}</button>
        <button class="link" onclick="App.paydDoc(${i},'receipt')">${t('ui.text_008')}</button>
        <button class="link" onclick="App.paydDoc(${i},'tax-invoice')">${t('ui.text_009')}</button>
      </td>`:''}
    </tr>`).join('')}</tbody></table></div>`;
}

function PAYD_invoicePanel(cid){
  if(!cid) return `<section class="panel"><p class="empty">${t('upload.text_025')}</p></section>`;
  const key = 'payd_inv_'+cid;
  const d = RS[key];
  if(!d || d.loading){
    if(!d){ RS[key]={loading:true}; PAYD_fetch(key,'/cases/'+cid+'/invoices').then(()=>render()); }
    return `<section class="panel"><div class="panel-head"><h2>${t('ui.text_010')}</h2></div><p class="empty">${t('members.loading')}</p></section>`;
  }
  if(d.error) return `<section class="panel"><div class="panel-head"><h2>${t('ui.text_010')}</h2></div><p class="empty">${PAYD_.err({status:d.status})}</p></section>`;
  S.paydInvoices = d;
  return `<section class="panel"><div class="panel-head"><h2>${t('ui.text_010')}</h2><span class="count">${d.length}건</span></div>${PAYD_invTable(d, true)}</section>`;
}

const PAYD_OLD_CONTRACT = VIEWS['cons-contract'];
VIEWS['cons-contract'] = () => {
  const base = PAYD_OLD_CONTRACT ? PAYD_OLD_CONTRACT() : '';
  const list = (RS.cases||[]);
  const cid = S.selContract || (list[0]&&list[0].case_id);
  return base + PAYD_invoicePanel(cid);
};

const PAYD_OLD_ENT = VIEWS['ent-contract'];
VIEWS['ent-contract'] = () => {
  const base = PAYD_OLD_ENT ? PAYD_OLD_ENT() : '';
  const c = (RS.cases||[])[0];
  return base + PAYD_invoicePanel(c && c.case_id);
};

App.paydDoc = async function(idx, kind){
  const invs = S.paydInvoices || [];
  const iv = invs[idx]; if(!iv) return;
  const urls = {
    'quotation.pdf': '/invoices/'+iv.invoice_id+'/quotation.pdf',
    'receipt': '/invoices/'+iv.invoice_id+'/receipt',
    'tax-invoice': '/invoices/'+iv.invoice_id+'/tax-invoice',
  };
  const path = urls[kind]; if(!path) return;
  const fname = (iv.invoice_no||iv.invoice_id)+'-'+kind.replace('/','.');
  try{
    const r = await apiFetch(path);
    if(!r.ok) return toast(await apiErr(r, t('ui.text_032')));
    const blob = await r.blob();
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = fname;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast(t('ui.text_032')); }
};

/* ── B. 컨설턴트 내 영업 정보 ── */
const PAYD_FIELDS = [
  ['display_name','ui.text_033'], ['company_name','ui.text_034'], ['biz_reg_no','ui.text_035'],
  ['phone','ui.text_036'], ['email','ui.text_037'], ['address','ui.text_038'],
  ['bank_name','ui.text_039'], ['bank_account','ui.text_040'], ['account_holder','ui.text_041'],
];

function PAYD_meForm(d){
  const p = d.profile || {};
  const row = ([k,label]) => `<div class="field"><label>${esc(t(label))}</label><input class="in" id="payd-f-${k}" value="${esc(p[k]||'')}"></div>`;
  return `<div class="grid-2">${PAYD_FIELDS.map(row).join('')}</div>
    <div class="inline"><button class="btn btn-primary" onclick="App.paydSave()">${t('ui.text_011')}</button><span class="inline-msg" id="payd-me-msg"></span></div>`;
}

function PAYD_qrHtml(qr){
  if(!qr) return `<p class="empty">${t('members.loading')}</p>`;
  if(qr.error) return `<p class="empty">${PAYD_.err(qr)}</p>`;
  return `<div class="summary">
      <div><span class="k">${t('ui.text_012')}</span><span class="v mono">${esc(qr.code||'')}</span></div>
      <div><span class="k">${t('ui.text_013')}</span><span class="v mono" style="word-break:break-all">${esc(qr.url||'')}</span></div>
      <div><span class="k">${t('ui.text_014')}</span><span class="v mono">${esc(String(qr.used_count||0))}</span></div>
    </div>
    <div class="inline"><button class="btn btn-sm" onclick="App.paydQr()">${t('ui.text_015')}</button></div>
    <form onsubmit="App.sendQrMail(event)" style="margin-top:16px">
      <div class="field"><label for="qr-mail-to">${t('invite.recipient')}</label><input class="in" id="qr-mail-to" type="email" maxlength="254" required autocomplete="email"></div>
      <p class="muted">${t('invite.qr_help')}</p>
      <button class="btn btn-primary" type="submit">${t('invite.qr_send')}</button><p role="status" class="inline-msg"></p>
    </form>`;
}

function PAYD_commHtml(cm){
  if(!cm) return `<p class="empty">${t('members.loading')}</p>`;
  if(cm.error) return `<p class="empty">${PAYD_.err(cm)}</p>`;
  const rate = cm.commission_rate;
  const invs = cm.invoices||[];
  const paid = cm.payouts||[];
  return `<div class="summary">
      <div><span class="k">${t('ui.text_016')}</span><span class="v mono">${esc(String(cm.client_count||0))}</span></div>
      <div><span class="k">${t('ui.text_017')}</span><span class="v mono">${esc(PAYD_money(cm.base_amount))}</span></div>
      <div><span class="k">${t('ui.text_018')}</span><span class="v mono">${rate===null||rate===undefined?'—':esc(rate+' %')}</span></div>
      <div><span class="k">${t('ui.text_019')}</span><span class="v mono">${cm.commission_amount===null||cm.commission_amount===undefined?'—':esc(PAYD_money(cm.commission_amount))}</span></div>
    </div>
    <div class="panel-head"><h2>${t('ui.text_020')}</h2><span class="count">${invs.length}건</span></div>
    ${invs.length?`<div class="tbl-wrap"><table><thead><tr><th>${t('ui.text_021')}</th><th>${t('ui.text_022')}</th><th class="num">${t('ui.text_023')}</th><th>${t('ui.text_024')}</th></tr></thead><tbody>${
      invs.map(iv=>`<tr><td class="mono">${esc(iv.invoice_no||'')}</td><td>${esc(iv.company_name||'')}</td><td class="num mono">${esc(PAYD_money(iv.amount))}</td><td class="muted mono" style="font-size:11px">${esc(iv.created_at||'')}</td></tr>`).join('')
    }</tbody></table></div>`:`<p class="empty">${t('ui.text_025')}</p>`}
    <div class="panel-head"><h2>${t('ui.text_026')}</h2><span class="count">${paid.length}건</span></div>
    ${paid.length?`<div class="tbl-wrap"><table><thead><tr><th>${t('ui.text_027')}</th><th class="num">${t('ui.text_005')}</th><th>${t('upload.text_013')}</th><th>${t('ui.text_028')}</th></tr></thead><tbody>${
      paid.map(p=>`<tr><td class="mono">${esc(p.period||'')}</td><td class="num mono">${esc(PAYD_money(p.amount))}</td><td><span class="badge">${esc(p.status||'')}</span></td><td class="muted mono" style="font-size:11px">${esc(p.paid_at||'')}</td></tr>`).join('')
    }</tbody></table></div>`:`<p class="empty">${t('ui.text_029')}</p>`}`;
}

const PAYD_OLD_NEW = VIEWS['cons-new'];
VIEWS['cons-new'] = () => {
  const base = PAYD_OLD_NEW ? PAYD_OLD_NEW() : '';
  if(!RS.payd_me){ RS.payd_me={loading:true}; PAYD_fetch('payd_me','/consultant/me').then(()=>render()); }
  if(!RS.payd_qr){ RS.payd_qr={loading:true}; PAYD_fetch('payd_qr','/consultant/qr/info').then(()=>render()); }
  if(!RS.payd_cm){ RS.payd_cm={loading:true}; PAYD_fetch('payd_cm','/consultant/commission').then(()=>render()); }
  const me = RS.payd_me;
  const meHtml = !me || me.loading ? `<p class="empty">${t('members.loading')}</p>`
    : me.error ? `<p class="empty">${PAYD_.err(me)}</p>` : PAYD_meForm(me);
  return base + `<section class="panel"><div class="panel-head"><h2>${t('ui.text_030')}</h2></div>${meHtml}</section>
    <section class="panel"><div class="panel-head"><h2>${t('ui.text_031')}</h2></div>${PAYD_qrHtml(RS.payd_qr)}</section>
    <section class="panel"><div class="panel-head"><h2>${t('ui.text_019')}</h2></div>${PAYD_commHtml(RS.payd_cm)}</section>`;
};

App.paydSave = async function(){
  const body = {};
  PAYD_FIELDS.forEach(([k])=>{
    const el = $('#payd-f-'+k); if(el) body[k] = (el.value||'').trim();
  });
  const msg = $('#payd-me-msg'); if(msg) msg.textContent = '';
  try{
    const r = await apiFetch('/consultant/me', {method:'PUT', body:JSON.stringify(body)});
    if(!r.ok){
      let code = ''; try{ const j = await r.json(); code = j && j.detail && j.detail.code ? ' ('+j.detail.code+')' : ''; }catch(e){}
      if(r.status===403) return toast(t('ui.text_042'));
      return toast(t('ui.text_043')+code);
    }
    RS.payd_me = null;
    await PAYD_fetch('payd_me','/consultant/me');
    render(); toast(t('ui.text_044'));
  }catch(e){ toast(t('ui.text_043')); }
};

App.paydQr = async function(){
  try{
    const r = await apiFetch('/consultant/qr');
    if(!r.ok) return toast(await apiErr(r, t('ui.text_045')));
    const blob = await r.blob();
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'consultant_qr.png';
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast(t('ui.text_045')); }
};
