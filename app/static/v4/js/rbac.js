/* ===== RBAC 게이트 — 403 나는 버튼은 아예 그리지 않는다 =====
   · CAN(actionId): 서버 단일 매트릭스(GET /rbac/actions, app/rbac.py ACTION_ROLES) 기준.
   · canCall(method, path): require_roles(...) 로 막힌 엔드포인트용 클라이언트 표(SERVER_ROLES_FOR).
   · canView(view): 화면 단위(VIEW_ROLES) — 사이드바에서 숨긴다.
   서버 역할(AUTH.role) 기준. admin 은 require_roles·require_action 모두 통과(auth.py:180, rbac.py:can).
   표에 없는 엔드포인트·매트릭스 미수신 시엔 허용(최종 판정은 서버 403). */
const RBAC = { actions:null, role:null, hidden:[] };

/* 엔드포인트 → 허용 서버 역할 (main.py require_roles 원문, 줄번호는 작성 시점). 문자열 값 = rbac action_id 위임 */
const SERVER_ROLES_FOR = {
  // ent
  'POST /invoices/{}/refund/request':            ['operator'],                                   // main.py:13869
  'POST /cleanings/{}/sign':                     ['penyelia_halal'],                             // main.py:17761
  'POST /cases/{}/parse-file':                   ['applicant','consultant'],                     // main.py:6248
  'POST /cases/{}/products':                     ['applicant','consultant'],                     // main.py:4272
  'DELETE /cases/{}/products/{}':                ['applicant','consultant','operator'],          // main.py:4775
  'POST /cases/{}/submit-application':           ['applicant','consultant'],                     // main.py:6173
  'POST /cases/{}/preassess/resubmit':           ['applicant','consultant'],                     // main.py:16494
  'POST /cases/{}/audit-report/resubmit':        ['applicant','consultant'],                     // main.py:10535
  'POST /cases/{}/save-draft':                   ['applicant','consultant'],                     // main.py:6159
  'POST /cases/{}/intake-zip-stream':            ['applicant','consultant'],                     // main.py:5794
  'PATCH /cases/{}/profile':                     ['applicant','consultant'],                     // main.py:5848
  'POST /cases/{}/profile/autofill':             ['applicant','consultant'],                     // main.py:6058
  'PUT /cases/{}/halal-org':                     ['applicant','consultant','auditor'],           // main.py:6894
  'POST /orgs/{}/vehicles':                      ['applicant','consultant','operator'],          // main.py:17609
  'PATCH /vehicles/{}':                          ['applicant','consultant','operator'],          // main.py:17630
  'DELETE /vehicles/{}':                         ['applicant','consultant','operator'],          // main.py:17648
  'DELETE /cleanings/{}':                        ['applicant','consultant','operator'],          // main.py:17793
  // aud
  'POST /cases/{}/materials':                    'material.add',                                 // main.py:4298
  'DELETE /materials/{}':                        'material.delete',                              // main.py:4318
  'PATCH /materials/{}/rename':                  ['applicant','consultant'],                     // main.py:4377
  'POST /documents/{}/reprocess':                ['consultant','operator'],                      // main.py:5318
  'PATCH /documents/{}/reclassify':              ['applicant','consultant','operator'],          // main.py:5039
  'POST /cases/{}/pending-extractions/apply':    'material.add',                                 // main.py:5295
  'PATCH /documents/{}/review':                  'document.review',                              // main.py:6357
  'GET /cases/{}/evidence-bundle.zip':           ['operator'],                                   // main.py:2107
  // 계약
  'POST /cases/{}/contract/request-signature':   ['auditor'],                                    // main.py:7376
  'POST /cases/{}/contract/confirm':             ['operator'],                                   // main.py:7636
  'POST /cases/{}/contract/approve':             ['operator'],                                   // main.py:7346
  'POST /cases/{}/contract/receive':             ['applicant','client','consultant'],            // main.py:7360
  // 자기선언 · 동반자
  'POST /cases/{}/pathway/confirm':              ['consultant'],                                 // main.py:16368
  'POST /cases/{}/pendamping/assign':            ['consultant'],                                 // main.py:16787
  'POST /cases/{}/pendamping/verify':            'pendamping.verify',                            // main.py:16807
  'GET /pendamping/assignments':                 ['pendamping_pph','operator'],                  // main.py:16834
  // SiHalal 신원
  'POST /cases/{}/sihalal/identity/link':        ['applicant','consultant'],                     // main.py:16989
  'POST /sihalal/identity/{}/verify':            ['consultant'],                                 // main.py:17004
  // adm (admin 전용 = require_roles() / require_roles("admin"))
  'GET /admin/users':                            [],                                             // main.py:1584
  'GET /admin/staff-signups':                    [],                                             // main.py:1517
  'GET /admin/orgs':                             [],                                             // main.py:1682
  'POST /admin/orgs':                            [],                                             // main.py:1706
  'GET /admin/audit-logs':                       [],                                             // main.py:1647
  'GET /admin/audit-log-verify':                 [],                                             // main.py:2010
  'GET /admin/compliance':                       [],                                             // main.py:13237
  'GET /admin/cases':                            [],                                             // main.py:1715
  'POST /admin/orgs/{}/activate-domain':         [],                                             // main.py:1037
  'GET /admin/lph-references':                   ['consultant'],                                 // main.py:11066
  'POST /admin/lph-references':                  [],                                             // main.py:11074
  'GET /admin/kma1360-exempt':                   [],                                             // main.py:1779
  'GET /admin/ontology/stats':                   [],                                             // main.py:1736
  'POST /admin/ontology/reseed':                 [],                                             // main.py:1744
  // adm (operator 전용 — admin 은 통과)
  'POST /ops/companies/{}/approve':              ['operator'],                                   // main.py:15049
  'POST /ops/companies/{}/reject':               ['operator'],                                   // main.py:15070
  'GET /admin/orgs/{}/tenant':                   ['operator'],                                   // main.py:972
};
/* 화면(사이드바) 단위 — 표에 없으면 노출 */
const VIEW_ROLES = {
  'adm-members': [],   // GET /admin/users(main.py:1584)·/admin/staff-signups(1517) 모두 admin 전용
};

