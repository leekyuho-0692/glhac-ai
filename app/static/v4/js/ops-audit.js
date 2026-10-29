/* ===== 관리자 운영 도구 · 감사·모니터링 탭 ===== */
const OPSA_LIMIT = 100;

RS.opsa = RS.opsa || {};

// ── 1. 감사 로그 ─────────────────────────────────────────
async function OPSA_loadLogs(){
  try{
    const q = `?limit=${OPSA_LIMIT}&offset=0`;
    const r = await apiFetch('/admin/audit-logs' + q);
    RS.opsa.logs = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.logs = {error:true}; }
}
async function OPSA_loadVerify(){
  try{
    const r = await apiFetch('/admin/audit-log-verify');
    RS.opsa.verify = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.verify = {error:true}; }
}
function OPSA_renderLogs(){
  const d = RS.opsa.logs;
  if(!d){
    if(RS.opsa._loadingLogs!==1){ RS.opsa._loadingLogs=1; OPSA_loadLogs().then(()=>{ RS.opsa._loadingLogs=0; render(); }); }
    return '<section class="panel"><h2>감사 로그</h2><p class="empty">불러오는 중…</p></section>';
  }
  if(d.error){
    const msg = d.status===403 ? '이 기능은 권한이 없습니다(필요 역할: admin).' : '감사 로그를 불러오지 못했습니다.';
    return `<section class="panel"><h2>감사 로그</h2><p class="empty">${esc(msg)}</p></section>`;
  }
  const items = d.items || [];
  const shown = items.slice(0, OPSA_LIMIT);
  const total = d.total || items.length;
  const over = total - shown.length;
  const v = RS.opsa.verify;
  let verifyHtml = '';
  if(v === undefined){
    if(RS.opsa._loadingVerify!==1){ RS.opsa._loadingVerify=1; OPSA_loadVerify().then(()=>{ RS.opsa._loadingVerify=0; render(); }); }
    verifyHtml = '<p class="note muted">해시체인 무결성 검증 중…</p>';
  } else if(v && v.error){
    verifyHtml = `<p class="note attn">체인 검증을 실행하지 못했습니다${v.status===403?' (권한 없음)':''}.</p>`;
  } else {
    const ok = v.integrity_ok;
    const tampered = v.tampered_count||0;
    const gaps = (v.deleted_predecessor||[]).length;
    const parts = [];
    parts.push(`검사 ${v.checked}행 · ${esc(v.summary||'')}`);
    if(v.break_at) parts.push(`첫 단절 위치: ${esc(v.break_at)}`);
    if(tampered) parts.push(`위조 의심 행: ${(v.tampered||[]).map(x=>esc(x)).join(', ')}`);
    if(gaps) parts.push(`선행로그 삭제 단절: ${(v.deleted_predecessor||[]).map(x=>esc(x)).join(', ')}`);
    if(v.note) parts.push(esc(v.note));
    verifyHtml = `<p class="note ${ok?'':'attn'}">${ok?'무결성 정상 · ':''}${parts.join(' · ')}</p>`;
  }
  const rows = shown.length
    ? shown.map(x=>`<tr>
        <td class="mono" style="white-space:nowrap">${esc(x.created_at||'')}</td>
        <td>${esc(x.actor_id||'')}<span class="muted"> ${esc(x.actor_role||'')}</span>${x.org_id?` <span class="muted mono">${esc(x.org_id)}</span>`:''}</td>
        <td>${esc(x.action||'')}</td>
        <td>${esc(x.resource_type||'')}${x.resource_id?` <span class="muted mono">${esc(x.resource_id)}</span>`:''}${x.case_id?` <span class="muted">case ${esc(x.case_id)}</span>`:''}</td>
      </tr>`).join('')
    : '<tr><td colspan="4" class="empty">로그가 없습니다.</td></tr>';
  return `<section class="panel">
    <div class="panel-head"><h2>감사 로그</h2>
      <div><span class="count">${total}건</span>
      <button class="btn btn-sm" onclick="App.opsaVerify()">해시체인 무결성 검증</button></div>
    </div>
    ${verifyHtml}
    <div class="tbl-wrap"><table>
      <thead><tr><th>시각</th><th>사용자·역할</th><th>행위</th><th>대상</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${over>0?`<p class="muted">외 ${over}건</p>`:''}
  </section>`;
}
App.opsaVerify = async function(){
  try{
    const r = await apiFetch('/admin/audit-log-verify');
    if(!r.ok) return toast(r.status===403?'권한이 없습니다(필요 역할: admin).':'검증에 실패했습니다.');
    RS.opsa.verify = await r.json();
    toast('해시체인 검증 완료');
    render();
  }catch(e){ toast('검증에 실패했습니다.'); }
};

