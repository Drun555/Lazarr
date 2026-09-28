/* Season-wide mapping draft. File identity includes the release, never just its index. */
let seasonMapping=null;
const mappingBugIcon='<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M9 7V5a3 3 0 0 1 6 0v2M8 3 6 1m10 2 2-2M5 10H2m3 5H2m17-5h3m-3 5h3M6 7 3 5m15 2 3-2M6 18l-3 3m15-3 3 3M12 8v13"/><rect x="5" y="7" width="14" height="14" rx="7"/></svg>';

const mappingKey=(release,index)=>`${release}:${index}`;
function mappingRows(data){
  return data.episodes.map(episode=>({subtask_id:episode.subtask_id,number:episode.number,title:episode.title,
    release_id:episode.release_id,video_index:episode.binding?.video_index??null,
    track_indices:(episode.binding?.tracks||[]).filter(track=>track.file_index!=null).map(track=>track.file_index)}));
}
async function seasonMappingDialog(task,season){
  const data=await api(`/tasks/${task}/seasons/${season}/mapping`);
  seasonMapping={task,season,data,rows:mappingRows(data),smart:true,selected:null,busy:false,addingRelease:false,releaseUrl:'',releaseOpen:false};
  renderSeasonMapping();
}
function mappingChip(release,file,assigned=false){
  const key=mappingKey(release.id,file.index),selected=seasonMapping.selected===key;
  return `<span class="mapping-chip-wrap"><button type="button" class="mapping-chip ${file.kind}${selected?' selected':''}" draggable="${file.kind!=='other'}" data-mapping-file="${key}" ${file.kind==='other'?'disabled':''} title="${esc(release.title+' / '+file.path)}" aria-pressed="${selected}"><span>${esc(file.path.split('/').pop())}${file.legacy?' · прежний файл':''}</span></button>${assigned?`<button type="button" class="mapping-remove" data-mapping-remove="${key}" aria-label="Вернуть ${esc(file.path)} в несопоставленные">×</button>`:''}</span>`;
}
function renderSeasonMapping(){
  const draft=seasonMapping;
  const used=new Set(draft.rows.flatMap(row=>row.release_id===null?[]:[row.video_index,...row.track_indices].filter(index=>index!==null).map(index=>mappingKey(row.release_id,index))));
  const pool=draft.data.releases.map(release=>{
    const rank={video:0,audio:1,subtitle:2,other:3};
    const files=release.files.filter(file=>!used.has(mappingKey(release.id,file.index))).sort((a,b)=>rank[a.kind]-rank[b.kind]||a.path.localeCompare(b.path,'ru',{numeric:true}));
    return `<section class="mapping-release"><div class="mapping-release-heading"><h4>${esc(release.title)} <span class="fine">${files.length}</span></h4><button type="button" class="mapping-bug" data-mapping-report="${release.id}" title="Сообщить об ошибке сопоставления" aria-label="Сообщить об ошибке сопоставления: ${esc(release.title)}" ${draft.reportLoading?'disabled':''}>${draft.reportLoading===release.id?'<span class="mapping-spinner" aria-hidden="true"></span>':mappingBugIcon}</button></div><div class="mapping-chips">${files.map(file=>mappingChip(release,file)).join('')||'<span class="fine">-</span>'}</div></section>`;
  }).join('');
  const rows=draft.rows.map(row=>{
    const release=draft.data.releases.find(release=>release.id===row.release_id);
    const cell=kind=>{
      const files=release?.files.filter(file=>file.kind===kind&&(kind==='video'?row.video_index===file.index:row.track_indices.includes(file.index)))||[];
      return `<td class="mapping-drop${files.length?'':' mapping-drop-empty'}" data-mapping-row="${row.subtask_id}" data-mapping-kind="${kind}" tabindex="0" role="button" aria-label="${{video:'Видео',audio:'Аудио',subtitle:'Субтитры'}[kind]} эпизода ${row.number}"><div class="mapping-chips">${files.map(file=>mappingChip(release,file,true)).join('')||'<span class="mapping-placeholder">Перенесите файлы сюда</span>'}</div></td>`;
    };
    return `<tr><td><div class="mapping-episode-fields"><input class="mapping-number" type="number" min="1" max="10000" step="1" aria-label="${row.subtask_id<0?'Номер нового эпизода':'Номер эпизода '+row.number}" data-mapping-number="${row.subtask_id}" value="${row.number}"><input aria-label="Имя эпизода ${row.number}" data-mapping-title="${row.subtask_id}" maxlength="500" value="${esc(row.title)}"></div></td>${cell('video')}${cell('audio')}${cell('subtitle')}</tr>`;
  }).join('');
  openModal(`Сопоставить файлы · Сезон ${draft.season}`,`<div id="season-mapping-editor" aria-busy="${draft.busy}">
    <section class="mapping-bank" data-mapping-pool tabindex="0" aria-label="Несопоставленные файлы"><div class="mapping-bank-heading"><label class="mapping-smart" title="При переносе видео автоматически добавляются связанные аудиофайлы и субтитры из той же раздачи. Неоднозначные совпадения нужно сопоставить вручную."><input id="mapping-smart" type="checkbox" ${draft.smart?'checked':''}> Smart-режим</label><button type="button" class="ghost" id="mapping-toggle-release" aria-expanded="${draft.releaseOpen}" aria-controls="mapping-add-release">Вручную добавить раздачу</button></div>
    <form id="mapping-add-release" ${draft.releaseOpen?'':'hidden'}><div class="inline-form"><label>URL раздачи<input name="url" type="url" required maxlength="2048" placeholder="https://…" value="${esc(draft.releaseUrl)}"></label><button type="submit" class="ghost">${draft.addingRelease?'<span class="mapping-spinner" aria-hidden="true"></span> Получаю файлы…':'Добавить раздачу'}</button></div><p class="form-error" role="status">${draft.addingRelease?'Получение файлов раздачи…':''}</p></form><div class="mapping-release-list">${pool||'<p class="fine">Добавьте раздачу, чтобы начать сопоставление.</p>'}</div></section>
    <p id="mapping-feedback" class="form-error" role="status"></p>
    <div class="mapping-table-scroll"><table class="mapping-table"><thead><tr><th>Эпизод</th><th>Видеофайл</th><th>Аудиофайлы</th><th>Субтитры</th></tr></thead><tbody>${rows}<tr class="mapping-add-row"><td colspan="4"><button type="button" class="ghost" id="mapping-add-episode" aria-label="Добавить эпизод" title="Добавить эпизод">+</button></td></tr></tbody></table></div>
    <div class="form-footer"><p class="fine">Изменения таблицы применяются при сохранении.</p><button type="button" class="primary" id="mapping-save">Сохранить сопоставление</button></div></div>`);
  if(draft.busy)$$('button,input', $('#season-mapping-editor')).forEach(node=>node.disabled=true);
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
  if(event.target.matches('#mapping-add-release input[name=url]')&&seasonMapping)seasonMapping.releaseUrl=event.target.value;
  if(event.target.dataset.mappingNumber&&seasonMapping){
    const row=seasonMapping.rows.find(row=>row.subtask_id===Number(event.target.dataset.mappingNumber));
    const oldTitle=`Эпизод ${row.number}`;row.number=event.target.value===''?'':Number(event.target.value);
    if(row.title===oldTitle){row.title=`Эпизод ${row.number}`;$(`[data-mapping-title="${row.subtask_id}"]`).value=row.title;}
  }
  if(event.target.dataset.mappingTitle&&seasonMapping){const row=seasonMapping.rows.find(row=>row.subtask_id===Number(event.target.dataset.mappingTitle));row.title=event.target.value;}
});
document.addEventListener('change',event=>{if(event.target.id==='mapping-smart'&&seasonMapping)seasonMapping.smart=event.target.checked;});
document.addEventListener('dragstart',event=>{
  const chip=event.target.closest('[data-mapping-file]');if(!chip||seasonMapping?.busy)return;
  event.dataTransfer.setData('application/x-lazarr-file',chip.dataset.mappingFile);event.dataTransfer.effectAllowed='move';
});
document.addEventListener('dragover',event=>{if(event.target.closest('.mapping-drop,[data-mapping-pool]')){event.preventDefault();event.dataTransfer.dropEffect='move';}});
document.addEventListener('drop',event=>{
  const target=event.target.closest('.mapping-drop,[data-mapping-pool]');if(!target||!seasonMapping||seasonMapping.busy)return;
  event.preventDefault();const key=event.dataTransfer.getData('application/x-lazarr-file');if(!key)return;
  if(target.hasAttribute('data-mapping-pool')){mappingUnassign(key);renderSeasonMapping();}else mappingPlace(key,target);
});
document.addEventListener('keydown',event=>{
  if((event.key==='Enter'||event.key===' ')&&event.target.matches('.mapping-drop,[data-mapping-pool]')){event.preventDefault();event.target.click();}
});
document.addEventListener('click',async event=>{
  const opener=event.target.closest('[data-season-mapping]');
  if(opener){try{opener.disabled=true;await seasonMappingDialog(Number(opener.dataset.seasonMapping),Number(opener.dataset.seasonNumber));}catch(exc){toast(exc.message,true);}finally{opener.disabled=false;}return;}
  if(!event.target.closest('#season-mapping-editor')||!seasonMapping||seasonMapping.busy)return;
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
  if(event.target.closest('#mapping-add-episode')){
    const number=Math.max(0,...seasonMapping.rows.map(row=>Number(row.number)))+1;
    const subtask_id=Math.min(0,...seasonMapping.rows.map(row=>row.subtask_id))-1;
    seasonMapping.rows.push({subtask_id,number,title:`Эпизод ${number}`,release_id:null,video_index:null,track_indices:[]});
    renderSeasonMapping();const input=$(`[data-mapping-number="${subtask_id}"]`);input.focus();input.scrollIntoView({block:'center'});return;
  }
  if(event.target.closest('#mapping-toggle-release')){seasonMapping.releaseOpen=!seasonMapping.releaseOpen;renderSeasonMapping();if(seasonMapping.releaseOpen)$('#mapping-add-release input').focus();return;}
  const remove=event.target.closest('[data-mapping-remove]');
  if(remove){mappingUnassign(remove.dataset.mappingRemove,Number(remove.closest("[data-mapping-row]").dataset.mappingRow));renderSeasonMapping();return;}
  const chip=event.target.closest('[data-mapping-file]');
  if(chip){seasonMapping.selected=seasonMapping.selected===chip.dataset.mappingFile?null:chip.dataset.mappingFile;renderSeasonMapping();return;}
  const target=event.target.closest('.mapping-drop');
  if(target&&seasonMapping.selected){mappingPlace(seasonMapping.selected,target);return;}
  if(event.target.closest('[data-mapping-pool]')&&seasonMapping.selected&&!event.target.closest('label,input,form,.mapping-manual-release')){mappingUnassign(seasonMapping.selected);seasonMapping.selected=null;renderSeasonMapping();return;}
  if(event.target.id==='mapping-save'){
    const draft=seasonMapping;
    if(draft.rows.some(row=>!row.title.trim())){$('#mapping-feedback').textContent='Укажите имя каждого эпизода.';return;}
    const numbers=draft.rows.map(row=>Number(row.number));
    if(numbers.some(number=>!Number.isInteger(number)||number<1||number>10000)||new Set(numbers).size!==numbers.length){$('#mapping-feedback').textContent='Укажите уникальные номера эпизодов от 1 до 10000.';return;}
    draft.busy=true;renderSeasonMapping();
    try{
      for(const row of draft.rows.filter(row=>row.subtask_id<0)){
        const created=await api(`/tasks/${draft.task}/seasons/${draft.season}/mapping/episodes`,'POST',{number:row.number,title:row.title.trim()});
        row.subtask_id=created.subtask_id;
      }
      await api(`/tasks/${draft.task}/seasons/${draft.season}/mapping`,'PUT',{rows:draft.rows,revisions:Object.fromEntries(draft.data.releases.filter(release=>release.revision).map(release=>[release.id,release.revision]))});$('#modal').close();toast('Сопоставление сохранено');await refreshTasks();await refreshLibraryView();}
    catch(exc){draft.busy=false;if(mappingEditorActive(draft)){renderSeasonMapping();$('#mapping-feedback').textContent=exc.message;}}
  }
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(form.id!=='mapping-add-release')return;
  event.preventDefault();const draft=seasonMapping;if(!draft||draft.busy)return;
  draft.releaseUrl=String(new FormData(form).get('url')).trim();
  draft.busy=true;draft.addingRelease=true;renderSeasonMapping();
  try{
    await api(`/tasks/${draft.task}/seasons/${draft.season}/mapping/releases`,'POST',{url:draft.releaseUrl});
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
    draft.rows=[...mappingRows(updated).map(row=>remapped.get(row.subtask_id)||row),...draft.rows.filter(row=>row.subtask_id<0).map(row=>remapped.get(row.subtask_id))];draft.data=updated;
    draft.busy=false;draft.addingRelease=false;draft.releaseUrl='';
    if(mappingEditorActive(draft))renderSeasonMapping();toast('Файлы раздачи готовы для сопоставления');
  }catch(exc){draft.busy=false;draft.addingRelease=false;if(mappingEditorActive(draft)){renderSeasonMapping();$('#mapping-add-release .form-error').textContent=exc.message;}}
});

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
