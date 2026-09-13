let librarySelection='series',libraryMedia=null,libraryGeneration=0;
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
  const libraries=await api('/libraries');if(generation!==libraryGeneration)return;
  state.libraries=libraries;renderLibraries();
}
function renderLibraries(){
  const libraries=state.libraries||[];
  $('#library-nav').innerHTML=libraries.map(l=>`<button class="ghost ${l.id===librarySelection?'active':''}" data-library="${esc(l.id)}" aria-pressed="${l.id===librarySelection}">${esc(l.name)} · ${l.items.length}</button>`).join('');
  const selected=libraries.find(l=>l.id===librarySelection);
  $('#library-items').innerHTML=selected?.items.length?selected.items.map(m=>{
    const download=m.download;
    return `<button class="library-tile" data-library-media="${m.id}">${m.poster?`<img src="${esc(posterUrl(m.poster))}" alt="" loading="lazy">`:'<span class="library-poster-empty">Нет обложки</span>'}<strong>${esc(m.title)}</strong><span class="fine">${m.year||'Год неизвестен'}${m.taxonomy_known?'':' · Классификация уточняется'}</span>${download?`<div class="library-tile-download"><span>${(progressPercent(download.progress)).toFixed(1)}%</span><span>${bytes(download.download_rate)}/с</span></div>${progressBar(download.progress,`Загрузка ${m.title}`)}`:''}</button>`;
  }).join(''):'<p class="muted">В этой библиотеке пока нет произведений.</p>';
}
function downloadDetails(download){
  if(!download)return '';
  const paused=['paused','stopped'].includes(download.state);
  return `<div class="episode-download"><div class="episode-download-heading">${statusPill(download.state)}<span class="fine">${progressPercent(download.progress).toFixed(1)}% · ${bytes(download.download_rate)}/с · осталось ${eta(download.eta)}</span><button class="ghost" data-download="${download.id}" data-action="${paused?'resume':'pause'}">${paused?'Продолжить':'Пауза'}</button></div>${progressBar(download.progress,'Прогресс серии')}<div class="episode-download-stats fine">Раздача: ${bytes(download.upload_rate)}/с · ratio ${Number(download.ratio||0).toFixed(2)} / ${download.seed_ratio??'∞'} · сиды/пиры ${download.seeds||0}/${download.peers||0}</div>${download.error?`<p class="form-error">${esc(download.error)}</p>`:''}</div>`;
}
function libraryFile(file){
  const label=file.current?'Текущая версия':file.pending?'Выбранная версия':'Предыдущая версия';
  const trackRows=kind=>file.tracks.filter(t=>t.kind===kind).map(t=>`${esc(languageName(t.language))}${t.codec?' · '+esc(t.codec):''}${t.channels?' · '+t.channels+' кан.':''} · ${t.external?'внешняя':'встроенная'}${t.title?' · '+esc(t.title):''}${t.path?'<small class="path">'+esc(t.path)+'</small>':''}`).join('<br>')||'Нет сведений';
  const href=/^https?:\/\//i.test(file.release.url)?file.release.url:null;
  return `<article class="library-file"><div class="part-row"><strong>${label}</strong>${statusPill(file.download_state)}</div>${downloadDetails(file.download)}<p class="fine">${file.verified?'Файл проверен':'Предварительные сведения; версия ещё не подтверждена'}</p><p class="path">${esc(file.directory)}/${esc(file.path)}</p><dl><dt>Видео</dt><dd>${file.resolution?file.resolution+'p':'Качество неизвестно'}${file.codec?' · '+esc(file.codec):''}${file.width?' · '+file.width+'×'+file.height:''} · ${bytes(file.size)}</dd><dt>Аудиодорожки</dt><dd>${trackRows('audio')}</dd><dt>Субтитры</dt><dd>${trackRows('subtitle')}</dd><dt>Раздача · ${esc(file.release.provider)}</dt><dd>${href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(file.release.title)}</a>`:esc(file.release.title)}</dd></dl>${file.missing_subtitle_languages.length?`<p class="fine">Не найдены субтитры: ${esc(languageList(file.missing_subtitle_languages))}</p>`:''}</article>`;
}
function episodeActions(episode){
  const subtasks=episode.subtasks||[];
  if(!subtasks.length)return '';
  return `<div class="episode-actions">${subtasks.map((sub,index)=>`<button class="ghost" data-candidates="${sub.id}">${subtasks.length>1?`Раздачи · задача ${index+1}`:'Выбрать раздачу'}</button>`).join('')}</div>`;
}
function mediaTaskActions(identity){
  const tasks=(state.tasks||[]).filter(task=>task.media_id===identity);
  return tasks.map(task=>chooseAllButton(task)).filter(Boolean).join(' ');
}
function renderEpisode(episode){
  const number=episode.episode==null?'Фильм':`${episode.episode}.`;
  const image=episode.still?`<img class="episode-still" src="${esc(posterUrl(episode.still))}" alt="" loading="lazy">`:'';
  const status=episode.statuses?.length?episode.statuses.map(s=>esc(statuses[s]||s)).join(', '):episode.released?'Не запрошено':'Ожидание выхода';
  return `<details class="library-episode" data-episode="${esc(episode.id)}"><summary><div class="episode-summary">${image}<div><strong>${esc(number)} ${esc(episode.title)}</strong><span class="fine">${libraryDate(episode.air_date)} · ${status}</span>${episode.download?progressBar(episode.download.progress,'Прогресс серии'):''}</div></div></summary>${episode.overview?`<p class="episode-overview">${esc(episode.overview)}</p>`:''}<div class="episode-meta"><span class="fine">Последний поиск: ${searchDate(episode.last_search_at)}</span>${episodeActions(episode)}</div>${episode.files?.length?episode.files.map(libraryFile).join(''):'<p class="muted">Файл и раздача пока не выбраны.</p>'}</details>`;
}
async function openLibraryMedia(identity,silent=false){
  const opened=new Set($$('.library-episode[open]').map(node=>node.dataset.episode));
  const generation=++libraryGeneration;libraryMedia=identity;
  $('#library-detail').hidden=false;$('#library-overview').hidden=true;
  if(!silent)$('#library-detail-body').textContent='Загрузка…';
  const item=await api(`/libraries/media/${identity}`);if(generation!==libraryGeneration)return;
  const meta=item.metadata;
  const calendar=[...item.episodes].sort((a,b)=>(a.air_date||'9999').localeCompare(b.air_date||'9999'));
  const allActions=mediaTaskActions(identity);
  $('#library-detail-body').innerHTML=`<div class="library-heading">${item.poster?`<img src="${esc(posterUrl(item.poster))}" alt="">`:''}<div><h2>${esc(item.title)}</h2><p class="fine">${esc(meta.original_title||'')} · ${item.year||'—'}</p><p>${esc(meta.overview||'Описание отсутствует.')}</p><p class="fine">${esc((meta.genres||[]).join(', '))}</p><p>Последний поиск: ${searchDate(item.last_search_at)}</p><p class="fine">Задач: ${item.task_count} · Источник: ${esc(meta.provider||'tmdb')} · ID: ${esc(meta.id)}</p>${allActions?`<div class="media-task-actions">${allActions}</div>`:''}</div></div><details class="library-calendar"><summary>Календарь выхода · ${calendar.length}</summary><ol>${calendar.map(e=>`<li><time>${libraryDate(e.air_date)}</time> — ${e.episode==null?'Фильм':`S${e.season} · E${e.episode}`} · ${esc(e.title)}${e.air_date&&!e.released?' · Ожидается':''}</li>`).join('')}</ol></details><div class="library-episodes">${item.episodes.map(renderEpisode).join('')||'<p>Сведения об эпизодах ещё не получены.</p>'}</div>`;
  for(const node of $$('.library-episode'))if(opened.has(node.dataset.episode))node.open=true;
}
async function refreshLibraryView(){
  if(state.tab!=='library')return;
  if(libraryMedia)await openLibraryMedia(libraryMedia,true);else await loadLibraries(true);
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  try{
    if(button.dataset.library){librarySelection=button.dataset.library;renderLibraries();}
    else if(button.dataset.libraryMedia)await openLibraryMedia(Number(button.dataset.libraryMedia));
    else if(button.id==='library-back'){libraryGeneration++;libraryMedia=null;$('#library-detail').hidden=true;$('#library-overview').hidden=false;}
    else if(button.id==='library-refresh')await openLibraryMedia(libraryMedia);
    else if(button.id==='library-subtitles'){
      const defaults=state.settings?.defaults?.subtitle_languages||[];
      openModal('Загрузить субтитры',`<form id="subtitle-download-form" data-media="${libraryMedia}" class="stack">${languagePicker('subtitle_download_languages','Языки субтитров',defaults,'Пусто — языки из требований задач, затем значения по умолчанию.')}<p class="fine">Включённые провайдеры субтитров будут опрошены для готовых файлов, в которых этих языков ещё нет. Файлы сохранятся рядом с видео.</p><div class="form-footer"><p class="form-error"></p><button type="submit" class="primary">Загрузить</button></div></form>`);
    }
    else if(button.id==='library-delete'){
      const title=$('#library-detail-body h2')?.textContent||'это произведение';
      openModal('Удаление из библиотеки',`<form id="delete-media-form" data-media="${libraryMedia}" class="stack"><p>Удалить «${esc(title)}» и все связанные задачи и загрузки из Lazarr?</p><label class="check"><input type="checkbox" role="switch" name="delete_files">Удалить также скачанные файлы</label><p class="fine">Без переключателя файлы останутся на диске. Общие раздачи, нужные другим произведениям, сохранятся.</p><div class="form-footer"><p class="form-error"></p><button type="submit" class="primary">Удалить произведение</button></div></form>`);
    }
  }catch(error){toast(error.message,true);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(!['delete-media-form','subtitle-download-form'].includes(form.id))return;
  event.preventDefault();
  try{
    const data=new FormData(form);
    if(form.id==='subtitle-download-form'){
      const button=form.querySelector('button[type=submit]');button.disabled=true;button.textContent='Загрузка…';
      const result=await api(`/libraries/media/${form.dataset.media}/subtitles`,'POST',{languages:selectedLanguages(form,'subtitle_download_languages')});
      $('#modal').close();await openLibraryMedia(Number(form.dataset.media));
      const message=result.downloaded.length?`Загружено субтитров: ${result.downloaded.length}`:result.not_found.length?'Подходящие субтитры не найдены':'Новые субтитры не требуются';
      toast(result.errors.length?`${message}. ${result.errors[0].message}`:message,Boolean(result.errors.length));
      return;
    }
    const result=await api(`/libraries/media/${form.dataset.media}`,'DELETE',{delete_files:data.get('delete_files')==='on'});
    $('#modal').close();libraryGeneration++;libraryMedia=null;
    $('#library-detail').hidden=true;$('#library-overview').hidden=false;
    await Promise.all([loadLibraries(),refreshTasks(),refreshDownloads()]);
    toast(result.cleanup_pending?'Произведение удалено. Очистка файлов будет повторена автоматически.':'Произведение удалено из библиотеки');
  }catch(error){form.querySelector('.form-error').textContent=error.message;}
});
