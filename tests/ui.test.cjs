const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {execFileSync}=require('node:child_process');
const {JSDOM}=require('jsdom');
const html=execFileSync(process.env.LAZARR_TEST_PYTHON||'python',['-c',`
from jinja2 import Environment, FileSystemLoader
from lazarr.languages import LABELS, ALIASES
env=Environment(loader=FileSystemLoader('src/lazarr/templates'))
env.globals.update(language_labels=LABELS,language_aliases=ALIASES)
print(env.get_template('index.html').render(csrf='test',user={'username':'test'}))
`],{encoding:'utf8',env:{...process.env,PYTHONPATH:'src'}});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function setup(){
  const dom=new JSDOM(html,{url:'http://localhost/',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,errors=[],calls=[];
  w.addEventListener('error',event=>errors.push(event.error));
  w.HTMLElement.prototype.scrollIntoView=function(){};
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
  w.HTMLDialogElement.prototype.close=function(){this.open=false};
  const defaults={audio_languages:['ru'],subtitle_languages:[],min_resolution:720,max_resolution:1080,keyword:''};
  let activity={running:false,state:'idle',pending_requests:0,history:[]};
  const status=()=>({engine_available:true,content_providers:['demo'],search:activity});
  const tasks=[];
  let taskChoices=[];
  let providers=[];
  let libraries=[],libraryDetail={};
  w.fetch=async(url,options={})=>{
    const payload=options.body?JSON.parse(options.body):undefined;calls.push({url,method:options.method,payload});
    let result;
    if(url==='/api/v1/settings')result={defaults,prefer_full_subtitles:true,movie_path:'/tmp/movies',series_path:'/tmp/series',search_start:'00:00',seed_ratio:1,plugin_repository:''};
    else if(url==='/api/v1/status')result=status();
    else if(url==='/api/v1/providers/order'){
      providers.sort((a,b)=>payload.ids.indexOf(a.id)-payload.ids.indexOf(b.id));result={ok:true};
    }
    else if(url==='/api/v1/providers')result=providers;
    else if(url==='/api/v1/libraries')result=libraries;
    else if(url.startsWith('/api/v1/libraries/media/')&&options.method==='DELETE'){
      const id=Number(url.split('/').pop());
      libraries=libraries.map(group=>({...group,items:group.items.filter(item=>item.id!==id)}));
      result={ok:true,cleanup_pending:false,tasks_deleted:2};
    }
    else if(url.endsWith('/subtitles')&&options.method==='POST')result={downloaded:[{item:'S2E14',language:'ru',path:'episode.opensubtitles.ru.srt'}],not_found:[],skipped:0,errors:[]};
    else if(url.startsWith('/api/v1/libraries/media/'))result=libraryDetail;
    else if(url==='/api/v1/tasks'&&options.method==='POST'){
      result={id:1,search_queued:true};
      tasks.push({id:1,title:'Test',year:2020,season:1,requirements:payload.requirements,subtasks:[],paused:false});
      activity={running:true,state:'running',pending_requests:1,groups_total:2,groups_done:1,candidates_found:3,candidates_checked:1,message:'Чтение описания',provider:'Demo',media:'Test',season:1,episodes:[1,2],history:[{time:1,message:'Demo: поиск Test'}]};
    }else if(/^\/api\/v1\/tasks\/\d+\/candidates$/.test(url))result=taskChoices;
    else if(/^\/api\/v1\/candidates\/\d+\/choice-all$/.test(url)&&options.method==='POST')result={selected:8,total:8,skipped:0};
    else if(url==='/api/v1/tasks')result=tasks;
    else if(url==='/api/v1/search/run'){activity={...activity,running:true,state:'running',pending_requests:1};result={queued:true};}
    else if(url.startsWith('/api/v1/metadata/'))result={provider:'tmdb',id:'42',kind:'tv',title:'Test',year:2020,seasons:[{number:1,title:'Season 1',episode_count:2}],episode_numbering:{}};
    else result=[];
    return {ok:true,status:200,json:async()=>result};
  };
  // A single realm matches ordered classic defer scripts in the real document.
  w.eval(fs.readFileSync('src/lazarr/static/language-picker.js','utf8')+'\n'+fs.readFileSync('src/lazarr/static/library.js','utf8')+'\n'+fs.readFileSync('src/lazarr/static/app.js','utf8'));
  await settle();await settle();
  return {dom,w,document:w.document,errors,calls,setActivity:value=>activity=value,setProviders:value=>providers=value,setTasks:value=>{tasks.splice(0,tasks.length,...value)},setTaskChoices:value=>taskChoices=value,setLibrary:(groups,detail)=>{libraries=groups;libraryDetail=detail}};
}
function input(w,node,value){node.value=value;node.dispatchEvent(new w.Event('input',{bubbles:true}));}
function key(w,node,key){node.dispatchEvent(new w.KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));}

test('language autocomplete pills, aliases, keyboard, removal, and no duplicates',async()=>{
  const {dom,w,document:d,errors}=await setup();
  try{
    d.querySelector('[data-tab=settings]').click();await settle();
    const picker=d.querySelector('[data-language-picker=default_audio_languages]'),query=picker.querySelector('.language-query');
    assert.equal(picker.querySelector('.language-pill').textContent,'Русский×');
    query.focus();input(w,query,'jap');
    assert.equal(picker.querySelectorAll('[data-add-language]').length,1);
    assert.equal(picker.querySelector('[data-add-language]').textContent,'Японский');
    picker.querySelector('[data-add-language=ja]').click();
    assert.deepEqual([...picker.querySelectorAll('input[type=hidden]')].map(i=>i.value),['ru','ja']);
    input(w,query,'japanese');assert.equal(picker.querySelectorAll('[data-add-language]').length,0);
    input(w,query,'eng');key(w,query,'ArrowDown');key(w,query,'Enter');
    assert.equal(picker.querySelectorAll('.language-pill').length,3);
    picker.querySelector('[data-remove-language=ja]').click();
    assert.equal(picker.querySelectorAll('.language-pill').length,2);
    key(w,query,'Backspace');assert.equal(picker.querySelectorAll('.language-pill').length,1);
    key(w,query,'Escape');assert.equal(query.getAttribute('aria-expanded'),'false');
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});

test('full subtitles preference loads and saves as a checkbox',async()=>{
  const {dom,w,document:d,calls,errors}=await setup();
  try{
    d.querySelector('[data-tab=settings]').click();await settle();await settle();
    const preference=d.querySelector('[name=prefer_full_subtitles]');
    assert.equal(preference.checked,true);
    preference.checked=false;
    d.querySelector('#settings-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
    await settle();await settle();
    const saved=calls.find(c=>c.url==='/api/v1/settings'&&c.method==='PUT');
    assert.equal(saved.payload.prefer_full_subtitles,false);
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});

test('create task sends canonical picks and immediately opens Libraries with live activity',async()=>{
  const {dom,w,document:d,errors,calls}=await setup();
  try{
    d.querySelector('#search-results').innerHTML='<button data-media-id="42" data-provider="tmdb" data-kind="tv">Test</button>';
    d.querySelector('[data-media-id]').click();await settle();await settle();
    const picker=d.querySelector('[data-language-picker=audio_languages]'),query=picker.querySelector('.language-query');
    input(w,query,'Япон');picker.querySelector('[data-add-language=ja]').click();
    d.querySelector('#task-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await settle();await settle();
    const created=calls.find(c=>c.url==='/api/v1/tasks'&&c.method==='POST');
    assert.deepEqual(created.payload.requirements.audio_languages,['ru','ja']);
    assert.equal(d.querySelector('#tab-library').hidden,false);
    assert.equal(d.querySelector('#tab-search').hidden,true);
    assert.equal(d.querySelector('#search-activity').hidden,false);
    assert.equal(d.querySelector('#search-current').textContent,'Чтение описания');
    assert.equal(d.querySelector('#search-progress').getAttribute('aria-valuenow'),'1');
    assert.equal(d.querySelector('#search-progress span').style.width,'50%');
    assert.equal(d.querySelector('#run-queue').disabled,true);
    assert.match(d.querySelector('#search-query').textContent,/Demo.*Test.*1, 2/);
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});

test('manual queue button starts search without waiting for results',async()=>{
  const {dom,document:d,calls,errors}=await setup();
  try{
    d.querySelector('[data-tab=library]').click();await settle();
    assert.equal(d.querySelector('#run-queue').disabled,false);
    d.querySelector('#run-queue').click();await settle();await settle();
    assert.equal(calls.filter(c=>c.url==='/api/v1/search/run').length,1);
    assert.equal(d.querySelector('#run-queue').disabled,true);
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});

test('failed search shows cooldown cause and disabled provider separately',async()=>{
  const {dom,document:d,setActivity,errors}=await setup();
  try{
    setActivity({running:false,state:'error',message:'Поиск не выполнен',pending_requests:0,history:[],providers:[
      {id:'nyaa',name:'Nyaa',state:'cooldown',reason:'Временная пауза после ошибки: HTTP 504',retry_at:2000000000,requests:0,candidates:0},
      {id:'rutracker',name:'Rutracker',state:'disabled',reason:'Выключен в настройках',requests:0,candidates:0}
    ]});
    d.querySelector('[data-tab=library]').click();await settle();await settle();
    assert.equal(d.querySelector('#search-state').textContent,'Поиск не выполнен');
    assert.equal(d.querySelectorAll('.search-provider').length,2);
    assert.match(d.querySelector('#search-providers').textContent,/Nyaa.*504.*Rutracker.*Выключен/s);
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});


test('enabled provider priority controls save and display the new order',async()=>{
  const {dom,document:d,calls,errors,setProviders}=await setup();
  try{
    setProviders([
      {id:'nyaa',name:'Nyaa',kind:'content',enabled:true,config_fields:[],config:{},configured_secrets:[]},
      {id:'rutracker',name:'Rutracker',kind:'content',enabled:true,config_fields:[],config:{},configured_secrets:[]},
      {id:'off',name:'Disabled',kind:'content',enabled:false,config_fields:[],config:{},configured_secrets:[]}
    ]);
    d.querySelector('[data-tab=settings]').click();await settle();await settle();
    assert.equal(d.querySelectorAll('.provider-order li').length,2);
    assert.equal(d.querySelector('[data-provider-move=nyaa][data-direction="-1"]').disabled,true);
    d.querySelector('[data-provider-move=rutracker][data-direction="-1"]').click();await settle();await settle();
    const saved=calls.find(c=>c.url==='/api/v1/providers/order');
    assert.equal(saved.method,'PUT');
    assert.deepEqual(saved.payload.ids,['rutracker','nyaa']);
    assert.equal(d.querySelector('.provider-order li span').textContent,'Rutracker');
    assert.equal(d.querySelector('[data-provider-move=rutracker][data-direction="-1"]').disabled,true);
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});

test('activity distinguishes actual checks, filtering, failures and delayed retry',async()=>{
  const {dom,document:d,setActivity}=await setup();
  try{
    setActivity({running:false,state:'queued',pending_requests:1,next_attempt_at:Date.now()/1000+120,groups_total:1,groups_done:1,candidates_found:50,candidates_checked:7,candidates_filtered:26,candidates_failed:1,candidates_deferred:16,message:'Повтор поиска запланирован после паузы провайдера',history:[]});
    d.querySelector('[data-tab=library]').click();await settle();
    assert.match(d.querySelector('#search-counts').textContent,/Найдено: 50.*Проверено: 7.*Отсеяно: 26.*Отложено: 16.*Ошибки проверки: 1/);
    assert.match(d.querySelector('#search-current').textContent,/Не раньше/);
  }finally{dom.window.close();}
});

test('one release can be selected for every matching episode in a task',async()=>{
  const {dom,document:d,calls,errors,setTasks,setTaskChoices,setLibrary}=await setup();
  try{
    const subtasks=Array.from({length:8},(_,index)=>({id:index+1,episode:index+1,title:`Эпизод ${index+1}`,status:'needs_selection',missing_subtitle_languages:[],error:null}));
    setTasks([{id:7,media_id:7,title:'Nathan for You',season:1,subtasks,requirements:{audio_languages:['ru'],subtitle_languages:[],min_resolution:720,max_resolution:1080},paused:false}]);
    setLibrary([{id:'series',name:'Сериалы',items:[{id:7,title:'Nathan for You',year:2013,taxonomy_known:true}]},{id:'movies',name:'Кино',items:[]},{id:'anime',name:'Аниме',items:[]}],{title:'Nathan for You',year:2013,metadata:{overview:'',genres:[]},task_count:1,last_search_at:1,episodes:subtasks.map(part=>({id:part.id,season:1,episode:part.episode,title:part.title,air_date:'2013-01-01',released:true,statuses:[part.status],subtasks:[{id:part.id,task_id:7,status:part.status}],files:[]}))});
    setTaskChoices([{id:91,total:8,matched:8,candidate:{title:'Nathan For You S01 1080p',provider:'demo',url:'https://example.org/release',size:1024,seeds:10}}]);
    d.querySelector('[data-tab=library]').click();await settle();await settle();
    d.querySelector('[data-library-media="7"]').click();await settle();await settle();
    const button=d.querySelector('[data-task-candidates="7"]');
    assert.equal(button.textContent,'Выбрать для всех серий');
    button.click();await settle();await settle();
    assert.match(d.querySelector('#modal-body').textContent,/Сопоставлено по результатам проверки: 8 из 8/);
    d.querySelector('[data-choose-all="91"]').click();await settle();await settle();
    const call=calls.find(item=>item.url==='/api/v1/candidates/91/choice-all');
    assert.equal(call.method,'POST');
    assert.deepEqual(call.payload,{});
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});


test('libraries switch categories, show details and delete media',async()=>{
  const {dom,w,document:d,errors,calls,setLibrary}=await setup();
  try{
    const download={id:5,state:'downloading',progress:.42,eta:120,download_rate:2048,upload_rate:128,ratio:.1,seed_ratio:1,seeds:3,peers:4};
    setLibrary([{id:'series',name:'Сериалы',items:[]},{id:'movies',name:'Кино',items:[]},{id:'anime',name:'Аниме',items:[{id:2,title:'Re:Zero',year:2016,taxonomy_known:true,download:{progress:.42,download_rate:2048}}]}],{title:'Re:Zero',kind:'tv',year:2016,metadata:{overview:'Описание',original_title:'Original',genres:['Анимация']},task_count:2,last_search_at:1,episodes:[{id:1,season:1,episode:1,title:'Начало',overview:'',still:null,air_date:'2016-04-04',released:true,statuses:[],subtasks:[],last_search_at:null,files:[]},{id:14,season:2,episode:14,title:'Пари',overview:'Описание серии',still:'https://image.tmdb.org/t/p/w342/still.jpg',air_date:'2021-01-06',released:true,statuses:['downloading'],subtasks:[{id:11,task_id:7,status:'downloading'}],download,last_search_at:1,files:[{current:false,pending:true,verified:false,path:'episode.mkv',directory:'/downloads',resolution:1080,size:1024,tracks:[{kind:'audio',language:'ja',external:false}],release:{provider:'rutracker',title:'Selected release',url:'https://example.org/topic'},missing_subtitle_languages:['ru'],download_state:'downloading',download}]}]});
    d.querySelector('[data-tab=library]').click();await settle();await settle();
    assert.equal(d.querySelectorAll('[data-library]').length,3);
    d.querySelector('[data-library=anime]').click();
    assert.ok(d.querySelector('.library-poster-empty'));assert.equal(d.querySelector('.library-poster-frame img'),null);
    assert.equal(d.querySelector('.library-tile .progress').getAttribute('aria-valuenow'),'42.0');
    d.querySelector('[data-library-media="2"]').click();await settle();await settle();
    assert.equal(d.querySelector('#library-overview').hidden,true);
    const detail=d.querySelector('#library-detail-body').textContent;
    assert.match(detail,/Описание серии/);assert.match(detail,/Календарь выхода/);assert.match(detail,/14\. Пари/);
    assert.equal(d.querySelectorAll('.library-season').length,2);assert.match(d.querySelector('.library-season').textContent,/Сезон 1/);
    assert.equal(d.querySelector('[data-season="2"]').open,true);assert.ok(d.querySelector('[data-season="1"]').classList.contains('is-disabled'));assert.ok(d.querySelector('.episode-still-empty'));
    assert.match(detail,/1080p/);assert.match(detail,/Японский/);assert.match(detail,/Selected release/);assert.match(detail,/Предварительные сведения/);
    assert.equal(d.querySelector('.episode-still').getAttribute('src'),'/api/v1/posters/tmdb/still.jpg');
    assert.equal(d.querySelector('.episode-download .progress').getAttribute('aria-valuenow'),'42.0');
    assert.equal(d.querySelector('[data-candidates="11"]').textContent,'Выбрать раздачу');
    d.querySelector('#library-delete').click();
    assert.equal(d.querySelector('#modal').open,true);
    const form=d.querySelector('#delete-media-form');
    assert.match(form.textContent,/файлы останутся на диске/);
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await settle();await settle();
    const removed=calls.find(call=>call.url==='/api/v1/libraries/media/2'&&call.method==='DELETE');
    assert.deepEqual(removed.payload,{delete_files:false});
    assert.equal(d.querySelector('#library-overview').hidden,false);
    assert.equal(d.querySelector('[data-library-media="2"]'),null);
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});

test('missing library seasons stay disabled and open a prefilled task form',async()=>{
  const {dom,document:d,errors,setTasks,setLibrary}=await setup();
  try{
    setTasks([{id:7,media_id:2,title:'Show',season:1,canonical_season:1,subtasks:[],requirements:{audio_languages:['ru'],subtitle_languages:[],min_resolution:720,max_resolution:1080},paused:false}]);
    const metadata={provider:'tmdb',id:'42',kind:'tv',title:'Show',year:2020,overview:'Описание',genres:[],seasons:[{number:1,title:'Сезон 1',episode_count:8},{number:2,title:'Сезон 2',episode_count:10},{number:3,title:'Сезон 3',episode_count:6}],episode_numbering:{}};
    setLibrary([{id:'series',name:'Сериалы',items:[{id:2,title:'Show',year:2020,taxonomy_known:true}]},{id:'movies',name:'Кино',items:[]},{id:'anime',name:'Аниме',items:[]}],{id:2,title:'Show',kind:'tv',year:2020,metadata,seasons:metadata.seasons,task_count:1,last_search_at:1,episodes:[{id:1,season:1,episode:1,title:'Первая',air_date:'2020-01-01',released:true,statuses:['queued'],subtasks:[{id:1,task_id:7,status:'queued'}],files:[]}]});
    d.querySelector('[data-tab=library]').click();await settle();await settle();
    d.querySelector('[data-library-media="2"]').click();await settle();await settle();
    assert.equal(d.querySelectorAll('.library-season').length,3);
    assert.equal(d.querySelectorAll('.library-season.is-disabled').length,2);
    assert.equal(d.querySelector('[data-season="2"]').open,false);
    d.querySelector('[data-download-season="3"]').click();await settle();await settle();
    assert.equal(d.querySelector('#tab-search').hidden,false);
    assert.equal(d.querySelector('#selection').hidden,false);
    assert.equal(d.querySelector('#selection-title').textContent,'Show · 2020');
    assert.equal(d.querySelector('#season-select').value,'3');
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});

test('library subtitle button downloads selected language and refreshes details',async()=>{
  const {dom,w,document:d,errors,calls,setLibrary}=await setup();
  try{
    setLibrary([{id:'series',name:'Сериалы',items:[{id:2,title:'Show',year:2020,taxonomy_known:true}]},{id:'movies',name:'Кино',items:[]},{id:'anime',name:'Аниме',items:[]}],{title:'Show',year:2020,metadata:{overview:'',genres:[]},task_count:1,last_search_at:1,episodes:[]});
    d.querySelector('[data-tab=library]').click();await settle();await settle();
    d.querySelector('[data-library-media="2"]').click();await settle();await settle();
    d.querySelector('#library-subtitles').click();
    const form=d.querySelector('#subtitle-download-form'),query=form.querySelector('.language-query');
    input(w,query,'Рус');form.querySelector('[data-add-language=ru]').click();
    form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await settle();await settle();
    const call=calls.find(item=>item.url==='/api/v1/libraries/media/2/subtitles');
    assert.equal(call.method,'POST');assert.deepEqual(call.payload,{languages:['ru']});
    assert.equal(d.querySelector('#modal').open,false);
    assert.deepEqual(errors,[]);
  }finally{dom.window.close();}
});
