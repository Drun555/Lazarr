/* Season-wide mapping draft. File identity includes the release, never just its index. */
let seasonMapping=null;
const mappingBugIcon='<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M9 7V5a3 3 0 0 1 6 0v2M8 3 6 1m10 2 2-2M5 10H2m3 5H2m17-5h3m-3 5h3M6 7 3 5m15 2 3-2M6 18l-3 3m15-3 3 3M12 8v13"/><rect x="5" y="7" width="14" height="14" rx="7"/></svg>';

const mappingKey=(release,index)=>`${release}:${index}`;
function mappingRows(data){
  return data.episodes.map(episode=>({subtask_id:episode.subtask_id,number:episode.number,title:episode.title,
    ...(episode.special_position?{special_position:{mode:episode.special_position.mode,...episode.special_position.position}}:{}),
    release_id:episode.release_id,video_index:episode.binding?.video_index??null,
    track_indices:(episode.binding?.tracks||[]).filter(track=>track.file_index!=null).map(track=>track.file_index)}));
}
async function mappingPromptUrl(value){
  try{return window.prompt('Введите URL раздачи',value);}catch{
    return new Promise(resolve=>{
      const dialog=document.createElement('dialog');dialog.className='mapping-url-prompt';
      dialog.setAttribute('aria-label','Введите URL раздачи');
      dialog.innerHTML=`<form><h3>Введите URL раздачи</h3><input type="url" aria-label="URL раздачи" required maxlength="2048" placeholder="https://…" value="${esc(value)}"><div class="form-footer"><button type="button" class="ghost">Отмена</button><button type="submit" class="primary">Добавить</button></div></form>`;
      let result=null;
      dialog.querySelector('form').addEventListener('submit',event=>{event.preventDefault();event.stopPropagation();result=dialog.querySelector('input').value;dialog.close();});
      dialog.querySelector('[type=button]').addEventListener('click',()=>dialog.close());
      dialog.addEventListener('close',()=>{dialog.remove();resolve(result);});
      document.body.append(dialog);dialog.showModal();dialog.querySelector('input').focus();
    });
  }
}
async function seasonMappingDialog(task,season){
  const data=await api(`/tasks/${task}/seasons/${season}/mapping`);
  seasonMapping={task,season,data,seasonTitle:data.season_title??'',deletedSubtaskIds:[],hiddenReleaseIds:data.hidden_release_ids||[],rows:mappingRows(data),smart:true,selected:null,busy:false,addingRelease:false,releaseUrl:''};
  renderSeasonMapping();
}
function mappingChip(release,file,assigned=false){
  const key=mappingKey(release.id,file.index),selected=seasonMapping.selected===key;
  return `<span class="mapping-chip-wrap"><button type="button" class="mapping-chip ${file.kind}${selected?' selected':''}" draggable="${file.kind!=='other'}" data-mapping-file="${key}" ${file.kind==='other'?'disabled':''} title="${esc(release.title+' / '+file.path)}" aria-pressed="${selected}"><span>${esc(file.path.split('/').pop())}${file.legacy?' · прежний файл':''}</span></button>${assigned?`<button type="button" class="mapping-remove" data-mapping-remove="${key}" aria-label="Вернуть ${esc(file.path)} в несопоставленные">×</button>`:''}</span>`;
}
function mappingClosePicker(){
  const picker=$('.mapping-episode-picker');
  if(!picker)return;
  const chip=picker.mappingChip;
  chip.setAttribute('aria-expanded','false');
  picker.mappingEvents.abort();
  picker.remove();
  return chip;
}
function mappingTogglePicker(chip){
  const wasOpen=chip.getAttribute('aria-expanded')==='true';
  mappingClosePicker();
  if(wasOpen)return;
  const key=chip.dataset.mappingFile,{release,file}=mappingFind(key);
  seasonMapping.selected=key;
  const rows=seasonMapping.rows.filter(row=>file.kind==='video'?row.video_index===null:row.video_index!==null&&row.release_id===release.id);
  const picker=document.createElement('div');picker.id='mapping-episode-picker';picker.className=`mapping-episode-picker ${file.kind}`;
  picker.setAttribute('popover','manual');
  picker.mappingChip=chip;picker.mappingEvents=new AbortController();
  picker.setAttribute('role','group');picker.setAttribute('aria-label','Назначить в серию');
  picker.innerHTML=`<div class="mapping-picker-heading"><span>Назначить в серию</span><span class="mapping-picker-count">${rows.length}</span></div><div class="mapping-picker-options">${rows.map(row=>`<button type="button" data-mapping-episode="${row.subtask_id}" data-mapping-key="${key}" aria-label="${esc(row.number)}. ${esc(row.title)}"><span class="mapping-picker-number">${esc(row.number)}.</span><span class="mapping-picker-title">${esc(row.title)}</span><span class="mapping-picker-arrow" aria-hidden="true">↗</span></button>`).join('')||`<p class="mapping-picker-empty">${file.kind==='video'?'Все серии уже заняты видеофайлами':'Сначала назначьте видео из этой раздачи'}</p>`}</div>`;
  mappingMountPicker(picker,chip);
}
function mappingMountPicker(picker,chip,preferredWidth=340,preferredHeight=340){
  chip.setAttribute('aria-expanded','true');chip.setAttribute('aria-controls',picker.id);
  $('#season-mapping-editor').append(picker);
  picker.showPopover?.();
  picker.positionPicker=()=>{
  const rect=chip.getBoundingClientRect(),gap=8,edge=12;
  const viewportWidth=document.documentElement.clientWidth||window.innerWidth,viewportHeight=document.documentElement.clientHeight||window.innerHeight;
  const width=Math.min(preferredWidth,viewportWidth-edge*2);
  picker.style.width=`${width}px`;
  const below=viewportHeight-rect.bottom-gap-edge,above=rect.top-gap-edge;
  const upward=below<220&&above>below;
  picker.style.maxHeight=`${Math.max(80,Math.min(preferredHeight,upward?above:below))}px`;
  picker.style.left=`${Math.max(edge,Math.min(rect.left,viewportWidth-width-edge))}px`;
  picker.style.top=`${(upward?rect.top-gap-picker.getBoundingClientRect().height:rect.bottom+gap)}px`;
  };picker.positionPicker();
  const options={signal:picker.mappingEvents.signal};
  window.addEventListener('resize',mappingClosePicker,options);
  document.addEventListener('scroll',event=>{if(!picker.contains(event.target))mappingClosePicker();},{...options,capture:true});
  $('#modal').addEventListener('close',mappingClosePicker,options);
  picker.querySelector('button')?.focus({preventScroll:true});
}
async function mappingExistingReleases(chip){
  if(chip.getAttribute('aria-expanded')==='true'){mappingClosePicker();return;}
  mappingClosePicker();
  const draft=seasonMapping,picker=document.createElement('div');
  picker.id='mapping-release-picker';picker.className='mapping-episode-picker mapping-release-picker';
  picker.setAttribute('popover','manual');picker.setAttribute('role','region');picker.setAttribute('aria-label','Существующие раздачи');
  picker.mappingChip=chip;picker.mappingEvents=new AbortController();
  picker.innerHTML='<div class="mapping-picker-heading">Существующие раздачи</div><div class="mapping-release-options" role="status">Загрузка раздач…</div>';
  mappingMountPicker(picker,chip,760,560);
  try{
    const choices=await api(`/tasks/${draft.task}/seasons/${draft.season}/candidates`);
    if(!picker.isConnected||!mappingEditorActive(draft))return;
    draft.candidateChoices=choices;
    picker.querySelector('.mapping-release-options').innerHTML=choices.map(choice=>{
      const added=draft.data.releases.some(release=>release.id===choice.release_id)&&!draft.hiddenReleaseIds.includes(choice.release_id);
      return `<article class="candidate mapping-release-choice" ${added?'':`data-mapping-candidate="${choice.id}"`}><div class="candidate-report-heading"><h3><a href="${esc(choice.candidate.url)}" target="_blank" rel="noopener noreferrer">${esc(choice.candidate.title)} ↗</a></h3>${candidateReportButton(choice.id,'season')}</div><p class="fine">${esc(choice.candidate.provider)} · ${bytes(choice.candidate.size)} · ${choice.candidate.seeds??'—'} сидов</p><p>Сопоставлено в этом сезоне: <strong>${choice.matched} из ${choice.total}</strong></p><button type="button" class="ghost" ${added?'disabled':''}>${added?'Уже добавлена':'Добавить файлы'}</button></article>`;
    }).join('')||'<p class="mapping-picker-empty">Пока нет раздач для сезона. Запустите поиск задачи или добавьте раздачу вручную.</p>';
    picker.positionPicker();
  }catch(error){if(picker.isConnected)picker.querySelector('.mapping-release-options').textContent=error.message;}
}
function mappingPositionCell(row){
  const p=row.special_position||{mode:'auto'};
  const kind=row.position_kind||(p.mode==='auto'?'auto':p.airsafter_season?'after':p.airsbefore_episode?'episode':p.airsbefore_season?'before':'none');
  const seasons=seasonMapping.data.placement_seasons||[];
  const selected=p.airsafter_season||p.airsbefore_season;
  const select=(field,label,options,value)=>`<select data-mapping-position="${row.subtask_id}" data-position-field="${field}" aria-label="${label} спецэпизода ${row.number}">${options.map(([id,title])=>`<option value="${id}" ${String(id)===String(value)?'selected':''}>${esc(title)}</option>`).join('')}</select>`;
  const choices=[['auto','По дате выхода'],['none','Не задано'],['before','До сезона'],['after','После сезона'],['episode','До конкретного эпизода']];
  const automatic=seasonMapping.data.episodes.find(ep=>ep.subtask_id===row.subtask_id)?.special_position?.automatic||{};
  const description=automatic.airsafter_season?`После сезона ${automatic.airsafter_season}`:automatic.airsbefore_season?`До сезона ${automatic.airsbefore_season}${automatic.airsbefore_episode?', эпизода '+automatic.airsbefore_episode:''}`:'Недостаточно данных о датах';
  return `<td class="mapping-position">${select('kind','Порядок показа',choices,kind)}${['before','after','episode'].includes(kind)?select('season','Сезон',[['','Выберите сезон'],...seasons.map(s=>[s.number,`Сезон ${s.number}`])],selected):''}${kind==='episode'?select('episode','Эпизод',[['','Выберите эпизод'],...(seasons.find(s=>s.number===selected)?.episodes||[]).map(ep=>[ep.number,`Эпизод ${ep.number}`])],p.airsbefore_episode):''}${kind==='auto'?`<span class="fine">${description}</span>`:''}</td>`;
}
function renderSeasonMapping(){
  mappingClosePicker();
  const draft=seasonMapping;
  const used=new Set(draft.rows.flatMap(row=>row.release_id===null?[]:[row.video_index,...row.track_indices].filter(index=>index!==null).map(index=>mappingKey(row.release_id,index))));
  const pool=draft.data.releases.filter(release=>!draft.hiddenReleaseIds.includes(release.id)).map(release=>{
    const rank={video:0,audio:1,subtitle:2,other:3};
    const files=release.files.filter(file=>!used.has(mappingKey(release.id,file.index))).sort((a,b)=>rank[a.kind]-rank[b.kind]||a.path.localeCompare(b.path,'ru',{numeric:true}));
    return `<section class="mapping-release"><div class="mapping-release-heading"><h4>${esc(release.title)} <span class="fine">${files.length}</span><button type="button" class="mapping-release-remove" data-mapping-remove-release="${release.id}" aria-label="Убрать раздачу ${esc(release.title)}" title="Убрать из списка файлов">×</button></h4><button type="button" class="mapping-bug" data-mapping-report="${release.id}" title="Сообщить об ошибке сопоставления" aria-label="Сообщить об ошибке сопоставления: ${esc(release.title)}" ${draft.reportLoading?'disabled':''}>${draft.reportLoading===release.id?'<span class="mapping-spinner" aria-hidden="true"></span>':mappingBugIcon}</button></div><div class="mapping-chips">${files.map(file=>mappingChip(release,file)).join('')||'<span class="fine">-</span>'}</div></section>`;
  }).join('');
  const rows=draft.rows.map(row=>{
    const release=draft.data.releases.find(release=>release.id===row.release_id);
    const cell=kind=>{
      const files=release?.files.filter(file=>file.kind===kind&&(kind==='video'?row.video_index===file.index:row.track_indices.includes(file.index)))||[];
      return `<td class="mapping-drop${files.length?'':' mapping-drop-empty'}" data-mapping-row="${row.subtask_id}" data-mapping-kind="${kind}" tabindex="0" role="button" aria-label="${{video:'Видео',audio:'Аудио',subtitle:'Субтитры'}[kind]} эпизода ${row.number}"><div class="mapping-chips">${files.map(file=>mappingChip(release,file,true)).join('')||'<span class="mapping-placeholder">Перенесите файлы сюда</span>'}</div></td>`;
    };
    return `<tr><td><div class="mapping-episode-fields"><input class="mapping-number" type="number" min="1" max="10000" step="1" aria-label="${row.subtask_id<0?'Номер нового эпизода':'Номер эпизода '+row.number}" data-mapping-number="${row.subtask_id}" value="${row.number}"><input aria-label="Имя эпизода ${row.number}" data-mapping-title="${row.subtask_id}" maxlength="500" value="${esc(row.title)}"><button type="button" class="mapping-delete-episode" data-mapping-delete-episode="${row.subtask_id}" aria-label="Удалить эпизод ${esc(row.number)}" title="Удалить серию">×</button></div></td>${cell('video')}${cell('audio')}${draft.season===0?mappingPositionCell(row):''}${cell('subtitle')}</tr>`;
  }).join('');
  openModal(`Сопоставить файлы · Сезон ${draft.season}`,`<div id="season-mapping-editor" aria-busy="${draft.busy}">
    <section class="mapping-bank" data-mapping-pool tabindex="0" aria-label="Несопоставленные файлы"><div class="mapping-bank-heading"><input class="mapping-season-title" id="mapping-season-title" aria-label="Название сезона" title="Название сезона" maxlength="500" value="${esc(draft.seasonTitle)}" placeholder="Название сезона"><label class="mapping-smart" title="При переносе видео автоматически добавляются связанные аудиофайлы и субтитры из той же раздачи. Неоднозначные совпадения нужно сопоставить вручную."><input id="mapping-smart" type="checkbox" ${draft.smart?'checked':''}> Smart-режим</label><button type="button" class="ghost" id="mapping-toggle-release">${draft.addingRelease?'<span class="mapping-spinner" aria-hidden="true"></span> Получаю файлы…':'Вручную добавить раздачу'}</button><button type="button" class="ghost" id="mapping-existing-release" aria-expanded="false">Выбрать раздачу из существующих</button></div>
    <div class="mapping-release-list">${pool||'<p class="fine">Добавьте раздачу, чтобы начать сопоставление.</p>'}</div></section>
    <p id="mapping-feedback" class="form-error" role="status"></p>
    <div class="mapping-table-scroll"><table class="mapping-table"><thead><tr><th>Эпизод</th><th>Видеофайл</th><th>Аудиофайлы</th>${draft.season===0?'<th>Порядок показа</th>':''}<th>Субтитры</th></tr></thead><tbody>${rows}<tr class="mapping-add-row"><td colspan="${draft.season===0?5:4}"><button type="button" class="ghost" id="mapping-add-episode" aria-label="Добавить эпизод" title="Добавить эпизод">+</button></td></tr></tbody></table></div>
    <div class="form-footer"><p class="fine">Изменения и удаление серий применяются при сохранении.</p><button type="button" class="primary" id="mapping-save">Сохранить сопоставление</button></div></div>`);
  if(draft.busy)$$('button,input,select', $('#season-mapping-editor')).forEach(node=>node.disabled=true);
}
function mappingEditorActive(draft){return seasonMapping===draft&&$('#modal').open&&Boolean($('#season-mapping-editor'));}
function mappingFind(key){
  const [releaseId,index]=key.split(':').map(Number);
  const release=seasonMapping.data.releases.find(release=>release.id===releaseId);
  return {release,file:release?.files.find(file=>file.index===index)};
}
function mappingUnassign(key,subtaskId=null){
  const {release,file}=mappingFind(key);
  if(!file)return;
  for(const row of seasonMapping.rows){
    if(row.release_id!==release.id||(subtaskId!==null&&row.subtask_id!==subtaskId))continue;
    if(row.video_index===file.index){row.release_id=null;row.video_index=null;row.track_indices=[];}
    else row.track_indices=row.track_indices.filter(index=>index!==file.index);
  }
}
function mappingPlace(key,target){
  const draft=seasonMapping;if(!draft||draft.busy)return;
  const {release,file}=mappingFind(key);if(!file||file.kind==='other')return;
  const row=draft.rows.find(row=>row.subtask_id===Number(target.dataset.mappingRow));
  if(!row)return;
  const fail=message=>{$('#mapping-feedback').textContent=message;};
  if(file.kind!==target.dataset.mappingKind)return fail('Перенесите файл в колонку соответствующего типа.');
  if(file.kind!=='video'&&(row.video_index===null||row.release_id!==release.id))return fail('Сначала назначьте видео из этой же раздачи: одна сабтаска использует одну раздачу.');
  if(file.kind==='video'){
    if(row.release_id===release.id&&row.video_index===file.index)return;
    mappingUnassign(key);
    row.release_id=release.id;row.video_index=file.index;row.track_indices=[];
    if(draft.smart){
      for(const index of file.related){
        const occupied=draft.rows.some(other=>other!==row&&other.release_id===release.id&&other.track_indices.includes(index));
        if(!occupied)row.track_indices.push(index);
      }
    }
  }else{
    mappingUnassign(key);row.track_indices.push(file.index);
  }
  draft.selected=null;renderSeasonMapping();
}
document.addEventListener('input',event=>{
  if(event.target.id==='mapping-season-title'&&seasonMapping)seasonMapping.seasonTitle=event.target.value;
  if(event.target.dataset.mappingNumber&&seasonMapping){
    const row=seasonMapping.rows.find(row=>row.subtask_id===Number(event.target.dataset.mappingNumber));
    const oldTitle=`Эпизод ${row.number}`;row.number=event.target.value===''?'':Number(event.target.value);
    if(row.title===oldTitle){row.title=`Эпизод ${row.number}`;$(`[data-mapping-title="${row.subtask_id}"]`).value=row.title;}
  }
  if(event.target.dataset.mappingTitle&&seasonMapping){const row=seasonMapping.rows.find(row=>row.subtask_id===Number(event.target.dataset.mappingTitle));row.title=event.target.value;}
});
document.addEventListener('change',event=>{
  const input=event.target;
  if(!input.dataset.mappingPosition||!seasonMapping||seasonMapping.busy)return;
  const row=seasonMapping.rows.find(row=>row.subtask_id===Number(input.dataset.mappingPosition));
  const current=row.special_position||{};
  row.position_kind=row.position_kind||(current.mode==='auto'?'auto':current.airsafter_season?'after':current.airsbefore_episode?'episode':current.airsbefore_season?'before':'none');
  const field=input.dataset.positionField,value=input.value;
  if(field==='kind'){
    row.position_kind=value;
    const season=current.airsbefore_season||current.airsafter_season||seasonMapping.data.placement_seasons?.[0]?.number;
    const episode=current.airsbefore_episode||seasonMapping.data.placement_seasons?.find(s=>s.number===season)?.episodes?.[0]?.number;
    row.special_position=value==='auto'?{mode:'auto'}:value==='none'?{mode:'manual'}:value==='after'?{mode:'manual',airsafter_season:season}:{mode:'manual',airsbefore_season:season,...(value==='episode'?{airsbefore_episode:episode}: {})};
  }else if(field==='season'){
    const key=(row.position_kind==='after'||current.airsafter_season)?'airsafter_season':'airsbefore_season';
    current[key]=Number(value)||null;
    if(row.position_kind==='episode'||current.airsbefore_episode)current.airsbefore_episode=seasonMapping.data.placement_seasons.find(s=>s.number===Number(value))?.episodes?.[0]?.number;
  }else current.airsbefore_episode=Number(value)||null;
  renderSeasonMapping();
});
document.addEventListener('change',event=>{if(event.target.id==='mapping-smart'&&seasonMapping)seasonMapping.smart=event.target.checked;});
document.addEventListener('dragstart',event=>{
  const chip=event.target.closest('[data-mapping-file]');if(!chip||seasonMapping?.busy)return;
  mappingClosePicker();
  event.dataTransfer.setData('application/x-lazarr-file',chip.dataset.mappingFile);event.dataTransfer.effectAllowed='move';
});
document.addEventListener('dragover',event=>{if(event.target.closest('.mapping-drop,[data-mapping-pool]')){event.preventDefault();event.dataTransfer.dropEffect='move';}});
document.addEventListener('drop',event=>{
  const target=event.target.closest('.mapping-drop,[data-mapping-pool]');if(!target||!seasonMapping||seasonMapping.busy)return;
  event.preventDefault();const key=event.dataTransfer.getData('application/x-lazarr-file');if(!key)return;
  if(target.hasAttribute('data-mapping-pool')){mappingUnassign(key);renderSeasonMapping();}else mappingPlace(key,target);
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&$('.mapping-episode-picker')){event.preventDefault();mappingClosePicker()?.focus();return;}
  if((event.key==='Enter'||event.key===' ')&&event.target.matches('.mapping-drop,[data-mapping-pool]')){event.preventDefault();event.target.click();}
});
document.addEventListener('click',async event=>{
  if(!event.target.closest('.mapping-episode-picker,[data-mapping-file],#mapping-existing-release'))mappingClosePicker();
  const opener=event.target.closest('[data-season-mapping]');
  if(opener){try{opener.disabled=true;await seasonMappingDialog(Number(opener.dataset.seasonMapping),Number(opener.dataset.seasonNumber));}catch(exc){toast(exc.message,true);}finally{opener.disabled=false;}return;}
  if(!event.target.closest('#season-mapping-editor')||!seasonMapping||seasonMapping.busy)return;
  if(event.target.closest('#mapping-existing-release')){await mappingExistingReleases(event.target.closest('button'));return;}
  const removeRelease=event.target.closest('[data-mapping-remove-release]');
  if(removeRelease){const id=Number(removeRelease.dataset.mappingRemoveRelease);seasonMapping.hiddenReleaseIds.push(id);for(const row of seasonMapping.rows){if(row.release_id===id){row.release_id=null;row.video_index=null;row.track_indices=[];}}seasonMapping.selected=null;renderSeasonMapping();return;}
  const candidate=event.target.closest('[data-mapping-candidate]');
  if(candidate&&!event.target.closest('a,[data-candidate-report]')){await mappingAddRelease({candidate_id:Number(candidate.dataset.mappingCandidate)});return;}
  const reportButton=event.target.closest('[data-mapping-report]');
  if(reportButton){
    const draft=seasonMapping;if(draft.reportLoading)return;
    draft.reportLoading=Number(reportButton.dataset.mappingReport);renderSeasonMapping();
    try{
      const report=await api(`/tasks/${draft.task}/seasons/${draft.season}/mapping/releases/${draft.reportLoading}/report`,'POST',{});
      if(mappingEditorActive(draft)){
        draft.reportLoading=null;renderSeasonMapping();showMappingReport(report,draft);
      }
    }catch(error){if(mappingEditorActive(draft))toast(error.message,true);}
    finally{draft.reportLoading=null;if(mappingEditorActive(draft)&&!$('#mapping-report-dialog'))renderSeasonMapping();}
    return;
  }
  const deleteEpisode=event.target.closest('[data-mapping-delete-episode]');
  if(deleteEpisode){
    const identity=Number(deleteEpisode.dataset.mappingDeleteEpisode);
    if(identity>0)seasonMapping.deletedSubtaskIds.push(identity);
    seasonMapping.rows=seasonMapping.rows.filter(row=>row.subtask_id!==identity);
    seasonMapping.selected=null;renderSeasonMapping();return;
  }
  if(event.target.closest('#mapping-add-episode')){
    const number=Math.max(0,...seasonMapping.rows.map(row=>Number(row.number)))+1;
    const subtask_id=Math.min(0,...seasonMapping.rows.map(row=>row.subtask_id))-1;
    seasonMapping.rows.push({subtask_id,number,title:`Эпизод ${number}`,release_id:null,video_index:null,track_indices:[]});
    renderSeasonMapping();const input=$(`[data-mapping-number="${subtask_id}"]`);input.focus();input.scrollIntoView({block:'center'});return;
  }
  if(event.target.closest('#mapping-toggle-release')){const url=await mappingPromptUrl(seasonMapping.releaseUrl);if(url===null||!url.trim())return;seasonMapping.releaseUrl=url.trim();await mappingAddRelease({url:seasonMapping.releaseUrl});return;}
  const remove=event.target.closest('[data-mapping-remove]');
  if(remove){mappingUnassign(remove.dataset.mappingRemove,Number(remove.closest("[data-mapping-row]").dataset.mappingRow));renderSeasonMapping();return;}
  const chip=event.target.closest('[data-mapping-file]');
  if(chip?.closest('[data-mapping-pool]')){mappingTogglePicker(chip);return;}
  if(chip){seasonMapping.selected=seasonMapping.selected===chip.dataset.mappingFile?null:chip.dataset.mappingFile;renderSeasonMapping();return;}
  const episode=event.target.closest('[data-mapping-episode]');
  if(episode){
    const key=episode.dataset.mappingKey,{file}=mappingFind(key);
    const row=seasonMapping.rows.find(row=>row.subtask_id===Number(episode.dataset.mappingEpisode));
    if(!row||(file.kind==='video'&&row.video_index!==null))return;
    mappingPlace(key,{dataset:{mappingRow:episode.dataset.mappingEpisode,mappingKind:file.kind}});
    $(`[data-mapping-row="${row.subtask_id}"][data-mapping-kind="${file.kind}"] [data-mapping-file="${key}"]`)?.focus();return;
  }
  const target=event.target.closest('.mapping-drop');
  if(target&&seasonMapping.selected){mappingPlace(seasonMapping.selected,target);return;}
  if(event.target.closest('[data-mapping-pool]')&&seasonMapping.selected&&!event.target.closest('label,input,form,.mapping-manual-release')){mappingUnassign(seasonMapping.selected);seasonMapping.selected=null;renderSeasonMapping();return;}
  if(event.target.id==='mapping-save'){
    const draft=seasonMapping;
    if(draft.rows.some(row=>!row.title.trim())){$('#mapping-feedback').textContent='Укажите имя каждого эпизода.';return;}
    const numbers=draft.rows.map(row=>Number(row.number));
    if(numbers.some(number=>!Number.isInteger(number)||number<1||number>10000)||new Set(numbers).size!==numbers.length){$('#mapping-feedback').textContent='Укажите уникальные номера эпизодов от 1 до 10000.';return;}
    if(draft.season===0&&draft.rows.some(row=>{const p=row.special_position||{};return (['before','after','episode'].includes(row.position_kind)&&!(p.airsbefore_season||p.airsafter_season))||(row.position_kind==='episode'&&!p.airsbefore_episode);})){$('#mapping-feedback').textContent='Выберите сезон и, при необходимости, эпизод для порядка показа.';return;}
    draft.busy=true;renderSeasonMapping();
    try{
      for(const row of draft.rows.filter(row=>row.subtask_id<0)){
        const restored=draft.data.episodes.find(episode=>draft.deletedSubtaskIds.includes(episode.subtask_id)&&episode.number===row.number);
        if(restored){row.subtask_id=restored.subtask_id;draft.deletedSubtaskIds=draft.deletedSubtaskIds.filter(id=>id!==row.subtask_id);continue;}
        const created=await api(`/tasks/${draft.task}/seasons/${draft.season}/mapping/episodes`,'POST',{number:row.number,title:row.title.trim()});
        row.subtask_id=created.subtask_id;
      }
      await api(`/tasks/${draft.task}/seasons/${draft.season}/mapping`,'PUT',{pool_release_ids:draft.data.releases.filter(release=>!draft.hiddenReleaseIds.includes(release.id)).map(release=>release.id),deleted_subtask_ids:draft.deletedSubtaskIds,season_title:draft.seasonTitle,rows:draft.rows,revisions:Object.fromEntries(draft.data.releases.filter(release=>release.revision).map(release=>[release.id,release.revision]))});$('#modal').close();toast('Сопоставление сохранено');await refreshTasks();await refreshLibraryView();}
    catch(exc){draft.busy=false;if(mappingEditorActive(draft)){renderSeasonMapping();$('#mapping-feedback').textContent=exc.message;}}
  }
});
async function mappingAddRelease(payload){
  const draft=seasonMapping;if(!draft||draft.busy)return;
  draft.busy=true;draft.addingRelease=true;renderSeasonMapping();
  try{
    const result=await api(`/tasks/${draft.task}/seasons/${draft.season}/mapping/releases`,'POST',payload);
    const updated=await api(`/tasks/${draft.task}/seasons/${draft.season}/mapping`);
    const remapped=new Map(draft.rows.map(row=>{
      if(row.release_id===null)return [row.subtask_id,row];
      const previous=draft.data.releases.find(release=>release.id===row.release_id);
      const current=updated.releases.find(release=>release.id===row.release_id);
      const translate=index=>{
        const file=previous?.files.find(file=>file.index===index);
        const next=current?.files.find(next=>next.path===file?.path&&next.size===file?.size);
        if(!next)throw new Error('Список файлов изменился. Откройте редактор заново, чтобы загрузить актуальное сопоставление.');
        return next.index;
      };
      return [row.subtask_id,{...row,video_index:row.video_index===null?null:translate(row.video_index),track_indices:row.track_indices.map(translate)}];
    }));
    const added=result.release_id??(payload.candidate_id?draft.candidateChoices?.find(choice=>choice.id===payload.candidate_id)?.release_id:updated.releases.find(release=>release.url===payload.url)?.id);
    draft.hiddenReleaseIds=draft.hiddenReleaseIds.filter(id=>id!==added);
    draft.rows=[...mappingRows(updated).filter(row=>!draft.deletedSubtaskIds.includes(row.subtask_id)).map(row=>remapped.get(row.subtask_id)||row),...draft.rows.filter(row=>row.subtask_id<0).map(row=>remapped.get(row.subtask_id))];draft.data=updated;
    draft.busy=false;draft.addingRelease=false;draft.releaseUrl='';
    if(mappingEditorActive(draft))renderSeasonMapping();toast('Файлы раздачи готовы для сопоставления');
  }catch(exc){draft.busy=false;draft.addingRelease=false;if(mappingEditorActive(draft)){renderSeasonMapping();$('#mapping-feedback').textContent=exc.message;}}
}