function _rbacKey(method, path){
  return String(method||'GET').toUpperCase()+' '+String(path||'').split('?')[0].replace(/\{[^}]*\}/g,'{}');
}
function CAN(actionId){
  if(AUTH.role === 'admin') return true;
  if(!RBAC.actions) return true;            // 매트릭스 미수신 — 서버가 최종 판정
  return RBAC.actions.has(actionId);
}
function canCall(method, path){
  const e = SERVER_ROLES_FOR[_rbacKey(method, path)];
  if(e === undefined) return true;
  if(typeof e === 'string') return CAN(e);
  return AUTH.role === 'admin' || e.includes(AUTH.role);
}
function canView(v){
  const r = VIEW_ROLES[v];
  return !r || AUTH.role === 'admin' || r.includes(AUTH.role);
}
/* 사이드바 — 이전 로그인 때 숨긴 항목은 되돌리고 현재 역할 기준으로 다시 숨긴다 */
function rbacNav(){
  for(let j=RBAC.hidden.length-1; j>=0; j--){
    const [k,v,i] = RBAC.hidden[j];
    if(NAV[k] && !NAV[k].includes(v)) NAV[k].splice(Math.min(i, NAV[k].length), 0, v);
  }
  RBAC.hidden = [];
  Object.keys(NAV).forEach(k=>{
    for(let i=NAV[k].length-1; i>=0; i--){
      if(!canView(NAV[k][i])){ RBAC.hidden.push([k, NAV[k][i], i]); NAV[k].splice(i,1); }
    }
  });
}
/* 로그인·새로고침 복원 공통(enterRole) — 데이터 로드 전에 부른다. 실패해도 화면은 뜬다 */
async function loadRbac(){
  RBAC.actions = null; RBAC.role = AUTH.role;
  try{
    const r = await apiFetch('/rbac/actions');
    if(r.ok){
      const d = await r.json(); const a = d.actions || {};
      RBAC.actions = new Set(Object.keys(a).filter(k=>a[k]));
      if(d.role){ RBAC.role = d.role; AUTH.role = d.role; }
    }
  }catch(e){}
  try{ rbacNav(); }catch(e){}
}
