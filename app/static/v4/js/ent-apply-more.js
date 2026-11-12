const ENTA_I18N = {
  "docs.ai_unavailable": ["AI가 비활성 상태여서 검증하지 못했습니다.","AI verification is unavailable because AI is disabled.","Verifikasi belum dilakukan karena AI tidak aktif."],
  "members.forbidden": ["권한 없음", "Access denied", "Tidak memiliki izin"],
  "members.loading": ["불러오는 중…", "Loading…", "Memuat…"],
  "members.reject": ["반려", "Reject", "Tolak"],
  "docs.ai_unchecked": ["AI 미검증", "AI not verified", "Belum diverifikasi AI"],
  "docs.ai_match": ["일치", "Matches", "Sesuai"],
  "docs.ai_review": ["확인 필요", "Review needed", "Perlu diperiksa"],
  "upload.archive_done": ["압축 내 서류 {count}건 처리", "Processed {count} documents in the archive", "Memproses {count} dokumen dalam arsip"],
  "upload.zip_hint": ["압축 파일은 위 ZIP 버튼으로 올려 주세요.", "Use the ZIP button above for archives.", "Gunakan tombol ZIP di atas untuk arsip."],
  "upload.skipped": ["건너뜀", "Skipped", "Dilewati"],
  "upload.skip.METADATA": ["시스템 메타데이터", "System metadata", "Metadata sistem"],
  "upload.skip.UNSUPPORTED_TYPE": ["지원하지 않는 형식", "Unsupported format", "Format tidak didukung"],
  "upload.skip.DEPTH_LIMIT": ["중첩 압축 깊이 제한(최대 2)", "Archive depth limit (maximum 2)", "Batas kedalaman arsip (maksimal 2)"],
  "upload.skip.ENTRY_LIMIT": ["압축 전체 파일 수 제한", "Archive file count limit", "Batas jumlah berkas arsip"],
  "upload.new_version": ["새 버전", "New version", "Versi baru"],
  "upload.status.queued": ["대기", "Queued", "Antrean"],
  "upload.status.uploading": ["올리는 중", "Uploading", "Mengunggah"],
  "upload.status.failed": ["실패", "Failed", "Gagal"],
  "upload.status.pending": ["분석 대기", "Awaiting analysis", "Menunggu analisis"],
  "upload.status.complete": ["분석 완료", "Analysis complete", "Analisis selesai"],
  "upload.status.duplicate": ["이미 올림", "Already uploaded", "Sudah diunggah"],
  "upload.check_count": ["필수 {ok}/{total} 제출", "Required: {ok}/{total} submitted", "Wajib: {ok}/{total} terkirim"],
  "upload.missing_count": ["미제출 필수 서류 {count}건: ", "Missing required documents ({count}): ", "Dokumen wajib belum dikirim ({count}): "],
  "upload.failed_code": ["업로드 실패({code})", "Upload failed ({code})", "Unggahan gagal ({code})"],
  "upload.materials_pending": ["원재료 {count}건이 확인 대기에 있습니다(원재료 등록 창에서 확인).", "{count} raw material entries await confirmation in the material registration dialog.", "{count} bahan menunggu konfirmasi di dialog pendaftaran bahan."],
  "upload.starting": ["처리 시작… ({count}건)", "Starting… ({count} files)", "Memulai… ({count} berkas)"],
  "upload.processing": ["처리 중 {index}/{total} — {filename}", "Processing {index}/{total} — {filename}", "Memproses {index}/{total} — {filename}"],
  "upload.progress_error": ["진행 상황 조회 실패: ", "Could not retrieve progress: ", "Gagal mengambil progres: "],
  "upload.zip_error": ["ZIP 업로드 실패: ", "ZIP upload failed: ", "Unggahan ZIP gagal: "],
  "upload.multiple": ["여러 서류 올리기", "Upload multiple documents", "Unggah beberapa dokumen"],
  "upload.drop": ["서류 여러 개를 여기로 끌어다 놓으세요", "Drop multiple documents here", "Letakkan beberapa dokumen di sini"],
  "upload.limit": ["파일당 최대 20MB · 한 번에 하나씩 올립니다.", "Up to 20 MB per file · Files upload one at a time.", "Maksimal 20 MB per berkas · Diunggah satu per satu."],
  "upload.wait": ["진행 중인 업로드·분석을 기다려 주세요.", "Wait for the current upload and analysis to finish.", "Tunggu unggahan dan analisis saat ini selesai."],
  "upload.file_error": ["파일 읽기 또는 서버 연결 실패", "Could not read the file or connect to the server", "Gagal membaca berkas atau menghubungi server"],
  "upload.analysis_wait": ["자동 분석 대기 — 재처리가 필요할 수 있습니다.", "Waiting for automatic analysis; reprocessing may be needed.", "Menunggu analisis otomatis; pemrosesan ulang mungkin diperlukan."],
  "upload.analysis_failed": ["AI 분석 실패 — 재처리 필요", "AI analysis failed; reprocessing needed", "Analisis AI gagal; perlu diproses ulang"],
  "upload.progress_failed": ["진행 상황을 불러오지 못했습니다. 문서함에서 다시 확인해 주세요.", "Could not load progress. Check the document library.", "Gagal memuat progres. Periksa kembali di pustaka dokumen."],
  "upload.timeout": ["3분간 확인했습니다. 분석 대기 서류는 문서함에서 다시 확인해 주세요.", "Checked for three minutes. Review documents awaiting analysis in the document library.", "Pemeriksaan berlangsung tiga menit. Periksa dokumen yang menunggu analisis di pustaka dokumen."],
  "upload.interrupted": ["업로드 중단", "Upload interrupted", "Unggahan dihentikan"],
  "upload.check_error": ["서류 정보를 확인하지 못했습니다. 다시 불러와 주세요.", "Could not read document information. Please reload.", "Informasi dokumen tidak dapat dibaca. Silakan muat ulang."],
  "upload.draft_saved": ["기업 정보를 임시저장했습니다. 품목·공장·조직은 각 등록 버튼으로 저장해 주세요.", "Company draft saved. Save products, facilities and team changes using their own buttons.", "Draf perusahaan tersimpan. Simpan produk, fasilitas, dan tim dengan tombol masing-masing."],
  "upload.zip_no_result": ["완료 결과를 받지 못했습니다. 문서함을 확인해 주세요.", "No completion result received. Check the document library.", "Hasil penyelesaian belum diterima. Periksa pustaka dokumen."],
  "upload.zip_disconnected": ["처리 중 연결이 끊겼습니다.", "Connection lost during processing.", "Koneksi terputus saat pemrosesan."],
  "upload.text_001": ["필요 서류 현황", "Required documents status", "Status berkas yang diperlukan"],
  "upload.text_002": ["제출", "Submit", "Kirim"],
  "upload.text_003": ["해당 없음", "Not applicable", "Tidak berlaku"],
  "upload.text_004": ["미제출", "Not submitted", "Belum diajukan"],
  "upload.text_005": ["서류", "Documents", "Berkas"],
  "upload.text_006": ["필수", "Required", "Wajib"],
  "upload.text_007": ["사유·비고", "Reason·note", "Alasan·catatan"],
  "upload.text_008": ["선택", "Select", "Pilihan"],
  "upload.text_009": ["서류 정보가 없습니다.", "No document information.", "Tidak ada informasi berkas."],
  "upload.text_010": ["임시저장 · 일괄 업로드", "Draft saved · Bulk upload", "Draf tersimpan · Unggah massal"],
  "upload.text_011": ["ZIP 파일로 서류 일괄 올리기", "Bulk upload documents as ZIP file", "Unggah berkas massal sebagai file ZIP"],
  "upload.text_012": ["ZIP·RAR·7z 를 압축 해제해 서류를 자동 분류·채웁니다. 처리 중에는 창을 닫지 마세요.", "Extracts ZIP·RAR·7z and auto-classifies·fills documents. Do not close the window during processing.", "Ekstrak ZIP·RAR·7z dan klasifikasi·isi berkas otomatis. Jangan tutup jendela selama proses."],
  "upload.text_013": ["상태", "Status", "Status"],
  "upload.text_014": ["결과", "Result", "Hasil"],
  "upload.text_015": ["환불 요청", "Refund request", "Permintaan pengembalian dana"],
  "upload.text_016": ["환불 가능한(결제 완료) 인보이스가 없습니다.", "No refundable (paid) invoices.", "Tidak ada invoice yang dapat dikembalikan (pembayaran selesai)."],
  "upload.text_017": ["환불 사유", "Refund reason", "Alasan pengembalian dana"],
  "upload.text_018": ["금액 (비우면 전액)", "Amount (blank = full amount)", "Nominal (kosong = penuh)"],
  "upload.text_019": ["관리자 승인(maker-checker) 후 처리됩니다.", "Processed after admin approval (maker-checker).", "Diproses setelah persetujuan admin (maker-checker)."],
  "upload.text_020": ["취소", "Cancel", "Batal"],
  "upload.text_021": ["요청", "Request", "Permintaan"],
  "upload.text_022": ["환불 사유를 입력하세요", "Enter refund reason", "Masukkan alasan pengembalian dana"],
  "upload.text_023": ["예: 1500000", "e.g. 1500000", "contoh: 1500000"],
  "upload.text_024": ["불러오지 못했습니다.", "Failed to load.", "Tidak dapat dimuat."],
  "upload.text_025": ["대상 업체가 없습니다.", "No target companies.", "Tidak ada perusahaan sasaran."],
  "upload.text_026": ["이 작업은 권한이 없습니다.", "You do not have permission for this action.", "Anda tidak memiliki izin untuk tindakan ini."],
  "upload.text_027": ["임시저장에 실패했습니다.", "Failed to save draft.", "Gagal menyimpan draf."],
  "upload.text_028": ["ZIP 파일을 올리는 중…", "Uploading ZIP file…", "Mengunggah file ZIP…"],
  "upload.text_029": ["파일을 읽을 수 없습니다.", "Cannot read file.", "Tidak dapat membaca file."],
  "upload.text_030": ["업로드에 실패했습니다.", "Upload failed.", "Gagal mengunggah."],
  "upload.text_031": ["처리 실패", "Processing Failed", "Gagal dalam Pemrosesan"],
  "upload.text_032": ["마무리 중…", "Finishing…", "Menyelesaikan…"],
  "upload.text_033": ["서류를 처리했습니다.", "Documents processed.", "Berkas telah diproses."],
  "upload.text_034": ["환불 사유를 입력하세요.", "Enter refund reason.", "Masukkan alasan pengembalian dana."],
  "upload.text_035": ["이 작업은 권한이 없습니다. (operator 전용)", "No permission for this action. (operator only)", "Tidak ada izin untuk tindakan ini. (khusus operator)"],
  "upload.text_036": ["환불 요청에 실패했습니다.", "Refund request failed.", "Permintaan pengembalian dana gagal."],
  "upload.text_037": ["환불을 요청했습니다. 관리자 승인 후 처리됩니다.", "Refund requested. Processed after admin approval.", "Pengembalian dana diminta. Diproses setelah persetujuan admin."],
  "upload.draft": ["기업 정보 임시저장", "Save company draft", "Simpan draf perusahaan"],
  "upload.retry": ["다시 올리기", "Upload again", "Unggah lagi"],
  "upload.too_large": ["용량 초과", "File too large", "Berkas terlalu besar"],
  "upload.locked": ["잠긴 신청서", "Application locked", "Permohonan terkunci"],
  "upload.unsupported": ["지원하지 않는 형식", "Unsupported format", "Format tidak didukung"],
  "upload.mismatch": ["확장자와 내용이 다름", "Extension does not match content", "Ekstensi tidak sesuai dengan isi"],
  "upload.filename": ["파일명", "Filename", "Nama berkas"],
  "upload.text_038": ["이 화면을 볼 권한이 없습니다.", "You do not have permission to view this screen.", "Anda tidak memiliki izin untuk melihat layar ini."]
};
for(const [key,values] of Object.entries(ENTA_I18N)){ ['ko','en','id'].forEach((lang,i)=>{ I18N[lang]=I18N[lang]||{}; I18N[lang][key]=values[i]; }); }
/* ===== 인증기업 보강(서류 체크리스트·임시저장/ZIP 일괄업로드·환불 요청) ===== */