// ── 2. 워크플로 모니터 ────────────────────────────────────
async function OPSA_loadWf(){
  try{
    const r = await apiFetch('/admin/workflow-monitor');
    RS.opsa.wf = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.wf = {error:true}; }
}
function OPSA_gateLabel(g){
  return g==='ok'?'통과' : g==='wait'?'대기' : '—';
}
function OPSA_renderWf(){
  const d = RS.opsa.wf;
  if(!d){
    if(RS.opsa._loadingWf!==1){ RS.opsa._loadingWf=1; OPSA_loadWf().then(()=>{ RS.opsa._loadingWf=0; render(); }); }
    return '<section class="panel"><h2>워크플로 모니터</h2><p class="empty">불러오는 중…</p></section>';
  }
  if(d.error){
    const msg = d.status===403 ? '이 기능은 권한이 없습니다(필요 역할: fatwa_liaison, operator).' : '워크플로 모니터를 불러오지 못했습니다.';
    return `<section class="panel"><h2>워크플로 모니터</h2><p class="empty">${esc(msg)}</p></section>`;
  }
  const cases = d.cases || d.rows || [];
  const pipeline = d.pipeline || [];
  const gateSum = d.gate_sum || {};
  const blocked = d.blocked_total != null ? d.blocked_total : cases.filter(c=>(c.blocker_count||0)>0).length;
  const cls = (g)=> 'tag';
  const pipelineHtml = pipeline.length
    ? pipeline.map(p=>`<span class="tag">${esc(p.label||p.key)} <span class="count">${p.count}</span></span>`).join(' ')
    : '<span class="muted">단계 정보가 없습니다.</span>';
  const summary = `<div class="summary">
    <div><span class="k">전체 케이스</span><span class="v mono">${cases.length}</span></div>
    <div><span class="k">차단</span><span class="v mono">${blocked}</span></div>
    <div><span class="k">결제 통과/대기</span><span class="v mono">${(gateSum.payment||{}).ok||0} / ${(gateSum.payment||{}).wait||0}</span></div>
    <div><span class="k">AI 사전평가 통과/대기</span><span class="v mono">${(gateSum.ai_report||{}).ok||0} / ${(gateSum.ai_report||{}).wait||0}</span></div>
    <div><span class="k">파트와 통과/대기</span><span class="v mono">${(gateSum.fatwa||{}).ok||0} / ${(gateSum.fatwa||{}).wait||0}</span></div>
  </div>`;
  const rows = cases.length
    ? cases.map(c=>`<tr>
        <td>${esc(c.company||c.company_name||'')}</td>
        <td>${esc(c.phase_label||c.phase_key||'')}</td>
        <td>${esc(c.status_label||c.status||'')}</td>
        <td>${esc(c.owner||'')}</td>
        <td>${c.next_label?esc(c.next_label):'<span class="muted">—</span>'}</td>
        <td>${(c.blocker_count>0)?`<span class="badge attn">${c.blocker_count}</span> ${(c.blockers||[]).map(b=>esc(b)).join(', ')}`:'<span class="muted">—</span>'}</td>
        <td class="mono">${esc(c.due_date||'')}</td>
        <td><span class="tag">결제 ${OPSA_gateLabel((c.gates||{}).payment)}</span> <span class="tag">AI ${OPSA_gateLabel((c.gates||{}).ai_report)}</span> <span class="tag">파트와 ${OPSA_gateLabel((c.gates||{}).fatwa)}</span></td>
      </tr>`).join('')
    : '<tr><td colspan="8" class="empty">케이스가 없습니다.</td></tr>';
  return `<section class="panel">
    <div class="panel-head"><h2>워크플로 모니터</h2><span class="count">${cases.length}건</span></div>
    ${summary}
    <div style="margin:12px 0">${pipelineHtml}</div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>업체</th><th>단계</th><th>상태</th><th>담당</th><th>다음 전이</th><th>차단</th><th>기한</th><th>게이트</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
  </section>`;
}

