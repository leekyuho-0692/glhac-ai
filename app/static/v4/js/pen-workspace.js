/* ===== 동반자(PPH · pendamping_pph) 작업공간 — UI 6번째 역할 'pen' =====
   종전엔 pendamping_pph 가 인증기업(ent) 화면으로 들어가 신청서 버튼이 전부 403 이었다.
   전용 화면: 내 배정건 GET /pendamping/assignments(main.py:16834) → 검증·재작업·반려
   POST /cases/{cid}/pendamping/verify {decision: verified|rework|rejected, note, signature_ref}(main.py:16807, schemas.PendampingVerify).
   ROLE_MAP·ROLES·NAV.pen 은 index.html, 버튼 블록·검증 액션은 self-declare.js(SDC_blocks·App.sdcVerify*)를 재사용. */
Object.assign(I18N.ko, {'ws.pen':'동반자 (PPH)','role.pen.sub':'자기선언 동반 검증','nav.pen-home':'동반 검증함'});
if(I18N.en) Object.assign(I18N.en, {'ws.pen':'PPH Facilitator','role.pen.sub':'Self-declare verification','nav.pen-home':'Verification Inbox'});
if(I18N.id) Object.assign(I18N.id, {'ws.pen':'Pendamping PPH','role.pen.sub':'Verifikasi pernyataan mandiri','nav.pen-home':'Kotak Verifikasi'});

const PEN_DECISION = {verified:'검증 완료', rework:'재작업 요청', rejected:'반려'};
const _penOpen = it => !it.decision || it.decision === 'rework';

async function loadPen(){
  try{
    const r = await apiFetch('/pendamping/assignments');
    RS.pen = r.ok ? await r.json() : {items:[], error:true, status:r.status};
  }catch(e){ RS.pen = {items:[], error:true}; }
}
NAVC['pen-home'] = () => ((RS.pen && RS.pen.items) || []).filter(_penOpen).length;

VIEWS['pen-home'] = () => {
  const d = RS.pen;
  if(!d) return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  if(d.error) return `<section class="panel"><p class="empty">${d.status===403?'동반자(PPH) 계정만 볼 수 있습니다.':'배정 목록을 불러오지 못했습니다.'}</p></section>`;
  const list = d.items || [];
  const open = list.filter(_penOpen).length;
  const head = `<section class="summary">
    <div><span class="k">배정</span><span class="v mono">${list.length}</span></div>
    <div><span class="k">검증 대기</span><span class="v mono">${open}</span></div>
    <div><span class="k">완료</span><span class="v mono">${list.filter(x=>x.decision==='verified').length}</span></div>
  </section>`;
  if(!list.length) return head + '<section class="panel"><p class="empty">배정된 자기선언 건이 없습니다. 컨설턴트가 동반자로 배정하면 여기에 나타납니다.</p></section>';
  let cur = (S.selPen && list.find(x=>x.case_id===S.selPen)) ? S.selPen : list[0].case_id;
  S.selPen = cur;
  const nav = `<nav class="mlist" aria-label="배정 목록"><div class="mh">배정 ${list.length}건</div>${list.map(x=>
    `<button class="mitem ${x.case_id===cur?'on':''}" onclick="App.penSel('${esc(x.case_id)}')"><span class="t"><span>${esc(x.company_name||x.case_id)}</span><span class="badge ${x.decision==='verified'?'strong':(_penOpen(x)?'attn':'')}">${esc(PEN_DECISION[x.decision]||'검증 대기')}</span></span><span class="muted mono" style="font-size:11px">${esc(x.status||'')} · ${esc(x.pathway||'')}</span></button>`
  ).join('')}</nav>`;
  const it = list.find(x=>x.case_id===cur);
  const done = it.decision && it.decision !== 'rework';
  const detail = `<section class="panel"><div class="panel-head"><h2>${esc(it.company_name||'')}</h2><span class="badge">${esc(it.pathway||'-')}</span></div>
    <dl class="dl">
      <div><dt>상태</dt><dd class="mono">${esc(it.status||'')}</dd></div>
      <div><dt>내 결정</dt><dd>${esc(PEN_DECISION[it.decision]||'검증 대기')}</dd></div>
      ${it.verified_at?`<div><dt>결정 시각</dt><dd class="mono">${esc(String(it.verified_at).slice(0,19))}</dd></div>`:''}
      ${it.note?`<div><dt>메모</dt><dd>${esc(it.note)}</dd></div>`:''}
    </dl>
    ${it.pathway!=='self_declare'?'<div class="note attn"><p>이 건은 자기선언 경로가 아닙니다(반려 등으로 정규 전환). 추가 검증은 필요 없습니다.</p></div>':''}
    ${done?'<div class="note"><p>검증 결정을 마쳤습니다. 자기선언서를 다시 확인하려면 아래 미리보기를 쓰세요.</p></div>':''}
    ${SDC_blocks(cur)}
  </section>`;
  return head + `<div class="md">${nav}<div style="display:flex;flex-direction:column;gap:22px;min-width:0">${detail}</div></div>`;
};
App.penSel = function(id){ S.selPen = id; render(); };