const ENTA_LANG = () => (typeof S !== 'undefined' && S.lang) ? S.lang : 'ko';
const ENTA_CASE = () => (RS.cases && RS.cases[0] && RS.cases[0].case_id) || null;

function ENTA_t(key,params={}){ const text=(I18N[ENTA_LANG()]||I18N.ko)[key]||I18N.ko[key]||key; return text.replace(/\{(\w+)\}/g,(m,k)=>params[k]??m); }

async function ENTA_loadChecklist(cid){
  const lang=ENTA_LANG();
  try{
    const r = await apiFetch(`/cases/${cid}/doc-checklist?lang=${encodeURIComponent(lang)}`);
    const data=r.ok ? (await r.json()) : {error:true, status:r.status};
    if(lang===ENTA_LANG() && cid===ENTA_CASE()) RS.entaCheck=data;
  }catch(e){ if(lang===ENTA_LANG() && cid===ENTA_CASE()) RS.entaCheck={error:true}; }
}
async function ENTA_loadInvoices(cid){
  try{
    const r = await apiFetch(`/cases/${cid}/invoices`);
    RS.entaInv = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.entaInv = {error:true}; }
}

function ENTA_checklistPanel(){
  const cid = ENTA_CASE();
  if(!cid) return '';
  const d = RS.entaCheck;
  if(!d){
    if(RS._entaChkLoading !== cid){ RS._entaChkLoading = cid; ENTA_loadChecklist(cid).then(()=>{ RS._entaChkLoading=null; render(); }); }
    return `<section class="panel"><div class="panel-head"><h2>${ENTA_t('upload.text_001')}</h2></div><p class="empty">${ENTA_t('members.loading')}</p></section>`;
  }
  if(d.error){
    return `<section class="panel"><div class="panel-head"><h2>${ENTA_t('upload.text_001')}</h2></div>
      <p class="empty">${d.status===403?ENTA_t('upload.text_038'):ENTA_t('upload.text_024')}</p></section>`;
  }
  const rows = Array.isArray(d) ? d : (Array.isArray(d.checklist) ? d.checklist : null);
  if(!rows) return `<section class="panel"><h2>${ENTA_t('upload.text_001')}</h2><p class="empty">${ENTA_t('upload.check_error')}</p></section>`;
  const req = rows.filter(r=>r.required && r.applicable !== false);
  const reqOk = req.filter(r=>r.satisfied).length;
  const missing = req.filter(r=>!r.satisfied);
  const stTag = (r) => r.status==='ok' ? `<span class="badge strong">${ENTA_t('upload.text_002')}</span>`
    : r.status==='rejected' ? `<span class="badge attn">${ENTA_t('members.reject')}</span>`
    : r.status==='not_applicable' ? `<span class="badge">${ENTA_t('upload.text_003')}</span>`
    : `<span class="badge attn">${ENTA_t('upload.text_004')}</span>`;
  const body = rows.length ? `<div class="tbl-wrap"><table><thead><tr>
      <th>${ENTA_t('upload.text_005')}</th><th>${ENTA_t('upload.text_006')}</th><th>${ENTA_t('upload.text_002')}</th><th>${ENTA_t('upload.text_007')}</th></tr></thead><tbody>
      ${rows.map(r=>`<tr>
        <td>${esc(r.doc_type_ko||r.doc_type||'')}${r.source?`<div class="muted" style="font-size:11px">${esc(r.source)}</div>`:''}</td>
        <td>${r.required?`<span class="tag req">${ENTA_t('upload.text_006')}</span>`:`<span class="muted">${ENTA_t('upload.text_008')}</span>`}</td>
        <td>${stTag(r)} <span class="muted mono">${r.file_count||0}</span>${r.cert_no_on_file?` <span class="count">${r.cert_no_on_file}</span>`:''}</td>
        <td class="muted" style="font-size:12px">${esc(r.na_reason||r.note||r.requirement||'')}</td>
      </tr>`).join('')}</tbody></table></div>` : `<p class="empty">${ENTA_t('upload.text_009')}</p>`;
  return `<section class="panel"><div class="panel-head"><h2>${ENTA_t('upload.text_001')}</h2>
      <span class="muted">${ENTA_t('upload.check_count',{ok:reqOk,total:req.length})}</span></div>
    ${missing.length?`<p class="note attn">${ENTA_t('upload.missing_count',{count:missing.length})}${missing.map(m=>esc(m.doc_type_ko||m.doc_type)).join(', ')}</p>`:''}
    ${body}</section>`;
}

