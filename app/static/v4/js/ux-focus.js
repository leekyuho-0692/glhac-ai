/* 현재 신청의 다음 작업. 서버의 단계/권한은 그대로 사용한다. */
function UX_roleLabel(){
  const special={penyelia_halal:{ko:'할랄 감독자',en:'Halal Supervisor',id:'Penyelia Halal'},operator:{ko:'운영 담당자',en:'Operations',id:'Operasional'}};
  return special[AUTH.role] ? special[AUTH.role][S.lang] || special[AUTH.role].ko : t(roleByCode(S.role).label);
}
function UX_next(c){
  if(c.closed) return ['종결된 신청입니다.','인증 문서함 확인','ent-flow'];
  if(c.done) return ['인증 결과와 발급 문서를 확인하세요.','인증 문서함 확인','ent-flow'];
  if(AUTH.role==='penyelia_halal') return ['기업의 제출 자료와 감독자 정보를 점검하세요.','담당 정보 확인','ent-apply'];
  if(['onboarding','application_draft'].includes(c.status)) return ['기업 정보와 품목을 저장한 뒤 필요한 서류를 제출하세요.','신청서 이어 쓰기','ent-apply'];
  if(c.hold) return ['요청된 보완 내용과 제출 자료를 확인하세요.','보완 요청 확인',c.step8>=3?'ent-prep':'ent-pre'];
  if(c.step8===2) return ['견적과 계약서가 도착하면 내용을 확인하세요.','계약 진행 확인','ent-contract'];
  if(c.step8===3 || c.step8===4) return ['심사 일정과 준비할 자료를 확인하세요.','심사 준비 확인','ent-prep'];
  if(c.step8>=5) return ['담당자의 심의·발급 처리를 기다리고 있습니다.','진행 문서 확인','ent-flow'];
  return ['사전심사 결과와 추가 요청을 확인하세요.','사전심사 결과 확인','ent-pre'];
}
{
  const home=VIEWS['ent-home'];
  VIEWS['ent-home']=()=>{
    const c=(Array.isArray(RS.cases)?RS.cases:[])[0];
    if(!c) return home();
    const [message,label,view]=UX_next(c);
    return `<section class="panel" aria-label="현재 신청의 다음 작업"><div class="panel-head"><h2>지금 할 일</h2><span data-noi18n>${esc(c.company_name||'')}</span></div><p>${esc(message)}</p><div class="row-act" style="margin-top:14px"><button class="btn btn-primary" onclick="App.go('${view}')">${esc(label)}</button></div></section>`+home();
  };
}
// 보조 기능은 필요할 때 펼친다. 기존 이벤트/버튼과 권한 판정은 보존한다.
{
  const draw=render;
  render=function(){
    draw();
    if(!S.role) return;
    const ws=document.querySelector('.side-ws'); if(ws) ws.textContent=UX_roleLabel();
    if(AUTH.role==='operator'){
      const sub=document.querySelector('.side-user span'); if(sub) sub.textContent=UX_roleLabel();
    }
    const page=document.querySelector('.main .page'); if(!page) return;
    if(S.view==='ent-home'){
      [...page.children].forEach(panel=>{
        const h=panel.querySelector('h2');
        if(!h || !['신청 철회','원재료 정리 도구'].includes(h.textContent.trim())) return;
        const details=document.createElement('details'), summary=document.createElement('summary');
        details.className='panel'; summary.textContent=h.textContent;
        details.append(summary); panel.replaceWith(details); details.append(panel);
        panel.style.margin='0'; panel.style.border='0';
      });
    }
    if(S.view==='ent-apply'){
      const product=ENTP_PRODUCTS.get(ENTP_key(ENTP_cid()))||{};
      Object.entries(product).forEach(([id,value])=>{ const el=document.getElementById(id); if(el) el.value=value; });
      const profile=[...page.children].find(p=>p.querySelector('[data-entp-case]'));
      if(profile) page.prepend(profile);
    }
  };
}

