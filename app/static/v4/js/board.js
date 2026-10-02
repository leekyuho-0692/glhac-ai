/* ===== 홈페이지 문의함 (board) ===== */
const BRD_ST = { open: '미답변', answered: '답변완료', closed: '종결' };
const BRD_ST_ORDER = ['all', 'open', 'answered', 'closed'];

RS.brd = RS.brd || null;          // {items, page, pages, total, ...} | {error:true}
RS.brdDetail = RS.brdDetail || {}; // post_id -> detail | {error}
RS.brdLoading = false;

const BRD_state = function() {
  if (!S.brdSt) S.brdSt = 'all';
  if (S.brdPage == null) S.brdPage = 1;
  if (S.brdQ == null) S.brdQ = '';
  if (S.brdMine == null) S.brdMine = (AUTH && AUTH.role === 'consultant');
  return { st: S.brdSt, page: S.brdPage, q: S.brdQ, mine: S.brdMine };
};

const BRD_load = async function() {
  if (RS.brdLoading) return;
  RS.brdLoading = true;
  const st = BRD_state();
  const parts = ['page=' + st.page, 'size=20'];
  if (st.st && st.st !== 'all') parts.push('status=' + encodeURIComponent(st.st));
  if (st.q) parts.push('q=' + encodeURIComponent(st.q));
  if (st.mine) parts.push('mine=true');
  try {
    const r = await apiFetch('/board/posts?' + parts.join('&'));
    if (!r.ok) {
      RS.brd = { error: true, status: r.status, items: [], total: 0, page: 1, pages: 0 };
    } else {
      const d = await r.json();
      RS.brd = d;
    }
  } catch (e) {
    RS.brd = { error: true, items: [], total: 0, page: 1, pages: 0 };
  }
  RS.brdLoading = false;
};

const BRD_loadDetail = async function(postId) {
  try {
    const r = await apiFetch('/board/posts/' + postId);
    if (!r.ok) {
      RS.brdDetail[postId] = { error: true, status: r.status };
    } else {
      RS.brdDetail[postId] = await r.json();
    }
  } catch (e) {
    RS.brdDetail[postId] = { error: true };
  }
};

const BRD_badge = function(st) {
  const cls = st === 'answered' ? 'badge strong' : (st === 'closed' ? 'badge' : 'badge attn');
  return `<span class="${cls}">${esc(BRD_ST[st] || st || '')}</span>`;
};

const BRD_fmtDate = function(s) {
  if (!s) return '';
  return String(s).slice(0, 16).replace('T', ' ');
};

/* ---- 목록 ---- */
const BRD_list = function() {
  const st = BRD_state();
  const tabs = BRD_ST_ORDER.map(k =>
    `<button class="${st.st === k ? 'on' : ''}" onclick="App.brdTab('${k}')">${esc(k === 'all' ? '전체' : BRD_ST[k])}</button>`).join('');
  const mineBtn = (AUTH && AUTH.role === 'consultant')
    ? `<button class="btn btn-sm ${st.mine ? 'btn-primary' : 'btn-ghost'}" onclick="App.brdMine()">내 영업 건만</button>`
    : '';
  const head = `<div class="panel-head"><h2>문의 목록</h2></div>
    <div class="chipbar" style="margin-bottom:10px">${tabs}</div>
    <div class="inline" style="margin-bottom:10px">
      <input id="brd-q" class="in" placeholder="제목·내용·이름·연락처·영업코드" value="${esc(st.q)}"
        onkeydown="if(event.key==='Enter'){event.preventDefault();App.brdSearch();}">
      <button class="btn" onclick="App.brdSearch()">검색</button>
      ${mineBtn}
    </div>`;

  if (!RS.brd) {
    if (!RS.brdLoading) { RS.brd = null; BRD_load().then(() => render()); }
    return `<section class="panel">${head}<p class="empty">불러오는 중…</p></section>`;
  }
  if (RS.brd.error) {
    const msg = RS.brd.status === 403 ? '이 화면을 볼 권한이 없습니다.' : '불러오지 못했습니다.';
    return `<section class="panel">${head}<p class="empty">${msg}</p></section>`;
  }
  const items = RS.brd.items || [];
  if (!items.length) {
    return `<section class="panel">${head}<p class="empty">문의가 없습니다.</p></section>`;
  }
  const rows = items.map(it => `
    <button class="mitem ${RS.brdSel === it.post_id ? 'on' : ''}" onclick="App.brdSel('${it.post_id}')">
      <span class="t">
        <span><span class="mono muted" style="font-size:11px">#${esc(String(it.post_no != null ? it.post_no : ''))}</span> ${esc(it.title || '')}</span>
        ${BRD_badge(it.status)}
      </span>
      <span class="muted" style="font-size:11px">${esc(it.author_name || '')} · ${esc(it.contact || '')} · 답글 ${esc(String(it.reply_count || 0))} · ${esc(BRD_fmtDate(it.created_at))}</span>
    </button>`).join('');
  const page = RS.brd.page || 1, pages = RS.brd.pages || 0;
  const pager = (pages > 1)
    ? `<div class="inline" style="margin-top:10px;justify-content:space-between">
        <button class="btn btn-sm" ${page <= 1 ? 'disabled' : ''} onclick="App.brdPage(${page - 1})">이전</button>
        <span class="muted mono" style="font-size:12px">${page} / ${pages}</span>
        <button class="btn btn-sm" ${page >= pages ? 'disabled' : ''} onclick="App.brdPage(${page + 1})">다음</button>
      </div>`
    : '';
  return `<section class="panel">${head}<div class="mlist">${rows}</div>${pager}</section>`;
};

