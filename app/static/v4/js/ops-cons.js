/* ===== 운영 도구 · 컨설턴트·오디터 ===== */

const OPSC_TAB = { key: "people", label: "컨설턴트·오디터", render: null };
const OPSC_URL = {
  consultants: "/admin/consultants",
  clients: "/admin/consultant-clients",
  workloads: "/ops/auditors/workload",
};

const OPSC_err = (d) => {
  if (d && d.status === 403) return "이 기능은 권한이 없습니다(운영자·관리자).";
  return "불러오지 못했습니다.";
};

const OPSC_detail = async (r) => {
  try { return (await r.json()).detail || null; } catch (e) { return null; }
};

async function OPSC_loadConsultants() {
  try {
    const r = await apiFetch(OPSC_URL.consultants);
    RS.opscCons = r.ok ? await r.json() : { error: true, status: r.status };
  } catch (e) { RS.opscCons = { error: true }; }
}
async function OPSC_loadClients() {
  try {
    const r = await apiFetch(OPSC_URL.clients);
    RS.opscClients = r.ok ? await r.json() : { error: true, status: r.status };
  } catch (e) { RS.opscClients = { error: true }; }
}
async function OPSC_loadWorkloads() {
  try {
    const r = await apiFetch(OPSC_URL.workloads);
    RS.opscWork = r.ok ? await r.json() : { error: true, status: r.status };
  } catch (e) { RS.opscWork = { error: true }; }
}
async function OPSC_loadCommission(cid, from, to) {
  const qs = [];
  if (from) qs.push("date_from=" + encodeURIComponent(from));
  if (to) qs.push("date_to=" + encodeURIComponent(to));
  const path = "/admin/consultants/" + encodeURIComponent(cid) + "/commission" +
    (qs.length ? "?" + qs.join("&") : "");
  try {
    const r = await apiFetch(path);
    RS.opscComm = RS.opscComm || {};
    RS.opscComm[cid] = r.ok ? await r.json() : { error: true, status: r.status };
  } catch (e) {
    RS.opscComm = RS.opscComm || {};
    RS.opscComm[cid] = { error: true };
  }
}

const OPSC_money = (n, cur) => (typeof _money === "function" ? _money(n, cur) : (n == null ? "-" : String(n)));

function OPSC_rowCons(c, sel) {
  const p = c.profile || {};
  return `<tr>
    <td>${esc(c.display_name || c.username || "")}<div class="muted mono" style="font-size:11px">${esc(c.username || "")}</div></td>
    <td class="mono">${esc(c.consultant_id || "")}</td>
    <td class="num">${p.commission_rate != null ? esc(String(p.commission_rate)) + "%" : '<span class="muted">미설정</span>'}</td>
    <td>${c.status ? `<span class="badge ${c.status === "active" ? "strong" : "attn"}">${esc(c.status)}</span>` : '<span class="muted">-</span>'}</td>
    <td class="num">${esc(String(c.client_count != null ? c.client_count : 0))}</td>
    <td><div class="row-act">
      <button class="btn btn-sm" onclick="App.opscEdit('${esc(c.consultant_id)}')">수정</button>
      <button class="btn btn-sm" onclick="App.opscCommission('${esc(c.consultant_id)}')">수수료</button>
      <button class="btn btn-sm btn-ghost" onclick="App.opscConfirmDelete('${esc(c.consultant_id)}',${Math.min(Number(c.client_count)||0,99999)})">삭제</button>
    </div></td>
  </tr>`;
}

function OPSC_panelConsultants() {
  const head = `<div class="panel-head"><h2>컨설턴트 관리</h2>
    <button class="btn btn-sm btn-primary" onclick="App.opscAdd()">컨설턴트 추가</button></div>`;
  const d = RS.opscCons;
  if (!d) {
    if (!RS._opscConsLoading) { RS._opscConsLoading = 1; OPSC_loadConsultants().then(() => { RS._opscConsLoading = 0; render(); }); }
    return `<section class="panel">${head}<p class="empty">불러오는 중…</p></section>`;
  }
  if (d.error) return `<section class="panel">${head}<p class="empty">${OPSC_err(d)}</p></section>`;
  const items = d.items || [];
  if (!items.length) return `<section class="panel">${head}<p class="empty">등록된 컨설턴트가 없습니다.</p></section>`;
  const shown = items.slice(0, 100);
  const more = items.length > 100 ? `<p class="muted" style="margin-top:8px">외 ${items.length - 100}건</p>` : "";
  const rows = shown.map(c => OPSC_rowCons(c, S.opscSel)).join("");
  return `<section class="panel">${head}
    <div class="tbl-wrap"><table>
      <thead><tr><th>이름</th><th>코드</th><th class="num">수수료율</th><th>상태</th><th class="num">담당 업체</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>${more}
    ${OPSC_commissionBlock()}
  </section>`;
}

