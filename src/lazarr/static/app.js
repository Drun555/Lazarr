'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const posterUrl = value => /^https:\/\/image\.tmdb\.org\/t\/p\/[^/]+\/[A-Za-z0-9_-]+\.(jpg|png|webp)$/.test(value)?'/api/v1/posters/tmdb/'+value.split('/').pop():value;
const languageLabels=JSON.parse($('#language-labels')?.textContent||'{}');
const languageName=value=>languageLabels[value]||value;
const languageList=values=>values.map(languageName).join(', ');
const csrf = () => $('meta[name="csrf-token"]').content;
const applyThemeColor = color => { document.documentElement.dataset.accent=color||'purple'; };
document.addEventListener('change',event=>{if(event.target.matches('input[name="theme_color"]'))applyThemeColor(event.target.value);});
const state = {tab:'search', settings:null, selected:null, tasks:[], generation:0, expanded:new Set()};
const statuses = {removed:'Удалено · выберите раздачу', queued:'В очереди', searching:'Поиск', starting:'Начало скачивания', downloading:'Скачивание', ready:'Готово к просмотру', done:'Готово', waiting_release:'Ожидание выхода', paused:'На паузе', error:'Ошибка', needs_selection:'Требуется выбор', seeding:'На раздаче', stopped:'Остановлено', replaced:'Заменено'};
const statusPill = status => `<span class="pill ${esc(status)}">${esc(statuses[status] || status)}</span>`;
const bytes = value => { if (!value) return '0 Б'; const units=['Б','КиБ','МиБ','ГиБ','ТиБ']; const power=Math.min(4,Math.floor(Math.log(value)/Math.log(1024))); return `${(value/1024**power).toFixed(power>1?1:0)} ${units[power]}`; };
const eta = value => value == null ? '—' : value<60 ? `${value} с` : value<3600 ? `${Math.ceil(value/60)} мин` : `${Math.floor(value/3600)} ч ${Math.ceil(value%3600/60)} мин`;
let toastTimer;
function toast(message, error=false) { const node=$('#toast'); node.textContent=message; node.className=`visible${error?' error':''}`; clearTimeout(toastTimer); toastTimer=setTimeout(()=>node.className='',5000); }
async function api(path, method='GET', payload) {
  const response=await fetch('/api/v1'+path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:payload===undefined?undefined:JSON.stringify(payload)});
  if (response.status===401 && !$('#login-form')) { location.assign('/login'); throw new Error('Войдите в аккаунт'); }
  const result=await response.json();
  if (!response.ok) { const detail=result.detail; throw new Error(Array.isArray(detail)?detail.map(e=>e.msg).join('; '):detail || 'Не удалось выполнить запрос'); }
  return result;
}
async function submitForm(form, callback) { const button=$('button[type="submit"]',form); const error=$('.form-error',form); if(error)error.textContent=''; if(button)button.disabled=true; try { await callback(); } catch(exc) { if(error)error.textContent=exc.message; else toast(exc.message,true); } finally { if(button)button.disabled=false; } }
function openModal(title, content) { $('#modal-title').textContent=title; $('#modal-body').innerHTML=content; if(!$('#modal').open)$('#modal').showModal(); }
function requirementFields(values, prefix='') {
  const resolutions=[480,576,720,1080,1440,2160,4320];
  const options=selected=>resolutions.map(r=>`<option value="${r}" ${r===selected?'selected':''}>${r}p</option>`).join('');
  return `<div class="form-grid">${languagePicker(prefix+'audio_languages','Языки аудио',values.audio_languages,'Все указанные языки обязательны. Пусто — без ограничений.')}${languagePicker(prefix+'subtitle_languages','Языки субтитров',values.subtitle_languages,'Отсутствие субтитров не блокирует загрузку.')}<label>Минимальное качество<select name="${prefix}min_resolution">${options(values.min_resolution)}</select></label><label>Максимальное качество<select name="${prefix}max_resolution">${options(values.max_resolution)}</select></label></div><label>Обязательная фраза<input name="${prefix}keyword" value="${esc(values.keyword)}" placeholder="Например, название озвучки или релиз-группы" maxlength="200"></label>`;
}
function readRequirements(form,prefix='') { const data=new FormData(form); const langs=name=>selectedLanguages(form,prefix+name); return {audio_languages:langs('audio_languages'),subtitle_languages:langs('subtitle_languages'),min_resolution:Number(data.get(prefix+'min_resolution')),max_resolution:Number(data.get(prefix+'max_resolution')),keyword:String(data.get(prefix+'keyword')||'')}; }
function episodeList(value) { if(!value.trim())return null; const result=new Set(); for(const part of value.split(',')) { const match=part.trim().match(/^(\d+)(?:\s*[-–]\s*(\d+))?$/); if(!match)throw new Error('Серии: используйте формат 1, 2, 5–8'); const first=Number(match[1]),last=Number(match[2]||match[1]); if(last<first||last-first>1000)throw new Error('Некорректный диапазон серий'); for(let i=first;i<=last;i++)result.add(i); } return [...result]; }
function seasonOptions(item) {
  const groups=new Map();
  for(const [key,aliases] of Object.entries(item.episode_numbering||{}))for(const alias of aliases){
    if(!groups.has(alias.season))groups.set(alias.season,{episodes:new Set(),canonical:new Set()});
    groups.get(alias.season).episodes.add(alias.episode);groups.get(alias.season).canonical.add(key.split(':')[0]);
  }
  const alternatives=[...groups].filter(([n,g])=>n>0&&g.canonical.size===1).sort((a,b)=>a[0]-b[0]);
  const regular=(item.seasons||[]).map(s=>`<option value="${s.number}">${esc(s.title)} · ${s.episode_count} серий</option>`).join('');
  if(!alternatives.length)return regular;
  return '<optgroup label="По сезонам">'+alternatives.map(([n,g])=>`<option value="alt:${n}">Сезон ${n} · ${g.episodes.size} серий</option>`).join('')+'</optgroup><optgroup label="Нумерация TMDB">'+regular+'</optgroup>';
}
function addTaskSeason(preferredSeason=null) {
  const selected=new Set($$('[data-task-season] select').map(node=>node.value));
  const row=document.createElement('div');row.className='task-season-row';row.dataset.taskSeason='';
  row.innerHTML=`<label>Сезон<select name="season">${seasonOptions(state.selected)}</select></label><label>Серии<input name="episodes" placeholder="Все серии сезона"><small>Пусто — все; либо 1, 2, 5–8</small></label><button type="button" class="ghost" data-remove-task-season aria-label="Убрать сезон из задачи">Убрать</button>`;
  const select=$('select',row),options=[...select.options];
  const available=options.find(option=>!selected.has(option.value)&&option.value!=='0')||options.find(option=>!selected.has(option.value));
  if(!available)return;
  select.value=preferredSeason!==null?String(preferredSeason):available.value;
  $('#task-seasons').append(row);updateTaskSeasonControls();
}
function updateTaskSeasonControls() {
  const rows=$$('[data-task-season]');
  rows.forEach((row,index)=>{ $('select',row).id=index===0?'season-select':'';$('[data-remove-task-season]',row).disabled=rows.length===1; });
  const selects=rows.map(row=>$('select',row));
  const chosen=new Set(selects.map(select=>select.value));
  for(const select of selects)for(const option of select.options)option.disabled=option.value!==select.value&&chosen.has(option.value);
  $('#add-task-season').disabled=!selects.length||chosen.size>=selects[0].options.length;
}
function taskSeasonSelections() {
  const values=$$('[data-task-season]').map(row=>{
    const value=$('select',row).value;
    if(!value)throw new Error('Выберите сезон');
    return {season:Number(value.replace('alt:','')),episodes:episodeList($('input',row).value),...(value.startsWith('alt:')?{numbering_season:Number(value.slice(4))}:{})};
  });
  if(!values.length)throw new Error('Выберите хотя бы один сезон');
  return values;
}
document.addEventListener('change',event=>{if(event.target.matches('[data-task-season] select'))updateTaskSeasonControls();});
function showTaskForm(item,preferredSeason=null) { state.selected=item; $('#selection-title').textContent=item.title+(item.year?` · ${item.year}`:''); $('#selection-overview').textContent=item.overview; $('#task-requirements').innerHTML=requirementFields(state.settings.defaults); $('#season-fields').hidden=item.kind!=='tv'; $('#task-seasons').innerHTML='';if(item.kind==='tv')addTaskSeason(preferredSeason); $('#selection').hidden=false; $('#selection').scrollIntoView({behavior:'smooth',block:'center'}); }
async function selectMedia(button) { const generation=++state.generation; button.disabled=true; try { const item=await api(`/metadata/${encodeURIComponent(button.dataset.provider)}/${button.dataset.kind}/${encodeURIComponent(button.dataset.mediaId)}`); if(generation!==state.generation)return;showTaskForm(item); } finally { button.disabled=false; } }
function partRows(task) { return task.subtasks.map(part=>`<div class="part-row"><span class="part-number">${part.episode==null?'ФИЛЬМ':`${part.season!=null?'S'+String(part.season).padStart(2,'0'):''}E${String(part.episode).padStart(2,'0')}`}</span><div class="part-title">${esc(part.title)}${part.missing_subtitle_languages.length?`<small>Нет субтитров: ${esc(languageList(part.missing_subtitle_languages))}</small>`:''}${part.error?`<small>${esc(part.error)}</small>`:''}</div>${statusPill(part.status)}<button class="ghost" data-candidates="${part.id}">Раздачи</button></div>`).join(''); }
function chooseAllButton(task) { return task.subtasks.length>1&&task.subtasks.some(part=>part.status==='needs_selection')?`<button class="ghost" data-task-candidates="${task.id}">Выбрать для всех серий</button>`:''; }
async function refreshTasks() {
  state.tasks=await api('/tasks'); $('#task-count').textContent=state.tasks.length;
  $('#task-list').innerHTML=state.tasks.length?state.tasks.map(task=>`<article class="task-card"><div class="task-main">${task.poster?`<img class="task-poster" src="${esc(posterUrl(task.poster))}" alt="">`:'<span class="task-poster"></span>'}<div class="task-info"><h3>${esc(task.title)}</h3><div class="task-meta"><span>${task.year||'—'}</span><span>${(task.seasons||[]).length?`Сезоны ${esc(task.seasons.map(s=>s.season).join(', '))}`:task.season!=null?`Сезон ${task.season}`:'Фильм'}</span><span>${task.subtasks.filter(s=>s.status==='done').length}/${task.subtasks.length} готово</span>${task.completed?'<span>Завершена</span>':''}</div></div><div class="task-actions"><button class="primary" data-library-media="${task.media_id}">Открыть</button></div></div></article>`).join(''):'<div class="empty"><p>Нет задач. Найдите произведение, чтобы добавить задачу.</p></div>';
}
async function switchTab(tab) {
  state.tab=tab;
  $$('.tab-panel').forEach(p=>p.hidden=p.id!==`tab-${tab}`);
  $$('.nav-tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  if(tab==='settings'){
    await Promise.all([loadSettings(),loadProviders(),loadAccounts(),loadTelegram()]);
    const challenge=state.providers.find(p=>p.kind==='content'&&p.enabled&&p.auth_methods?.includes('captcha')&&p.error?.startsWith('auth_required:')&&p.error.includes('CAPTCHA'));
    if(challenge)await providerAuth(challenge.id);
  }
  if(tab==='library'){await refreshDownloads();if(libraryMedia)await openLibraryMedia(libraryMedia);else await loadLibraries();}
}
function renderSearchActivity(status) {
  const search=status.search||{};state.search=search;
  const visible=search.state!=='idle'||search.pending_requests;
  const cooldown=Boolean(search.pending_requests&&(search.next_attempt_at>Date.now()/1000||(search.providers||[]).some(provider=>provider.state==='cooldown')));
  $('#search-activity').hidden=!visible;
  $('#run-queue').disabled=Boolean(search.running||(search.pending_requests&&!cooldown)||!status.engine_available||!status.content_providers.length);
  $('#run-queue').textContent=search.running?'Поиск выполняется':cooldown?'Сбросить паузу и запустить поиск':search.pending_requests?'Все задачи поставлены в очередь':'Запустить поиск по всем задачам';
  $('#search-state').textContent=search.running?'Поиск раздач':({queued:'Ожидание поиска',blocked:'Поиск ожидает настройки',error:'Поиск не выполнен',finished:'Поиск завершён'}[search.state]||'Поиск');
  $('#search-activity').classList.toggle('is-searching',Boolean(search.running));
  const total=search.groups_total||0, done=search.groups_done||0;
  $('#search-counts').textContent=total?`Группы запросов: ${done} / ${total} · Страниц: ${search.pages_checked||0} · Найдено: ${search.candidates_found||0} · Проверено: ${search.candidates_checked||0} · Отсеяно: ${search.candidates_filtered||0} · Отложено: ${search.candidates_deferred||0} · Ошибки проверки: ${search.candidates_failed||0}`:'';
  const progress=$('#search-progress');
  progress.classList.toggle('indeterminate',Boolean(search.running&&!total));
  const pages=search.group_pages_checked||0;
  const completed=total?Math.round(1000*(done+(search.running?0.9*pages/(pages+9):0))/total)/10:search.state==='finished'?100:0;
  progress.setAttribute('aria-valuemin','0');progress.setAttribute('aria-valuemax','100');
  if(search.running&&!total)progress.removeAttribute('aria-valuenow');else progress.setAttribute('aria-valuenow',String(completed));
  $('span',progress).style.width=`${completed}%`;
  $('#search-current').textContent=(search.message||'')+(search.next_attempt_at>Date.now()/1000?` · Не раньше ${new Date(search.next_attempt_at*1000).toLocaleTimeString()}`:'');
  $('#search-query').textContent=[search.provider,search.media,search.season!=null?`Сезон ${search.season}`:'',search.episodes?.length?`Серии: ${search.episodes.join(', ')}`:''].filter(Boolean).join(' · ');
  const providerStates={disabled:'Пропущен',skipped:'Пропущен',cooldown:'Временная пауза',waiting:'Ожидает',searching:'Поиск',checking:'Проверка кандидатов',completed:'Готово',error:'Ошибка'};
  $('#search-providers').innerHTML=(search.providers||[]).map(p=>`<div class="search-provider"><div><strong>${esc(p.name)}</strong><span class="fine">${esc(providerStates[p.state]||p.state)}</span></div><p>${esc(p.reason)}${p.retry_at&&['cooldown','error'].includes(p.state)?` · Повтор не раньше ${new Date(p.retry_at*1000).toLocaleTimeString()}`:''}</p>${p.requests?`<small>Получено страниц: ${p.requests} · Кандидатов: ${p.candidates}</small>`:''}</div>`).join('');
  const history=JSON.stringify(search.history||[]);
  if(state.searchHistory!==history){state.searchHistory=history;$('#search-history').innerHTML=(search.history||[]).map(item=>`<li><time>${new Date(item.time*1000).toLocaleTimeString()}</time><span>${esc(item.message)}</span></li>`).join('');}
}
async function refreshDownloads() { const [downloads,status]=await Promise.all([api('/downloads'),api('/status')]); state.downloads=downloads;renderSearchActivity(status); $('#engine-notice').innerHTML=status.engine_error?`<div class="notice">${esc(status.engine_error)}</div>`:!status.content_providers.length?'<div class="notice">Включите провайдеры контента в настройках.</div>':''; }
async function loadSettings() { state.settings=await api('/settings'); applyThemeColor(state.settings.theme_color); const form=$('#settings-form'); $('#settings-requirements').innerHTML=requirementFields(state.settings.defaults,'default_'); $('#settings-jellyfin').innerHTML=languagePicker('jellyfin_audio_languages','Приоритет аудио',state.settings.jellyfin.audio_languages)+languagePicker('jellyfin_subtitle_languages','Приоритет субтитров',state.settings.jellyfin.subtitle_languages); for(const [key,value] of Object.entries(state.settings)){const field=form.elements[key];if(key==='defaults'||key==='jellyfin'||!field)continue;if(field.type==='checkbox')field.checked=Boolean(value);else field.value=value??'';} }
async function reorderProvider(source,target,after) {
  const ids=state.providers.filter(p=>p.kind==='content').map(p=>p.id),original=[...ids];
  if(source===target||!ids.includes(source)||!ids.includes(target))return;
  ids.splice(ids.indexOf(source),1);
  ids.splice(ids.indexOf(target)+(after?1:0),0,source);
  if(ids.every((id,index)=>id===original[index]))return;
  try {await api('/providers/order','PUT',{ids});toast('Порядок поиска сохранён');}
  finally {await loadProviders();}
}

