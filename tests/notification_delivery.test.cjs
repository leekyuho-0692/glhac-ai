const {test}=require('node:test');
const assert=require('node:assert/strict');
const {harness,source}=require('./fixtures/uiv4_frontend.cjs');

function setup(lang){
  const {c}=harness(lang);
  c.SHELL_EXT={sideFoot:[]};
  c.load('notify-status.js');
  c.load('notify-bell.js');
  c.load('ops-audit.js');
  return c;
}
function notification(){
  return {id:'n1',title:'Delivery test',body:'Original body',read:true,status:'partial',
    channels:['inapp','email','sms','whatsapp'],created_at:'2026-11-15',
    channel_status:{
      inapp:{status:'sent',attempts:1},
      email:{status:'skipped',reason:'no_credentials',attempts:0},
      sms:{status:'skipped',reason:'no_contact',attempts:0},
      whatsapp:{status:'failed',reason:'twilio_error:timeout',attempts:1},
    },last_error:'email:no_credentials;sms:no_contact;whatsapp:twilio_error:timeout'};
}
const labels={
  ko:['일부만 발송','발송됨','발송 건너뜀','발송 실패','설정 없음','연락처 없음','메시지 서비스 전송 오류'],
  en:['Partially sent','Sent','Skipped','Failed','Not configured','No contact details','Messaging service delivery error'],
  id:['Terkirim sebagian','Terkirim','Dilewati','Gagal dikirim','Belum dikonfigurasi','Kontak tidak tersedia','Kesalahan pengiriman layanan pesan'],
};

for(const lang of ['ko','en','id']){
  test(`${lang}: administrator history and auditor bell show per-channel delivery results`,()=>{
    const c=setup(lang),n=notification();
    c.AUTH.role='admin';
    c.RS.opsa.channels={channels:{email:{status:'unset'},sms:{status:'connected'}}};
    c.RS.opsa.notifs={items:[n],counts:{partial:1,skipped:2},total:1};
    const admin=c.OPSA_renderNotifs();
    c.AUTH.role='auditor';
    c.RS.ntfList=[n];
    const auditor=c.run('NTF_modalBody()');
    for(const html of [admin,auditor]){
      for(const label of labels[lang]) assert.ok(html.includes(label),label);
      assert.match(html,/badge ntf-skipped/);
      assert.match(html,/badge ntf-failed/);
      assert.match(html,/badge ntf-partial/);
      assert.doesNotMatch(html,/no_credentials|no_contact|twilio_error|notify\.(?:status|reason)\./);
    }
    assert.ok(admin.includes(c.run("t('notify.status.skipped')")+'</span><span class="v mono">2'));
    assert.match(auditor,/Original body/);
  });
  test(`${lang}: skipped-only partial delivery is not labelled a failure`,()=>{
    const c=setup(lang),n=notification();
    n.channels.pop();delete n.channel_status.whatsapp;
    c.RS.ntfList=[n];
    const html=c.run('NTF_modalBody()');
    assert.ok(html.includes(labels[lang][0]));
    assert.ok(html.includes(labels[lang][2]));
    assert.doesNotMatch(html,/ntf-failed/);
    assert.ok(!html.includes(labels[lang][3]));
  });
  test(`${lang}: reason messages and queue confirmation use the reader language`,()=>{
    const c=setup(lang);
    const reasons=['no_credentials','no_contact','not_implemented','consent_skipped',
      'case_closed','no_result','smtp_tls_failed','smtp_error:timeout','twilio_503',
      'webhook_503','send_error','unexpected provider error'];
    for(const reason of reasons){
      const text=c.NTF_reasonText(reason,'failed');
      assert.ok(text && !text.startsWith('notify.'));
      assert.notEqual(text,reason);
      if(lang!=='ko') assert.doesNotMatch(text,/[가-힣]/);
    }
    let body='';c.openModal=(_title,html)=>{body=html};
    c.App.opsaDrainConfirm();
    assert.match(body,new RegExp(lang==='ko'?'건너뛰':lang==='en'?'skipped':'dilewati'));
    assert.doesNotMatch(body,/<strong>sent<\/strong>/);
    if(lang!=='ko') assert.doesNotMatch(body,/[가-힣]/);
  });
}

test('legacy delivery records do not fabricate successful channel results',()=>{
  const c=setup('en');
  const html=c.NTF_channelResults({status:'sent',channels:['email']});
  assert.match(html,/No result recorded/);
  assert.doesNotMatch(html,/ntf-sent/);
  assert.match(c.NTF_deliveryReasons({status:'partial',last_error:'email:no_credentials;sms:no_contact'}),/Not configured/);
  assert.match(c.NTF_deliveryReasons({status:'skipped',last_error:'case_closed'}),/Case closed/);
  assert.match(c.NTF_channelResults({status:'unsent',channels:['email']}),/Not sent/);
});

test('delivery rendering escapes channel names and never renders provider errors as HTML',()=>{
  const c=setup('en'),channel='<img src=x onerror=alert(1)>';
  const n={status:'failed',channels:[channel],channel_status:{[channel]:{status:'failed',reason:channel}}};
  const html=c.NTF_channelResults(n)+c.NTF_deliveryReasons(n)+c.NTF_statusBadge(channel);
  assert.doesNotMatch(html,/<img/);
  assert.match(html,/&lt;img/);
  assert.match(html,/Error after delivery attempt/);
});

