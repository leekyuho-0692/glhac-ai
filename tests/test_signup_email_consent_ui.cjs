/* 두 가입 화면의 실제 제출 함수·번역 사전을 실행한다. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../app/static');
const sources = {
  v4: fs.readFileSync(path.join(root, 'v4/index.html'), 'utf8'),
  legacy: fs.readFileSync(path.join(root, 'index.html'), 'utf8'),
};
const consentKo = '[필수] 인증 진행 안내 메일 수신에 동의합니다. 제출 서류의 문제(판독 실패·파일 손상·보완 필요 등)와 심사 진행 상황을 위 업체 이메일로 보내드립니다.';
const emailKo = '올바른 업체 이메일을 200자 이내로 입력해 주세요.';
const consentErrorKo = '인증 진행 안내 메일 수신에 동의해 주세요.';

function between(source, start, end){
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, start);
  return source.slice(a, b);
}

function screen(kind, lang='ko'){
  const source = sources[kind], dom = {}, calls = [], notices = [];
  const ids = kind === 'v4' ? {email:'es-company-email', consent:'es-email-consent'} : {email:'regCompanyEmail', consent:'regEmailConsent'};
  const values = kind === 'v4'
    ? {'es-pw':'Passw0rd!23','es-pw2':'Passw0rd!23','es-id':'login@example.com','es-biz':'123-45-67890','m1-email':'pic@example.com'}
    : {regUser:'signup_user',regPass:'Passw0rd!23',regPass2:'Passw0rd!23'};
  for(const [id,value] of Object.entries(values)) dom[id] = {value};
  dom[ids.email] = {value:'  Company@Example.COM  '};
  dom[ids.consent] = {checked:true};
  const c = vm.createContext({
    App:{}, S:{lang}, LANG:{ko:1,en:0,id:2}[lang], API_BASE:'', AUTH:{}, TK:'token',
    ROLES:{enterprise:{code:'ent',home:'ent-home'}}, BASE_FEE:{},
    $:selector=>dom[selector.slice(1)] || null, v:id=>dom[id]?.value || '', esc:String,
    document:{getElementById:id=>dom[id] || null}, window:{scrollTo(){}},
    localStorage:{setItem(){}}, authSave(){}, uiRoleFromServer:()=>'ent',
    URL,location:new URL('http://localhost/ui/v4/'),history:{state:null,replaceState(){}},
    loadEnt:async()=>{}, render(){}, boot(){}, agreeBlock:()=>'',
    toast:message=>notices.push(message), EMSG:error=>error.message || error.code,
  });
  let serverError = null;
  const reply = {token:'token',refresh_token:'refresh',role:'applicant',username:'signup_user',org_id:'org1'};
  c.fetch = async(url, options)=>{
    calls.push({url, body:JSON.parse(options.body)});
    return new Response(JSON.stringify(serverError ? {detail:serverError} : reply), {status:serverError ? 422 : 200});
  };
  c.API = async(url, options)=>{
    calls.push({url, body:JSON.parse(options.body)});
    if(serverError) throw serverError;
    return reply;
  };
  if(kind === 'v4'){
    vm.runInContext(between(source, 'const I18N = {', '/* ---------- sample data'), c);
    vm.runInContext(source.match(/^const t = .*$/m)[0], c);
    vm.runInContext(between(source, 'function consumeSignupLink(){', 'async function apiRefresh(){'), c);
    vm.runInContext(between(source, 'function entSignup(){', 'function consSignup(){'), c);
    vm.runInContext('App.submitEnt=({' + between(source, '  async submitEnt(e){', '  dupReset()') + '}).submitEnt;', c);
  }else{
    vm.runInContext(between(source, 'var EN_STRINGS={', '\nfunction T('), c);
    vm.runInContext(between(source, 'const UI_STRINGS={', '/* 전 언어 렌더 후처리'), c);
    vm.runInContext(source.match(/^function _dualSplit\(s\).*$/m)[0] + '\n' + source.match(/^function T\(s\).*$/m)[0], c);
    vm.runInContext(between(source, 'async function doRegister(e){', 'let _inviteTimer='), c);
  }
  return {dom, ids, calls, notices,
    submit:()=> (kind === 'v4' ? c.App.submitEnt : c.doRegister)({preventDefault(){},target:{querySelector:()=>({})}}),
    fail:error=>{serverError=error;},
    text:(key,ko)=>vm.runInContext(kind === 'v4' ? `t(${JSON.stringify(key)})` : `T(${JSON.stringify(ko)})`, c),
    markup:()=>kind === 'v4' ? c.entSignup() : between(source, '<form class="loginbox" id="registerForm"', '</form>'),
  };
}

for(const kind of ['v4','legacy']){
  test(kind+' 업체 이메일·동의는 필수이며 이메일 패턴이 도메인을 검사한다',()=>{
    const s = screen(kind), markup = s.markup();
    const email = markup.match(new RegExp('<input[^>]*id="'+s.ids.email+'"[^>]*>'))[0];
    assert.match(email, /type="email"/); assert.match(email, /required/); assert.match(email, /maxlength="200"/);
    const pattern = new RegExp(email.match(/pattern="([^"]+)"/)[1], 'v');
    assert.ok(pattern.test('company@example.com'));
    for(const value of ['a@localhost','a b@example.com','a@@example.com']) assert.ok(!pattern.test(value));
    const consent = markup.match(new RegExp('<input[^>]*id="'+s.ids.consent+'"[^>]*>'))[0];
    assert.match(consent, /type="checkbox"/); assert.match(consent, /required/);
    assert.ok(!/\schecked(?:\s|>|=)/.test(consent));
    assert.ok(markup.includes(consentKo));
  });

  for(const lang of ['ko','en','id']){
    test(kind+' '+lang+' 잘못된 업체 메일·미동의를 안내하고 전송하지 않는다',async()=>{
      const s = screen(kind, lang);
      for(const invalid of ['', 'pic-only', 'a@localhost', 'a b@example.com', 'a'.repeat(196)+'@a.co']){
        s.dom[s.ids.email].value = invalid;
        await s.submit();
        assert.equal(s.notices.at(-1), s.text('signup.email_invalid', emailKo));
      }
      s.dom[s.ids.email].value = 'company@example.com';
      s.dom[s.ids.consent].checked = false;
      await s.submit();
      assert.equal(s.notices.at(-1), s.text('signup.consent_required', consentErrorKo));
      assert.equal(s.calls.length, 0);
      const text = s.text('signup.email_consent', consentKo);
      assert.match(text, {ko:/^\[필수\]/,en:/^\[Required\]/,id:/^\[Wajib\]/}[lang]);
    });

    test(kind+' '+lang+' 서버 422를 번역된 안내로 표시한다',async()=>{
      const s = screen(kind, lang);
      for(const [error,key,ko] of [
        [{code:'EMAIL_CONSENT_REQUIRED'},'signup.consent_required',consentErrorKo],
        [{code:'VALIDATION_ERROR',field:'company_email',message:'String should match pattern'},'signup.email_invalid',emailKo],
        [{code:'VALIDATION_ERROR',field:'email_consent',message:'Field required'},'signup.consent_required',consentErrorKo],
      ]){
        s.fail(error);
        await s.submit();
        assert.equal(s.notices.at(-1), s.text(key,ko));
      }
      assert.equal(s.calls.length, 3);
    });
  }

  test(kind+' 정규화한 업체 메일과 명시적 동의를 전송한다',async()=>{
    const s = screen(kind);
    await s.submit();
    assert.equal(s.calls.length, 1);
    assert.equal(s.calls[0].url, '/auth/register');
    assert.equal(s.calls[0].body.company_email, 'company@example.com');
    assert.equal(s.calls[0].body.email_consent, true);
    if(kind === 'v4') assert.equal(s.calls[0].body.contacts.pic.email, 'pic@example.com');
  });
}