function providerField(provider,field) {
  const configured=field.secret&&provider.configured_secrets.includes(field.name);
  const input=`<input name="${esc(field.name)}" type="${field.secret?'password':'text'}" value="${field.secret?'':esc(provider.config[field.name]??field.default)}" placeholder="${configured?'***':''}" autocomplete="off">`;
  if(!field.secret)return `<label>${esc(field.label)}${input}</label>`;
  return `<label>${esc(field.label)}<span class="secret-field">${input}<button type="button" class="secret-toggle" data-provider-secret="${esc(provider.id)}" data-secret-field="${esc(field.name)}" aria-label="Показать ${esc(field.label)}" aria-pressed="false" title="Показать">${secretEyeIcon()}</button></span></label>`;
}
function secretEyeIcon() { return '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6Z"/><circle cx="12" cy="12" r="3"/></svg>'; }
function providerCard(provider) {
  const content=provider.kind==='content',bodyId=`provider-body-${provider.id}`;
  const heading=`<div><h3>${esc(provider.name)}</h3><span class="fine">${esc(provider.version||'—')}</span></div>`;
  const switchControl=content?`<label class="provider-switch"><input type="checkbox" role="switch" name="enabled" aria-label="Включить ${esc(provider.name)}" ${provider.enabled?'checked':''}><span>Включён</span></label>`:`<label class="check"><input type="checkbox" name="enabled" ${provider.enabled?'checked':''}>Включён</label>`;
  const header=content?`<div class="provider-header"><span class="provider-drag-handle" data-provider-drag="${esc(provider.id)}" draggable="true" role="button" tabindex="0" aria-label="Переместить ${esc(provider.name)}. Используйте стрелки вверх и вниз" title="Перетащить для изменения порядка" aria-grabbed="false">⋮⋮</span><button type="button" class="provider-expand" data-provider-expand aria-expanded="false" aria-controls="${esc(bodyId)}">${heading}<span class="provider-chevron" aria-hidden="true">⌄</span></button>${switchControl}</div>`:`<div class="provider-header">${heading}<div class="provider-controls"><button class="icon-button revert" type="button" data-provider-revert="${esc(provider.id)}" title="Revert — значения по умолчанию" aria-label="Revert: ${esc(provider.name)}">↶</button>${switchControl}</div></div>`;
  const revert=content?`<button class="icon-button revert" type="button" data-provider-revert="${esc(provider.id)}" title="Revert — значения по умолчанию" aria-label="Revert: ${esc(provider.name)}">↶</button>`:'';
  return `<form class="panel provider-form${content?' provider-accordion':''}" data-provider-form="${esc(provider.id)}">${header}<div id="${esc(bodyId)}" class="provider-body" ${content?'hidden':''}><div class="stack">${provider.config_fields.map(field=>providerField(provider,field)).join('')}</div>${provider.error?`<p class="form-error">${esc(provider.error)}</p>`:''}<div class="provider-actions">${revert}<button class="primary" type="submit">Сохранить</button><button class="ghost" type="button" data-provider-health="${esc(provider.id)}">Проверить</button></div></div></form>`;
}
async function loadProviders() { const providers=await api('/providers');state.providers=providers; $('#provider-list').innerHTML=[['metadata','Провайдеры метаинформации'],['content','Провайдеры контента']].map(([kind,title])=>`<section class="provider-group provider-group-${kind}"><h3>${title}</h3>${kind==='content'?'<p class="fine provider-order-hint">Перетащите карточки, чтобы изменить порядок поиска. Изменения сохраняются сразу.</p>':''}<div class="provider-grid">${providers.filter(p=>p.kind===kind).map(providerCard).join('')}</div></section>`).join('')+providers.filter(p=>!['metadata','content'].includes(p.kind)).map(p=>`<div class="notice">${esc(p.name)}: ${esc(p.error||'Провайдер недоступен')}</div>`).join(''); }

