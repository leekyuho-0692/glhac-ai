/* ===== 생성 문서 보강 (기업 정보서·보고서 재제출·심의 문서·성분 보고서) ===== */
const GDM_ERR = (r, d) => {
  if (r.status === 403) return '이 작업은 권한이 없습니다.';
  const code = d && d.detail && d.detail.code ? ` (${d.detail.code})` : '';
  return '요청에 실패했습니다.' + code;
};

async function GDM_post(path, body){
  try{
    const r = await apiFetch(path, {method:'POST', body: JSON.stringify(body || {})});
    let d = {}; try{ d = await r.json(); }catch(e){}
    return {ok:r.ok, status:r.status, data:d};
  }catch(e){ return {ok:false, status:0, data:{}}; }
}

async function GDM_get(path){
  try{ const r = await apiFetch(path); let d={}; try{ d = await r.json(); }catch(e){}
    return {ok:r.ok, status:r.status, data:d}; }
  catch(e){ return {ok:false, status:0, data:{}}; }
}

function GDM_preModal(title, text){
  const body = `<pre style="white-space:pre-wrap;margin:0;font-size:12.5px;line-height:1.6">${esc(text)}</pre>`;
  openModal(esc(title), body, `<button class="btn" onclick="App.closeModal()">닫기</button>`);
}

function GDM_htmlModal(title, html){
  openModal(esc(title), html, `<button class="btn" onclick="App.closeModal()">닫기</button>`, true);
}

/* 기업 정보서 미리보기(JSON 구조를 사람이 읽게 요약, 구조 미확정 필드는 방어적으로) */
function GDM_ciFormHtml(form){
  if(!form || typeof form !== 'object') return `<p class="empty">미리보기 데이터가 없습니다.</p>`;
  const secs = Array.isArray(form.sections) ? form.sections : [];
  if(!secs.length){
    return `<div class="note">${esc(JSON.stringify(form))}</div>`;
  }
  return secs.map(s=>{
    const rows = (s.rows||[]).map(r=>{
      const lab = r.label_ko || r.label_en || '';
      const v = (r.value==null||r.value==='') ? '-' : String(r.value);
      return `<div><dt>${esc(lab)}</dt><dd>${esc(v)}</dd></div>`;
    }).join('');
    return `<fieldset style="margin-bottom:14px"><legend>${esc(s.name||'')}</legend><div class="dl">${rows}</div></fieldset>`;
  }).join('');
}

/* ── A. 컨설턴트: 기업 정보서 ── */
function GDM_coPanel(){
  const list = (RS.cases||[]).filter(c=>!c.done);
  if(!list.length) return `<section class="panel"><p class="empty">대상 업체가 없습니다.</p></section>`;
  let cur = (S.selDocCo && list.find(c=>c.case_id===S.selDocCo)) ? S.selDocCo : list[0].case_id;
  S.selDocCo = cur;
  const opts = list.map(c=>`<option value="${esc(c.case_id)}" ${c.case_id===cur?'selected':''}>${esc(c.company_name||c.case_id.slice(0,8))}</option>`).join('');
  return `<section class="panel">
    <div class="panel-head"><h2>기업 정보서 (Form.1)</h2><span class="count">${list.length}개사</span></div>
    <div class="field"><label>대상 업체</label>
      <select class="in" onchange="App.gdmCoSel(this.value)">${opts}</select></div>
    <div class="row-act" style="margin-top:10px">
      <button class="btn btn-primary" onclick="App.gdmCoGen()">기업 정보서 생성</button>
      <button class="btn" onclick="App.gdmCoPrev()">미리보기</button>
      <button class="btn btn-ghost" onclick="App.gdmCoPdf()">PDF</button>
    </div>
    <p class="note" style="margin-top:10px">회원가입 OCR·신청서 자동채움 값으로 생성됩니다. 생성 결과는 생성문서(company_info) 이력으로 저장됩니다.</p>
  </section>`;
}
App.gdmCoSel = function(id){ S.selDocCo = id; render(); };
App.gdmCoGen = async function(){
  const cid = S.selDocCo; if(!cid) return;
  const r = await GDM_post(`/cases/${cid}/docs/company-info`, {});
  if(!r.ok) return toast(GDM_ERR(r, r.data));
  toast('기업 정보서를 생성했습니다.');
  GDM_preModal('기업 정보서', r.data.document || '');
};
App.gdmCoPrev = async function(){
  const cid = S.selDocCo; if(!cid) return;
  const r = await GDM_get(`/cases/${cid}/docs/company-info/preview`);
  if(!r.ok) return toast(GDM_ERR(r, r.data));
  const d = r.data;
  if(d && typeof d === 'object' && !Array.isArray(d) && d.document){
    return GDM_preModal('기업 정보서 미리보기', d.document);
  }
  GDM_htmlModal('기업 정보서 미리보기', GDM_ciFormHtml(d));
};
App.gdmCoPdf = function(){
  const cid = S.selDocCo; if(!cid) return;
  const nm = `company-info_${cid.slice(0,8)}.pdf`;
  App.gdmDownload(`/cases/${cid}/docs/company-info.pdf`, nm);
};