function OPSC_commissionBlock() {
  const cid = S.opscSel;
  if (!cid) return "";
  const c = (RS.opscComm || {})[cid];
  if (!c) {
    if (RS._opscCommLoading !== cid) {
      RS._opscCommLoading = cid;
      OPSC_loadCommission(cid, S.opscFrom || "", S.opscTo || "").then(() => { RS._opscCommLoading = null; render(); });
    }
    return `<section class="panel" style="margin-top:16px"><p class="empty">불러오는 중…</p></section>`;
  }
  if (c.error) return `<section class="panel" style="margin-top:16px"><p class="empty">${OPSC_err(c)}</p></section>`;
  const rate = c.commission_rate;
  const invs = c.invoices || [];
  const paid = c.payouts || [];
  const invShow = invs.slice(0, 100);
  const paidShow = paid.slice(0, 100);
  return `<section class="panel" style="margin-top:16px">
    <div class="panel-head"><h2>수수료 — ${esc(cid)}</h2>
      <button class="btn btn-sm btn-ghost" onclick="App.opscCommissionClose()">닫기</button></div>
    <div class="summary">
      <div><span class="k">담당 업체</span><span class="v mono">${esc(String(c.client_count != null ? c.client_count : 0))}</span></div>
      <div><span class="k">실적 건수</span><span class="v mono">${esc(String(c.invoice_count != null ? c.invoice_count : 0))}</span></div>
      <div><span class="k">과세표준</span><span class="v mono">${OPSC_money(c.base_amount)}</span></div>
      <div><span class="k">수수료율</span><span class="v mono">${rate != null ? esc(String(rate)) + "%" : "미설정"}</span></div>
      <div><span class="k">수수료액</span><span class="v mono">${c.commission_amount != null ? OPSC_money(c.commission_amount) : "요율 미설정"}</span></div>
    </div>
    ${invShow.length ? `<div class="tbl-wrap" style="margin-top:10px"><table>
      <thead><tr><th>인보이스</th><th>업체</th><th>유형</th><th class="num">금액</th><th>생성</th></tr></thead>
      <tbody>${invShow.map(iv => `<tr>
        <td class="mono">${esc(iv.invoice_no || iv.invoice_id || "")}</td>
        <td>${esc(iv.company_name || "")}</td>
        <td>${esc(iv.service_type || "")}</td>
        <td class="num">${OPSC_money(iv.amount)}</td>
        <td class="mono">${esc(String(iv.created_at || "").slice(0, 10))}</td>
      </tr>`).join("")}</tbody></table></div>
      ${invs.length > 100 ? `<p class="muted">외 ${invs.length - 100}건</p>` : ""}` : `<p class="empty">결제 완료된 인보이스가 없습니다.</p>`}
    <div class="panel-head" style="margin-top:14px"><h2 style="font-size:14px">지급 내역</h2>
      <button class="btn btn-sm" onclick="App.opscPayoutOpen()">지급 생성</button></div>
    ${paidShow.length ? `<div class="tbl-wrap"><table>
      <thead><tr><th>기간</th><th class="num">금액</th><th>상태</th><th>지급일</th><th></th></tr></thead>
      <tbody>${paidShow.map(p => `<tr>
        <td class="mono">${esc(p.period || "")}</td>
        <td class="num">${OPSC_money(p.amount)}</td>
        <td>${p.status === "paid" ? '<span class="badge strong">paid</span>' : '<span class="badge attn">pending</span>'}</td>
        <td class="mono">${esc(String(p.paid_at || "").slice(0, 10))}</td>
        <td>${p.status !== "paid" ? `<div class="row-act"><button class="btn btn-sm" onclick="App.opscPayoutPaid('${esc(p.payout_id)}')">지급 완료 처리</button></div>` : ""}</td>
      </tr>`).join("")}</tbody></table></div>
      ${paid.length > 100 ? `<p class="muted">외 ${paid.length - 100}건</p>` : ""}` : `<p class="empty">지급 내역이 없습니다.</p>`}
  </section>`;
}