async function providerAuth(identity,values={}) { const result=await api(`/providers/${identity}/authenticate`,'POST',{values}); if(result.status==='challenge') { openModal('CAPTCHA провайдера',`<p class="muted">${esc(result.message)}</p>${result.image_url?`<img src="${esc(result.image_url)}" alt="CAPTCHA">`:''}<form id="challenge-form" data-provider="${esc(identity)}" class="stack">${result.fields.map(field=>`<label>${esc(field.label)}<input name="${esc(field.name)}" required autocomplete="off"></label>`).join('')}<button class="primary" type="submit">Продолжить</button><p class="form-error"></p></form>`); } else if(result.status==='authenticated'||result.status==='anonymous'){if($('#modal').open)$('#modal').close();toast('Авторизация выполнена');await loadProviders();}else{throw new Error(result.message||'Требуются учётные данные');} }
async function loadAccounts() { const accounts=await api('/accounts'); $('#account-list').innerHTML=accounts.map(account=>`<div class="account-row"><span class="avatar">${esc(account.username[0].toUpperCase())}</span><strong>${esc(account.username)} <span class="fine">/ ${esc(account.role)}</span></strong><span class="fine">${account.active?'Активен':'Отключён'}</span><button class="ghost" data-password="${account.id}">Пароль</button><button class="ghost" data-account="${account.id}" data-active="${account.active}">${account.active?'Отключить':'Включить'}</button></div>`).join(''); }
function candidateSearchLog(search={},open=false) {
  const history=search.history||[];
  return `<details class="candidate-search-log" ${open?'open':''}><summary>Лог поиска · <span data-search-log-count>${history.length}</span></summary><p class="fine" role="status" data-search-log-status>${esc(search.message||'Поиск ещё не запускался')}</p><ol data-search-log-items>${history.length?history.map(item=>`<li><time>${new Date(item.time*1000).toLocaleTimeString()}</time><span>${esc(item.message)}</span></li>`).join(''):'<li class="muted">Событий пока нет.</li>'}</ol></details>`;
}
function updateCandidateSearchLog(search={}) {
  const root=$('#modal[open] [data-candidate-dialog]');if(!root)return;
  const history=search.history||[];
  $('[data-search-log-count]',root).textContent=history.length;
  $('[data-search-log-status]',root).textContent=search.message||'Поиск ещё не запускался';
  $('[data-search-log-items]',root).innerHTML=history.length?history.map(item=>`<li><time>${new Date(item.time*1000).toLocaleTimeString()}</time><span>${esc(item.message)}</span></li>`).join(''):'<li class="muted">Событий пока нет.</li>';
}
async function refreshCandidateSearchLog() {
  const root=$('#modal[open] [data-candidate-dialog]');if(!root)return false;
  updateCandidateSearchLog(await api(`/subtasks/${root.dataset.candidateDialog}/search`));
  return true;
}
function manualCandidateForm({subtask=null,task=null,season=null}) {
  const scope=subtask!==null?`data-subtask="${subtask}"`:season!==null?`data-task="${task}" data-season="${season}"`:`data-task="${task}"`;
  return `<form id="manual-candidate-form" ${scope} class="inline-form manual-candidate-form"><label>URL раздачи<input name="url" type="url" required maxlength="2048" placeholder="https://…"></label><button class="primary" type="submit">Добавить по URL</button><p class="form-error"></p></form>`;
}
async function candidateDialog(subtaskId) {
  const logOpen=$('.candidate-search-log')?.open??false;
  const [choices,search]=await Promise.all([api(`/subtasks/${subtaskId}/candidates`),api(`/subtasks/${subtaskId}/search`)]);
  const cards=choices.length?choices.map(choice=>`<article class="candidate"><h3><a href="${esc(choice.candidate.url)}" target="_blank" rel="noopener noreferrer">${esc(choice.candidate.title)} ↗</a></h3><p class="fine">${esc(choice.candidate.provider)} · ${bytes(choice.candidate.size)} · ${choice.candidate.seeds??'—'} сидов${choice.action==='rejected'?' · Отклонено':''}</p>${choice.used_in_season?.length?`<p class="fine"><strong>Используется в других сериях этого сезона</strong> · Серии: ${esc(choice.used_in_season.join(', '))}</p>${choice.episode_missing?'<p class="fine">В этой раздаче пока нет выбранной серии (по последней проверке файлов).</p>':''}`:''}${choice.report.criteria.map(c=>`<div class="criterion"><span class="${esc(c.result)}">${c.result==='MATCH'?'✓':c.result==='MISMATCH'?'×':'?'}</span><span>${esc(c.reason)}${c.required?'':' <span class="fine">(не блокирует)</span>'}</span></div>`).join('')}<div class="candidate-footer"><button class="primary" data-choose="${choice.id}" data-has-binding="${Boolean(choice.report.binding)}">Выбрать раздачу</button><button class="ghost" data-map-files="${choice.id}">Сопоставить файлы</button><button class="ghost" data-reject="${choice.id}">Отклонить</button></div></article>`).join(''):'<div class="empty compact"><h3>Пока нет кандидатов</h3><p>Результаты появятся после поиска.</p></div>';
  openModal('Раздачи и результаты проверки',`<div data-candidate-dialog="${subtaskId}"><p class="fine">Замена удалит прежние файлы серии, если они не нужны другим сериям.</p>${manualCandidateForm({subtask:subtaskId})}<button class="ghost" data-search-alternatives="${subtaskId}">Найти другие раздачи</button>${candidateSearchLog(search,logOpen)}${cards}</div>`);
}
async function taskCandidateDialog(taskId) {
  const choices=await api(`/tasks/${taskId}/candidates`);
  const cards=choices.length?choices.map(choice=>`<article class="candidate"><h3><a href="${esc(choice.candidate.url)}" target="_blank" rel="noopener noreferrer">${esc(choice.candidate.title)} ↗</a></h3><p class="fine">${esc(choice.candidate.provider)} · ${bytes(choice.candidate.size)} · ${choice.candidate.seeds??'—'} сидов</p><p>Сопоставлено по результатам проверки: <strong>${choice.matched} из ${choice.total}</strong></p><div class="candidate-footer"><button class="primary" data-choose-all="${choice.id}" ${choice.matched?'':'disabled'}>${choice.matched?'Выбрать для всех серий':'Не подходит ни одной серии'}</button></div></article>`).join(''):'<div class="empty compact"><h3>Пока нет общих кандидатов</h3><p>Запустите поиск Worker или выбирайте раздачи отдельно для серий.</p></div>';
  openModal('Раздача для всех серий',`<p class="muted small">Worker отдельно сопоставит каждый видеофайл с серией и добавит в загрузку только подходящие файлы.</p>${manualCandidateForm({task:taskId})}${cards}`);
}
async function seasonCandidateDialog(taskId,season) {
  const choices=await api(`/tasks/${taskId}/seasons/${season}/candidates`);
  const cards=choices.length?choices.map(choice=>`<article class="candidate"><h3><a href="${esc(choice.candidate.url)}" target="_blank" rel="noopener noreferrer">${esc(choice.candidate.title)} ↗</a></h3><p class="fine">${esc(choice.candidate.provider)} · ${bytes(choice.candidate.size)} · ${choice.candidate.seeds??'—'} сидов</p><p>Сопоставлено в этом сезоне: <strong>${choice.matched} из ${choice.total}</strong></p><div class="candidate-footer"><button class="primary" data-choose-season="${choice.id}" data-season-task="${taskId}" data-season-number="${season}" ${choice.matched?'':'disabled'}>${choice.matched?'Выбрать для сезона':'Не подходит ни одной серии'}</button></div></article>`).join(''):'<div class="empty compact"><h3>Пока нет кандидатов для сезона</h3><p>Запустите поиск задачи или выберите раздачу отдельно для серии.</p></div>';
  openModal(`Раздача для сезона ${season}`,`<p class="muted small">Раздача будет применена только к сериям сезона ${season}. Другие сезоны задачи не изменятся.</p>${manualCandidateForm({task:taskId,season})}${cards}`);
}
function mappingFileOrder(a,b) {
  const left=a.episode_order,right=b.episode_order;
  if(left&&right){const difference=left[0]-right[0]||left[1]-right[1];if(difference)return difference;}
  else if(left||right)return left?-1:1;
  return a.path.localeCompare(b.path,'ru',{numeric:true,sensitivity:'base'})||a.index-b.index;
}
async function mappingDialog(identity) { const [files,selection]=await Promise.all([api(`/candidates/${identity}/files`),api(`/candidates/${identity}/selection`)]); files.sort(mappingFileOrder); const videos=files.filter(f=>/\.(mkv|mp4|avi|m4v|ts|m2ts|webm|mov|mpg|mpeg)$/i.test(f.path)); const tracks=files.filter(f=>/\.(mka|ac3|eac3|aac|flac|dts|mp3|ogg|wav|m4a|srt|ass|ssa|vtt|sub|idx|sup)$/i.test(f.path)); openModal('Ручное сопоставление',`<p class="muted small">Укажите видео этой серии и относящиеся к нему внешние дорожки. Прежние файлы удаляются, если они не нужны другим сериям. Выбор сохраняет исключение для этой раздачи; фактические противоречия после загрузки всё равно будут показаны.</p><form id="mapping-form" data-choice="${identity}"><label>Видео<select name="video_index" required><option value="">Выберите файл</option>${videos.map(f=>`<option value="${f.index}" ${selection?.video_index===f.index?"selected":""}>${esc(f.path)}</option>`).join('')}</select></label><h3 style="margin-top:20px">Внешние дорожки</h3>${tracks.map(f=>`<div class="file-choice"><input type="checkbox" name="track" value="${f.index}" id="file-${f.index}" ${selection?.tracks?.some(t=>t.file_index===f.index)?"checked":""}><label for="file-${f.index}">${esc(f.path)} <span class="fine">${bytes(f.size)}</span></label></div>`).join('')||'<p class="muted">Отдельных файлов дорожек нет.</p>'}<div class="form-footer"><p class="form-error"></p><button class="primary" type="submit">Применить выбор</button></div></form>`); }
document.addEventListener('change',async event=>{
  const field=event.target;
  if(!field.matches('#mapping-form select[name="video_index"]'))return;
  const form=field.form,value=field.value,generation=(Number(form.dataset.mappingGeneration)||0)+1;
  form.dataset.mappingGeneration=String(generation);
  const button=$('button[type="submit"]',form),error=$('.form-error',form);
  $$('input[name="track"]',form).forEach(input=>{input.checked=false;input.disabled=true;});
  button.disabled=true;error.textContent='';
  const current=()=>form.isConnected&&Number(form.dataset.mappingGeneration)===generation;
  try{
    const binding=value===''?null:await api(`/candidates/${form.dataset.choice}/selection?video_index=${encodeURIComponent(value)}`);
    if(!current())return;
    const selected=new Set((binding?.tracks||[]).map(track=>track.file_index));
    $$('input[name="track"]',form).forEach(input=>{input.checked=selected.has(Number(input.value));input.disabled=false;});
    button.disabled=value==='';
  }catch(exc){if(current())error.textContent='Не удалось загрузить привязку: '+exc.message;}
});