// ── 3. 알림 발송 큐·채널 ──────────────────────────────────
async function OPSA_loadChannels(){
  try{
    const r = await apiFetch('/admin/notify-channels');
    RS.opsa.channels = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.channels = {error:true}; }
}
async function OPSA_loadNotifs(){
  try{
    const r = await apiFetch('/admin/notifications?limit='+OPSA_LIMIT);
    RS.opsa.notifs = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.notifs = {error:true}; }
}
function OPSA_channelBadge(ch){
  const state = ch.status || (ch.configured ? 'connected' : (ch.implemented ? 'unset' : 'stub'));
  if(state==='connected') return '<span class="badge strong">연결됨</span>';
  if(state==='unset') return '<span class="badge attn">미설정</span>';
  if(state==='stub') return '<span class="badge">스텁</span>';
  return `<span class="badge">${esc(state)}</span>`;
}
function OPSA_renderNotifs(){
  const c = RS.opsa.channels;
  const n = RS.opsa.notifs;
  let chHtml, qHtml;

  if(c === undefined){
    if(RS.opsa._loadingCh!==1){ RS.opsa._loadingCh=1; OPSA_loadChannels().then(()=>{ RS.opsa._loadingCh=0; render(); }); }
    chHtml = '<p class="empty">불러오는 중…</p>';
  } else if(c.error){
    chHtml = `<p class="empty">${c.status===403?'이 기능은 권한이 없습니다(필요 역할: operator).':'채널 상태를 불러오지 못했습니다.'}</p>`;
  } else {
    const list = c.channels || {};
    const keys = Object.keys(list);
    chHtml = keys.length
      ? `<div class="chipbar">${keys.map(k=>{
          const ch = list[k]||{};
          return `<span class="chip">${esc(k)} ${OPSA_channelBadge(ch)}</span>`;
        }).join('')}</div>`
      : '<p class="muted">채널 정보가 없습니다.</p>';
  }

  if(n === undefined){
    if(RS.opsa._loadingN!==1){ RS.opsa._loadingN=1; OPSA_loadNotifs().then(()=>{ RS.opsa._loadingN=0; render(); }); }
    qHtml = '<p class="empty">불러오는 중…</p>';
  } else if(n.error){
    qHtml = `<p class="empty">${n.status===403?'이 기능은 권한이 없습니다(필요 역할: operator).':'알림 큐를 불러오지 못했습니다.'}</p>`;
  } else {
    const counts = n.counts || {};
    const items = (n.items || []).slice(0, OPSA_LIMIT);
    const summ = `<div class="summary">
      <div><span class="k">미발송</span><span class="v mono">${counts.unsent||0}</span></div>
      <div><span class="k">발송됨</span><span class="v mono">${counts.sent||0}</span></div>
      <div><span class="k">실패</span><span class="v mono">${counts.failed||0}</span></div>
      <div><span class="k">발송중</span><span class="v mono">${counts.sending||0}</span></div>
    </div>`;
    const rows = items.length
      ? items.map(x=>`<tr>
          <td>${esc(x.title||x.event_type||'')}<div class="muted mono" style="font-size:11px">${esc(x.event_type||'')}</div></td>
          <td>${(x.channels||[]).map(ch=>`<span class="tag">${esc(ch)}</span>`).join(' ')}</td>
          <td><span class="badge ${x.status==='sent'?'strong':(x.status==='failed'?'attn':'')}">${esc(x.status||'')}</span></td>
          <td class="num mono">${x.attempts!=null?esc(x.attempts):''}</td>
          <td>${x.last_error?`<span class="muted">${esc(x.last_error)}</span>`:'<span class="muted">—</span>'}</td>
          <td class="mono" style="white-space:nowrap">${esc(x.created_at||'')}</td>
        </tr>`).join('')
      : '<tr><td colspan="6" class="empty">알림이 없습니다.</td></tr>';
    qHtml = `${summ}
      <div class="tbl-wrap"><table>
        <thead><tr><th>제목/이벤트</th><th>채널</th><th>상태</th><th class="num">시도</th><th>마지막 오류</th><th>시각</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      ${n.total>items.length?`<p class="muted">외 ${n.total-items.length}건</p>`:''}`;
  }

  return `<section class="panel">
    <div class="panel-head"><h2>알림 발송 큐·채널</h2></div>
    ${chHtml}
    <div style="margin-top:16px">
      <div class="panel-head"><h2>알림 큐</h2>
        <button class="btn btn-sm btn-primary" onclick="App.opsaDrainConfirm()">큐 비우기(발송 실행)</button>
      </div>
      ${qHtml}
    </div>
  </section>`;
}
App.opsaDrainConfirm = function(){
  openModal('큐 비우기(발송 실행)', '<p>미발송 알림 큐를 즉시 발송합니다.<br>크리덴셜 미설정 채널은 스텁 로그 후 <strong>sent</strong> 처리됩니다. 계속할까요?</p>',
    '<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="App.opsaDrain()">실행</button>');
};
App.opsaDrain = async function(){
  try{
    const r = await apiFetch('/admin/notifications/drain', {method:'POST', body:JSON.stringify({})});
    if(!r.ok){
      let extra='';
      try{ const j = await r.json(); if(j && j.detail && j.detail.code) extra = ' (' + j.detail.code + ')'; }catch(e){}
      App.closeModal();
      return toast(r.status===403?'권한이 없습니다(필요 역할: operator).':'큐 발송 실행에 실패했습니다.'+extra);
    }
    const stats = await r.json();
    App.closeModal();
    delete RS.opsa.notifs;
    await OPSA_loadNotifs();
    toast('큐 발송 실행 완료');
    render();
  }catch(e){ App.closeModal(); toast('큐 발송 실행에 실패했습니다.'); }
};

