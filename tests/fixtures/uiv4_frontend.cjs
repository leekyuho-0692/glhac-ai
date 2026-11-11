const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const assert=require('node:assert/strict');
const root=path.join(__dirname,'../../app/static/v4');
const source=fs.readFileSync(path.join(root,'index.html'),'utf8');
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a);assert.ok(a>=0&&b>a,start);return source.slice(a,b);}
function harness(lang='ko'){
  const dom={},calls=[],notices=[];
  const c=vm.createContext({console,Map,Set,URL,URLSearchParams,TextEncoder,TextDecoder,Uint8Array,AbortController,AbortSignal,
    crypto:require('node:crypto').webcrypto,btoa:s=>Buffer.from(s,'binary').toString('base64'),setTimeout,clearTimeout,
    S:{lang,role:'ent',view:'ent-apply'},AUTH:{role:'applicant',username:'applicant1',org_id:'org1'},API_BASE:'',
    RS:{cases:[{case_id:'c1',company_name:'식품 기업'}],entDocs:{}},App:{go(v){c.S.view=v},setLang(l){c.S.lang=l},logout(){}},
    VIEWS:{'ent-home':()=>'', 'ent-apply':()=>'', 'ent-contract':()=>'', 'cons-contract':()=>'', 'cons-new':()=>''},
    NAV:{aud:[],adm:[],cons:[]},NAVC:{},S8_LABELS:Array.from({length:8},(_,i)=>String(i)),pad:n=>String(n).padStart(2,'0'),
    _s8label:c=>c.step8_label||'',_money:String,ROLES:{enterprise:{code:'ent',home:'ent-home'}},
    esc:s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])),jsq:String,
    $:sel=>dom[sel.slice(1)]||null,document:{addEventListener(){},getElementById(){return null},querySelector(){return null}},
    window:{scrollTo(){},addEventListener(){}},localStorage:{setItem(){},getItem(){return null}},
    location:new URL('http://localhost/ui/v4/'),history:{state:null,replaceState(state,unused,url){this.state=state;c.location=new URL(url,c.location)}},
    render(){},toast:s=>notices.push(s),agreeBlock:()=>'',uiRoleFromServer:()=>'ent',authSave(){},loadEnt:async()=>{},
    canCall:()=>true,openModal(){},apiFetch:async()=>({ok:true,status:200,json:async()=>[]}),
  });
  c.run=js=>vm.runInContext(js,c);
  c.load=file=>c.run(fs.readFileSync(path.join(root,'js',file),'utf8'));
  c.run(between('const I18N = {','/* ---------- sample data'));
  c.run(source.match(/^const t = .*$/m)[0]);
  c.run(between('function consumeSignupLink(){','async function apiRefresh(){'));
  return {c,dom,calls,notices};
}
function signup(lang='ko',sector='cosmetics'){
  const h=harness(lang),{c,dom,calls}=h;
  c.run(between('function entSignup(){','function consSignup(){'));
  c.run('App.submitEnt=({'+between('  async submitEnt(e){','  dupReset()')+'}).submitEnt;');
  const html=c.entSignup(),select=html.match(/<select[^>]*id="es-cat"[^>]*>([\s\S]*?)<\/select>/)[1];
  const options=[...select.matchAll(/<option value="([^"]+)">([^<]+)<\/option>/g)].map(m=>({value:m[1],label:m[2]}));
  assert.equal(options.length,5,'sector options must have explicit codes');
  assert.ok(options.some(o=>o.value===sector));
  for(const [id,value] of Object.entries({'es-pw':'Passw0rd!23','es-pw2':'Passw0rd!23','es-id':`uiv4_${lang}_${sector}@example.com`,'es-name':'식품 기업','es-name-en':'Test Company','es-ceo':'Contact','es-addr':'Seoul','es-biz':'123-45-67890','es-cat':sector,'es-company-email':'company@example.com','m1-email':'pic@example.com'})) dom[id]={value};
  dom['es-email-consent']={checked:true};
  c.fetch=async(url,opts)=>{calls.push({url,body:JSON.parse(opts.body)});return {ok:true,status:200,json:async()=>({role:'applicant',username:'test',token:'token'})}};
  return {...h,html,options,async submit(){await c.App.submitEnt({preventDefault(){},target:{querySelector:()=>({})}});assert.equal(calls.length,1);return calls[0].body;}};
}
module.exports={harness,signup,between,source,root};
if(require.main===module)(async()=>{
 const payloads=[];for(const lang of ['ko','en','id'])for(const sector of ['food','cosmetics','household','warehouse','transport']){const h=signup(lang,sector);payloads.push({lang,sector,body:await h.submit()});}
 process.stdout.write(JSON.stringify(payloads));
})().catch(e=>{console.error(e);process.exitCode=1;});