function ENTA_applyPanel(){
  const cid = ENTA_CASE();
  if(!cid) return '';
  // 임시저장·ZIP 일괄 업로드 — 신청기업·컨설턴트 전용(main.py:6159·5794). 할랄감독자(penyelia)는 403
  const canDraft = canCall('POST','/cases/{}/save-draft'), canZip = canCall('POST','/cases/{}/intake-zip-stream');
  const canBulk = canCall('POST','/cases/{}/documents');
  if(!canDraft && !canZip && !canBulk) return '';
  const prog = (S.entaProg||'');
  const bulk = S.entaBulk && S.entaBulk.cid===cid && S.entaBulk.owner===AUTH.username ? S.entaBulk : null;
  return `<section class="panel"><div class="panel-head"><h2>${ENTA_t('upload.text_010')}</h2></div>
    <div class="inline" style="flex-wrap:wrap;gap:8px">
      ${canDraft?`<button class="btn" onclick="once(this, ()=>App.entaSaveDraft())">${ENTA_t('upload.draft')}</button>`:''}
      ${canZip?`<button class="btn btn-primary" onclick="const z=$('#enta-zip'); if(z) z.click()">${ENTA_t('upload.text_011')}</button>`:''}
      <input type="file" id="enta-zip" accept=".zip,.rar,.7z" hidden onchange="App.entaZip(this.files[0])">
    </div>
    <p class="muted" style="font-size:12px;margin-top:6px">${ENTA_t('upload.text_012')}</p>
    ${prog?`<p class="note">${esc(prog)}</p>`:''}
    ${ENTA_skippedPanel(cid)}
    ${canBulk?`<div class="field"><label for="enta-files">${ENTA_t('upload.multiple')}</label>
      <input class="in" type="file" id="enta-files" multiple accept=".pdf,.jpg,.jpeg,.png,.xlsx,.xls,.docx,.doc,.csv,.txt,.hwp,.zip,.rar,.7z" ${bulk&&bulk.busy?'disabled':''} onchange="App.entaUpload(this.files)"></div>
      <div class="dropzone" ondragover="event.preventDefault();this.classList.add('over')" ondragleave="this.classList.remove('over')" ondrop="App.entaDrop(event)">
        <b>${ENTA_t('upload.drop')}</b><span class="muted">${ENTA_t('upload.limit')}</span></div>`:''}
    ${bulk?`<div aria-live="polite">${bulk.note?`<p class="note">${esc(bulk.note)}</p>`:''}
      <div class="tbl-wrap"><table><thead><tr><th>${ENTA_t('upload.filename')}</th><th>${ENTA_t('upload.text_013')}</th><th>${ENTA_t('upload.text_014')}</th></tr></thead><tbody>
      ${bulk.items.map((it,i)=>`<tr><td>${esc(it.filename)}${it.newVersion?` <span class="badge">${ENTA_t('upload.new_version')}</span>`:''}</td><td>${esc(ENTA_t('upload.status.'+it.status))}</td><td>${esc(it.doc_type_ko||'')}
        ${it.verdict?`<span class="badge ${it.verdict==='ok'?'strong':['warn','fail'].includes(it.verdict)?'attn':''}">${esc(ENTA_t(it.verdict==='ok'?'docs.ai_match':['warn','fail'].includes(it.verdict)?'docs.ai_review':'docs.ai_unchecked'))}</span>`:''}
        ${it.reason?`<span>${esc(it.reason)}</span>`:''}
        ${canBulk&&it.status==='failed'&&it.file?`<button class="btn btn-sm" ${bulk.busy?'disabled':''} onclick="App.entaRetry(${i})">${ENTA_t('upload.retry')}</button>`:''}</td></tr>`).join('')}</tbody></table></div></div>`:''}
  </section>`;
}

