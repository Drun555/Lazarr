let librarySelection='series',libraryMedia=null,libraryGeneration=0;
const libraryDate=value=>value?new Date(value.length===10?value+'T12:00:00':value).toLocaleDateString('ru-RU'):'Дата неизвестна';
const searchDate=value=>value?new Date(value*1000).toLocaleString('ru-RU'):'Не записана';
async function loadLibraries(){
  const generation=++libraryGeneration;
  $('#library-items').textContent='Загрузка библиотек…';
  const libraries=await api('/libraries');if(generation!==libraryGeneration)return;
  state.libraries=libraries;renderLibraries();
}
function renderLibraries(){
  const libraries=state.libraries||[];
  $('#library-nav').innerHTML=libraries.map(l=>`<button class="ghost ${l.id===librarySelection?'active':''}" data-library="${esc(l.id)}" aria-pressed="${l.id===librarySelection}">${esc(l.name)} · ${l.items.length}</button>`).join('');
  const selected=libraries.find(l=>l.id===librarySelection);
  $('#library-items').innerHTML=selected?.items.length?selected.items.map(m=>`<button class="library-tile" data-library-media="${m.id}">${m.poster?`<img src="${esc(posterUrl(m.poster))}" alt="" loading="lazy">`:'<span class="library-poster-empty">Нет обложки</span>'}<strong>${esc(m.title)}</strong><span class="fine">${m.year||'Год неизвестен'}${m.taxonomy_known?'':' · Классификация уточняется'}</span></button>`).join(''):'<p class="muted">В этой библиотеке пока нет произведений.</p>';
}
function libraryFile(file){
  const label=file.current?'Текущая версия':file.pending?'Выбранная версия':'Предыдущая версия';
  const trackRows=kind=>file.tracks.filter(t=>t.kind===kind).map(t=>`${esc(languageName(t.language))}${t.codec?' · '+esc(t.codec):''}${t.channels?' · '+t.channels+' кан.':''} · ${t.external?'внешняя':'встроенная'}${t.title?' · '+esc(t.title):''}${t.path?'<small class="path">'+esc(t.path)+'</small>':''}`).join('<br>')||'Нет сведений';
  const href=/^https?:\/\//i.test(file.release.url)?file.release.url:null;
  return `<article class="library-file"><div class="part-row"><strong>${label}</strong>${statusPill(file.download_state)}</div><p class="fine">${file.verified?'Файл проверен':'Предварительные сведения; версия ещё не подтверждена'}</p><p class="path">${esc(file.directory)}/${esc(file.path)}</p><dl><dt>Видео</dt><dd>${file.resolution?file.resolution+'p':'Качество неизвестно'}${file.codec?' · '+esc(file.codec):''}${file.width?' · '+file.width+'×'+file.height:''} · ${bytes(file.size)}</dd><dt>Аудиодорожки</dt><dd>${trackRows('audio')}</dd><dt>Субтитры</dt><dd>${trackRows('subtitle')}</dd><dt>Раздача · ${esc(file.release.provider)}</dt><dd>${href?`<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(file.release.title)}</a>`:esc(file.release.title)}</dd></dl>${file.missing_subtitle_languages.length?`<p class="fine">Не найдены субтитры: ${esc(languageList(file.missing_subtitle_languages))}</p>`:''}</article>`;
}
async function openLibraryMedia(identity){
  const generation=++libraryGeneration;libraryMedia=identity;
  $('#library-detail').hidden=false;$('#library-overview').hidden=true;
  $('#library-detail-body').textContent='Загрузка…';
  const item=await api(`/libraries/media/${identity}`);if(generation!==libraryGeneration)return;
  const meta=item.metadata;
  const calendar=[...item.episodes].sort((a,b)=>(a.air_date||'9999').localeCompare(b.air_date||'9999'));
  $('#library-detail-body').innerHTML=`<div class="library-heading">${item.poster?`<img src="${esc(posterUrl(item.poster))}" alt="">`:''}<div><h2>${esc(item.title)}</h2><p class="fine">${esc(meta.original_title||'')} · ${item.year||'—'}</p><p>${esc(meta.overview||'Описание отсутствует.')}</p><p class="fine">${esc((meta.genres||[]).join(', '))}</p><p>Последний поиск: ${searchDate(item.last_search_at)}</p><p class="fine">Задач: ${item.task_count} · Источник: ${esc(meta.provider||'tmdb')} · ID: ${esc(meta.id)}</p></div></div><details class="library-calendar"><summary>Календарь выхода · ${calendar.length}</summary><ol>${calendar.map(e=>`<li><time>${libraryDate(e.air_date)}</time> — ${e.episode==null?'Фильм':`S${e.season} · E${e.episode}`} · ${esc(e.title)}${e.air_date&&!e.released?' · Ожидается':''}</li>`).join('')}</ol></details><div class="library-episodes">${item.episodes.map(e=>`<details class="library-episode"><summary><strong>${e.episode==null?'Фильм':`Сезон ${e.season} · Серия ${e.episode}`} — ${esc(e.title)}</strong><span class="fine">${libraryDate(e.air_date)} · ${e.statuses.length?e.statuses.map(s=>esc(statuses[s]||s)).join(', '):e.released?'Не запрошено':'Ожидание выхода'}</span></summary><p class="fine">Последний поиск: ${searchDate(e.last_search_at)}</p>${e.files.length?e.files.map(libraryFile).join(''):'<p class="muted">Файл и раздача пока не выбраны.</p>'}</details>`).join('')||'<p>Сведения об эпизодах ещё не получены.</p>'}</div>`;
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