// 계약/청구가 아직 없는 상태는 실제 금액이나 기한처럼 보이지 않게 안내한다.
{ const contract=VIEWS['ent-contract']; VIEWS['ent-contract']=()=>{
  const html=contract(), c=(RS.cases||[])[0], d=c && (RS.bleCt||{})[c.case_id];
  if(d && d.contract && d.contract.exists===false && d.quote && !d.quote.quote && Array.isArray(d.invoices) && !d.invoices.length && d.notices && !(d.notices.items||[]).length){
    return CN_bar(false,true)+`<section class="panel"><h2>계약 준비 대기</h2><p>사전심사와 정식 신청 접수 후 담당자가 견적·계약서를 발송합니다. 도착하면 이 화면에서 확인할 수 있습니다.</p><button class="btn" onclick="App.go('ent-pre')">사전심사 결과 확인</button></section>`;
  }
  return html;
}; }

// 새로 추가한 안내 문구의 번역. 사용자 데이터는 i18n-dom에서 별도로 보호한다.
if(typeof I18N_DOM!=='undefined'){
  const texts={
    '기업 정보·담당자':['Company information · Contacts','Informasi perusahaan · Penanggung jawab'],
    '기업 기본 정보':['Company information','Informasi perusahaan'],
    '추가 기업 정보':['Additional company information','Informasi perusahaan tambahan'],
    '저장된 기업 정보':['Company information saved','Informasi perusahaan tersimpan'],
    '저장하지 않은 변경 있음':['Unsaved changes','Perubahan belum disimpan'],
    '기업 정보 임시저장':['Save company draft','Simpan draf perusahaan'],
    '기업 정보를 저장했습니다.':['Company information saved.','Informasi perusahaan tersimpan.'],
    '기업 정보를 저장하지 못했습니다. 입력 내용은 유지됩니다.':['Could not save company information. Your changes are retained.','Informasi perusahaan belum tersimpan. Perubahan tetap dipertahankan.'],
    '기업 정보를 임시저장했습니다. 품목·공장·조직은 각 등록 버튼으로 저장해 주세요.':['Company draft saved. Save products, facilities and team changes using their own buttons.','Draf perusahaan tersimpan. Simpan produk, fasilitas, dan tim dengan tombol masing-masing.'],
    '평가 진행 중':['Assessment in progress','Penilaian berlangsung'],
    '미평가':['Not assessed','Belum dinilai'],
    '미평가 항목 있음':['Some areas not assessed','Ada bagian belum dinilai'],
    '미평가는 미등록 또는 검토 미완료 상태입니다. 준비도는 인증 판정을 대신하지 않습니다.':['Not assessed means data or review is incomplete. Readiness does not replace a certification decision.','Belum dinilai berarti data atau peninjauan belum lengkap. Kesiapan tidak menggantikan keputusan sertifikasi.'],
    '신청서 이어 쓰기':['Continue application','Lanjutkan permohonan'],
    '기업 정보와 품목을 저장한 뒤 필요한 서류를 제출하세요.':['Save company information and products, then submit the required documents.','Simpan informasi perusahaan dan produk, lalu kirim dokumen yang diperlukan.'],
    '계약 준비 대기':['Waiting for contract preparation','Menunggu persiapan kontrak'],
    '사전심사와 정식 신청 접수 후 담당자가 견적·계약서를 발송합니다. 도착하면 이 화면에서 확인할 수 있습니다.':['After pre-assessment and formal intake, your contact will send the quote and contract. You can review them here.','Setelah pra-penilaian dan penerimaan permohonan resmi, petugas akan mengirim penawaran dan kontrak. Anda dapat meninjaunya di sini.'],
    '부서별 담당자':['Department contacts','Penanggung jawab departemen'],
    '입력 중인 품목을 먼저 추가해 주세요.':['Add the product you are editing first.','Tambahkan produk yang sedang diedit terlebih dahulu.'],
    '할랄 관리 조직의 변경 내용을 먼저 저장해 주세요.':['Save your halal team changes first.','Simpan perubahan tim halal terlebih dahulu.']
  };
  for(const [ko,values] of Object.entries(texts)){ for(const [i,lang] of ['en','id'].entries()){ if(I18N_DOM[lang]) I18N_DOM[lang][ko]=values[i]; } }
  if(typeof I18D_RE_CACHE!=='undefined'){ delete I18D_RE_CACHE.en; delete I18D_RE_CACHE.id; }
}