/* ---- 상세 ---- */
const BRD_detail = function(postId) {
  const d = RS.brdDetail[postId];
  if (!d) {
    if (RS._brdDLoading !== postId) {
      RS._brdDLoading = postId;
      BRD_loadDetail(postId).then(() => { RS._brdDLoading = null; render(); });
    }
    return '<section class="panel"><p class="empty">불러오는 중…</p></section>';
  }
  if (d.error) {
    const msg = d.status === 403 ? '이 문의를 볼 권한이 없습니다.'
      : (d.status === 404 ? '문의를 찾을 수 없습니다.' : '불러오지 못했습니다.');
    return `<section class="panel"><p class="empty">${msg}</p></section>`;
  }

  const stBtns = [];
  if (d.status !== 'answered') stBtns.push(`<button class="btn btn-primary btn-sm" onclick="App.brdStatus('${esc(postId)}','answered')">답변완료</button>`);
  if (d.status !== 'closed') stBtns.push(`<button class="btn btn-sm" onclick="App.brdStatus('${esc(postId)}','closed')">종결</button>`);
  if (d.status !== 'open') stBtns.push(`<button class="btn btn-ghost btn-sm" onclick="App.brdStatus('${esc(postId)}','open')">다시 열기</button>`);

  const head = `<div class="panel-head"><h2>
    <span class="mono muted" style="font-size:13px">#${esc(String(d.post_no != null ? d.post_no : ''))}</span>
    ${esc(d.title || '')}</h2>
    <div class="row-act">${BRD_badge(d.status)}</div></div>`;

  const meta = `<div class="summary">
    <div><span class="k">작성자</span><span class="v">${esc(d.author_name || '')}</span></div>
    <div><span class="k">연락처</span><span class="v mono">${esc(d.contact || '')}</span></div>
    <div><span class="k">영업코드</span><span class="v mono">${esc(d.ref_code || '')}</span></div>
    <div><span class="k">작성일</span><span class="v mono">${esc(BRD_fmtDate(d.created_at))}</span></div>
    ${d.edited_at ? `<div><span class="k">수정일</span><span class="v mono">${esc(BRD_fmtDate(d.edited_at))}</span></div>` : ''}
  </div>`;

  const body = `<div class="note" style="white-space:pre-wrap;margin:12px 0">${esc(d.body || '')}</div>`;

  const replies = d.replies || [];
  const byId = {};
  replies.forEach(r => { byId[r.reply_id] = r; });
  const thread = replies.length
    ? `<ul class="rows">${replies.map(r => {
        const isChild = !!r.parent_reply_id;
        const pr = isChild ? byId[r.parent_reply_id] : null;
        const who = r.author_role === 'client' ? '글쓴이' : (ROLE_LABEL[r.author_role] || r.author_role || '담당자');
        return `<li class="row" style="${isChild ? 'margin-left:26px;border-left:2px solid var(--line,#ddd);padding-left:10px' : ''}">
          <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap">
            <strong>${esc(who)}${r.author_name ? ' · ' + esc(r.author_name) : ''}</strong>
            <span class="muted mono" style="font-size:11px">${esc(BRD_fmtDate(r.created_at))}</span>
          </div>
          ${isChild && pr ? `<div class="muted" style="font-size:11px">↳ ${esc((pr.body || '').slice(0, 40))}${(pr.body || '').length > 40 ? '…' : ''}</div>` : ''}
          <div style="white-space:pre-wrap;margin-top:6px">${esc(r.body || '')}</div>
          <div class="row-act" style="margin-top:6px">
            <button class="link" onclick="App.brdReplyTo('${esc(r.reply_id)}'${r.parent_reply_id ? '' : ',"1"'})">답글</button>
          </div>
        </li>`;
      }).join('')}</ul>`
    : '<p class="empty">아직 답변이 없습니다.</p>';

  const replyBox = `<fieldset style="margin-top:14px"><legend>답글</legend>
    ${S.brdReplyTo ? `<div class="note">“${esc((byId[S.brdReplyTo] && byId[S.brdReplyTo].body || '').slice(0, 40))}…” 답글에 작성 중
      <button class="link" onclick="App.brdReplyCancel()">취소</button></div>` : ''}
    <textarea id="brd-reply" class="in" rows="3" placeholder="답변을 입력하세요"></textarea>
    <div class="inline" style="margin-top:8px">
      <button class="btn btn-primary" onclick="App.brdReply('${esc(postId)}')">답글 등록</button>
      <span class="muted" style="font-size:12px">직원 토큰으로 등록됩니다.</span>
    </div>
    <div class="inline-msg" id="brd-reply-msg"></div>
  </fieldset>`;

  const statusRow = `<div class="row-act" style="margin-top:10px">${stBtns.join(' ')}</div>`;

  return `<section class="panel">${head}${meta}${body}${thread}${replyBox}${statusRow}</section>`;
};