function ENTA_skippedPanel(cid){
  const saved=S.entaSkipped;
  if(!saved || saved.cid!==cid || saved.owner!==AUTH.username || !saved.items.length) return '';
  return `<details class="note"><summary>${ENTA_t('upload.skipped')} (${saved.items.length})</summary><ul>${saved.items.map(x=>`<li>${esc(x.filename)} — ${esc(ENTA_t('upload.skip.'+x.reason))}</li>`).join('')}</ul></details>`;
}

function ENTA_refundPanel(){
  const cid = ENTA_CASE();
  if(!cid) return '';
  // 환불 요청은 운영자 전용(main.py:13869) — 인증기업 화면에선 403 이라 패널 자체를 숨긴다
  if(!canCall('POST','/invoices/{}/refund/request')) return '';
  const d = RS.entaInv;
  if(!d){
    if(RS._entaInvLoading !== cid){ RS._entaInvLoading = cid; ENTA_loadInvoices(cid).then(()=>{ RS._entaInvLoading=null; render(); }); }
    return `<section class="panel"><div class="panel-head"><h2>${ENTA_t('upload.text_015')}</h2></div><p class="empty">${ENTA_t('members.loading')}</p></section>`;
  }
  if(d.error){
    return `<section class="panel"><div class="panel-head"><h2>${ENTA_t('upload.text_015')}</h2></div>
      <p class="empty">${d.status===403?ENTA_t('upload.text_038'):ENTA_t('upload.text_024')}</p></section>`;
  }
  const rows = (Array.isArray(d)?d:[]).filter(i=>i.status==='paid');
  const body = rows.length ? `<ul class="rows">${rows.map(i=>`<li class="row">
      <span>${esc(i.invoice_no||i.invoice_id)} <span class="tag">${esc(i.service_type||'')}</span></span>
      <span class="num mono">${esc(_money(i.total, 'IDR'))}</span>
      <span class="row-act"><button class="btn btn-sm" onclick="App.entaRefund('${i.invoice_id}')">${ENTA_t('upload.text_015')}</button></span>
    </li>`).join('')}</ul>` : `<p class="empty">${ENTA_t('upload.text_016')}</p>`;
  return `<section class="panel"><div class="panel-head"><h2>${ENTA_t('upload.text_015')}</h2></div>${body}</section>`;
}