function OPSC_panelClients() {
  const head = `<div class="panel-head"><h2>업체별 담당 컨설턴트</h2></div>`;
  const d = RS.opscClients;
  if (!d) {
    if (!RS._opscClientsLoading) { RS._opscClientsLoading = 1; OPSC_loadClients().then(() => { RS._opscClientsLoading = 0; render(); }); }
    return `<section class="panel">${head}<p class="empty">불러오는 중…</p></section>`;
  }
  if (d.error) return `<section class="panel">${head}<p class="empty">${OPSC_err(d)}</p></section>`;
  const items = d.items || [];
  if (!items.length) return `<section class="panel">${head}<p class="empty">등록된 업체가 없습니다.</p></section>`;
  const shown = items.slice(0, 100);
  return `<section class="panel">${head}
    <div class="tbl-wrap"><table>
      <thead><tr><th>업체</th><th class="num">케이스</th><th>담당 컨설턴트</th><th>연결일</th></tr></thead>
      <tbody>${shown.map(o => `<tr>
        <td>${esc(o.name || "")}<div class="muted mono" style="font-size:11px">${esc(o.org_id || "")}</div></td>
        <td class="num">${esc(String(o.cases != null ? o.cases : 0))}</td>
        <td>${o.consultant_id ? esc(o.consultant_name || o.consultant_id) : '<span class="badge attn">미지정</span>'}</td>
        <td class="mono">${esc(String(o.linked_at || "").slice(0, 10))}</td>
      </tr>`).join("")}</tbody></table></div>
    ${items.length > 100 ? `<p class="muted" style="margin-top:8px">외 ${items.length - 100}건</p>` : ""}
  </section>`;
}

function OPSC_panelWorkloads() {
  const head = `<div class="panel-head"><h2>오디터 업무 현황</h2></div>`;
  const d = RS.opscWork;
  if (!d) {
    if (!RS._opscWorkLoading) { RS._opscWorkLoading = 1; OPSC_loadWorkloads().then(() => { RS._opscWorkLoading = 0; render(); }); }
    return `<section class="panel">${head}<p class="empty">불러오는 중…</p></section>`;
  }
  if (d.error) return `<section class="panel">${head}<p class="empty">${OPSC_err(d)}</p></section>`;
  const rows = d.auditors || [];
  if (!rows.length) return `<section class="panel">${head}<p class="empty">등록된 오디터가 없습니다.</p></section>`;
  const shown = rows.slice(0, 100);
  return `<section class="panel">${head}
    <div class="summary">
      <div><span class="k">오디터</span><span class="v mono">${esc(String(d.count || rows.length))}</span></div>
      <div><span class="k">미배정 케이스</span><span class="v mono">${esc(String(d.unassigned != null ? d.unassigned : 0))}</span></div>
      <div><span class="k">이번주</span><span class="v mono">${esc(String(d.week_start || ""))} ~ ${esc(String(d.week_end || ""))}</span></div>
    </div>
    <div class="tbl-wrap" style="margin-top:10px"><table>
      <thead><tr>
        <th>오디터</th><th>전문</th><th class="num">담당</th><th class="num">부하</th>
        <th class="num">수락대기</th><th class="num">거절</th><th class="num">이번주 현장</th><th class="num">미해결 부적합</th>
      </tr></thead>
      <tbody>${shown.map(a => `<tr>
        <td>${esc(a.username || "")}<div class="muted mono" style="font-size:11px">${esc(a.user_id || "")}</div></td>
        <td>${esc(a.specialty || "")}${a.languages && a.languages.length ? `<div class="muted" style="font-size:11px">${esc(a.languages.join(", "))}</div>` : ""}</td>
        <td class="num">${esc(String(a.assigned != null ? a.assigned : 0))}/${esc(String(a.capacity != null ? a.capacity : ""))}</td>
        <td class="num"><span class="badge ${(Number(a.load_pct) || 0) >= 100 ? "attn" : ""}">${esc(String(a.load_pct != null ? a.load_pct : 0))}%</span></td>
        <td class="num">${esc(String(a.pending_accept != null ? a.pending_accept : 0))}</td>
        <td class="num">${esc(String(a.rejected != null ? a.rejected : 0))}</td>
        <td class="num">${esc(String(a.week_onsite != null ? a.week_onsite : 0))}</td>
        <td class="num">${esc(String(a.open_findings != null ? a.open_findings : 0))}</td>
      </tr>`).join("")}</tbody></table></div>
    ${rows.length > 100 ? `<p class="muted" style="margin-top:8px">외 ${rows.length - 100}건</p>` : ""}
  </section>`;
}