function showMappingReport(report,draft){
  const dialog=document.createElement('dialog');dialog.id='mapping-report-dialog';
  const searchReport=report.issue_type==='search_flow';
  const release=report.release||{title:report.task.media.title};
  const heading=searchReport?'Сообщить о некорректном ходе поиска':'Сообщить об ошибке сопоставления';
  const source={live:'Актуальное описание получено от провайдера.',cache:'Провайдер не вернул описание. В отчёте сохранённая копия.',unavailable:'Описание раздачи получить не удалось. Остальные данные включены в отчёт.'}[release.description_source];
  dialog.innerHTML=`<div class="modal-heading"><h2>${heading}</h2><button type="button" class="icon-button" data-report-close aria-label="Закрыть отчёт">×</button></div><p><strong>${esc(release.title)}</strong></p><p class="fine">${searchReport?'Отчёт относится к ходу поиска раздач.':esc(release.provider_name)+' · '+esc(source||'')}</p><label>Комментарий<textarea id="mapping-report-comment" rows="4" placeholder="Что сопоставилось неправильно и какой результат ожидался?"></textarea></label><p class="fine">Скачайте JSON-отчёт и приложите его к Issue на GitHub. ${searchReport?'В нём описание задачи, состояние поиска, провайдеры и версия движка.':'В нём описание задачи, файлы, описание раздачи и результаты сопоставления.'} Логи не включены.</p><div class="form-footer"><a class="ghost" id="mapping-report-download" href="#" download="lazarr-search-report.json">Скачать отчёт</a><a class="primary" id="mapping-report-issue" target="_blank" rel="noopener noreferrer">Открыть Issue</a></div>`;
  let objectUrl=null;
  const issue=dialog.querySelector('#mapping-report-issue');
  const comment=dialog.querySelector('textarea');
  if(searchReport){comment.placeholder='Что произошло при поиске и какой результат ожидался?';dialog.querySelector('#mapping-report-download').download='lazarr-search-flow-report.json';}
  function issueLink(){
    let body=`## Комментарий\n${comment.value.trim()||'<!-- Что сопоставилось неправильно и какой результат ожидался? -->'}\n\n## Раздача\nURL: ${release.url}\nНазвание: ${release.title}\nПровайдер: ${release.provider_name} (${release.provider})`;
    if(searchReport)body=`## Комментарий\n${comment.value.trim()||'<!-- Что произошло при поиске и какой результат ожидался? -->'}\n\n## Тип проблемы\nIssue относится именно к некорректному ходу поиска раздач.\n\n## Произведение\n${release.title}`;
    issue.href='https://github.com/Drun555/lazarr-search-engine/issues/new?'+new URLSearchParams({title:`${searchReport?'Некорректный ход поиска':'Ошибка сопоставления'}: ${release.title.slice(0,160)}`,body});
  }
  comment.addEventListener('input',issueLink);issueLink();
  dialog.querySelector('#mapping-report-download').addEventListener('click',event=>{
    if(objectUrl)URL.revokeObjectURL(objectUrl);
    const contents={...report,comment:comment.value,...(draft?{editor_draft:{saved:false,season:draft.season,episodes:draft.rows.map(row=>({...row,track_indices:[...row.track_indices]}))}}:{})};
    objectUrl=URL.createObjectURL(new Blob([JSON.stringify(contents,null,2)],{type:'application/json;charset=utf-8'}));
    event.currentTarget.href=objectUrl;
  });
  dialog.querySelector('[data-report-close]').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{if(objectUrl)URL.revokeObjectURL(objectUrl);dialog.remove();});
  document.body.append(dialog);dialog.showModal();
}