/* --- 뷰 확장 --- */
{
  const f = VIEWS['ent-home'];
  VIEWS['ent-home'] = () => f() + ENTA_checklistPanel();
}
{
  const f = VIEWS['ent-apply'];
  VIEWS['ent-apply'] = () => ENTA_applyPanel() + f();
}
{
  const f = VIEWS['ent-contract'];
  VIEWS['ent-contract'] = () => f() + ENTA_refundPanel();
}

/* --- 액션 --- */
async function ENTA_fileB64(file, buffer){
  const buf = buffer || await file.arrayBuffer(), bytes = new Uint8Array(buf);
  let bin = '';
  for(let i=0;i<bytes.length;i+=0x8000) bin+=String.fromCharCode.apply(null, bytes.subarray(i,i+0x8000));
  return btoa(bin);
}
async function ENTA_fileHash(buffer){
  // SHA-256 of raw bytes, matching the server's lowercase file_hash.
  try{
    const digest=await globalThis.crypto.subtle.digest('SHA-256',buffer);
    return Array.from(new Uint8Array(digest),n=>n.toString(16).padStart(2,'0')).join('');
  }catch(e){ return null; } // No hash means no proven duplicate; upload conservatively.
}
function ENTA_errorText(code){
  code=String(code||'UNKNOWN');
  if(/(?:CLOSED|FROZEN|LOCKED)/.test(code)) return ENTA_t('upload.locked');
  return ({UNSUPPORTED_TYPE:ENTA_t('upload.unsupported'), CONTENT_MISMATCH:ENTA_t('upload.mismatch'),
    UPLOAD_TOO_LARGE:ENTA_t('upload.too_large'), NOT_AUTHORIZED:ENTA_t('members.forbidden'), FORBIDDEN:ENTA_t('members.forbidden'),
    ORG_FORBIDDEN:ENTA_t('members.forbidden'), '401':ENTA_t('members.forbidden'), '403':ENTA_t('members.forbidden')})[code] || ENTA_t('upload.failed_code',{code});
}
async function ENTA_uploadError(r){
  const d = await r.json().catch(()=>({}));
  return ENTA_errorText((d.detail&&d.detail.code)||d.code||r.status);
}
function ENTA_cancelBulk(bulk){
  if(!bulk || bulk.cancelled) return;
  bulk.cancelled=true; bulk.busy=false;
  if(bulk.controller) bulk.controller.abort();
  if(bulk.wake) bulk.wake();
  for(const it of bulk.items){
    if(['queued','uploading'].includes(it.status)){ it.status='failed'; it.reason=ENTA_t('upload.interrupted'); }
  }
}
function ENTA_bulkActive(bulk){
  if(S.entaBulk!==bulk || AUTH.username!==bulk.owner || S.view!=='ent-apply' || ENTA_CASE()!==bulk.cid) ENTA_cancelBulk(bulk);
  return !bulk.cancelled;
}
App.entaDrop = function(e){
  e.preventDefault(); e.currentTarget.classList.remove('over');
  return App.entaUpload(e.dataTransfer.files);
};
App.entaUpload = async function(files){
  const cid = ENTA_CASE();
  if(!cid || S.view!=='ent-apply' || !canCall('POST','/cases/{}/documents')) return;
  const previous=S.entaBulk;
  if(previous && ENTA_bulkActive(previous) && previous.busy) return toast(ENTA_t('upload.wait'));
  const selected = Array.from(files||[]);
  if(!selected.length) return;
  const items=selected.map(file=>({file,filename:file.name,status:'queued'}));
  const old=previous && previous.cid===cid && previous.owner===AUTH.username ? previous.items : [];
  ENTA_cancelBulk(previous);
  const bulk={cid,owner:AUTH.username,busy:true,cancelled:false,items:[...old,...items],note:'',controller:new AbortController()};
  S.entaBulk = bulk; render();
  await ENTA_runBulk(bulk,items);
};
App.entaRetry = async function(i){
  const previous=S.entaBulk;
  if(!previous || previous.busy || S.view!=='ent-apply' || previous.cid!==ENTA_CASE() || previous.owner!==AUTH.username || !canCall('POST','/cases/{}/documents')) return;
  const it=previous.items[i];
  if(!it || it.status!=='failed' || !it.file) return;
  ENTA_cancelBulk(previous);
  const bulk={...previous,busy:true,cancelled:false,note:'',controller:new AbortController(),wake:null};
  it.status='queued'; it.reason=''; it.document_id=null; it.verdict=null; it.doc_type_ko='';
  S.entaBulk=bulk; render();
  await ENTA_runBulk(bulk,[it],true);
};
async function ENTA_runBulk(bulk, items, retry=false){
  const active=()=>ENTA_bulkActive(bulk);
  try{
    let existing=[];
    try{
      const r=await apiFetch(`/cases/${bulk.cid}/documents?lang=${encodeURIComponent(ENTA_LANG())}`,{signal:bulk.controller.signal});
      if(r.ok){ const d=await r.json(); if(Array.isArray(d)) existing=d; }
    }catch(e){ /* A failed lookup cannot establish a duplicate. */ }
    // 파일 읽기까지 순차 실행해 여러 개의 base64를 동시에 메모리에 올리지 않는다.
    for(const it of items){
      if(!active()) break;
      const file=it.file;
      if(file.size>20*1024*1024){ it.status='failed'; it.reason=ENTA_t('upload.too_large'); render(); continue; }
      it.status='uploading'; render();
      try{
        if(/\.(zip|rar|7z)$/i.test(file.name)){
          if(!canCall('POST','/cases/{}/intake-zip-stream')){
            it.status='failed'; it.reason=ENTA_t('upload.zip_hint'); render(); continue;
          }
          const result=await App.entaZip(file,{bulk,signal:bulk.controller.signal});
          if(!active()) break;
          it.status='complete'; it.reason=ENTA_t('upload.archive_done',{count:result.classified?.length||0});
          render(); continue;
        }
        const buffer=await file.arrayBuffer();
        it.file_hash=await ENTA_fileHash(buffer);
        if(!active()) break;
        const known=[...existing,...bulk.items.filter(x=>x!==it && x.document_id)];
        const uploaded=!retry && it.file_hash && known.find(x=>x.file_hash===it.file_hash);
        if(uploaded){
          Object.assign(it,{status:'duplicate',document_id:uploaded.document_id,doc_type_ko:uploaded.doc_type_ko,
            verdict:uploaded.verdict||(uploaded.verify||{}).verdict,reason:'',newVersion:false});
          render(); continue;
        }
        it.newVersion=known.some(x=>String(x.filename||'').normalize('NFC')===file.name.normalize('NFC'));
        const b64=await ENTA_fileB64(file,buffer);
        if(!active()) break;
        const r=await apiFetch(`/cases/${bulk.cid}/documents`, {method:'POST',body:JSON.stringify({filename:file.name,file_b64:b64}),signal:bulk.controller.signal});
        if(!active()) break;
        if(!r.ok){
          const reason=await ENTA_uploadError(r);
          if(!active()) break;
          it.status='failed'; it.reason=reason;
        }
        else{
          const d=await r.json();
          if(!active()) break;
          it.document_id=d.document_id; it.status=d.recovered && d.confidence!=null?'complete':'pending'; it.reason='';
          it.doc_type_ko=d.doc_type_ko||'';
          if(d.recovered) ENTA_applyParsedStatus(it,d);
          if(it.status==='pending' && !d.autoparse_queued) it.reason=ENTA_t('upload.analysis_wait');
        }
      }catch(e){ if(active()){ it.status='failed'; it.reason=e.uploadReason||ENTA_t('upload.file_error'); } }
      if(active()) render();
    }
    if(active()) await ENTA_pollBulk(bulk, active);
  }finally{
    bulk.busy=false;
    if(active()){
      if(typeof PKG_load==='function') await PKG_load(bulk.cid);
      if(active()) render();
    }
  }
}
function ENTA_applyParsedStatus(it,d){
  it.doc_type_ko=d.doc_type_ko||d.doc_type; it.verdict=(d.verify||{}).verdict;
  if((d.fields||{})._llm_error==='LLM_UNAVAILABLE'){ it.status='complete'; it.verdict='unknown'; it.reason=ENTA_t('docs.ai_unavailable'); }
  else if((d.fields||{})._llm_error){ it.status='failed'; it.reason=ENTA_t('upload.analysis_failed'); }
  else if(d.confidence!=null){ it.status='complete'; it.reason=''; }
}
async function ENTA_pollBulk(bulk, active){
  const end=Date.now()+180000;
  while(active() && Date.now()<end && bulk.items.some(it=>it.document_id && it.status==='pending')){
    await new Promise(resolve=>{
      const timer=setTimeout(done, Math.min(3000,end-Date.now()));
      function done(){ clearTimeout(timer); bulk.wake=null; resolve(); }
      bulk.wake=done;
    });
    if(!active() || Date.now()>=end) break;
    try{
      const r=await apiFetch(`/cases/${bulk.cid}/documents?lang=${encodeURIComponent(ENTA_LANG())}`, {signal:AbortSignal.any([bulk.controller.signal,AbortSignal.timeout(end-Date.now())])});
      if(!active()) return;
      if(!r.ok){ bulk.note=ENTA_t('upload.progress_error')+await ENTA_uploadError(r); if(r.status===403) break; }
      else{
        const docs=await r.json();
        if(!active()) return;
        bulk.note='';
        for(const it of bulk.items){
          if(it.status==='duplicate') continue;
          const d=docs.find(x=>x.document_id===it.document_id);
          if(!d) continue;
          ENTA_applyParsedStatus(it,d);
        }
      }
    }catch(e){ bulk.note=ENTA_t('upload.progress_failed'); }
    if(active()) render();
  }
  if(!active()) return;
  if(bulk.items.some(it=>it.status==='pending')) bulk.note=bulk.note||ENTA_t('upload.timeout');
  if(Date.now()>=end) return;
  try{
    const r=await apiFetch(`/cases/${bulk.cid}/pending-extractions`, {signal:bulk.controller.signal});
    if(!active()) return;
    if(r.ok){
      const d=await r.json(), ids=new Set(bulk.items.map(it=>it.document_id));
      if(!active()) return;
      const n=(d.items||[]).filter(it=>it.kind==='material_names' && ids.has(it.document_id)).length;
      if(n) bulk.note+=(bulk.note?' ':'')+ENTA_t('upload.materials_pending',{count:n});
    }
  }catch(e){ /* 업로드 결과는 유지하고 다음 점검에서 확인 대기를 다시 조회한다. */ }
}

