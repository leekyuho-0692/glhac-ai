/* ===== 케이스 도구(구 화면 이식) — 심사원 풀·문서 재분류·처리 이력·내보내기·조직도 대조·증빙 자동 연결·원재료 자동완성 ===== */

RS.ctlPool = RS.ctlPool || {};

/* ---------- 공통 헬퍼 ---------- */

const CTL_download = async function(path, filename){
  try{
    const r = await apiFetch(path);
    if(!r.ok){
      if(r.status===403) return toast('권한이 없습니다.');
      toast('파일을 불러올 수 없습니다.');
      return;
    }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(()=>{ URL.revokeObjectURL(a.href); a.remove(); }, 4000);
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); }
};

const CTL_errMsg = function(r, j, what){
  if(r.status===403){ toast('권한이 없습니다.'); return; }
  let code = '';
  try{ if(j && j.detail && typeof j.detail === 'object' && j.detail.code) code = j.detail.code; }catch(e){}
  toast(what + '에 실패했습니다.' + (code ? ' (' + code + ')' : ''));
};

/* ---------- 상태 저장 ---------- */
RS._ctlPoolLoading = RS._ctlPoolLoading || {};
RS._ctlPoolErr = RS._ctlPoolErr || {};
RS._ctlPoolList = RS._ctlPoolList || {};

const CTL_render = function(){ try{ render(); }catch(e){} };

/* ---------- A. 심사원 풀 ---------- */

const CTL_poolCanView = function(){
  const r = S.role;
  return r==='adm' || r==='aud' || r==='sha' || AUTH.role==='operator' || AUTH.role==='admin' || AUTH.role==='auditor' || AUTH.role==='fatwa_liaison';
};

const CTL_poolCanEdit = function(){
  return AUTH.role==='operator' || AUTH.role==='admin';
};

const CTL_loadPool = async function(cid){
  RS._ctlPoolLoading[cid] = true;
  if(typeof RS.dep !== 'undefined' && RS.dep) delete RS.dep[cid];
  try{
    const r = await apiFetch('/cases/' + cid + '/auditor-pool');
    if(r.ok){
      RS.ctlPool[cid] = await r.json();
      delete RS._ctlPoolErr[cid];
    } else if(r.status===403){
      RS.ctlPool[cid] = {error:true, status:403};
    } else {
      RS.ctlPool[cid] = {error:true, status:r.status};
    }
  }catch(e){
    RS.ctlPool[cid] = {error:true};
  }
  RS._ctlPoolLoading[cid] = false;
  return RS.ctlPool[cid];
};

const CTL_poolPanel = function(cid){
  if(!cid || !CTL_poolCanView()) return '';
  const pool = RS.ctlPool[cid];
  if(typeof pool === 'undefined'){
    if(!RS._ctlPoolLoading[cid]){
      RS._ctlPoolLoading[cid] = true;
      CTL_loadPool(cid).then(()=>{ render(); });
    }
    return '<section class="panel"><div class="panel-head"><h2>현장 심사팀(심사원 풀)</h2></div><p class="empty">불러오는 중…</p></section>';
  }
  if(pool && pool.error){
    if(pool.status===403) return '';
    return '<section class="panel"><div class="panel-head"><h2>현장 심사팀(심사원 풀)</h2></div><p class="empty">불러오지 못했습니다.</p></section>';
  }
  const list = Array.isArray(pool) ? pool : [];
  const canEdit = CTL_poolCanEdit();
  const roleKo = (x)=> x==='ketua' ? '팀장' : '팀원';

  let rows;
  if(!list.length){
    rows = '<p class="empty">등록된 심사원이 없습니다.</p>';
  } else {
    rows = '<div class="tbl-wrap"><table><thead><tr><th>이름</th><th>역할</th><th>자격번호</th>'
      + (canEdit ? '<th></th>' : '')
      + '</tr></thead><tbody>'
      + list.map((m,i)=>'<tr><td>' + esc(m.name) + '</td>'
        + '<td>' + esc(roleKo(m.role_in_team)) + '</td>'
        + '<td class="mono">' + esc(m.cert_no||'') + '</td>'
        + (canEdit ? '<td><button class="btn btn-sm btn-ghost" onclick="App.ctlPoolDel(\'' + cid + '\',' + i + ')">삭제</button></td>' : '')
        + '</tr>').join('')
      + '</tbody></table></div>';
  }

  const addForm = canEdit ? '<div class="inline" style="margin-top:10px">'
    + '<input class="in" id="ctl-pn" placeholder="이름">'
    + '<input class="in" id="ctl-pc" placeholder="자격번호(선택)">'
    + '<select class="in" id="ctl-pr"><option value="anggota">팀원</option><option value="ketua">팀장</option></select>'
    + '<button class="btn btn-sm btn-primary" onclick="App.ctlPoolAdd(\'' + cid + '\')">추가</button>'
    + '</div>' : '';

  return '<section class="panel"><div class="panel-head"><h2>현장 심사팀(심사원 풀)</h2></div>'
    + rows + addForm + '</section>';
};

