const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const {harness,between}=require('./fixtures/uiv4_frontend.cjs');
const digest=s=>createHash('sha256').update(s).digest('hex');
function apiHarness(){
  const h=harness(),c=h.c; h.timers=[]; h.calls=[];
  c.Response=Response; c.atob=atob;
  c.setTimeout=(fn,ms)=>{h.timers.push(ms); if(ms<10000) queueMicrotask(fn); return 1;};
  c.clearTimeout=()=>{};
  c.apiRefresh=async()=>false;
  c.run(between('function apiUploadBody(opts){','/* refresh 까지'));
  c.fetch=async(url,opts)=>{h.calls.push([url,opts]);return new Response('{}');};
  return h;
}
const payload={filename:'한글.pdf',file_b64:btoa('AAAA'),upload_id:'request-00000000-aaaa'};
const stored={document_id:'saved',filename:'한글.pdf',file_size:4,file_hash:digest('AAAA'),confidence:null,upload_id:payload.upload_id};

test('every file upload uses the long timeout independent of endpoint spelling',async()=>{
  for(const path of ['/cases/c1/documents','/cases/c1/parse-file','/cases/c1/sjph-evidence','/admin/forms/new-version','/feedback/1/images','/corrections/1']){
    const h=apiHarness(); await h.c.apiFetch(path,{method:'POST',body:JSON.stringify(payload)});
    assert.equal(h.timers[0],600000,path);
  }
  const h=apiHarness();await h.c.apiFetch('/cases/c1/intake-zip-stream',{method:'POST',body:JSON.stringify({zip_b64:'YWJj'})});
  assert.equal(h.timers[0],600000);
  await h.c.apiFetch('/intake/status');assert.equal(h.timers[1],30000);
  const options={method:'POST',body:JSON.stringify(payload),timeoutMs:90000,headers:{'X-Test':'keep'}};
  await h.c.apiFetch('/custom',options); assert.equal(h.timers[2],90000);
  assert.equal(options.signal,undefined);assert.equal(options.headers.Authorization,undefined);
  assert.equal(h.calls[2][1].timeoutMs,undefined);
});

test('network loss reconciles the same upload_id without reposting',async()=>{
  const h=apiHarness(); let posts=0,gets=0;
  h.c.fetch=async(url,opts)=>{
    if(opts.method==='POST'){posts++;throw new TypeError('connection lost');}
    gets++; assert.equal(url,'/cases/c1/documents'); return new Response(JSON.stringify([stored]));
  };
  const response=await h.c.apiFetch('/cases/c1/documents',{method:'POST',body:JSON.stringify(payload)});
  const result=await response.json();
  assert.equal(result.recovered,true);assert.equal(result.document_id,'saved');assert.equal(result.autoparse_queued,true);
  assert.equal(posts,1);assert.equal(gets,1);
});

test('a timeout can reconcile an already completed document',async()=>{
  const h=apiHarness();
  h.c.setTimeout=(fn,ms)=>{if(ms===600000)queueMicrotask(fn);return 1;};
  h.c.fetch=async(url,opts)=>{
    if(opts.method==='POST') return new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('timeout','AbortError'))));
    return new Response(JSON.stringify([{...stored,confidence:0.9}]));
  };
  const r=await h.c.apiFetch('/cases/c1/documents',{method:'POST',body:JSON.stringify(payload)});
  assert.equal((await r.json()).autoparse_queued,false);
});

test('old or other-uploader documents never recover even with identical name size and hash',async()=>{
  for(const doc of [{...stored,upload_id:null},{...stored,upload_id:'previous-upload-0000'},
      {...stored,upload_id:null,file_hash:null,uploaded_by:'someone-else'}]){
    const h=apiHarness();
    h.c.fetch=async(url,opts)=>{if(opts.method==='POST')throw new TypeError('lost');return new Response(JSON.stringify([doc]));};
    await assert.rejects(()=>h.c.apiFetch('/cases/c1/documents',{method:'POST',body:JSON.stringify(payload)}),/lost/);
  }
});

test('explicit cancellation and a security rejection do not become recovered success',async()=>{
  const h=apiHarness(),abort=new AbortController();abort.abort();let gets=0;
  h.c.fetch=async(url,opts)=>{if(!opts.method)gets++;throw new DOMException('cancelled','AbortError');};
  await assert.rejects(()=>h.c.apiFetch('/cases/c1/documents',{method:'POST',body:JSON.stringify(payload),signal:abort.signal}));
  assert.equal(gets,0);
  h.c.fetch=async()=>new Response('{}',{status:415});
  const r=await h.c.apiFetch('/cases/c1/documents',{method:'POST',body:JSON.stringify(payload)});
  assert.equal(r.status,415);
});

