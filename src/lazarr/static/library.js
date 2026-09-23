let librarySelection='series',libraryMedia=null,libraryItem=null,libraryGeneration=0,libraryDetailUpdated=0,libraryRequests=0;
async function readLibrary(path){libraryRequests++;try{return await api(path);}finally{libraryRequests--;}}
const loadedLibrarySeasons=new Set(),loadingLibrarySeasons=new Set();
const libraryDate=value=>value?new Date(value.length===10?value+'T12:00:00':value).toLocaleDateString('ru-RU'):'Дата неизвестна';
const searchDate=value=>value?new Date(value*1000).toLocaleString('ru-RU'):'Не записана';
const progressPercent=value=>Math.min(100,Math.max(0,(Number(value)||0)*100));
function progressBar(value,label='Прогресс'){
  const percent=progressPercent(value);
  return `<div class="progress compact" role="progressbar" aria-label="${esc(label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(1)}"><span style="width:${percent}%"></span></div>`;
}
async function loadLibraries(silent=false){
  const generation=++libraryGeneration;
  if(!silent)$('#library-items').textContent='Загрузка библиотек…';
  const libraries=await readLibrary('/libraries');if(generation!==libraryGeneration)return;
  state.libraries=libraries;renderLibraries(silent);
}
function renderLibraries(silent=false){
  const libraries=state.libraries||[];
  $('#library-nav').innerHTML=libraries.map(l=>`<button class="ghost ${l.id===librarySelection?'active':''}" data-library="${esc(l.id)}" aria-pressed="${l.id===librarySelection}">${esc(l.name)} · ${l.items.length}</button>`).join('');
  const selected=libraries.find(l=>l.id===librarySelection);
  const tiles=selected?.items.length?selected.items.map(m=>{
    const download=m.download;
    const initial=esc((m.title||'?').trim().charAt(0).toUpperCase()||'?');
    const poster=`<span class="library-poster-frame"><span class="library-poster-empty" aria-hidden="true"><b>${initial}</b><small>Нет обложки</small></span>${m.poster?`<img src="${esc(posterUrl(m.poster))}" alt="" loading="lazy">`:''}</span>`;
    return `<button class="library-tile" data-library-media="${m.id}">${poster}<strong>${esc(m.title)}</strong><span class="fine">${m.year||'Год неизвестен'}${m.taxonomy_known?'':' · Классификация уточняется'}</span>${download?`<div class="library-tile-status"><div class="library-tile-download"><span>${(progressPercent(download.progress)).toFixed(1)}%</span><span>${bytes(download.download_rate)}/с</span></div>${progressBar(download.progress,`Загрузка ${m.title}`)}</div>`:''}</button>`;
  }).join(''):'<p class="muted">В этой библиотеке пока нет произведений.</p>';
  const container=$('#library-items');
  if(silent){
    const next=document.createElement('div');next.className=container.className;next.innerHTML=tiles;
    if(container.childNodes.length===next.childNodes.length){updateLibraryNodes(container,next);return;}
  }
  container.innerHTML=tiles;
}
function downloadDetails(download){
  if(!download)return '';
  const paused=['paused','stopped'].includes(download.state);
  return `<div class="episode-download"><div class="episode-download-heading">${statusPill(download.state)}<span class="fine">${progressPercent(download.progress).toFixed(1)}% · ${bytes(download.download_rate)}/с · осталось ${eta(download.eta)}</span><button class="ghost" data-download="${download.id}" data-action="${paused?'resume':'pause'}">${paused?'Продолжить':'Пауза'}</button></div>${progressBar(download.progress,'Прогресс серии')}<div class="episode-download-stats fine">Раздача: ${bytes(download.upload_rate)}/с · ratio ${Number(download.ratio||0).toFixed(2)} / ${download.seed_ratio??'∞'} · сиды/пиры ${download.seeds||0}/${download.peers||0}</div>${download.error?`<p class="form-error">${esc(download.error)}</p>`:''}</div>`;
}
function libraryFile(file){
  const label=file.current?'Проверенный файл':'Выбранный файл';
  const trackRows=kind=>file.tracks.filter(t=>t.kind===kind).map(t=>`${esc(languageName(t.language))}${t.codec?' · '+esc(t.codec):''}${t.channels?' · '+t.channels+' кан.':''} · ${t.external?'внешняя':'встроенная'}${t.title?' · '+esc(t.title):''}${t.path?'<small class="path">'+esc(t.path)+'</small>':''}`).join('<br>')||'Нет сведений';
  const href=/^https?:\/\//i.test(file.release.url)?file.release.url:null;
  return `<article class="library-file"><div class="part-row"><strong>${label}</strong>${statusPill(file.download_state)}</div>${downloadDetails(file.download)}<p class="fine">${file.verified?'Файл проверен':'Предварительные сведения; версия ещё не подтверждена'}</p><p class="path">${esc(file.directory)}/${esc(file.path)}</p><dl><dt>Видео</dt><dd>${file.resolution?file.resolution+'p':'Качество неизвестно'}${file.codec?' · '+esc(file.codec):''}${file.width?' · '+file.width+'×'+file.height:''} · ${bytes(file.size)}</dd><dt>Аудиодорожки</dt><dd>${trackRows('audio')}</dd><dt>Субтитры</dt><dd>${trackRows('subtitle')}</dd><dt>Раздача · ${esc(file.release.provider)}</dt><dd>${href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(file.release.title)}</a>`:esc(file.release.title)}</dd></dl>${file.missing_subtitle_languages.length?`<p class="fine">Не найдены субтитры: ${esc(languageList(file.missing_subtitle_languages))}</p>`:''}</article>`;
}
function episodeActions(episode){
  const subtasks=episode.subtasks||[];
  if(!subtasks.length)return `<div class="episode-actions">${episode.episode!=null&&!episode.files?.length?`<button class="primary" data-download-season="${esc(episode.season)}" data-download-episode="${esc(episode.episode)}">Скачать серию</button>`:''}<button class="ghost" data-delete-episode="${esc(episode.id)}" ${episode.files?.length?'':'disabled'}>Удалить</button></div>`;
  return `<div class="episode-actions">${subtasks.map(sub=>`<button class="ghost" data-candidates="${sub.id}">Выбрать раздачу</button>${sub.selected_candidate_id?`<button class="ghost" data-map-files="${sub.selected_candidate_id}">Изменить файлы</button>`:''}<button class="ghost" data-delete-selection="${sub.id}">Удалить</button>`).join('')}</div>`;
}
function renderMediaTask(item, identity){
  const task=item.task??(state.tasks||[]).find(t=>t.media_id===identity);
  if(!task)return `<details class="library-task" id="media-task"><summary><strong>Задача</strong><span class="fine">Не создана</span></summary>${item.kind==='movie'?'<button class="primary" data-create-media-task>Скачать фильм</button>':'<p class="muted">Нажмите «Скачать» у нужного сезона, чтобы создать задачу.</p>'}</details>`;
  const requirements=task.requirements,search=item.search||{};
  const seasons=task.seasons||[{season:task.season,whole_season:task.whole_season}];
  const seasonList=task.kind==='movie'?'Фильм':seasons.filter(s=>s.season!=null).map(s=>{
    const numbers=task.subtasks.filter(p=>(p.season??task.season)===s.season).map(p=>p.episode);
    return `Сезон ${esc(s.season)}: ${s.whole_season?'все серии, включая новые':esc(numbers.join(', '))}`;
  }).join('<br>');
  const cooldown=Boolean(search.pending_requests&&(search.next_attempt_at>Date.now()/1000||(search.providers||[]).some(provider=>provider.state==='cooldown')));
  const busy=search.running||(search.pending_requests&&!cooldown);
  const searchMessage=search.state==='idle'&&item.last_search_at?`Последний поиск: ${searchDate(item.last_search_at)}. Подробный журнал появится при следующем запуске.`:search.message||'Поиск ещё не запускался';
  return `<details class="library-task" id="media-task" open><summary><strong>Задача</strong><span class="fine">${task.completed?'Завершена':task.paused?'На паузе':search.running?'Поиск':search.pending_requests?'В очереди':'Активна'}</span></summary><div class="media-task-body"><dl><dt>Сезоны и серии</dt><dd>${seasonList||'Фильм'}</dd><dt>Качество</dt><dd>${requirements.min_resolution}–${requirements.max_resolution}p</dd><dt>Аудиодорожки</dt><dd>${esc(languageList(requirements.audio_languages||[])||'Без ограничений')}</dd><dt>Субтитры</dt><dd>${esc(languageList(requirements.subtitle_languages||[])||'Не запрошены')}</dd>${requirements.keyword?`<dt>Обязательная фраза</dt><dd>${esc(requirements.keyword)}</dd>`:''}</dl><div class="task-actions"><button class="primary" data-run-task="${task.id}" ${task.completed||task.paused||busy?'disabled':''}>${cooldown?'Сбросить паузу и запустить поиск':'Запустить поиск'}</button><button class="ghost" data-pause-task="${task.id}" data-paused="${task.paused}">${task.paused?'Продолжить':'Пауза'}</button><button class="ghost" data-edit-task="${task.id}">Изменить</button><button class="ghost" data-delete-task="${task.id}">Удалить задачу</button>${chooseAllButton(task)}</div><section class="media-task-search"><h3>Поиск раздач</h3><p role="status">${esc(task.completed?'Все запрошенные серии скачаны и проверены':task.paused?'Задача на паузе':searchMessage)}${search.next_attempt_at>Date.now()/1000?` · Повтор ${searchDate(search.next_attempt_at)}`:''}</p>${search.groups_total?progressBar(search.groups_done/search.groups_total,'Поиск по сезонам'):''}<p class="fine">${search.season!=null?`Сезон ${esc(search.season)} · `:''}${esc(search.provider||'')}${search.candidates_checked!=null?` · Проверено раздач: ${search.candidates_checked}`:''}</p>${search.history?.length?`<details class="task-search-log"><summary>Ход поиска</summary><ol>${search.history.map(event=>`<li>${esc(event.message)}</li>`).join('')}</ol></details>`:''}${task.subtasks.some(p=>p.error)?`<p class="form-error">${esc([...new Set(task.subtasks.map(p=>p.error).filter(Boolean))].join('; '))}</p>`:''}</section></div></details>`;
}
function renderEpisode(episode){
  const number=episode.episode==null?'Фильм':`${episode.episode}.`;
  const visual=`<span class="episode-visual"><span class="episode-still-empty" aria-hidden="true"><b>${episode.episode==null?'▶':esc(episode.episode)}</b><small>${episode.episode==null?'Видео':'Серия'}</small></span>${episode.still?`<img class="episode-still" src="${esc(posterUrl(episode.still))}" alt="" loading="lazy">`:''}</span>`;
  const status=episode.statuses?.length?episode.statuses.map(s=>esc(statuses[s]||s)).join(', '):episode.released?'Не запрошено':'Ожидание выхода';
  return `<details class="library-episode" data-episode="${esc(episode.id)}"><summary><div class="episode-summary">${visual}<div><strong>${esc(number)} ${esc(episode.title)}</strong><span class="fine">${libraryDate(episode.air_date)} · ${status}</span>${episode.download?progressBar(episode.download.progress,'Прогресс серии'):''}</div></div></summary>${episode.overview?`<p class="episode-overview">${esc(episode.overview)}</p>`:''}<div class="episode-meta"><span class="fine">Последний поиск: ${searchDate(episode.last_search_at)}</span>${episodeActions(episode)}</div>${episode.files?.length?episode.files.map(libraryFile).join(''):'<p class="muted">Файл и раздача пока не выбраны.</p>'}</details>`;
}
function renderSeason(item,number,episodes,info={}){
  const ready=episodes.filter(episode=>episode.files?.some(file=>file.verified)).length;
  const downloading=episodes.filter(episode=>episode.download&&['starting','downloading'].includes(episode.download.state)).length;
  const activity=downloading?` · загружается ${downloading}`:ready?` · готово ${ready}`:'';
  const label=number==null?'Без сезона':number===0?'Дополнительно':`Сезон ${number}`;
  const count=info.episode_count??episodes.length;
  const seasonTask=(state.tasks||[]).find(task=>task.media_id===item.id&&(task.seasons||[{season:task.season,canonical_season:task.canonical_season}]).some(season=>season.season===number||(!season.numbering_season&&season.canonical_season===number)));
  const hasTask=episodes.some(episode=>episode.subtasks?.length)||Boolean(seasonTask);
  const hasFiles=episodes.some(episode=>episode.files?.length);
  if(number!==null&&!hasTask&&!hasFiles)return `<details class="library-season is-unrequested" name="library-seasons" data-season="${esc(number)}"><summary><strong>${esc(label)}</strong><span class="library-season-summary"><span class="fine">${count} серий · не добавлен</span><button type="button" class="primary" data-download-season="${esc(number)}">Скачать</button></span></summary><div class="library-season-episodes">${episodes.length?episodes.map(renderEpisode).join(''):'<p class="muted">Сведения о сериях пока не получены.</p>'}</div></details>`;
  const seasonChoice=seasonTask&&number!==null?`<div class="library-season-actions"><button type="button" class="ghost" data-season-candidates="${seasonTask.id}" data-season-number="${esc(number)}">Выбрать раздачу для сезона</button></div>`:'';
  return `<details class="library-season" name="library-seasons" data-season="${esc(number??'none')}"><summary><strong>${esc(label)}</strong><span class="library-season-summary"><span class="fine">${episodes.length} серий${activity}</span>${!hasTask&&number!==null?`<button type="button" class="primary" data-download-season="${esc(number)}">Скачать</button>`:''}${number!==null?`<button type="button" class="ghost" data-delete-season="${esc(number)}">Удалить сезон</button>`:''}</span></summary>${seasonChoice}<div class="library-season-episodes">${episodes.map(renderEpisode).join('')}</div></details>`;
}
function renderEpisodeGroups(item){
  if(item.kind!=='tv')return item.episodes.map(renderEpisode).join('');
  const groups=new Map();
  const seasonInfo=new Map((item.seasons||item.metadata?.seasons||[]).map(season=>[season.number,season]));
  for(const season of seasonInfo.values())groups.set(season.number,[]);
  for(const episode of item.episodes){
    const season=episode.season??episode.canonical_season??null;
    if(!groups.has(season))groups.set(season,[]);
    groups.get(season).push(episode);
  }
  return [...groups.entries()].sort(([left],[right])=>(left??-1)-(right??-1)).map(([season,episodes])=>renderSeason(item,season,episodes,seasonInfo.get(season))).join('');
}
function updateLibraryNodes(current, next){
  if(current.nodeType!==next.nodeType||current.nodeName!==next.nodeName){current.replaceWith(next.cloneNode(true));return;}
  if(current.nodeType===Node.TEXT_NODE){if(current.textContent!==next.textContent)current.textContent=next.textContent;return;}
  if(current.nodeType!==Node.ELEMENT_NODE)return;
  if(current.id&&!next.id)next.id=current.id;
  for(const name of current.getAttributeNames())if(!next.hasAttribute(name)&&name!=='open')current.removeAttribute(name);
  for(const name of next.getAttributeNames())if(name!=='open'&&current.getAttribute(name)!==next.getAttribute(name))current.setAttribute(name,next.getAttribute(name));
  const oldChildren=[...current.childNodes],newChildren=[...next.childNodes];
  if(oldChildren.length!==newChildren.length){
    // Replace only the changed section; other episode images stay mounted.
    current.replaceChildren(...newChildren.map(node=>node.cloneNode(true)));
    return;
  }
  for(let i=0;i<oldChildren.length;i++)updateLibraryNodes(oldChildren[i],newChildren[i]);
}
async function openLibraryMedia(identity,silent=false){
  const taskOpen=$('#media-task')?.open??true;
  const logOpen=$('.task-search-log')?.open??false;
  const opened=new Set($$('.library-episode[open]').map(node=>node.dataset.episode));
  const openedSeasons=new Set($$('.library-season[open]').map(node=>node.dataset.season));
  const generation=++libraryGeneration;libraryMedia=identity;
  $('#library-detail').hidden=false;$('#library-overview').hidden=true;
  $('#all-tasks-control').hidden=true;
  if(!silent)$('#library-detail-body').textContent='Загрузка…';
  const item=await readLibrary(`/libraries/media/${identity}`);if(generation!==libraryGeneration)return;libraryItem=item;
  libraryDetailUpdated=Date.now();
  if(item.task){state.tasks=state.tasks.filter(t=>t.media_id!==identity);state.tasks.push(item.task);}
  const meta=item.metadata;
  const calendar=[...item.episodes].sort((a,b)=>(a.air_date||'9999').localeCompare(b.air_date||'9999'));
  const next=document.createElement('div');
  next.innerHTML=`<div class="library-heading">${item.poster?`<img src="${esc(posterUrl(item.poster))}" alt="">`:''}<div><h2>${esc(item.title)}</h2><p class="fine">${esc(meta.original_title||'')} · ${item.year||'—'}</p><p>${esc(meta.overview||'Описание отсутствует.')}</p><p class="fine">${esc((meta.genres||[]).join(', '))}</p><p>Последний поиск: ${searchDate(item.last_search_at)}</p><p class="fine">Источник: ${esc(meta.provider||'tmdb')} · ID: ${esc(meta.id)}</p></div></div><details class="library-calendar"><summary>Календарь выхода · ${calendar.length}</summary><ol>${calendar.map(e=>`<li><time>${libraryDate(e.air_date)}</time> — ${e.episode==null?'Фильм':`S${e.season} · E${e.episode}`} · ${esc(e.title)}${e.air_date&&!e.released?' · Ожидается':''}</li>`).join('')}</ol></details><div class="library-episodes">${renderEpisodeGroups(item)||'<p>Сведения об эпизодах ещё не получены.</p>'}</div>`;
  next.querySelector('.library-episodes').insertAdjacentHTML('beforebegin',renderMediaTask(item,identity));
  const body=$('#library-detail-body');
  if(silent&&body.children.length===next.children.length)updateLibraryNodes(body,next);
  else body.replaceChildren(...next.childNodes);
  const seasons=$$('.library-season');
  $('#media-task').open=taskOpen;
  if($('.task-search-log'))$('.task-search-log').open=logOpen;
  for(const node of seasons)if(openedSeasons.has(node.dataset.season))node.open=true;
  if(!silent&&!openedSeasons.size){const first=seasons.find(node=>!node.classList.contains('is-unrequested'));if(first)first.open=true;}
  for(const node of $$('.library-episode'))if(opened.has(node.dataset.episode))node.open=true;
}
async function refreshLibraryView(){
  if(state.tab!=='library')return;
  if(libraryMedia)await openLibraryMedia(libraryMedia,true);else await loadLibraries(true);
}
function refreshLibraryProgress(downloads){
  if(!libraryItem)return false;
  const byId=new Map(downloads.map(download=>[download.id,download]));
  for(const episode of libraryItem.episodes){
    for(const file of episode.files||[]){
      if(!file.download)continue;
      const current=byId.get(file.download.id);
      if(!current||current.state!==file.download.state)return false;
      const stats=current.stats||{},part=(stats.bindings||{})[String(file.download.subtask_id)]||{};
      Object.assign(file.download,{progress:part.progress??stats.progress??0,eta:part.eta??stats.eta,download_rate:stats.download_rate||0,upload_rate:stats.upload_rate||0,seeds:stats.seeds||0,peers:stats.peers||0,error:stats.error,ratio:current.ratio,seed_ratio:current.seed_ratio});
    }
    const active=(episode.files||[]).map(file=>file.download).filter(Boolean);
    episode.download=active.find(item=>['starting','downloading','paused'].includes(item.state))||active[0]||null;
    const node=$$('.library-episode').find(node=>node.dataset.episode===String(episode.id));
    if(node){const open=node.open,shell=document.createElement('div');shell.innerHTML=renderEpisode(episode);updateLibraryNodes(node,shell.firstElementChild);node.open=open;}
  }
  return true;
}
document.addEventListener('toggle',async event=>{
  const node=event.target;
  if(!(node instanceof HTMLDetailsElement)||!node.classList.contains('library-season')||!node.open||!libraryItem)return;
  const identity=libraryMedia,season=Number(node.dataset.season),key=`${identity}:${season}`;
  if(!Number.isInteger(season)||libraryItem.episodes.some(e=>e.season===season)||loadedLibrarySeasons.has(key)||loadingLibrarySeasons.has(key))return;
  loadingLibrarySeasons.add(key);
  node.querySelector('.library-season-episodes').textContent='Загрузка серий…';
  try{
    await api(`/libraries/media/${identity}/seasons/${season}/episodes`);
    loadedLibrarySeasons.add(key);
    if(libraryMedia===identity)await openLibraryMedia(identity,true);
  }catch(error){if(node.isConnected)node.querySelector('.library-season-episodes').textContent=`${error.message}. Закройте и откройте сезон для повтора.`;}
  finally{loadingLibrarySeasons.delete(key);}
},true);
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  try{
    if(button.dataset.library){librarySelection=button.dataset.library;renderLibraries();}
    else if(button.dataset.libraryMedia){libraryMedia=Number(button.dataset.libraryMedia);await switchTab('library');}
    else if(button.id==='library-back'){libraryGeneration++;libraryMedia=null;libraryItem=null;$('#library-detail').hidden=true;$('#library-overview').hidden=false;$('#all-tasks-control').hidden=false;await loadLibraries();}
    else if(button.dataset.downloadSeason!==undefined){event.preventDefault();if(!libraryItem)return;button.disabled=true;try{const season=Number(button.dataset.downloadSeason);const aliases=Object.values(libraryItem.metadata.episode_numbering||{}).flat();const single=button.dataset.downloadEpisode!==undefined;const selection={season,...(aliases.some(alias=>alias.season===season)?{numbering_season:season}:{}),...(single?{episodes:[Number(button.dataset.downloadEpisode)]}:{})};await api(`/libraries/media/${libraryMedia}/seasons`,'POST',selection);await refreshTasks();await refreshDownloads();await openLibraryMedia(libraryMedia,true);toast(single?'Серия добавлена в задачу':'Сезон добавлен в задачу');}finally{button.disabled=false;}}
    else if(button.hasAttribute('data-create-media-task')){button.disabled=true;try{const meta=libraryItem.metadata;await api('/tasks','POST',{provider:meta.provider,media_id:meta.id,kind:'movie'});await refreshTasks();await refreshLibraryView();}finally{button.disabled=false;}}
    else if(button.dataset.deleteSeason!==undefined){
      event.preventDefault();
      const number=button.dataset.deleteSeason;
      openModal('Удаление сезона',`<form id="delete-selection-form" data-season="${esc(number)}" data-endpoint="/libraries/media/${libraryMedia}/seasons/${esc(number)}" class="stack"><p>Удалить сезон ${esc(number)} из задачи и удалить все его файлы и выбранные раздачи? Файлы нельзя восстановить без повторного скачивания.</p><p class="fine">Файлы, нужные другим сезонам и произведениям, сохранятся. Сезон можно будет снова добавить кнопкой «Скачать». При удалении последнего сезона пустая задача тоже будет удалена.</p><div class="form-footer"><p class="form-error"></p><button type="submit" class="primary">Удалить сезон</button></div></form>`);
    }
    else if(button.dataset.deleteSelection||button.dataset.deleteEpisode){
      const endpoint=button.dataset.deleteSelection?`/subtasks/${button.dataset.deleteSelection}/selection`:`/libraries/media/${libraryMedia}/episodes/${button.dataset.deleteEpisode}/selection`;
      openModal('Удаление файлов серии',`<form id="delete-selection-form" data-endpoint="${esc(endpoint)}" class="stack"><p>Удалить файлы этой серии и сбросить выбранную раздачу? Удалённые файлы нельзя восстановить без повторного скачивания.</p><p class="fine">Файлы, используемые другими сериями, сохранятся. Серия останется в списке; автоматическое скачивание остановится до нового выбора раздачи.</p><div class="form-footer"><p class="form-error"></p><button type="submit" class="primary">Удалить</button></div></form>`);
    }
    else if(button.id==='library-delete'){
      const title=$('#library-detail-body h2')?.textContent||'это произведение';
      openModal('Удаление из библиотеки',`<form id="delete-media-form" data-media="${libraryMedia}" class="stack"><p>Удалить «${esc(title)}» и все связанные задачи и загрузки из Lazarr?</p><label class="check"><input type="checkbox" role="switch" name="delete_files">Удалить также скачанные файлы</label><p class="fine">Без переключателя файлы останутся на диске. Общие раздачи, нужные другим произведениям, сохранятся.</p><div class="form-footer"><p class="form-error"></p><button type="submit" class="primary">Удалить произведение</button></div></form>`);
    }
  }catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='delete-selection-form')return;
  event.preventDefault();
  const button=form.querySelector('button[type="submit"]');button.disabled=true;
  try{
    const result=await api(form.dataset.endpoint,'DELETE');
    $('#modal').close();
    await Promise.all([refreshTasks(),refreshDownloads()]);await refreshLibraryView();
    toast(result.cleanup_pending?'Удалено. Очистка файлов будет повторена автоматически.':form.dataset.season!==undefined?'Сезон удалён из задачи вместе с файлами':'Файлы серии и выбранная раздача удалены');
  }catch(error){form.querySelector('.form-error').textContent=error.message;button.disabled=false;}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='delete-media-form')return;
  event.preventDefault();
  try{
    const data=new FormData(form);
    const result=await api(`/libraries/media/${form.dataset.media}`,'DELETE',{delete_files:data.get('delete_files')==='on'});
    $('#modal').close();libraryGeneration++;libraryMedia=null;libraryItem=null;
    $('#library-detail').hidden=true;$('#library-overview').hidden=false;
    $('#all-tasks-control').hidden=false;
    await Promise.all([loadLibraries(),refreshTasks(),refreshDownloads()]);
    toast(result.cleanup_pending?'Произведение удалено. Очистка файлов будет повторена автоматически.':'Произведение удалено из библиотеки');
  }catch(error){form.querySelector('.form-error').textContent=error.message;}
});