/* ---- 공통 뷰 ---- */
const BRD_view = function() {
  const list = BRD_list();
  const items = (RS.brd && RS.brd.items) || [];
  let cur = null;
  if (RS.brdSel && items.some(it => it.post_id === RS.brdSel)) cur = RS.brdSel;
  else if (items.length) cur = items[0].post_id;
  else RS.brdSel = null;
  if (cur) RS.brdSel = cur;
  const detail = cur ? BRD_detail(cur) : '<section class="panel"><p class="empty">문의를 선택하세요.</p></section>';
  return `<div class="md">${list}<div style="display:flex;flex-direction:column;gap:22px;min-width:0">${detail}</div></div>`;
};

/* ---- 액션 ---- */
App.brdTab = function(st) {
  S.brdSt = st; S.brdPage = 1; RS.brd = null;
  BRD_load().then(() => render());
};

App.brdSearch = function() {
  const el = $('#brd-q');
  S.brdQ = ((el || {}).value || '').trim();
  S.brdPage = 1; RS.brd = null;
  BRD_load().then(() => render());
};

App.brdMine = function() {
  S.brdMine = !S.brdMine; S.brdPage = 1; RS.brd = null;
  BRD_load().then(() => render());
};

App.brdPage = function(p) {
  S.brdPage = p; RS.brd = null;
  BRD_load().then(() => render());
};

App.brdSel = async function(postId) {
  RS.brdSel = postId;
  S.brdReplyTo = null;
  if (!RS.brdDetail[postId]) await BRD_loadDetail(postId);
  render();
};

App.brdReplyTo = function(replyId, isTop) {
  S.brdReplyTo = (isTop === '1') ? null : replyId;
  render();
};

App.brdReplyCancel = function() {
  S.brdReplyTo = null;
  render();
};

App.brdReply = async function(postId) {
  const ta = $('#brd-reply');
  const body = ((ta || {}).value || '').trim();
  const msg = $('#brd-reply-msg');
  if (!body) { if (msg) msg.textContent = '내용을 입력하세요.'; return; }
  const payload = { body: body };
  if (S.brdReplyTo) payload.parent_reply_id = S.brdReplyTo;
  try {
    const r = await apiFetch('/board/posts/' + postId + '/replies', { method: 'POST', body: JSON.stringify(payload) });
    if (!r.ok) {
      let code = '';
      try { const e = await r.json(); code = (e && e.detail && e.detail.code) || (e && e.code) || ''; } catch (x) {}
      const t = r.status === 403 ? '권한이 없습니다.' : '답글 등록에 실패했습니다.';
      toast(code ? t + ' (' + code + ')' : t);
      return;
    }
    S.brdReplyTo = null;
    delete RS.brdDetail[postId];
    await BRD_loadDetail(postId);
    RS.brd = null;
    await BRD_load();
    render();
    toast('답글을 등록했습니다.');
  } catch (e) {
    toast('답글 등록에 실패했습니다.');
  }
};

App.brdStatus = async function(postId, status) {
  try {
    const r = await apiFetch('/board/posts/' + postId + '/status', { method: 'PATCH', body: JSON.stringify({ status: status }) });
    if (!r.ok) {
      let code = '';
      try { const e = await r.json(); code = (e && e.detail && e.detail.code) || (e && e.code) || ''; } catch (x) {}
      const t = r.status === 403 ? '권한이 없습니다.' : '상태 변경에 실패했습니다.';
      toast(code ? t + ' (' + code + ')' : t);
      return;
    }
    delete RS.brdDetail[postId];
    await BRD_loadDetail(postId);
    RS.brd = null;
    await BRD_load();
    render();
    toast('상태를 변경했습니다.');
  } catch (e) {
    toast('상태 변경에 실패했습니다.');
  }
};

/* ---- 등록 ---- */
Object.assign(I18N.ko, { 'nav.cons-board': '홈페이지 문의함' });
if (I18N.en) Object.assign(I18N.en, { 'nav.cons-board': 'Website Inquiries' });
if (I18N.id) Object.assign(I18N.id, { 'nav.cons-board': 'Pertanyaan Situs' });

VIEWS['cons-board'] = BRD_view;
if (NAV.cons && !NAV.cons.includes('cons-board')) NAV.cons.push('cons-board');

NAVC['cons-board'] = () => {
  const items = (RS.brd && RS.brd.items) || [];
  return items.filter(it => it.status === 'open').length;
};

if (typeof OPS_TABS !== 'undefined' && Array.isArray(OPS_TABS)) {
  OPS_TABS.push({ key: 'board', label: '홈페이지 문의함', render: BRD_view });
}