// ── 4. 컴플라이언스·전자서명 상태 ─────────────────────────
async function OPSA_loadCompliance(){
  try{
    const r = await apiFetch('/admin/compliance');
    RS.opsa.compliance = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.compliance = {error:true}; }
}
async function OPSA_loadPsre(){
  try{
    const r = await apiFetch('/admin/psre-status');
    RS.opsa.psre = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.psre = {error:true}; }
}
async function OPSA_loadTsa(){
  try{
    const r = await apiFetch('/admin/tsa-status');
    RS.opsa.tsa = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.tsa = {error:true}; }
}
function OPSA_statusBadge(st){
  if(st==='met') return '<span class="badge strong">충족</span>';
  if(st==='pending') return '<span class="badge attn">대기</span>';
  if(st==='action_required') return '<span class="badge attn">조치 필요</span>';
  if(st==='not_configured') return '<span class="badge">미설정</span>';
  return `<span class="badge">${esc(st||'')}</span>`;
}
function OPSA_renderCompliance(){
  const comp = RS.opsa.compliance;
  const psre = RS.opsa.psre;
  const tsa = RS.opsa.tsa;

  let compHtml;
  if(!canCall('GET','/admin/compliance')){
    compHtml = null;   // admin 전용(main.py:13237) — operator 에겐 패널 생략
  } else if(comp === undefined){
    if(RS.opsa._loadingComp!==1){ RS.opsa._loadingComp=1; OPSA_loadCompliance().then(()=>{ RS.opsa._loadingComp=0; render(); }); }
    compHtml = '<p class="empty">불러오는 중…</p>';
  } else if(comp.error){
    compHtml = `<p class="empty">${comp.status===403?'이 기능은 권한이 없습니다(필요 역할: admin).':'컴플라이언스 상태를 불러오지 못했습니다.'}</p>`;
  } else {
    const items = comp.items || comp || [];
    compHtml = Array.isArray(items) && items.length
      ? `<div class="dl">${items.map(it=>`<div>
          <dt>${esc(it.label||it.key||'')} ${OPSA_statusBadge(it.status)}</dt>
          <dd>${esc(it.detail||'')}</dd>
        </div>`).join('')}</div>`
      : '<p class="muted">항목이 없습니다.</p>';
  }

  let psreHtml;
  if(psre === undefined){
    if(RS.opsa._loadingPsre!==1){ RS.opsa._loadingPsre=1; OPSA_loadPsre().then(()=>{ RS.opsa._loadingPsre=0; render(); }); }
    psreHtml = '<p class="empty">불러오는 중…</p>';
  } else if(psre.error){
    psreHtml = `<p class="empty">${psre.status===403?'이 기능은 권한이 없습니다(필요 역할: operator).':'PSrE 상태를 불러오지 못했습니다.'}</p>`;
  } else {
    const p = psre.psre || {};
    psreHtml = `<div class="dl">
      <div><dt>설정 여부</dt><dd>${OPSA_statusBadge(p.configured?'met':'not_configured')}</dd></div>
      <div><dt>제공자</dt><dd class="mono">${esc(p.provider||'—')}</dd></div>
      <div><dt>구현</dt><dd>${p.implemented?'<span class="badge strong">구현됨</span>':'<span class="badge">미구현</span>'}</dd></div>
      <div><dt>상태</dt><dd>${esc(p.status||'')}</dd></div>
      <div><dt>비고</dt><dd class="muted">${esc(p.note||'')}</dd></div>
    </div>`;
  }

  let tsaHtml;
  if(tsa === undefined){
    if(RS.opsa._loadingTsa!==1){ RS.opsa._loadingTsa=1; OPSA_loadTsa().then(()=>{ RS.opsa._loadingTsa=0; render(); }); }
    tsaHtml = '<p class="empty">불러오는 중…</p>';
  } else if(tsa.error){
    tsaHtml = `<p class="empty">${tsa.status===403?'이 기능은 권한이 없습니다(필요 역할: operator).':'TSA 상태를 불러오지 못했습니다.'}</p>`;
  } else {
    const t = tsa.tsa || {};
    tsaHtml = `<div class="dl">
      <div><dt>설정 여부</dt><dd>${OPSA_statusBadge(t.configured?'met':'not_configured')}</dd></div>
      <div><dt>구현</dt><dd>${t.implemented?'<span class="badge strong">구현됨</span>':'<span class="badge">미구현</span>'}</dd></div>
      <div><dt>상태</dt><dd>${esc(t.status||'')}</dd></div>
      <div><dt>비고</dt><dd class="muted">${esc(t.note||'')}</dd></div>
    </div>`;
  }

  return `${compHtml===null?'':`<section class="panel">
    <div class="panel-head"><h2>컴플라이언스 상태</h2></div>
    ${compHtml}
  </section>`}
  <section class="panel">
    <div class="panel-head"><h2>PSrE 공인 전자서명</h2></div>
    ${psreHtml}
  </section>
  <section class="panel">
    <div class="panel-head"><h2>TSA 신뢰 타임스탬프</h2></div>
    ${tsaHtml}
  </section>`;
}