function OPSC_render() {
  return OPSC_panelConsultants() + OPSC_panelClients() + OPSC_panelWorkloads();
}

OPSC_TAB.render = OPSC_render;
if (typeof OPS_TABS !== 'undefined' && Array.isArray(OPS_TABS)) OPS_TABS.push(OPSC_TAB);

// ---- 액션 ----

App.opscAdd = function () {
  const body = `
    <div class="grid-2">
      <div class="field"><label>아이디 <span class="req">*</span></label><input class="in" id="opsc-username" autocomplete="off"></div>
      <div class="field"><label>비밀번호 <span class="req">*</span></label><input class="in" id="opsc-password" type="password" autocomplete="new-password"></div>
      <div class="field"><label>이름</label><input class="in" id="opsc-display_name"></div>
      <div class="field"><label>회사명</label><input class="in" id="opsc-company_name"></div>
      <div class="field"><label>사업자등록번호</label><input class="in" id="opsc-biz_reg_no"></div>
      <div class="field"><label>전화</label><input class="in" id="opsc-phone"></div>
      <div class="field"><label>이메일</label><input class="in" id="opsc-email"></div>
      <div class="field"><label>주소</label><input class="in" id="opsc-address"></div>
      <div class="field"><label>은행</label><input class="in" id="opsc-bank_name"></div>
      <div class="field"><label>계좌번호</label><input class="in" id="opsc-bank_account"></div>
      <div class="field"><label>예금주</label><input class="in" id="opsc-account_holder"></div>
      <div class="field"><label>수수료율(%)</label><input class="in" id="opsc-commission_rate" type="number" step="0.01" min="0"></div>
    </div>
    <div class="field"><label>계약 메모</label><textarea class="in" id="opsc-contract_note" rows="2"></textarea></div>
    <div class="inline-msg" id="opsc-msg"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
    <button class="btn btn-primary" onclick="App.opscAddSubmit()">등록</button>`;
  openModal("컨설턴트 추가", body, foot, true);
};

App.opscAddSubmit = async function () {
  const val = (id) => (($("#" + id) || {}).value || "").trim();
  const username = val("opsc-username"), password = val("opsc-password");
  const msg = $("#opsc-msg");
  if (msg) msg.textContent = "";
  if (!username || !password) {
    if (msg) msg.textContent = "아이디와 비밀번호는 필수입니다.";
    return;
  }
  const rateRaw = val("opsc-commission_rate");
  const body = {
    username, password,
    display_name: val("opsc-display_name") || null,
    company_name: val("opsc-company_name") || null,
    biz_reg_no: val("opsc-biz_reg_no") || null,
    phone: val("opsc-phone") || null,
    email: val("opsc-email") || null,
    address: val("opsc-address") || null,
    bank_name: val("opsc-bank_name") || null,
    bank_account: val("opsc-bank_account") || null,
    account_holder: val("opsc-account_holder") || null,
    commission_rate: rateRaw === "" ? null : Number(rateRaw),
    contract_note: val("opsc-contract_note") || null,
  };
  try {
    const r = await apiFetch(OPSC_URL.consultants, { method: "POST", body: JSON.stringify(body) });
    if (!r.ok) {
      const d = await OPSC_detail(r);
      const code = d && d.code ? " (" + d.code + ")" : "";
      if (r.status === 403) toast("권한이 없습니다.");
      else toast("컨설턴트 등록에 실패했습니다." + code);
      return;
    }
    App.closeModal();
    RS.opscCons = null;
    await OPSC_loadConsultants();
    render();
    toast("컨설턴트를 등록했습니다.");
  } catch (e) { toast("컨설턴트 등록에 실패했습니다."); }
};

