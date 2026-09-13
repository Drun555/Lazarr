'use strict';
// Shared by defaults, new tasks and task editing. Selected values stay canonical.
const languageAliases = JSON.parse(document.querySelector('#language-aliases')?.textContent || '{}');
let pickerSerial = 0;
function languagePicker(name, label, values, help) {
  const id = `languages-${++pickerSerial}`;
  const selected = [...new Set(values)];
  return `<div class="language-field"><label id="${id}-label" for="${id}-input">${esc(label)}</label><div class="language-picker" data-language-picker="${name}"><div class="language-values">${selected.map(code=>languagePill(name,code)).join('')}<input id="${id}-input" class="language-query" type="text" placeholder="Добавить язык…" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${id}-options" aria-labelledby="${id}-label" autocomplete="off" spellcheck="false"></div><div id="${id}-options" class="language-options" role="listbox" aria-labelledby="${id}-label" hidden></div></div><small>${help}</small></div>`;
}
function languagePill(name, code) {
  return `<span class="language-pill" data-language="${esc(code)}">${esc(languageName(code))}<input type="hidden" name="${esc(name)}" value="${esc(code)}"><button type="button" data-remove-language="${esc(code)}" aria-label="Убрать ${esc(languageName(code))}">×</button></span>`;
}
function languageSuggestions(picker) {
  const query = picker.querySelector('.language-query');
  const list = picker.querySelector('.language-options');
  const term = query.value.trim().toLocaleLowerCase();
  const chosen = new Set([...picker.querySelectorAll('input[type=hidden]')].map(input=>input.value));
  const codes = Object.keys(languageLabels).filter(code=>!chosen.has(code)&&Object.entries(languageAliases).some(([alias,c])=>c===code&&alias.includes(term)));
  picker.dataset.activeOption = '-1';
  list.innerHTML = codes.length ? codes.map(code=>`<button type="button" tabindex="-1" role="option" aria-selected="false" id="${list.id}-${code}" data-add-language="${code}">${esc(languageName(code))}</button>`).join('') : '<div class="language-empty">Нет подходящих языков</div>';
  list.hidden = false;
  query.setAttribute('aria-expanded','true');
  query.removeAttribute('aria-activedescendant');
}
function closeLanguages(picker) {
  picker.querySelector('.language-options').hidden = true;
  const input = picker.querySelector('.language-query');
  input.setAttribute('aria-expanded','false');
  input.removeAttribute('aria-activedescendant');
}
function addLanguage(picker,code) {
  const input = picker.querySelector('.language-query');
  if (!languageLabels[code]) return;
  if (![...picker.querySelectorAll('input[type=hidden]')].some(i=>i.value===code)) input.insertAdjacentHTML('beforebegin',languagePill(picker.dataset.languagePicker,code));
  input.value=''; input.setCustomValidity(''); input.focus(); languageSuggestions(picker);
}
function selectedLanguages(form,name) {
  const picker = [...form.querySelectorAll('[data-language-picker]')].find(p=>p.dataset.languagePicker===name);
  const input = picker.querySelector('.language-query');
  const draft = input.value.trim().toLocaleLowerCase();
  if(draft){
    const code = languageAliases[draft];
    if(!code){ input.setCustomValidity('Выберите язык из списка');input.reportValidity();throw new Error('Выберите язык из списка'); }
    addLanguage(picker,code); closeLanguages(picker);
  }
  return [...picker.querySelectorAll('input[type=hidden]')].map(i=>i.value);
}
document.addEventListener('focusin',event=>{
  const picker=event.target.closest('[data-language-picker]');
  document.querySelectorAll('[data-language-picker]').forEach(p=>{if(p!==picker)closeLanguages(p);});
  if(event.target.matches('.language-query')) languageSuggestions(picker);
});
document.addEventListener('input',event=>{if(event.target.matches('.language-query')){event.target.setCustomValidity('');languageSuggestions(event.target.closest('[data-language-picker]'));}});
document.addEventListener('click',event=>{
  const picker=event.target.closest('[data-language-picker]');
  if(!picker){document.querySelectorAll('[data-language-picker]').forEach(closeLanguages);return;}
  const add=event.target.closest('[data-add-language]');
  const remove=event.target.closest('[data-remove-language]');
  if(add) addLanguage(picker,add.dataset.addLanguage);
  if(remove){remove.closest('.language-pill').remove();picker.querySelector('.language-query').focus();languageSuggestions(picker);}
});
document.addEventListener('keydown',event=>{
  if(!event.target.matches('.language-query'))return;
  const input=event.target, picker=input.closest('[data-language-picker]'),list=picker.querySelector('.language-options');
  if(event.key==='Escape'){closeLanguages(picker);event.preventDefault();event.stopPropagation();return;}
  if(event.key==='Tab'){closeLanguages(picker);return;}
  if(event.key==='Backspace'&&!input.value){const pills=picker.querySelectorAll('.language-pill');pills[pills.length-1]?.remove();languageSuggestions(picker);return;}
  if(!['ArrowDown','ArrowUp','Enter'].includes(event.key))return;
  event.preventDefault();
  if(list.hidden)languageSuggestions(picker);
  const options=[...list.querySelectorAll('[data-add-language]')];
  if(!options.length)return;
  let active=Number(picker.dataset.activeOption);
  if(event.key==='Enter'){addLanguage(picker,options[Math.max(0,active)].dataset.addLanguage);return;}
  active=(active+(event.key==='ArrowDown'?1:-1)+options.length)%options.length;
  picker.dataset.activeOption=active;
  options.forEach((option,i)=>option.setAttribute('aria-selected',String(i===active)));
  input.setAttribute('aria-activedescendant',options[active].id);
  options[active].scrollIntoView({block:'nearest'});
});