App.ctlPoolAdd = async function(cid){
  const name = (($('#ctl-pn')||{}).value||'').trim();
  const cert_no = (($('#ctl-pc')||{}).value||'').trim();
  const role_in_team = (($('#ctl-pr')||{}).value||'anggota');
  if(!name){ toast('이름을 입력해 주세요.'); return; }
  try{
    const r = await apiFetch('/cases/' + cid + '/auditor-pool', {method:'POST', body:JSON.stringify({name, cert_no, role_in_team})});
    if(!r.ok){
      let j = null; try{ j = await r.json(); }catch(e){}
      return CTL_errMsg(r, j, '심사팀 추가');
    }
    delete RS.ctlPool[cid];
    if(typeof RS.dep !== 'undefined' && RS.dep) delete RS.dep[cid];
    RS._ctlPoolLoading[cid] = false;
    await CTL_loadPool(cid);
    render();
    toast('심사팀을 갱신했습니다.');
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); }
};

App.ctlPoolDel = async function(cid, idx){
  const pool = RS.ctlPool[cid];
  if(!Array.isArray(pool) || !pool[idx]) return;
  const m = pool[idx];
  if(!confirm('이 심사원을 팀에서 뺄까요?')) return;
  try{
    const r = await apiFetch('/cases/' + cid + '/auditor-pool/' + encodeURIComponent(m.id), {method:'DELETE'});
    if(!r.ok){
      let j = null; try{ j = await r.json(); }catch(e){}
      return CTL_errMsg(r, j, '심사팀 삭제');
    }
    delete RS.ctlPool[cid];
    if(typeof RS.dep !== 'undefined' && RS.dep) delete RS.dep[cid];
    RS._ctlPoolLoading[cid] = false;
    await CTL_loadPool(cid);
    render();
    toast('심사팀을 갱신했습니다.');
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); }
};

/* ---------- B. 케이스 도구 ---------- */

const CTL_flowCanView = function(){
  const r = S.role;
  return r==='aud' || r==='adm' || r==='cons';
  return true;
};

const CTL_toolPanel = function(cid){
  if(!cid) return '';
  if(!CTL_flowCanView()) return '';
  const role = AUTH.role;
  const isConsultant = (role==='consultant');
  const isAuditor = (role==='auditor');
  const isOperator = (role==='operator');
  const isAdmin = (role==='admin');
  const showReclass = isConsultant || isAuditor || isAdmin;

  const btns = [];
  btns.push('<button class="btn btn-sm" onclick="App.ctlTimeline(\'' + cid + '\')">처리 이력</button>');
  btns.push('<button class="btn btn-sm" onclick="App.ctlExportJson(\'' + cid + '\')">JSON 내보내기</button>');
  btns.push('<button class="btn btn-sm" onclick="App.ctlPreassessDocx(\'' + cid + '\')">사전심사 보고서 DOCX</button>');
  if(showReclass) btns.push('<button class="btn btn-sm" onclick="App.ctlReclassify(\'' + cid + '\')">문서 재분류</button>');
  btns.push('<button class="btn btn-sm" onclick="App.ctlReconcile(\'' + cid + '\')">조직도 대조</button>');

  return '<section class="panel"><div class="panel-head"><h2>케이스 도구</h2></div>'
    + '<p class="muted" style="font-size:12px">처리 이력·내보내기·재분류 등 심사 보조 도구입니다.</p>'
    + '<div class="row-act">' + btns.join('') + '</div>'
    + '</section>';
};