function streamResult(result){
  const content=new TextEncoder().encode('data: '+JSON.stringify({type:'result',...result})+'\n\n');
  return new Response(new ReadableStream({start(c){c.enqueue(content);c.close();}}));
}
const file=name=>({name,size:4,arrayBuffer:async()=>new TextEncoder().encode('fake').buffer});
test('bulk list automatically routes ZIP/RAR/7z and displays escaped skipped paths',async()=>{
  const h=harness(),c=h.c,posts=[];c.load('ent-apply-more.js');c.run('ENTA_pollBulk=async()=>{}');
  c.apiFetch=async(path,opts={})=>{
    if(opts.method==='POST'){
      posts.push({path,body:JSON.parse(opts.body)});
      return streamResult({classified:[{filename:'doc.pdf'}],skipped_files:[{filename:'<script>.exe',reason:'UNSUPPORTED_TYPE'}]});
    }
    return new Response('[]');
  };
  await c.App.entaUpload([file('bundle.Zip'),file('bundle.RAR'),file('bundle.7z')]);
  assert.equal(posts.length,3);assert.ok(posts.every(p=>p.path.endsWith('/intake-zip-stream')&&p.body.zip_b64&&!p.body.file_b64));
  assert.ok(c.S.entaBulk.items.every(it=>it.status==='complete'));
  const html=c.run('ENTA_applyPanel()');assert.match(html,/건너뜀/);assert.match(html,/&lt;script&gt;/);assert.ok(!html.includes('<script>.exe'));
});

test('bulk archive without route permission gives ZIP guidance',async()=>{
  const h=harness(),c=h.c;c.load('ent-apply-more.js');c.run('ENTA_pollBulk=async()=>{}');
  c.canCall=(method,path)=>!path.includes('intake-zip');let posts=0;
  c.apiFetch=async(path,opts={})=>{if(opts.method==='POST')posts++;return new Response('[]');};
  await c.App.entaUpload([file('data.Zip')]);
  assert.equal(posts,0);assert.match(c.S.entaBulk.items[0].reason,/ZIP 버튼/);
});

test('bulk recovery displays completed state and avoids retry for saved document',async()=>{
  const h=apiHarness(),c=h.c;c.load('ent-apply-more.js');c.run('ENTA_pollBulk=async()=>{}');let gets=0,uploadId;
  c.fetch=async(path,opts)=>{
    if(opts.method==='POST'){uploadId=JSON.parse(opts.body).upload_id;throw new TypeError('lost response');}
    return new Response(JSON.stringify(++gets===1?[]:[{...stored,upload_id:uploadId,filename:'scan.txt',file_hash:digest('fake'),confidence:0.9}]));
  };
  await c.App.entaUpload([file('scan.txt')]);
  assert.equal(c.S.entaBulk.items[0].status,'complete');assert.equal(c.S.entaBulk.items[0].document_id,'saved');
  assert.equal(c.S.entaBulk.items[0].reason,'');
});

test('each request generates a fresh upload_id and keeps it through auth refresh',async()=>{
  const h=apiHarness(),ids=[];let attempts=0;
  h.c.apiRefresh=async()=>true;
  h.c.fetch=async(path,opts)=>{
    ids.push(JSON.parse(opts.body).upload_id);
    return new Response('{}',{status:++attempts===1?401:200});
  };
  const options={method:'POST',body:JSON.stringify({filename:'한글.pdf',file_b64:btoa('AAAA')})};
  await h.c.apiFetch('/cases/c1/documents',options);
  await h.c.apiFetch('/cases/c1/documents',options);
  assert.match(ids[0],/^[0-9a-f-]{36}$/);
  assert.equal(ids[0],ids[1]);assert.notEqual(ids[1],ids[2]);
  assert.equal(JSON.parse(options.body).upload_id,undefined);
});

test('caller cancellation still reaches a streaming upload after response headers',async()=>{
  const h=apiHarness(),outer=new AbortController();let signal;
  h.c.fetch=async(url,opts)=>{signal=opts.signal;return new Response('data: {}\n\n');};
  await h.c.apiFetch('/cases/c1/intake-zip-stream',{method:'POST',body:JSON.stringify({zip_b64:'YWJj'}),signal:outer.signal});
  outer.abort();assert.equal(signal.aborted,true);
});

test('loss of the JSON body after 200 headers also reconciles stored upload',async()=>{
  const h=apiHarness();let gets=0;
  h.c.fetch=async(url,opts)=>{
    if(opts.method==='POST') return new Response(new ReadableStream({start(c){c.error(new TypeError('body connection lost'));}}));
    gets++;return new Response(JSON.stringify([stored]));
  };
  const r=await h.c.apiFetch('/cases/c1/documents',{method:'POST',body:JSON.stringify(payload)});
  assert.equal((await r.json()).recovered,true);assert.equal(gets,1);
});
