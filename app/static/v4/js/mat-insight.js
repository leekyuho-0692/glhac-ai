/* ===== 원재료 해설·심사 참고 의견(구 화면 이식) ===== */
const MIN_V = { CLEARED:'적합', PASS:'적합', NEEDS_EVIDENCE:'증빙 필요', BLOCK:'부적합' };

const MIN_secHtml = (sec) => {
  if (!sec || !sec.label) return '';
  const tx = sec.text;
  const inner = Array.isArray(tx)
    ? `<ul>${tx.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`
    : `<p>${esc(tx||'')}</p>`;
  return `<h3>${esc(sec.label)}</h3>${inner}`;
};

const MIN_noteBody = (d) => {
  let h = `<div class="note attn"><b>${esc(d.draft_notice||'')}</b></div>`;
  h += MIN_secHtml(d.why);
  h += MIN_secHtml(d.exemption);
  h += MIN_secHtml(d.this_case);
  h += MIN_secHtml(d.action);
  if (d.cert_no) h += `<p class="muted">인증: ${esc(d.cert_issuer||'')} ${esc(d.cert_no)}</p>`;
  return h;
};

const MIN_explainBody = (d) => {
  const verdict = d.verdict || '';
  const cls = verdict === 'BLOCK' ? 'attn' : (verdict === 'NEEDS_EVIDENCE' ? '' : 'strong');
  const label = MIN_V[verdict] || verdict;
  let h = `<p><span class="badge ${cls}">${esc(label)}</span> ${d.najis?'<span class="badge attn">najis</span>':''} <span class="muted">${esc(d.status||'')} · ${esc((d.sources||[]).join(', '))}</span></p>`;
  h += `<p style="white-space:pre-wrap">${esc(d.explanation||'')}</p>`;
  if ((d.required_evidence||[]).length) h += `<h3>필요 증빙</h3><ul>${d.required_evidence.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`;
  if ((d.alternatives||[]).length) h += `<h3>할랄 대체재</h3><ul>${d.alternatives.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`;
  return h;
};

const MIN_explain = async (name) => {
  if (!name) return;
  toast('해설을 만드는 중입니다…');
  try {
    const r = await apiFetch('/ai/explain', { method:'POST', body: JSON.stringify({ name, lang:'ko' }) });
    if (!r.ok) { toast('해설을 불러오지 못했습니다.'); return; }
    const d = await r.json();
    openModal(`성분 해설 — ${esc(d.name || name)}`, MIN_explainBody(d), '<button class="btn" onclick="App.closeModal()">닫기</button>', true);
  } catch (e) {
    toast('네트워크 오류가 발생했습니다.');
  }
};

App.minNote = async function (cid) {
  const sel = $('#min-sel');
  const i = sel ? Number(sel.value) : 0;
  const arr = (RS.mats && RS.mats[cid]) || [];
  const m = arr[i];
  if (!m) { toast('참고 의견을 불러오지 못했습니다.'); return; }
  try {
    const r = await apiFetch(`/materials/${m.material_id}/auditor-note?lang=ko`);
    if (!r.ok) {
      if (r.status === 403) toast('권한이 없습니다.');
      else toast('참고 의견을 불러오지 못했습니다.');
      return;
    }
    const d = await r.json();
    openModal(`심사 참고 의견 — ${esc(m.name)}`, MIN_noteBody(d), '<button class="btn" onclick="App.closeModal()">닫기</button>', true);
  } catch (e) {
    toast('네트워크 오류가 발생했습니다.');
  }
};

App.minExplainSel = async function (cid) {
  const sel = $('#min-sel');
  const i = sel ? Number(sel.value) : 0;
  const arr = (RS.mats && RS.mats[cid]) || [];
  const m = arr[i];
  if (!m) { toast('해설을 불러오지 못했습니다.'); return; }
  await MIN_explain(m.name);
};

App.minExplainFree = async function () {
  const v = (($('#min-free') || {}).value || '').trim();
  if (!v) { toast('성분명을 입력해 주세요.'); return; }
  await MIN_explain(v);
};

{ const f = VIEWS['aud-materials'];
  VIEWS['aud-materials'] = () => {
    const base = f ? f() : '';
    const cid = S.selMat;
    const mats = (RS.mats && RS.mats[cid]) || [];
    let h = `<section class="panel"><div class="panel-head"><h2>원재료 해설 · 심사 참고</h2></div>`;
    h += `<p class="muted" style="font-size:12px">원재료를 고르면 걸린 이유·예외 조건·이 건의 근거·확인할 것을 보여줍니다. AI 초안이며 판단은 오디터가 합니다.</p>`;
    if (mats.length) {
      h += `<div class="row-act"><select id="min-sel">${mats.map((m,i)=>`<option value="${i}">${esc(m.name)}${m.e_number?' ('+esc(m.e_number)+')':''}</option>`).join('')}</select>`;
      h += `<button class="btn btn-sm" onclick="App.minNote('${esc(cid)}')">심사 참고 의견</button>`;
      h += `<button class="btn btn-sm btn-ghost" onclick="App.minExplainSel('${esc(cid)}')">AI 해설</button></div>`;
    } else {
      h += `<p class="empty">원재료가 없습니다.</p>`;
    }
    h += `<div class="row-act" style="margin-top:8px"><input id="min-free" class="in" placeholder="성분명 직접 입력 (예: gelatin, E471)"> <button class="btn btn-sm btn-ghost" onclick="App.minExplainFree()">해설 보기</button></div>`;
    h += `</section>`;
    return base + h;
  };
}