/* 공용 blob 다운로드 */
App.gdmDownload = async function(path, filename){
  try{
    const r = await apiFetch(path);
    if(r.status===403) return toast('이 파일을 내려받을 권한이 없습니다.');
    if(!r.ok) return toast('파일을 불러올 수 없습니다.');
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = filename;
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('파일을 불러올 수 없습니다.'); }
};

/* ── B. 컨설턴트: 보고서 재제출 ── */
function GDM_resubPanel(){
  const list = (RS.cases||[]).filter(c=>!c.done);
  if(!list.length) return `<section class="panel"><p class="empty">대상 업체가 없습니다.</p></section>`;
  let cur = (S.selCompany && list.find(c=>c.case_id===S.selCompany)) ? S.selCompany : list[0].case_id;
  S.selCompany = cur;
  const opts = list.map(c=>`<option value="${esc(c.case_id)}" ${c.case_id===cur?'selected':''}>${esc(c.company_name||c.case_id.slice(0,8))}</option>`).join('');
  return `<section class="panel">
    <div class="panel-head"><h2>심사보고서 재제출</h2></div>
    <div class="field"><label>대상 업체</label>
      <select class="in" onchange="App.gdmResubSel(this.value)">${opts}</select></div>
    <div class="field"><label>보완 메모</label>
      <textarea class="in" id="gdm-resub" rows="4" placeholder="반려 사유에 대한 보완 내용을 적어주세요."></textarea></div>
    <div class="row-act" style="margin-top:10px">
      <button class="btn btn-primary" onclick="App.gdmResubmit()">보완 재제출</button>
    </div>
    <p class="note" style="margin-top:10px">오디터 반려 후 보완한 문서를 재제출합니다. 오디터에게 알림이 전송됩니다.</p>
  </section>`;
}
App.gdmResubSel = function(id){ S.selCompany = id; render(); };
App.gdmResubmit = async function(){
  const cid = S.selCompany; if(!cid) return;
  const note = (($('#gdm-resub')||{}).value||'').trim();
  const r = await GDM_post(`/cases/${cid}/audit-report/resubmit`, {note});
  if(!r.ok) return toast(GDM_ERR(r, r.data));
  toast(`심사보고서를 재제출했습니다. (회차 ${r.data.round})`);
  const el = $('#gdm-resub'); if(el) el.value = '';
  if(typeof loadCons === 'function') await loadCons();
  render();
};

/* ── C. 샤리아: 심의 문서 ── */
function GDM_fatwaPanel(){
  const cid = S.selFatwa;
  if(!cid) return `<section class="panel"><p class="empty">선택된 케이스가 없습니다.</p></section>`;
  return `<section class="panel">
    <div class="panel-head"><h2>심의 문서</h2></div>
    <div class="row-act">
      <button class="btn btn-primary" onclick="App.gdmFatwaDoc()">파트와 문서 생성</button>
      <button class="btn" onclick="App.gdmKetetapanPrev()">결정서 미리보기</button>
    </div>
    <p class="note" style="margin-top:10px">파트와 문서·결정문은 sharia/operator/admin 전용입니다. 결정서는 self_declare 경로 + KFPH 승인 후에만 발급됩니다.</p>
  </section>`;
}
App.gdmFatwaDoc = async function(){
  const cid = S.selFatwa; if(!cid) return;
  const r = await GDM_post(`/cases/${cid}/fatwa/document`, {});
  if(!r.ok) return toast(GDM_ERR(r, r.data));
  GDM_preModal('파트와 결정문', r.data.document || '');
};
App.gdmKetetapanPrev = async function(){
  const cid = S.selFatwa; if(!cid) return;
  const r = await GDM_getHTML(`/cases/${cid}/committee/ketetapan/preview`);
  if(!r.ok){
    if(r.status===409) return toast(GDM_ERR(r, r.data));
    return toast(GDM_ERR(r, r.data));
  }
  GDM_htmlModal('KFPH 결정문 미리보기', r.html);
};
/* HTML 조각 응답 전용 */
async function GDM_getHTML(path){
  try{ const r = await apiFetch(path); const txt = await r.text(); return {ok:r.ok,status:r.status,html:txt,data:{}}; }
  catch(e){ return {ok:false,status:0,html:'',data:{}}; }
}