test('failed, skipped and partial badges have different foreground and background colors',()=>{
  const rules={};
  for(const status of ['failed','skipped','partial']){
    const rule=source.match(new RegExp('\\.badge\\.ntf-'+status+'[^{}]*\\{([^}]+)\\}'));
    assert.ok(rule,status);
    rules[status]={background:rule[1].match(/background:([^;]+)/)[1],color:rule[1].match(/(?:^|;)color:([^;]+)/)[1]};
  }
  assert.equal(new Set(Object.values(rules).map(rule=>rule.color)).size,3);
  assert.equal(new Set(Object.values(rules).map(rule=>rule.background)).size,3);
});

for(const lang of ['ko','en','id']){
  for(const [status,lastError] of [
    ['skipped','case_closed'],
    ['skipped','email:no_credentials;sms:no_contact'],
    ['failed','smtp_error:<img src=x onerror=alert(1)>'],
  ]){
    test(`${lang}: bell and history share last_error-only reasons (${lastError})`,()=>{
      const c=setup(lang),n={...notification(),status,channels:['email'],
        channel_status:{},last_error:lastError};
      c.RS.ntfList=[n];
      c.AUTH.role='admin';
      c.RS.opsa.channels={channels:{}};
      c.RS.opsa.notifs={items:[n],counts:{},total:1};
      const expected=c.NTF_deliveryReasons(n);
      assert.notEqual(expected,'<span class="muted">—</span>');
      for(const html of [c.run('NTF_modalBody()'),c.OPSA_renderNotifs()]){
        assert.ok(html.includes(expected));
        assert.doesNotMatch(html,/<img|case_closed|no_credentials|smtp_error|notify\.reason\./);
      }
    });
  }
  test(`${lang}: bell displays each channel reason once`,()=>{
    const c=setup(lang),n=notification();
    c.RS.ntfList=[n];
    const html=c.run('NTF_modalBody()');
    for(const label of labels[lang].slice(4)){
      assert.equal(html.split(label).length-1,1,label);
    }
  });
}

const deliveryReasonLabels={
  case_closed:['종결된 케이스','Case closed','Kasus ditutup'],
  no_credentials:['설정 없음','Not configured','Belum dikonfigurasi'],
  no_contact:['연락처 없음','No contact details','Kontak tidak tersedia'],
  not_implemented:['지원하지 않는 채널','Unsupported channel','Kanal tidak didukung'],
  consent_skipped:['수신 동의 없음','No notification consent','Tidak ada persetujuan notifikasi'],
  no_result:['전송 결과를 받지 못함','No delivery result received','Hasil pengiriman tidak diterima'],
  smtp_tls_failed:['메일 서버 보안 연결 실패','Mail server secure connection failed','Koneksi aman ke server email gagal'],
  send_error:['전송 시도 후 오류','Error after delivery attempt','Kesalahan setelah percobaan pengiriman'],
  error:['전송 시도 후 오류','Error after delivery attempt','Kesalahan setelah percobaan pengiriman'],
};
for(const [langIndex,lang] of ['ko','en','id'].entries()){
  for(const [reason,translations] of Object.entries(deliveryReasonLabels)){
    test(`${lang}: bell loads delivery_reason from the notifications API (${reason})`,async()=>{
      const c=setup(lang);
      const n={id:'n-api',case_id:'c1',event_type:'problem.documents',title:'Delivery test',
        body:'Original body',read:true,status:['no_result','smtp_tls_failed','send_error','error'].includes(reason)?'failed':'skipped',
        channels:['email'],channel_status:{},delivery_reason:reason,retry_pending:false,
        created_at:'2026-11-15 12:00:00'};
      c.apiFetch=async url=>{
        assert.equal(url,'/notifications?lang='+lang);
        return {ok:true,json:async()=>[n]};
      };
      await c.NTF_loadList();
      assert.ok(!Object.hasOwn(c.RS.ntfList[0],'last_error'));
      const html=c.run('NTF_modalBody()');
      assert.equal(html.split(translations[langIndex]).length-1,1);
      assert.doesNotMatch(html,/case_closed|no_credentials|no_contact|smtp_error|notify\.reason\./);
    });
  }
  test(`${lang}: safe delivery reason takes precedence over legacy errors`,()=>{
    const c=setup(lang);
    const html=c.NTF_deliveryReasons({status:'partial',channel_status:{},delivery_reason:'error',
      last_error:'smtp_error:private@example.invalid'});
    assert.ok(html.includes(deliveryReasonLabels.error[langIndex]));
    assert.doesNotMatch(html,/private|SMTP|smtp_error/);
  });
  test(`${lang}: notifications without an error keep the empty reason`,()=>{
    const c=setup(lang);
    assert.equal(c.NTF_deliveryReasons({status:'sent',channel_status:{},delivery_reason:null}),
      '<span class="muted">—</span>');
  });
}