App.opscEdit = function (cid) {
  const d = RS.opscCons && !RS.opscCons.error ? RS.opscCons : null;
  const c = d ? (d.items || []).find(x => x.consultant_id === cid) : null;
  if (!c) { toast("대상을 찾을 수 없습니다."); return; }
  const p = c.profile || {};
  const v = (x) => esc(x == null ? "" : String(x));
  const body = `
    <div class="grid-2">
      <div class="field"><label>이름</label><input class="in" id="opsc-e-display_name" value="${v(c.display_name)}"></div>
      <div class="field"><label>회사명</label><input class="in" id="opsc-e-company_name" value="${v(p.company_name || c.company_name)}"></div>
      <div class="field"><label>사업자등록번호</label><input class="in" id="opsc-e-biz_reg_no" value="${v(p.biz_reg_no)}"></div>
      <div class="field"><label>전화</label><input class="in" id="opsc-e-phone" value="${v(p.phone)}"></div>
      <div class="field"><label>이메일</label><input class="in" id="opsc-e-email" value="${v(p.email)}"></div>
      <div class="field"><label>주소</label><input class="in" id="opsc-e-address" value="${v(p.address)}"></div>
      <div class="field"><label>은행</label><input class="in" id="opsc-e-bank_name" value="${v(p.bank_name)}"></div>
      <div class="field"><label>계좌번호</label><input class="in" id="opsc-e-bank_account" value="${v(p.bank_account)}"></div>
      <div class="field"><label>예금주</label><input class="in" id="opsc-e-account_holder" value="${v(p.account_holder)}"></div>
      <div class="field"><label>수수료율(%)</label><input class="in" id="opsc-e-commission_rate" type="number" step="0.01" min="0" value="${p.commission_rate != null ? v(p.commission_rate) : ""}"></div>
      <div class="field"><label>상태</label><input class="in" id="opsc-e-status" value="${v(c.status)}"></div>
    </div>
    <div class="field"><label>계약 메모</label><textarea class="in" id="opsc-e-contract_note" rows="2">${v(p.contract_note)}</textarea></div>
    <div class="inline-msg" id="opsc-e-msg"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
    <button class="btn btn-primary" onclick="App.opscEditSubmit('${esc(cid)}')">저장</button>`;
  openModal("컨설턴트 수정", body, foot, true);
};

App.opscEditSubmit = async function (cid) {
  const val = (id) => (($("#" + id) || {}).value || "").trim();
  const rateRaw = val("opsc-e-commission_rate");
  const body = {
    display_name: val("opsc-e-display_name") || null,
    company_name: val("opsc-e-company_name") || null,
    biz_reg_no: val("opsc-e-biz_reg_no") || null,
    phone: val("opsc-e-phone") || null,
    email: val("opsc-e-email") || null,
    address: val("opsc-e-address") || null,
    bank_name: val("opsc-e-bank_name") || null,
    bank_account: val("opsc-e-bank_account") || null,
    account_holder: val("opsc-e-account_holder") || null,
    commission_rate: rateRaw === "" ? null : Number(rateRaw),
    contract_note: val("opsc-e-contract_note") || null,
    status: val("opsc-e-status") || null,
  };
  try {
    const r = await apiFetch(OPSC_URL.consultants + "/" + encodeURIComponent(cid), { method: "PUT", body: JSON.stringify(body) });
    if (!r.ok) {
      const d = await OPSC_detail(r);
      const code = d && d.code ? " (" + d.code + ")" : "";
      if (r.status === 403) toast("권한이 없습니다.");
      else toast("수정에 실패했습니다." + code);
      return;
    }
    App.closeModal();
    RS.opscCons = null;
    RS.opscClients = null;
    await OPSC_loadConsultants();
    render();
    toast("수정했습니다.");
  } catch (e) { toast("수정에 실패했습니다."); }
};