App.ctlTimeline = async function(cid){
  try{
    const r = await apiFetch('/cases/' + cid + '/timeline');
    if(!r.ok){
      let j = null; try{ j = await r.json(); }catch(e){}
      return CTL_errMsg(r, j, '처리 이력 조회');
    }
    const d = await r.json();
    const blockers = Array.isArray(d.blockers) ? d.blockers : [];
    const events = Array.isArray(d.events) ? d.events.slice().reverse().slice(0, 300) : [];

    let body = '';
    if(blockers.length){
      body += '<div class="note attn"><b>진행 막힘 ' + blockers.length + '건</b><p>'
        + blockers.map(b=> esc((b && b.code) ? b.code : JSON.stringify(b))).join(', ')
        + '</p></div>';
    }
    if(!events.length){
      body += '<p class="empty">이력이 없습니다.</p>';
    } else {
      body += '<div class="tbl-wrap"><table><thead><tr><th>#</th><th>처리</th><th>상태 변화</th><th>주체</th><th>해시</th></tr></thead><tbody>'
        + events.map((e,i)=>{
            const from = e.from == null ? '' : String(e.from);
            const to = e.to == null ? '' : String(e.to);
            const chg = (from === to) ? esc(to) : (esc(from) + ' → ' + esc(to));
            return '<tr><td class="num">' + (i+1) + '</td>'
              + '<td>' + esc(e.action||'') + '</td>'
              + '<td>' + chg + '</td>'
              + '<td>' + esc(e.actor||'') + '</td>'
              + '<td><span class="mono">' + esc(e.hash||'') + '</span></td></tr>';
          }).join('')
        + '</tbody></table></div>';
    }
    openModal('처리 이력', body, '<button class="btn" onclick="App.closeModal()">닫기</button>', true);
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); }
};

App.ctlExportJson = function(cid){
  CTL_download('/cases/' + cid + '/export.json', 'case_' + String(cid).slice(0,8) + '.json');
};

App.ctlPreassessDocx = function(cid){
  CTL_download('/cases/' + cid + '/preassess-report.docx?lang=ko', 'preassess_report.docx');
};

App.ctlReclassify = async function(cid){
  if(!confirm('업로드된 문서 종류를 다시 판정합니다. 진행할까요?')) return;
  try{
    const r = await apiFetch('/cases/' + cid + '/documents/re-classify', {method:'POST'});
    if(!r.ok){
      let j = null; try{ j = await r.json(); }catch(e){}
      return CTL_errMsg(r, j, '문서 재분류');
    }
    const d = await r.json();
    toast('문서 재분류: ' + (d.changed_count||0) + '건 변경');
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); }
};

App.ctlReconcile = async function(cid){
  try{
    const r = await apiFetch('/cases/' + cid + '/halal-org/reconcile', {method:'POST', body:JSON.stringify({})});
    if(!r.ok){
      if(r.status===400){
        let j = null; try{ j = await r.json(); }catch(e){}
        const code = (j && j.detail && typeof j.detail === 'object') ? j.detail.code : null;
        if(code === 'NO_ORG_CHART'){
          return toast('조직도 이미지가 없습니다. 매뉴얼 빌더의 조직도 섹션에 먼저 넣어 주세요.');
        }
        return CTL_errMsg(r, j, '조직도 대조');
      }
      let j = null; try{ j = await r.json(); }catch(e){}
      return CTL_errMsg(r, j, '조직도 대조');
    }
    const d = await r.json();
    const summary = d.summary || {total:0, matched:0, high:0};
    const mismatches = Array.isArray(d.mismatches) ? d.mismatches : [];

    let body = '';
    body += '<section class="summary">'
      + '<div><span class="k">담당자</span><span class="v mono">' + (summary.total||0) + '</span></div>'
      + '<div><span class="k">확인됨</span><span class="v mono">' + (summary.matched||0) + '</span></div>'
      + '<div><span class="k">고위험</span><span class="v mono">' + (summary.high||0) + '</span></div>'
      + '</section>';

    if(d.ocr_available === false){
      body += '<p class="note">OCR 엔진이 없어 이름 대조를 하지 못했습니다.</p>';
    }

    if(!mismatches.length){
      body += '<p class="muted">불일치 없음</p>';
    } else {
      body += '<ul class="rows">' + mismatches.map(m=>{
          const high = m.severity === 'high';
          return '<li class="row"><div><b>' + esc(m.name||'') + '</b>'
            + '<span class="muted">' + esc(m.role_ko||'') + ' · ' + esc(m.detail||'') + '</span></div>'
            + '<span class="badge ' + (high ? 'attn' : '') + '">' + (high ? '고위험' : '확인') + '</span></li>';
        }).join('') + '</ul>';
    }

    openModal('조직도 대조', body, '<button class="btn" onclick="App.closeModal()">닫기</button>', true);
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); }
};

