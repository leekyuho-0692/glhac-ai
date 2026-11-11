/* 문제 서류 — 기존 서류함 안에서 조회·재안내. 기존 t()/I18N 언어 선택을 따른다. */
Object.assign(I18N.ko, {
  'pdn.count':'문제 서류 {count}건', 'pdn.resend':'다시 안내', 'pdn.refresh':'목록 새로고침',
  'pdn.loading':'문제 서류를 불러오는 중…', 'pdn.error':'문제 서류를 불러오지 못했습니다.',
  'pdn.last':'마지막 안내', 'pdn.applicant':'업체', 'pdn.auditor':'담당 오디터', 'pdn.operator':'운영자',
  'pdn.none':'안내 이력 없음', 'pdn.uploaded':'올린 시각', 'pdn.sent':'안내를 요청했습니다.',
  'pdn.failed':'안내를 요청하지 못했습니다.', 'pdn.processing':'서류 처리 중입니다. 완료 후 다시 안내해 주세요.',
  'pdn.closed':'종결된 케이스에는 안내할 수 없습니다.', 'pdn.no_problems':'문제 서류가 없습니다.'
});
Object.assign(I18N.en, {
  'pdn.count':'{count} document(s) need attention', 'pdn.resend':'Send reminder', 'pdn.refresh':'Refresh list',
  'pdn.loading':'Loading document issues…', 'pdn.error':'Could not load document issues.',
  'pdn.last':'Last notification', 'pdn.applicant':'Company', 'pdn.auditor':'Assigned auditor', 'pdn.operator':'Operator',
  'pdn.none':'No notifications yet', 'pdn.uploaded':'Uploaded at', 'pdn.sent':'Notification requested.',
  'pdn.failed':'Could not request notification.', 'pdn.processing':'Documents are being processed. Please try again when processing finishes.',
  'pdn.closed':'Notifications cannot be sent for a closed case.', 'pdn.no_problems':'No document issues.'
});
Object.assign(I18N.id, {
  'pdn.count':'{count} dokumen perlu diperbaiki', 'pdn.resend':'Kirim pengingat', 'pdn.refresh':'Muat ulang daftar',
  'pdn.loading':'Memuat masalah dokumen…', 'pdn.error':'Gagal memuat masalah dokumen.',
  'pdn.last':'Pemberitahuan terakhir', 'pdn.applicant':'Perusahaan', 'pdn.auditor':'Auditor yang ditugaskan', 'pdn.operator':'Operator',
  'pdn.none':'Belum ada pemberitahuan', 'pdn.uploaded':'Waktu unggah', 'pdn.sent':'Pemberitahuan diminta.',
  'pdn.failed':'Gagal meminta pemberitahuan.', 'pdn.processing':'Dokumen sedang diproses. Coba lagi setelah selesai.',
  'pdn.closed':'Pemberitahuan tidak dapat dikirim untuk kasus yang sudah ditutup.', 'pdn.no_problems':'Tidak ada masalah dokumen.'
});
RS.problemDocs = RS.problemDocs || {};
const PDN_loading = new Set(), PDN_sending = new Set();
const PDN_key = cid => cid + ':' + S.lang;
const PDN_allowed = () => ['operator','admin','auditor','consultant'].includes(AUTH.role);

async function PDN_load(cid){
  const key = PDN_key(cid), lang = S.lang;
  if(PDN_loading.has(key)) return;
  PDN_loading.add(key);
  try{
    const r = await apiFetch('/cases/'+encodeURIComponent(cid)+'/problem-documents?lang='+encodeURIComponent(lang));
    RS.problemDocs[key] = r.ok ? await r.json() : {error:true};
  }catch(e){ RS.problemDocs[key] = {error:true}; }
  finally { PDN_loading.delete(key); }
}

function PDN_panel(cid){
  if(!cid || !PDN_allowed()) return '';
  const d = RS.problemDocs[PDN_key(cid)], safe = jsq(cid);
  const refresh = `<button class="btn btn-sm btn-ghost" onclick="App.problemDocsRefresh('${safe}')">${esc(t('pdn.refresh'))}</button>`;
  if(!d){
    if(!PDN_loading.has(PDN_key(cid))) PDN_load(cid).then(()=>render());
    return `<div class="note" aria-live="polite">${esc(t('pdn.loading'))}</div>`;
  }
  if(d.error) return `<div class="note attn" role="status">${esc(t('pdn.error'))} ${refresh}</div>`;
  return `<div class="note ${d.count?'attn':''}" aria-live="polite">
    <div class="row-act"><span class="badge ${d.count?'attn':''}">${esc(t('pdn.count').replace('{count}',d.count))}</span>
      <button class="btn btn-sm" ${!d.count||d.closed||PDN_sending.has(cid)?'disabled':''} onclick="App.problemDocsNotify('${safe}')">${esc(t('pdn.resend'))}</button>${refresh}</div>
    <p class="muted">${d.last_notified_at ? esc(t('pdn.last')+': '+d.last_notified_at+' · '+t('pdn.'+d.last_recipient_type)) : esc(t('pdn.none'))}</p>
    ${(d.documents||[]).map(doc=>`<details><summary><b>${esc(doc.filename)}</b> · ${esc(t('pdn.uploaded'))}: ${esc(doc.uploaded_at)}</summary>
      <ul>${(doc.reasons||[]).map(r=>`<li>${esc(r.explanation)}</li>`).join('')}</ul></details>`).join('')}
  </div>`;
}
App.problemDocsRefresh = async function(cid){ await PDN_load(cid); render(); };
App.problemDocsNotify = async function(cid){
  if(!PDN_allowed() || PDN_sending.has(cid)) return;
  PDN_sending.add(cid); render();
  try{
    const lang = S.lang, key = PDN_key(cid);
    const r = await apiFetch('/cases/'+encodeURIComponent(cid)+'/problem-documents/notify?lang='+encodeURIComponent(lang), {method:'POST'});
    if(!r.ok) return toast(t('pdn.failed'));
    const d = await r.json();
    // 다른 언어로 본 같은 케이스의 목록도 다음 조회 때 갱신한다.
    for(const k of Object.keys(RS.problemDocs)) if(k.startsWith(cid+':')) delete RS.problemDocs[k];
    RS.problemDocs[key] = d;
    toast(t(d.notified ? 'pdn.sent' : 'pdn.'+d.reason));
  }catch(e){ toast(t('pdn.failed')); }
  finally { PDN_sending.delete(cid); render(); }
};