App.entaSaveDraft = async function(){
  const cid = ENTA_CASE();
  if(!cid) return toast(ENTA_t('upload.text_025'));
  if(!await ENTP_saveProfile({quiet:true})) return;
  try{
    const r = await apiFetch(`/cases/${cid}/save-draft`, {method:'POST', body:JSON.stringify({})});
    if(!r.ok){
      if(r.status===403) return toast(ENTA_t('upload.text_026'));
      let code=''; try{ code=(await r.json()).code; }catch(e){}
      return toast(ENTA_t('upload.text_027')+(code?` (${code})`:''));
    }
    await loadEnt();
    render(); toast(ENTA_t('upload.draft_saved'));
  }catch(e){ toast(ENTA_t('upload.text_027')); }
};

App.entaZip = async function(file,options={}){
  const cid = ENTA_CASE();
  if(!cid) return toast(ENTA_t('upload.text_025'));
  if(!file || !canCall('POST','/cases/{}/intake-zip-stream')) return;
  const active=()=>options.bulk?ENTA_bulkActive(options.bulk):(ENTA_CASE()===cid && AUTH.username===owner);
  const owner=AUTH.username;
  const fail = msg => {
    if(active()){ S.entaProg=ENTA_t('upload.zip_error')+msg; render(); if(!options.bulk) toast(msg); }
    if(options.bulk) throw Object.assign(new Error(msg),{uploadReason:msg});
    return null;
  };
  if(file.size>20*1024*1024) return fail(ENTA_t('upload.too_large'));
  S.entaProg = ENTA_t('upload.text_028'); render();
  let b64;
  try{
    const buf = await file.arrayBuffer();
    let bin=''; const bytes=new Uint8Array(buf);
    const CH=0x8000;
    for(let i=0;i<bytes.length;i+=CH){ bin+=String.fromCharCode.apply(null, bytes.subarray(i,i+CH)); }
    b64 = btoa(bin);
  }catch(e){ return fail(ENTA_t('upload.text_029')); }

  if(!active()) return null;
  let r;
  try{
    r = await apiFetch(`/cases/${cid}/intake-zip-stream`, {method:'POST', body:JSON.stringify({zip_b64:b64}),signal:options.signal});
  }catch(e){ return fail(ENTA_t('upload.text_030')); }

  if(!r.ok){
    return fail(await ENTA_uploadError(r));
  }

  // SSE (text/event-stream) 스트림 — "data: {json}\n\n" 프레이밍
  const dec = new TextDecoder();
  let buf='', errMsg=null, result=null;
  const handle = (obj) => {
    if(!active()) return;
    if(obj.type==='start'){ S.entaProg=ENTA_t('upload.starting',{count:obj.file_count||obj.total||0}); render(); }
    else if(obj.type==='progress'){ S.entaProg=ENTA_t('upload.processing',{index:obj.index||obj.done||0,total:obj.total||obj.file_count||0,filename:obj.filename||obj.file||''}); render(); }
    else if(obj.type==='error'){ errMsg = ENTA_errorText(obj.code||obj.message||obj.detail||ENTA_t('upload.text_031')); }
    else if(obj.type==='result'){ result=obj; S.entaProg=ENTA_t('upload.text_032'); render(); }
  };
  try{
    const reader = r.body.getReader();
    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      if(!active()){ await reader.cancel(); return null; }
      buf += dec.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf('\n\n')) >= 0){
        const chunk = buf.slice(0, idx); buf = buf.slice(idx+2);
        for(const line of chunk.split('\n')){
          const t = line.trim();
          if(!t.startsWith('data:')) continue;
          try{ handle(JSON.parse(t.slice(5).trim())); }catch(e){}
        }
      }
    }
  }catch(e){ errMsg = errMsg || ENTA_t('upload.zip_disconnected'); }

  if(!active()) return null;
  if(errMsg || !result) return fail(errMsg||ENTA_t('upload.zip_no_result'));
  const previous=S.entaSkipped;
  S.entaSkipped={cid,owner,items:[...(previous?.cid===cid && previous.owner===owner?previous.items:[]),...(result.skipped_files||[])]};
  S.entaProg = ENTA_t('upload.text_033');
  if(options.bulk) return result;
  try{
    const pending=await apiFetch(`/cases/${cid}/pending-extractions`);
    if(pending.ok){
      const n=((await pending.json()).items||[]).filter(it=>it.kind==='material_names').length;
      if(n) S.entaProg+=' '+ENTA_t('upload.materials_pending',{count:n});
    }
  }catch(e){ /* 처리 결과는 유지한다. */ }
  await loadEnt();
  if(typeof PKG_load==='function') await PKG_load(cid);
  if(active()){ render(); toast(ENTA_t('upload.text_033')); }
  return result;
};