let telegramUsers=[];
function telegramRows(status){
  const users=telegramUsers.filter(user=>user.status===status);
  return users.length?users.map(user=>`<div class="telegram-user"><div><strong>${esc(user.name||'Пользователь')}</strong><p class="fine">${user.username?'@'+esc(user.username)+' · ':''}ID ${esc(user.user_id)}</p>${user.delivery_error?`<p class="form-error">${esc(user.delivery_error)}</p>`:user.reply_pending?'<p class="fine">Ответ ожидает отправки.</p>':''}</div><div class="telegram-actions">${status!=='approved'?`<button type="button" class="primary" data-telegram-user="${user.id}" data-status="approved">${status==='blocked'?'Разрешить доступ':'Принять'}</button>`:''}${status!=='blocked'?`<button type="button" class="ghost" data-telegram-user="${user.id}" data-status="blocked">${status==='pending'?'Отклонить':'Заблокировать'}</button>`:''}</div></div>`).join(''):'<p class="fine">Нет пользователей.</p>';
}
async function loadTelegram(editForm=true){
  const [cfg,users]=await Promise.all([api('/telegram'),api('/telegram/users')]);telegramUsers=users;
  if(editForm){$('#telegram-form input').placeholder=cfg.token_configured?'Токен сохранён':'Токен от BotFather';}
  $('#telegram-status').textContent=(cfg.enabled?'Бот включён':'Бот выключен')+(cfg.bot_username?' · @'+cfg.bot_username:'')+(cfg.error?' · '+cfg.error:'');
  $('#telegram-pending').innerHTML=telegramRows('pending');$('#telegram-approved').innerHTML=telegramRows('approved');
  $('#telegram-blocked').textContent=`Заблокированные пользователи (${users.filter(u=>u.status==='blocked').length})`;
  if($('#telegram-blocked-list'))$('#telegram-blocked-list').innerHTML=telegramRows('blocked');
}
if($('#telegram-panel'))setInterval(()=>{if(!document.hidden&&state.tab==='settings')loadTelegram(false).catch(()=>{});},5000);
let telegramSaveTimer,telegramSaving=false;
async function saveTelegramToken(){
  const input=$('#telegram-form input'),error=$('#telegram-form .form-error');
  if(telegramSaving||!input?.value.trim())return;
  const value=input.value.trim();telegramSaving=true;error.textContent='';
  $('#telegram-status').textContent='Проверяем токен…';
  try{await api('/telegram','PUT',{token:value});if(input.value.trim()===value)input.value='';await loadTelegram();toast('Бот подключён');}
  catch(exc){error.textContent=exc.message;}
  finally{telegramSaving=false;if(input.value.trim()&&input.value.trim()!==value)saveTelegramToken();}
}
if($('#telegram-form input')){
  $('#telegram-form input').addEventListener('input',()=>{clearTimeout(telegramSaveTimer);telegramSaveTimer=setTimeout(saveTelegramToken,1000);});
  $('#telegram-form input').addEventListener('change',()=>{clearTimeout(telegramSaveTimer);saveTelegramToken();});
  $('#telegram-form input').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();clearTimeout(telegramSaveTimer);saveTelegramToken();}});
}

