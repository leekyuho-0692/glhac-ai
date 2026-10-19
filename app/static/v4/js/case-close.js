/* ===== 케이스 종결(R1-1): 사유별 종결 상태 4종 — withdrawn·rejected·revoked·cancelled =====
   POST /cases/{id}/close  body {status: withdrawn|rejected|cancelled, reason(필수)}
   · 운영자(operator/admin): 3종 모두 — 운영 케이스 화면(adm-assign)의 '케이스 종결'
   · 기업 관리자(client_admin): withdrawn 만, 자기 조직 케이스 — 기업 홈의 '신청 철회'
   · revoked 는 이 화면으로 못 간다 — 인증서 철회가 완료되면 서버가 자동으로 넣는다.
   종결 케이스는 서버가 쓰기를 409 CASE_CLOSED 로 막는다(읽기는 그대로). */

const CC_ST = {withdrawn:'신청 철회', rejected:'심사 불합격', revoked:'인증서 철회', cancelled:'운영 취소'};
const CC_OPTS = [['withdrawn','업체 철회·포기'], ['rejected','심사 불합격·인증 불가'], ['cancelled','운영 취소(중복·오등록 등)']];

function CC_isOps(){ return ['operator','admin'].includes(AUTH.role); }

/* 종결 케이스 배지 — 목록·카드 어디서든 같은 문구 */
function CC_badge(c){
  return c && c.closed ? `<span class="badge attn">${esc(CC_ST[c.status]||'종결')}</span>` : '';
}

/* ---------- 운영자: 케이스 종결 ---------- */
function CC_opsPanel(cid){
  if(!CC_isOps()) return '';
  const c = (RS.cases||[]).find(x=>x.case_id===cid);
  if(!c) return '';
  if(c.closed){
    return `<section class="panel"><div class="panel-head"><h2>케이스 종결</h2>${CC_badge(c)}</div>
      <p class="muted">종결된 케이스는 변경할 수 없습니다. 재개가 필요하면 새 케이스를 만들어 주세요.</p></section>`;
  }
  return `<section class="panel"><div class="panel-head"><h2>케이스 종결</h2><span class="muted mono">${esc(c.status||'')}</span></div>
    <p class="muted">철회·불합격·운영 취소로 이 케이스를 끝냅니다. 종결하면 되돌릴 수 없습니다.</p>
    <div class="row-act" style="justify-content:flex-end"><button class="btn" onclick="App.ccOpen('${jsq(cid)}')">케이스 종결</button></div></section>`;
}
App.ccOpen = function(cid){
  openModal('케이스 종결',
    `<p>종결하면 이 케이스는 더 이상 변경할 수 없습니다. 미결 청구서와 진행 중인 승인 요청은 자동으로 취소됩니다. 재개가 필요하면 새 케이스를 만들어야 합니다.</p>
     <div class="field"><label for="cc-st">종결 사유 구분</label><select class="in" id="cc-st">${CC_OPTS.map(o=>`<option value="${esc(o[0])}">${esc(o[1])}</option>`).join('')}</select></div>
     <div class="field"><label for="cc-r">사유 (필수)</label><textarea class="in" id="cc-r"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="once(this, ()=>App.ccDo('${jsq(cid)}'))">종결 처리</button>`);
};
App.ccDo = async function(cid){
  const status = (($('#cc-st')||{}).value||'').trim();
  const reason = (($('#cc-r')||{}).value||'').trim();
  if(!reason) return toast('사유를 입력해 주세요.');
  const r = await apiFetch('/cases/'+cid+'/close', {method:'POST', body:JSON.stringify({status, reason})});
  if(!r.ok) return toast(await apiErr(r, '케이스 종결에 실패했습니다.'));
  closeModal();
  if(typeof loadAdm==='function') await loadAdm();
  render(); toast('케이스를 종결했습니다.');
};

/* ---------- 기업: 신청 철회 (client_admin 만) ---------- */
function CC_entPanel(c){
  if(!c || c.closed || AUTH.role!=='applicant') return '';
  if(RS.ccRole===undefined){            // 기업 관리자 여부는 /auth/me 의 company_role — 한 번만 불러온다
    if(!RS._ccRoleLoading){
      RS._ccRoleLoading = true;
      apiFetch('/auth/me').then(r=>r.ok?r.json():{}).then(d=>{ RS.ccRole = d.company_role||null; render(); })
        .catch(()=>{ RS.ccRole = null; });
    }
    return '';
  }
  if(RS.ccRole!=='client_admin') return '';
  return `<section class="panel"><div class="panel-head"><h2>신청 철회</h2></div>
    <p class="muted">더 이상 인증을 진행하지 않으려면 신청을 철회할 수 있습니다. 철회하면 되돌릴 수 없습니다.</p>
    <div class="row-act" style="justify-content:flex-end"><button class="btn" onclick="App.ccWdOpen('${jsq(c.case_id)}')">신청 철회</button></div></section>`;
}
App.ccWdOpen = function(cid){
  openModal('신청 철회',
    `<p>신청을 철회하면 이 케이스는 종결되며 되돌릴 수 없습니다. 다시 신청하려면 새로 신청서를 작성해야 합니다.</p>
     <div class="field"><label for="cc-wr">철회 사유 (필수)</label><textarea class="in" id="cc-wr"></textarea></div>`,
    `<button class="btn" onclick="App.closeModal()">취소</button><button class="btn btn-primary" onclick="once(this, ()=>App.ccWdDo('${jsq(cid)}'))">철회합니다</button>`);
};
App.ccWdDo = async function(cid){
  const reason = (($('#cc-wr')||{}).value||'').trim();
  if(!reason) return toast('사유를 입력해 주세요.');
  const r = await apiFetch('/cases/'+cid+'/close', {method:'POST', body:JSON.stringify({status:'withdrawn', reason})});
  if(!r.ok) return toast(await apiErr(r, '신청 철회에 실패했습니다.'));
  closeModal();
  if(typeof loadEnt==='function') await loadEnt();
  render(); toast('신청을 철회했습니다.');
};

// ── 화면 확장 ──
{ const f = VIEWS['adm-assign']; if(typeof f==='function') VIEWS['adm-assign'] = () => f() + (S.selAssign ? CC_opsPanel(S.selAssign) : ''); }
{ const f = VIEWS['ent-home']; if(typeof f==='function') VIEWS['ent-home'] = () => f() + CC_entPanel((Array.isArray(RS.cases)?RS.cases:[])[0]); }