/* ── D. 오디터: 성분 보고서 ── */
RS.gdmMatCheck = RS.gdmMatCheck || {};

function GDM_matPanel(){
  const list = (RS.cases||[]).filter(c=>!c.done);
  if(!list.length) return `<section class="panel"><p class="empty">대상 업체가 없습니다.</p></section>`;
  let cur = (S.selMat && list.find(c=>c.case_id===S.selMat)) ? S.selMat : list[0].case_id;
  S.selMat = cur;
  const opts = list.map(c=>`<option value="${esc(c.case_id)}" ${c.case_id===cur?'selected':''}>${esc(c.company_name||c.case_id.slice(0,8))}</option>`).join('');
  const ck = RS.gdmMatCheck[cur] || {};
  const ids = Object.keys(ck);
  const checkedN = ids.filter(k=>ck[k]).length;
  return `<section class="panel">
    <div class="panel-head"><h2>성분 분석 리포트</h2></div>
    <div class="field"><label>대상 업체</label>
      <select class="in" onchange="App.gdmMatSel(this.value)">${opts}</select></div>
    <div class="rows" style="margin-top:10px">
      <div class="note">체크한 성분 항목은 스냅샷에 기록되어 클라이언트에 전달됩니다. 체크 전 항목 수: ${ids.length} / 체크됨: ${checkedN}</div>
    </div>
    <div class="field"><label>메모</label>
      <textarea class="in" id="gdm-mat-note" rows="3" placeholder="스냅샷에 함께 기록할 메모"></textarea></div>
    <div class="row-act" style="margin-top:10px">
      <button class="btn btn-primary" onclick="App.gdmMatSnapshot()">스냅샷 저장</button>
      <button class="btn btn-ghost" onclick="App.gdmMatPdf()">성분 보고서 PDF</button>
    </div>
    <p class="note" style="margin-top:10px">체크 상태는 화면에서 관리되며, 스냅샷 저장 시 서버에 이력으로 보존됩니다.</p>
  </section>`;
}
App.gdmMatSel = function(id){ S.selMat = id; render(); };
App.gdmMatSnapshot = async function(){
  const cid = S.selMat; if(!cid) return;
  const note = (($('#gdm-mat-note')||{}).value||'').trim();
  const ck = RS.gdmMatCheck[cid] || {};
  const checked = Object.keys(ck).filter(k=>ck[k]);
  const r = await GDM_post(`/cases/${cid}/material-report/snapshot`, {checked, note});
  if(!r.ok) return toast(GDM_ERR(r, r.data));
  toast(`성분 보고서 스냅샷을 저장했습니다. (${r.data.checked}/${r.data.total})`);
  const el = $('#gdm-mat-note'); if(el) el.value = '';
  render();
};
App.gdmMatPdf = function(){
  const cid = S.selMat; if(!cid) return;
  App.gdmDownload(`/cases/${cid}/material-report.pdf`, `material-report_${cid.slice(0,8)}.pdf`);
};

/* ── 뷰 확장 ── */
(function(){
  const wrap = (vid, fn) => { const orig = VIEWS[vid]; if(typeof orig === 'function') VIEWS[vid] = () => orig() + fn(); else VIEWS[vid] = fn; };
  wrap('cons-docs', GDM_coPanel);
  wrap('cons-status', GDM_resubPanel);
  wrap('sha-review', GDM_fatwaPanel);
  wrap('aud-materials', GDM_matPanel);
})();