// Event delegation keeps dynamically rendered forms free of inline JavaScript.
let draggedProviderId=null;
function clearProviderDrag() {
  draggedProviderId=null;
  for(const form of $$('.provider-accordion'))form.classList.remove('is-dragging','drop-before','drop-after');
  for(const handle of $$('[data-provider-drag]'))handle.setAttribute('aria-grabbed','false');
}
document.addEventListener('dragstart',event=>{
  const handle=event.target.closest?.('[data-provider-drag]');
  if(!handle)return;
  draggedProviderId=handle.dataset.providerDrag;
  event.dataTransfer?.setData('text/plain',draggedProviderId);
  if(event.dataTransfer)event.dataTransfer.effectAllowed='move';
  handle.setAttribute('aria-grabbed','true');
  handle.closest('.provider-accordion').classList.add('is-dragging');
});
document.addEventListener('dragover',event=>{
  const form=event.target.closest?.('.provider-accordion');
  if(!draggedProviderId||!form||form.dataset.providerForm===draggedProviderId)return;
  event.preventDefault();
  for(const card of $$('.provider-accordion'))card.classList.remove('drop-before','drop-after');
  form.classList.add(event.clientY>form.getBoundingClientRect().top+form.getBoundingClientRect().height/2?'drop-after':'drop-before');
});
document.addEventListener('drop',async event=>{
  const form=event.target.closest?.('.provider-accordion'),source=draggedProviderId;
  if(!source||!form||form.dataset.providerForm===source)return;
  event.preventDefault();
  const after=event.clientY>form.getBoundingClientRect().top+form.getBoundingClientRect().height/2;
  clearProviderDrag();
  try{await reorderProvider(source,form.dataset.providerForm,after);}catch(exc){toast(exc.message,true);}
});
document.addEventListener('dragend',clearProviderDrag);
document.addEventListener('keydown',async event=>{
  const handle=event.target.closest?.('[data-provider-drag]');
  if(!handle||!['ArrowUp','ArrowDown'].includes(event.key))return;
  event.preventDefault();
  const forms=$$('.provider-group-content .provider-accordion'),index=forms.indexOf(handle.closest('.provider-accordion'));
  const next=forms[index+(event.key==='ArrowUp'?-1:1)];
  if(!next)return;
  try{await reorderProvider(handle.dataset.providerDrag,next.dataset.providerForm,event.key==='ArrowDown');
    $$('[data-provider-drag]').find(item=>item.dataset.providerDrag===handle.dataset.providerDrag)?.focus();
  }catch(exc){toast(exc.message,true);}
});
document.addEventListener('input',event=>{
  const input=event.target;
  if(!(input instanceof HTMLInputElement)||!['username','password'].includes(input.name)||!input.value.trim())return;
  const form=input.closest('.provider-accordion');
  if(form)form.elements.enabled.checked=true;
});
document.addEventListener('change',async event=>{
  const toggle=event.target;
  if(!(toggle instanceof HTMLInputElement)||toggle.name!=='enabled')return;
  const form=toggle.closest('.provider-accordion');
  if(!form)return;
  toggle.disabled=true;
  try{
    await api(`/providers/${encodeURIComponent(form.dataset.providerForm)}`,'PUT',{enabled:toggle.checked,config:{}});
    const provider=state.providers.find(item=>item.id===form.dataset.providerForm);
    if(provider)provider.enabled=toggle.checked;
    toast('Провайдер сохранён');
  }catch(exc){toggle.checked=!toggle.checked;toast(exc.message,true);}
  finally{toggle.disabled=false;}
});
document.addEventListener('click', async event=>{ const button=event.target.closest('button'); if(!button)return; try {
  if(button.hasAttribute('data-provider-expand')){const body=button.closest('.provider-form').querySelector('.provider-body');body.hidden=!body.hidden;button.setAttribute('aria-expanded',String(!body.hidden));}
  else if(button.dataset.tab) { await switchTab(button.dataset.tab); }
  else if(button.id==='run-queue'){button.disabled=true;try{await api('/search/run','POST',{});await refreshDownloads();await refreshLibraryView();}finally{if(!state.search?.running)button.disabled=false;}}
  else if(button.id==='logout'){await api('/session','DELETE');location.assign('/login');}
  else if(button.id==='clear-selection'){state.generation++;state.selected=null;$('#selection').hidden=true;}
  else if(button.id==='add-task-season')addTaskSeason();
  else if(button.hasAttribute('data-remove-task-season')){button.closest('[data-task-season]').remove();updateTaskSeasonControls();}
  else if(button.id==='close-modal')$('#modal').close();
  else if(button.id==='telegram-blocked'){await loadTelegram(false);openModal('Заблокированные пользователи',`<div id="telegram-blocked-list">${telegramRows('blocked')}</div>`);}
  else if(button.dataset.telegramUser){button.disabled=true;try{await api(`/telegram/users/${button.dataset.telegramUser}`,'PATCH',{status:button.dataset.status});await loadTelegram(false);toast(button.dataset.status==='approved'?'Доступ разрешён. Ответ поставлен в очередь.':'Пользователь заблокирован');}finally{button.disabled=false;}}
  else if(button.dataset.mediaId)await selectMedia(button);
  else if(button.dataset.expand){const id=Number(button.dataset.expand);state.expanded.has(id)?state.expanded.delete(id):state.expanded.add(id);await refreshTasks();}
  else if(button.dataset.runTask){button.disabled=true;try{await api(`/tasks/${button.dataset.runTask}/search`,'POST',{});await refreshLibraryView();}finally{button.disabled=false;}}
  else if(button.dataset.pauseTask){await api(`/tasks/${button.dataset.pauseTask}`,'PATCH',{paused:button.dataset.paused!=='true'});await refreshTasks();await refreshLibraryView();}
  else if(button.dataset.deleteTask){const task=state.tasks.find(t=>t.id===Number(button.dataset.deleteTask));openModal('Удаление задачи',`<form id="delete-task-form" data-task="${task.id}" class="stack"><p>${esc(task.title)}</p><label class="check"><input type="checkbox" role="switch" name="delete_media">Удалить также медиа и связанные загрузки</label><p class="fine">Общие загрузки, нужные другим задачам, сохранятся. Удаление файлов необратимо.</p><div class="form-footer"><p class="form-error"></p><button type="submit" class="primary">Удалить задачу</button></div></form>`);}
  else if(button.dataset.editTask){const task=state.tasks.find(t=>t.id===Number(button.dataset.editTask));openModal('Требования задачи',`<form id="edit-task-form" data-task="${task.id}">${requirementFields(task.requirements)}<p class="fine">Новые требования применяются к ещё не скачанным сериям. Готовые файлы сохраняются.</p><div class="form-footer"><p class="form-error"></p><button class="primary" type="submit">Сохранить</button></div></form>`);}
  else if(button.dataset.searchAlternatives){button.disabled=true;button.textContent='Поиск раздач…';try{await api(`/subtasks/${button.dataset.searchAlternatives}/candidates/search`,'POST',{});await candidateDialog(Number(button.dataset.searchAlternatives));}finally{button.disabled=false;button.textContent='Найти другие раздачи';}}
  else if(button.dataset.candidates)await candidateDialog(Number(button.dataset.candidates));
  else if(button.dataset.taskCandidates)await taskCandidateDialog(Number(button.dataset.taskCandidates));
  else if(button.dataset.seasonCandidates)await seasonCandidateDialog(Number(button.dataset.seasonCandidates),Number(button.dataset.seasonNumber));
  else if(button.dataset.chooseSeason){button.disabled=true;const task=Number(button.dataset.seasonTask),season=Number(button.dataset.seasonNumber);const result=await api(`/tasks/${task}/seasons/${season}/candidates/${button.dataset.chooseSeason}/choice`,'POST',{});$('#modal').close();toast(result.skipped?`Раздача выбрана для ${result.selected} из ${result.total} серий сезона`:`Раздача выбрана для всех ${result.total} серий сезона`);await Promise.all([refreshTasks(),refreshDownloads()]);await refreshLibraryView();}
  else if(button.dataset.chooseAll){button.disabled=true;const result=await api(`/candidates/${button.dataset.chooseAll}/choice-all`,'POST',{});$('#modal').close();toast(result.skipped?`Раздача выбрана для ${result.selected} из ${result.total} серий; остальные требуют отдельного выбора`:`Раздача выбрана для всех ${result.total} серий`);await Promise.all([refreshTasks(),refreshDownloads()]);await refreshLibraryView();}
  else if(button.dataset.choose){if(button.dataset.hasBinding!=='true'){await mappingDialog(button.dataset.choose);}else{await api(`/candidates/${button.dataset.choose}/choice`,'POST',{});$('#modal').close();toast('Раздача выбрана');await refreshTasks();await refreshLibraryView();}}
  else if(button.dataset.mapFiles)await mappingDialog(button.dataset.mapFiles);
  else if(button.dataset.reject){await api(`/candidates/${button.dataset.reject}/choice`,'POST',{reject:true});button.closest('.candidate').remove();toast('Кандидат отклонён');await refreshLibraryView();}
  else if(button.dataset.download){await api(`/downloads/${button.dataset.download}/action`,'POST',{action:button.dataset.action});await refreshDownloads();if(libraryMedia)await openLibraryMedia(libraryMedia,true);else await loadLibraries(true);}
  else if(button.dataset.providerSecret){
    const input=button.parentElement.querySelector('input');
    if(input.type==='password'){
      if(!input.value&&input.placeholder==='***'){
        button.disabled=true;
        try{
          const result=await api(`/providers/${encodeURIComponent(button.dataset.providerSecret)}/secrets/${encodeURIComponent(button.dataset.secretField)}/reveal`,'POST',{});
          input.value=result.value;
        }finally{button.disabled=false;}
      }
      input.type='text';button.setAttribute('aria-pressed','true');button.setAttribute('aria-label','Скрыть секрет');button.title='Скрыть';
    }else{
      input.type='password';button.setAttribute('aria-pressed','false');button.setAttribute('aria-label','Показать секрет');button.title='Показать';
    }
  }
  else if(button.dataset.providerHealth){await api(`/providers/${button.dataset.providerHealth}/health`,'POST',{});toast('Провайдер доступен');}
  else if(button.dataset.providerRevert){const provider=state.providers.find(p=>p.id===button.dataset.providerRevert);const form=button.closest('form');for(const field of provider.config_fields){form.elements[field.name].value=field.default||'';if(field.secret){form.elements[field.name].dataset.clearSecret='true';form.elements[field.name].placeholder='';}}toast('Значения по умолчанию. Нажмите «Сохранить», чтобы применить.');}
  else if(button.dataset.account){await api(`/accounts/${button.dataset.account}`,'PATCH',{active:button.dataset.active!=='true'});await loadAccounts();}
  else if(button.dataset.password)openModal('Новый пароль',`<form id="password-form" data-account="${button.dataset.password}" class="stack"><label>Пароль<input name="password" type="password" required minlength="10" autocomplete="new-password"></label><button class="primary" type="submit">Сохранить</button><p class="form-error"></p></form>`);
} catch(exc){toast(exc.message,true);} });