App.opscConfirmDelete = function (cid, clientCount) {
  const warn = clientCount > 0
    ? `<p class="note attn">담당 업체가 ${clientCount}개 있습니다. 담당을 넘기거나 해제하기 전에는 삭제할 수 없습니다.</p>`
    : `<p class="note">삭제하면 되돌릴 수 없습니다. 정산 이력이 있으면 거부됩니다.</p>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
    <button class="btn btn-primary" onclick="App.opscDelete('${esc(cid)}')">삭제</button>`;
  openModal("컨설턴트 삭제", warn, foot);
};

App.opscDelete = async function (cid) {
  try {
    const r = await apiFetch(OPSC_URL.consultants + "/" + encodeURIComponent(cid), { method: "DELETE" });
    if (!r.ok) {
      const d = await OPSC_detail(r);
      const code = d && d.code ? " (" + d.code + ")" : "";
      if (r.status === 403) toast("권한이 없습니다.");
      else toast("삭제에 실패했습니다." + code);
      return;
    }
    App.closeModal();
    RS.opscCons = null;
    RS.opscClients = null;
    if (RS.opscComm) RS.opscComm[cid] = null;
    if (S.opscSel === cid) S.opscSel = null;
    await OPSC_loadConsultants();
    render();
    toast("삭제했습니다.");
  } catch (e) { toast("삭제에 실패했습니다."); }
};

App.opscCommission = async function (cid) {
  S.opscSel = cid;
  if (!RS.opscComm || !RS.opscComm[cid]) {
    await OPSC_loadCommission(cid, S.opscFrom || "", S.opscTo || "");
  }
  render();
};

App.opscCommissionClose = function () { S.opscSel = null; render(); };

App.opscPayoutOpen = function () {
  const cid = S.opscSel;
  if (!cid) return;
  const body = `
    <div class="grid-2">
      <div class="field"><label>기간 시작 <span class="req">*</span></label><input class="in" id="opsc-p-from" type="date"></div>
      <div class="field"><label>기간 종료 <span class="req">*</span></label><input class="in" id="opsc-p-to" type="date"></div>
    </div>
    <div class="field"><label>메모</label><textarea class="in" id="opsc-p-note" rows="2"></textarea></div>
    <div class="inline-msg" id="opsc-p-msg"></div>`;
  const foot = `<button class="btn" onclick="App.closeModal()">취소</button>
    <button class="btn btn-primary" onclick="App.opscPayoutCreate()">생성</button>`;
  openModal("수수료 지급 생성", body, foot, true);
};

App.opscPayoutCreate = async function () {
  const cid = S.opscSel;
  if (!cid) return;
  const val = (id) => (($("#" + id) || {}).value || "").trim();
  const from = val("opsc-p-from"), to = val("opsc-p-to");
  const msg = $("#opsc-p-msg");
  if (msg) msg.textContent = "";
  if (!from || !to) { if (msg) msg.textContent = "기간을 입력하세요."; return; }
  const body = { consultant_id: cid, period_from: from, period_to: to, note: val("opsc-p-note") || null };
  try {
    const r = await apiFetch("/admin/consultants/payouts", { method: "POST", body: JSON.stringify(body) });
    if (!r.ok) {
      const d = await OPSC_detail(r);
      const code = d && d.code ? " (" + d.code + ")" : "";
      if (r.status === 403) toast("권한이 없습니다.");
      else toast("지급 생성에 실패했습니다." + code);
      return;
    }
    App.closeModal();
    await OPSC_loadCommission(cid, S.opscFrom || "", S.opscTo || "");
    render();
    toast("지급 기록을 생성했습니다.");
  } catch (e) { toast("지급 생성에 실패했습니다."); }
};

App.opscPayoutPaid = async function (pid) {
  try {
    const r = await apiFetch("/admin/consultants/payouts/" + encodeURIComponent(pid) + "/paid", { method: "POST" });
    if (!r.ok) {
      const d = await OPSC_detail(r);
      const code = d && d.code ? " (" + d.code + ")" : "";
      if (r.status === 403) toast("권한이 없습니다.");
      else toast("지급 완료 처리에 실패했습니다." + code);
      return;
    }
    const cid = S.opscSel;
    if (cid) await OPSC_loadCommission(cid, S.opscFrom || "", S.opscTo || "");
    render();
    toast("지급 완료로 처리했습니다.");
  } catch (e) { toast("지급 완료 처리에 실패했습니다."); }
};