function candidateReportButton(identity,scope){
  return `<button type="button" class="mapping-bug" data-candidate-report="${identity}" data-report-scope="${scope}" title="Сообщить об ошибке сопоставления" aria-label="Сообщить об ошибке сопоставления">${mappingBugIcon}</button>`;
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-candidate-report]');if(!button||button.disabled)return;
  const root=button.closest('.candidate');button.disabled=true;button.innerHTML='<span class="mapping-spinner" aria-hidden="true"></span>';
  try{
    const report=await api(`/candidates/${button.dataset.candidateReport}/report?scope=${button.dataset.reportScope}`,'POST',{});
    if(root.isConnected&&$('#modal').open&&!$('#mapping-report-dialog'))showMappingReport(report,null);
  }catch(error){if(root.isConnected)toast(error.message,true);}
  finally{button.disabled=false;button.innerHTML=mappingBugIcon;}
});

function searchReportButton(taskId){
  return `<button type="button" class="mapping-bug" data-search-report="${taskId}" title="Сообщить о некорректном ходе поиска" aria-label="Сообщить о некорректном ходе поиска">${mappingBugIcon}</button>`;
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-search-report]');if(!button)return;
  event.preventDefault();event.stopPropagation();if(button.disabled)return;
  const task=button.dataset.searchReport;button.disabled=true;button.innerHTML='<span class="mapping-spinner" aria-hidden="true"></span>';
  try{
    const report=await api(`/tasks/${task}/search/report`,'POST',{});
    if(document.querySelector(`[data-search-report="${task}"]`)&&!$('#mapping-report-dialog'))showMappingReport(report,null);
  }catch(error){toast(error.message,true);}
  finally{button.disabled=false;button.innerHTML=mappingBugIcon;}
});