// ── 메일 발송 기록(admin) ─────────────────────────────────
// 알림·임시 비밀번호·영업 QR 메일이 누구에게 언제 나갔는지, 실패 사유, 내용(클릭).
// 임시 비밀번호는 서버가 가려서 저장한다. 내용 열람은 감사로그에 남는다(mail_log.view).
const OPSA_MAIL_PAGE = 50;
RS.opsa.mailF = RS.opsa.mailF || {kind:'', status:'', q:'', offset:0};
async function OPSA_loadMail(){
  const f = RS.opsa.mailF;
  const qs = new URLSearchParams({limit:OPSA_MAIL_PAGE, offset:f.offset});
  if(f.kind) qs.set('kind', f.kind);
  if(f.status) qs.set('status', f.status);
  if(f.q) qs.set('q', f.q);
  try{
    const r = await apiFetch('/admin/mail-log?' + qs.toString());
    RS.opsa.mail = r.ok ? (await r.json()) : {error:true, status:r.status};
  }catch(e){ RS.opsa.mail = {error:true}; }
}
function OPSA_when(iso){
  if(!iso) return '';
  const d = new Date(iso);
  if(isNaN(d)) return iso;
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
/* EN/ID 화면 번역(i18n-dom 런타임 사전) — 이 스크립트가 사전보다 먼저 로드돼 첫 렌더 때 보탠다.
   이미 있는 문구는 덮지 않는다(다른 화면 번역을 바꾸지 않게). */
const OPSA_MAIL_I18N = {
  '메일 발송 기록': ['Mail send log', 'Riwayat pengiriman email'],
  '메일 발송 기록을 불러오지 못했습니다.': ['Could not load the mail send log.', 'Gagal memuat riwayat pengiriman email.'],
  '메일 발송 기록이 없습니다.': ['No mail has been sent yet.', 'Belum ada email yang dikirim.'],
  '알림·임시 비밀번호·영업 QR 메일의 발송 결과입니다. 행을 누르면 보낸 내용을 볼 수 있습니다(열람은 감사로그에 남습니다). 임시 비밀번호는 가려서 저장됩니다.':
    ['Delivery results of notification, temporary password and sales QR mails. Click a row to see what was sent (viewing is recorded in the audit log). Temporary passwords are stored masked.',
     'Hasil pengiriman email notifikasi, kata sandi sementara, dan QR penjualan. Klik baris untuk melihat isi yang dikirim (tercatat di log audit). Kata sandi sementara disimpan tersamar.'],
  '받는 주소·제목·케이스 검색': ['Search recipient · subject · case', 'Cari penerima · subjek · kasus'],
  '전체 종류': ['All types', 'Semua jenis'], '전체 상태': ['All statuses', 'Semua status'],
  '조회 결과': ['Results', 'Hasil'], '보낸 시각': ['Sent at', 'Waktu kirim'], '받는 사람': ['Recipient', 'Penerima'],
  '메일 내용': ['Mail content', 'Isi email'], '내용을 불러오지 못했습니다.': ['Could not load the content.', 'Gagal memuat isi.'],
  '임시 비밀번호': ['Temporary password', 'Kata sandi sementara'], '영업 QR': ['Sales QR', 'QR penjualan'],
  '발송됨': ['Sent', 'Terkirim'], '실패': ['Failed', 'Gagal'], '새로고침': ['Refresh', 'Muat ulang'],
  '조회': ['Search', 'Cari'], '종류': ['Type', 'Jenis'], '제목': ['Subject', 'Subjek'], '결과': ['Result', 'Hasil'],
  '이전': ['Previous', 'Sebelumnya'], '다음': ['Next', 'Berikutnya'], '닫기': ['Close', 'Tutup'], '케이스': ['Case', 'Kasus'],
};
function OPSA_mailI18n(){
  if(OPSA_mailI18n.done || typeof I18N_DOM === 'undefined' || !I18N_DOM) return;
  [['en', 0], ['id', 1]].forEach(([lang, i]) => {
    const d = I18N_DOM[lang];
    if(!d) return;
    Object.keys(OPSA_MAIL_I18N).forEach(k => { if(!(k in d)) d[k] = OPSA_MAIL_I18N[k][i]; });
  });
  if(typeof I18D_RE_CACHE !== 'undefined') Object.keys(I18D_RE_CACHE).forEach(k => delete I18D_RE_CACHE[k]);
  OPSA_mailI18n.done = true;
}
function OPSA_renderMail(){
  OPSA_mailI18n();
  const d = RS.opsa.mail, f = RS.opsa.mailF;
  const head = '<div class="panel-head"><h2>메일 발송 기록</h2><button class="btn btn-sm" onclick="App.opsaMailReload()">새로고침</button></div>';
  if(!d){
    if(RS.opsa._loadingMail!==1){ RS.opsa._loadingMail=1; OPSA_loadMail().then(()=>{ RS.opsa._loadingMail=0; render(); }); }
    return `<section class="panel">${head}<p class="empty">불러오는 중…</p></section>`;
  }
  if(d.error){
    return `<section class="panel">${head}<p class="empty">${d.status===403?'이 기능은 권한이 없습니다(필요 역할: admin).':'메일 발송 기록을 불러오지 못했습니다.'}</p></section>`;
  }
  const kinds = d.kinds || {};
  const opt = (v, label, cur) => `<option value="${esc(v)}"${v===cur?' selected':''}>${esc(label)}</option>`;
  const filters = `<form class="chipbar" onsubmit="App.opsaMailFilter(event)" style="gap:8px;flex-wrap:wrap;margin-bottom:12px">
      <select class="in" name="kind" style="width:auto">${opt('', '전체 종류', f.kind)}${Object.keys(kinds).map(k=>opt(k, kinds[k], f.kind)).join('')}</select>
      <select class="in" name="status" style="width:auto">${opt('', '전체 상태', f.status)}${opt('sent','발송됨',f.status)}${opt('failed','실패',f.status)}</select>
      <input class="in" name="q" value="${esc(f.q)}" placeholder="받는 주소·제목·케이스 검색" style="width:240px">
      <button class="btn btn-sm btn-primary" type="submit">조회</button>
    </form>`;
  const c = d.counts || {};
  const summ = `<div class="summary">
      <div><span class="k">발송됨</span><span class="v mono">${c.sent||0}</span></div>
      <div><span class="k">실패</span><span class="v mono">${c.failed||0}</span></div>
      <div><span class="k">조회 결과</span><span class="v mono">${d.total||0}</span></div>
    </div>`;
  const items = d.items || [];
  RS.opsa.mailIndex = {};
  items.forEach(x => { RS.opsa.mailIndex[x.log_id] = x; });
  const rows = items.length
    ? items.map(x=>`<tr style="cursor:pointer" onclick="App.opsaMailOpen('${esc(x.log_id)}')" title="눌러서 내용 보기">
        <td class="mono" style="white-space:nowrap">${esc(OPSA_when(x.created_at))}</td>
        <td style="white-space:nowrap"><span class="tag">${esc(x.kind_label||x.kind||'')}</span></td>
        <td class="mono">${esc(x.recipient||'')}</td>
        <td>${esc(x.subject||'')}${x.case_id?`<div class="muted mono" style="font-size:11px">case ${esc(x.case_id)}</div>`:''}</td>
        <td><span class="badge ${x.status==='sent'?'strong':'attn'}">${x.status==='sent'?'발송됨':'실패'}</span>${x.error?`<div class="muted" style="font-size:11px">${esc(x.error)}</div>`:''}</td>
      </tr>`).join('')
    : '<tr><td colspan="5" class="empty">메일 발송 기록이 없습니다.</td></tr>';
  const from = d.total ? f.offset + 1 : 0, to = f.offset + items.length;
  const pager = `<div class="chipbar" style="justify-content:space-between;margin-top:8px">
      <span class="muted">${from}–${to} / ${d.total||0}건</span>
      <span><button class="btn btn-sm" ${f.offset<=0?'disabled':''} onclick="App.opsaMailPage(-1)">이전</button>
      <button class="btn btn-sm" ${to>=(d.total||0)?'disabled':''} onclick="App.opsaMailPage(1)">다음</button></span>
    </div>`;
  return `<section class="panel">${head}
    <p class="muted" style="margin-top:0">알림·임시 비밀번호·영업 QR 메일의 발송 결과입니다. 행을 누르면 보낸 내용을 볼 수 있습니다(열람은 감사로그에 남습니다). 임시 비밀번호는 가려서 저장됩니다.</p>
    ${filters}${summ}
    <div class="tbl-wrap"><table>
      <thead><tr><th>보낸 시각</th><th>종류</th><th>받는 사람</th><th>제목</th><th>결과</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>${pager}
  </section>`;
}
App.opsaMailReload = function(){ RS.opsa.mail = undefined; render(); };
App.opsaMailFilter = function(e){
  e.preventDefault();
  const el = e.currentTarget.elements;
  RS.opsa.mailF = {kind:el.kind.value, status:el.status.value, q:el.q.value.trim(), offset:0};
  App.opsaMailReload();
};
App.opsaMailPage = function(dir){
  const f = RS.opsa.mailF;
  f.offset = Math.max(0, f.offset + dir * OPSA_MAIL_PAGE);
  App.opsaMailReload();
};
App.opsaMailOpen = async function(id){
  const x = (RS.opsa.mailIndex||{})[id] || {};
  openModal('메일 내용', '<p class="empty">불러오는 중…</p>', '<button class="btn" onclick="App.closeModal()">닫기</button>');
  let m;
  try{
    const r = await apiFetch('/admin/mail-log/' + encodeURIComponent(id));
    m = r.ok ? await r.json() : null;
  }catch(_){ m = null; }
  if(!m){
    openModal('메일 내용', '<p class="empty">내용을 불러오지 못했습니다.</p>', '<button class="btn" onclick="App.closeModal()">닫기</button>');
    return;
  }
  const body = `<div class="dl">
      <div><dt>보낸 시각</dt><dd class="mono">${esc(OPSA_when(m.created_at))}</dd></div>
      <div><dt>종류</dt><dd>${esc(m.kind_label||m.kind||'')}</dd></div>
      <div><dt>받는 사람</dt><dd class="mono">${esc(m.recipient||'')}</dd></div>
      <div><dt>제목</dt><dd>${esc(m.subject||'')}</dd></div>
      <div><dt>결과</dt><dd>${m.status==='sent'?'<span class="badge strong">발송됨</span>':'<span class="badge attn">실패</span>'}${m.error?` <span class="muted">${esc(m.error)}</span>`:''}</dd></div>
      ${m.case_id?`<div><dt>케이스</dt><dd class="mono">${esc(m.case_id)}</dd></div>`:''}
    </div>
    <pre style="white-space:pre-wrap;word-break:break-word;margin-top:12px;padding:12px;border:1px solid var(--line,#ddd);border-radius:8px;max-height:50vh;overflow:auto">${esc(m.body||'')}</pre>`;
  openModal('메일 내용', body, '<button class="btn" onclick="App.closeModal()">닫기</button>', true);
};

// ── 탭 렌더러 ─────────────────────────────────────────────
function OPSA_render(){
  return [
    canCall('GET','/admin/audit-logs') ? OPSA_renderLogs() : '',   // 감사 로그·체인 검증 admin 전용(main.py:1647·2010)
    OPSA_renderWf(),
    OPSA_renderNotifs(),
    canCall('GET','/admin/mail-log') ? OPSA_renderMail() : '',   // 메일 발송 기록 admin 전용
    OPSA_renderCompliance(),
  ].join('');
}

// ── 탭 등록 ───────────────────────────────────────────────
if(typeof OPS_TABS !== 'undefined' && Array.isArray(OPS_TABS)){
  OPS_TABS.push({key:'audit', label:'감사·모니터링', render: OPSA_render});
}