/* ---------- C. 증빙 자동 연결 ---------- */

const CTL_docCanView = function(){
  const r = AUTH.role;
  return r==='applicant' || r==='consultant' || r==='penyelia_halal';
};

const CTL_autoLinkPanel = function(cid){
  if(!cid) return '';
  if(!CTL_docCanView()) return '';
  return '<section class="panel"><div class="panel-head"><h2>증빙 자동 연결</h2></div>'
    + '<p class="muted" style="font-size:12px">올린 서류(할랄 인증서·성분표 등)를 원재료와 자동으로 짝지어 줍니다.</p>'
    + '<div class="row-act"><button class="btn btn-sm" onclick="App.ctlAutoLink(\'' + cid + '\')">자동 연결 실행</button></div>'
    + '</section>';
};

App.ctlAutoLink = async function(cid){
  try{
    const r = await apiFetch('/cases/' + cid + '/auto-link-materials', {method:'POST'});
    if(!r.ok){
      let j = null; try{ j = await r.json(); }catch(e){}
      return CTL_errMsg(r, j, '증빙 자동 연결');
    }
    const d = await r.json();
    if(typeof RS.mats !== 'undefined' && RS.mats) delete RS.mats[cid];
    render();
    toast('증빙 자동 연결: 새 연결 ' + (d.added||0) + '건 · 전체 ' + (d.total_links||0) + '건');
  }catch(e){ toast('네트워크 오류가 발생했습니다.'); }
};

/* ---------- D. 원재료명 자동완성 ---------- */

let CTL_timer = null;

const CTL_suggest = function(el){
  const q = (el.value || '').trim();
  if(q.length < 2) return;
  if(CTL_timer) clearTimeout(CTL_timer);
  CTL_timer = setTimeout(()=>{
    apiFetch('/meta/ingredient-suggest?q=' + encodeURIComponent(q))
      .then(r => r.ok ? r.json() : null)
      .then(list => {
        if(!Array.isArray(list)) return;
        let dl = document.getElementById('ctl-ing-dl');
        if(!dl){
          dl = document.createElement('datalist');
          dl.id = 'ctl-ing-dl';
          document.body.appendChild(dl);
        }
        while(dl.firstChild) dl.removeChild(dl.firstChild);
        list.slice(0, 12).forEach(it=>{
          const opt = document.createElement('option');
          opt.value = it.canonical_name || '';
          opt.textContent = ((it.e_number ? it.e_number + ' · ' : '') + (it.default_status || ''));
          dl.appendChild(opt);
        });
        el.setAttribute('list', 'ctl-ing-dl');
      })
      .catch(()=>{});
  }, 300);
};

document.addEventListener('input', e => {
  const el = e.target;
  if(!el || !el.id) return;
  if(el.id !== 'pk-mn' && el.id !== 'mt-name' && el.id !== 'min-free') return;
  CTL_suggest(el);
});

/* ---------- 화면 붙이기 ---------- */

(() => {
  /* A: aud-report 뒤 — 심사원 풀 (S.selOnsite) */
  {
    const f = VIEWS['aud-report'];
    if(typeof f === 'function'){
      VIEWS['aud-report'] = () => f() + CTL_poolPanel(S.selOnsite);
    }
  }

  /* B: aud-dossier, adm-flow 뒤 — 케이스 도구 (S.selFlow) */
  {
    const f = VIEWS['aud-dossier'];
    if(typeof f === 'function'){
      VIEWS['aud-dossier'] = () => f() + CTL_toolPanel(S.selFlow);
    }
  }
  {
    const f = VIEWS['adm-flow'];
    if(typeof f === 'function'){
      VIEWS['adm-flow'] = () => f() + CTL_toolPanel(S.selFlow);
    }
  }

  /* C: ent-docs, cons-docs 뒤 — 증빙 자동 연결 */
  {
    const f = VIEWS['ent-docs'];
    if(typeof f === 'function'){
      VIEWS['ent-docs'] = () => {
        const cid = ((RS.cases||[])[0] || {}).case_id;
        return f() + CTL_autoLinkPanel(cid);
      };
    }
  }
  {
    const f = VIEWS['cons-docs'];
    if(typeof f === 'function'){
      VIEWS['cons-docs'] = () => f() + CTL_autoLinkPanel(S.selDocCo);
    }
  }
})();