App.entaRefund = function(invoiceId){
  openModal(ENTA_t('upload.text_015'), `
    <div class="field"><label>${ENTA_t('upload.text_017')} <span class="req">*</span></label>
      <textarea class="in" id="enta-refund-reason" rows="3" placeholder="${ENTA_t('upload.text_022')}"></textarea></div>
    <div class="field"><label>${ENTA_t('upload.text_018')}</label>
      <input class="in" id="enta-refund-amount" inputmode="numeric" placeholder="${ENTA_t('upload.text_023')}"></div>
    <p class="muted" style="font-size:12px">${ENTA_t('upload.text_019')}</p>`,
    `<button class="btn" onclick="App.closeModal()">${ENTA_t('upload.text_020')}</button>
     <button class="btn btn-primary" onclick="App.entaRefundSubmit('${invoiceId}')">${ENTA_t('upload.text_021')}</button>`);
};

App.entaRefundSubmit = async function(invoiceId){
  const reason = (($('#enta-refund-reason')||{}).value||'').trim();
  const amtRaw = (($('#enta-refund-amount')||{}).value||'').trim();
  if(!reason){ const el=$('#enta-refund-reason'); if(el) el.focus(); return toast(ENTA_t('upload.text_034')); }
  const body = {reason};
  if(amtRaw){ const n = Number(amtRaw.replace(/[^0-9.]/g,'')); if(!isNaN(n)) body.amount = n; }
  try{
    const r = await apiFetch(`/invoices/${invoiceId}/refund/request`, {method:'POST', body:JSON.stringify(body)});
    if(!r.ok){
      if(r.status===403) return toast(ENTA_t('upload.text_035'));
      let code=''; try{ code=(await r.json()).code; }catch(e){}
      return toast(ENTA_t('upload.text_036')+(code?` (${code})`:''));
    }
    App.closeModal();
    await loadEnt();
    render(); toast(ENTA_t('upload.text_037'));
  }catch(e){ toast(ENTA_t('upload.text_036')); }
};