document.addEventListener('submit', event=>{const form=event.target;if(!(form instanceof HTMLFormElement))return;event.preventDefault();submitForm(form,async()=>{
  const data=new FormData(form);
  if(form.id==='setup-form'){await api('/setup','POST',Object.fromEntries(data));location.assign('/#settings');}
  else if(form.id==='login-form'){await api('/session','POST',Object.fromEntries(data));location.assign('/');}
  else if(form.id==='task-form'){if(!state.selected)throw new Error('Выберите произведение');const payload={provider:state.selected.provider,media_id:state.selected.id,kind:state.selected.kind,requirements:readRequirements(form)};if(state.selected.kind==='tv')payload.seasons=taskSeasonSelections();await api('/tasks','POST',payload);$('#selection').hidden=true;$('#search-results').innerHTML='';$('#media-search').value='';state.selected=null;toast('Задача создана. Поиск поставлен в очередь.');await refreshTasks();await switchTab('library');}
  else if(form.id==='settings-form'){const payload={...state.settings,defaults:readRequirements(form,'default_'),jellyfin:{audio_languages:selectedLanguages(form,'jellyfin_audio_languages'),subtitle_languages:selectedLanguages(form,'jellyfin_subtitle_languages')}};for(const key of ['movie_path','series_path','search_start','plugin_repository'])payload[key]=String(data.get(key));payload.theme_color=String(data.get('theme_color')||'purple');payload.seed_ratio=String(data.get('seed_ratio')).trim()===''?null:Number(data.get('seed_ratio'));payload.prefer_full_subtitles=data.get('prefer_full_subtitles')==='on';await api('/settings','PUT',payload);state.settings=payload;toast('Настройки сохранены');}
  else if(form.id==='account-form'){await api('/accounts','POST',Object.fromEntries(data));form.reset();toast('Аккаунт создан');await loadAccounts();}
  else if(form.id==='delete-task-form'){const result=await api(`/tasks/${form.dataset.task}`,'DELETE',{delete_media:data.get('delete_media')==='on'});$('#modal').close();await refreshTasks();await refreshDownloads();await refreshLibraryView();toast(result.cleanup_pending?'Задача удалена. Очистка файлов будет повторена автоматически.':'Задача удалена');}
  else if(form.id==='edit-task-form'){await api(`/tasks/${form.dataset.task}`,'PATCH',{requirements:readRequirements(form)});$('#modal').close();await refreshTasks();await refreshLibraryView();toast('Требования обновлены');}
  else if(form.dataset.providerForm){const values=Object.fromEntries(data);delete values.enabled;for(const input of $$('input[data-clear-secret]',form))if(!input.value)values[input.name]=null;await api(`/providers/${form.dataset.providerForm}`,'PUT',{enabled:data.get('enabled')==='on',config:values});toast('Провайдер сохранён');await loadProviders();}
  else if(form.id==='manual-candidate-form'){const subtask=form.dataset.subtask?Number(form.dataset.subtask):null,task=form.dataset.task?Number(form.dataset.task):null,season=form.dataset.season?Number(form.dataset.season):null;const path=subtask!==null?`/subtasks/${subtask}/candidates/manual`:season!==null?`/tasks/${task}/seasons/${season}/candidates/manual`:`/tasks/${task}/candidates/manual`;await api(path,'POST',{url:String(data.get('url')).trim()});toast('Раздача добавлена в список');if(subtask!==null)await candidateDialog(subtask);else if(season!==null)await seasonCandidateDialog(task,season);else await taskCandidateDialog(task);}
  else if(form.id==='mapping-form'){await api(`/candidates/${form.dataset.choice}/choice`,'POST',{video_index:Number(data.get('video_index')),track_indices:data.getAll('track').map(Number)});$('#modal').close();toast('Раздача выбрана');await refreshTasks();await refreshLibraryView();}
  else if(form.id==='challenge-form')await providerAuth(form.dataset.provider,Object.fromEntries(data));
  else if(form.id==='password-form'){await api(`/accounts/${form.dataset.account}`,'PATCH',{password:String(data.get('password'))});$('#modal').close();toast('Пароль изменён; прежние сессии завершены');}
});});
document.addEventListener('htmx:configRequest',event=>{event.detail.headers['X-CSRF-Token']=csrf();});
document.addEventListener('htmx:responseError',event=>{if(event.detail.xhr.status===401)location.assign('/login');else toast('Не удалось выполнить поиск',true);});
document.addEventListener('keydown',event=>{if(event.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)&&$('#media-search')){event.preventDefault();$('[data-tab="search"]').click();$('#media-search').focus();}});
if($('#task-list')){Promise.all([loadSettings(),refreshTasks(),refreshDownloads()]).catch(exc=>toast(exc.message,true));setInterval(()=>{if(document.hidden||$('#modal').open)return;refreshTasks().catch(()=>{});},10000);setInterval(async()=>{if(document.hidden)return;try{if($('#modal').open){await refreshCandidateSearchLog();return;}if(state.tab!=='library')return;await refreshDownloads();if(libraryMedia)await openLibraryMedia(libraryMedia,true);else await loadLibraries(true);}catch{}},3000);}

document.addEventListener('error',event=>{if(event.target instanceof HTMLImageElement){event.target.classList.add('failed');event.target.alt='Обложка недоступна';}},true);
if(location.hash==='#settings'&&$('[data-tab="settings"]'))$('[data-tab="settings"]').click();
