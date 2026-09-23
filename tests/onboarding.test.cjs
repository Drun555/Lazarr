const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync('src/lazarr/static/onboarding.js','utf8');
const template=fs.readFileSync('src/lazarr/templates/onboarding.html','utf8');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function setup(handler,config={}){
  const html=template.replace('{{ onboarding | tojson }}',JSON.stringify({current:0,states:['pending','pending','pending','pending'],trawlUrl:'http://trawl:8191',...config}));
  const dom=new JSDOM(html,{runScripts:'outside-only',url:'http://localhost/onboarding',pretendToBeVisual:true});
  const w=dom.window,requests=[],timers=new Map();let id=0;
  w.AbortSignal.timeout=()=>undefined;
  w.setTimeout=(fn,delay)=>{timers.set(++id,{fn,delay});return id;};
  w.clearTimeout=key=>timers.delete(key);
  w.fetch=async(url,options)=>{const payload=JSON.parse(options.body);requests.push({url,payload,headers:options.headers});const result=await handler(url,payload);return {ok:result.ok!==false,status:result.status||200,json:async()=>result};};
  w.eval(source);
  return {w,dom,requests,timers,$:s=>w.document.querySelector(s),input(selector,value){const el=w.document.querySelector(selector);el.value=value;el.dispatchEvent(new w.Event('input'));},async timer(){const [key,value]=[...timers][0];timers.delete(key);await value.fn();await settle();}};
}
test('Trawl automatically checks startup URL, locks input and advances after actual success',async()=>{
  let finish;const t=setup(()=>new Promise(r=>finish=r));try{
    assert.equal(t.$('#trawl-url').disabled,true);assert.ok(t.$('.spinner'));
    assert.equal(t.requests[0].payload.url,'http://trawl:8191');
    finish({ok:true});await settle();
    assert.equal(t.$('#screen-1').hidden,false);assert.equal(t.$('#trawl-url').disabled,true);
  }finally{t.dom.window.close();}
});
test('Trawl failure unlocks address and edits automatically retry',async()=>{
  const t=setup((url,p)=>p.url.includes('fixed')?{ok:true}:{ok:false,detail:'Недоступен'});try{
    await settle();assert.equal(t.$('#trawl-url').disabled,false);assert.equal(t.$('#screen-0').hidden,false);
    t.input('#trawl-url','http://fixed:8191');await t.timer();assert.equal(t.$('#screen-1').hidden,false);
  }finally{t.dom.window.close();}
});
test('TMDB debounce saves only latest value and failures remain on the same step',async()=>{
  const t=setup(()=>({ok:false,detail:'Неверный ключ'}),{current:1});try{
    t.input('#tmdb','a'.repeat(32));t.input('#tmdb','b'.repeat(32));assert.equal(t.timers.size,1);
    await t.timer();assert.equal(t.requests.length,1);assert.equal(t.requests[0].payload.api_key,'b'.repeat(32));
    assert.equal(t.$('#screen-1').hidden,false);assert.match(t.$('#screen-1 .feedback').textContent,/Неверный/);
    assert.equal(t.$('#tmdb').disabled,false);
  }finally{t.dom.window.close();}
});
test('Rutracker challenge is an error with skip available, without CAPTCHA UI',async()=>{
  const t=setup(()=>({status:'challenge',message:'CAPTCHA'}),{current:2});try{
    t.input('#rt-login','user');t.input('#rt-password','password');t.$('[data-check]').click();await settle();
    assert.equal(t.$('#captcha'),null);assert.match(t.$('#screen-2 .feedback').textContent,/дополнительную проверку/);
    assert.equal(t.$('#screen-2 [data-skip]').disabled,false);
  }finally{t.dom.window.close();}
});
test('completion is persisted before clean final screen and delayed redirect',async()=>{
  let resolve;const t=setup(()=>new Promise(r=>resolve=r),{current:3});try{
    t.$('#screen-3 [data-skip]').click();assert.equal(t.$('#screen-4').hidden,true);
    assert.equal(t.requests[0].url,'/api/v1/onboarding/complete');resolve({ok:true});await settle();
    assert.equal(t.$('#screen-4').hidden,false);assert.equal(t.$('#screen-4').textContent,'Всё готово');
    assert.equal(t.$('.steps').hidden,true);assert.equal([...t.timers.values()][0].delay,2000);
  }finally{t.dom.window.close();}
});
