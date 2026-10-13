const $ = id => document.getElementById(id);
let connected = false;
let authState = 'not_authorized';
let authDelivery = null;
let jobsFingerprint = '';
let pendingDeletion = null;
const statusNames = {queued:'В очереди',running:'Чтение истории',downloading:'Загрузка файлов',writing:'Создание архива',packaging:'Упаковка ZIP',completed:'Готово',completed_with_warnings:'Готово с замечаниями',paused:'Приостановлено',cancelled:'Отменено',failed:'Ошибка',waiting_auth:'Нужен вход',waiting_storage:'Недостаточно места',waiting_rate_limit:'Ожидание Telegram'};
if (location.hash.startsWith('#token=')) {
  $('token').value = decodeURIComponent(location.hash.slice(7));
  history.replaceState(null, '', location.pathname);
}
if (!$('token').value) $('token').value = sessionStorage.getItem('tgx_owner_token') || '';
if ($('token').value) sessionStorage.setItem('tgx_owner_token', $('token').value);
$('serverdetails').open=!$('token').value;
async function api(path, method='GET', body) {
  const response = await fetch('/api/v1/' + path, {method, headers:{Authorization:'Bearer '+$('token').value,'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  const content = await response.json().catch(()=>({detail:'Сервер не ответил. Проверьте, запущен ли TGX.'}));
  if (!response.ok) throw Error(typeof content.detail==='string'?content.detail:'Проверьте введённые данные.');
  return content;
}
function setNotice(target, text, error=false) { $(target).textContent=text; $(target).classList.toggle('error',error); }
async function action(button, target, work, loading) {
  const old=button.textContent;const busyText=loading||'Подождите…';button.disabled=true;button.textContent=busyText;
  setNotice(target,'');
  try { await work(); } catch(e) {
    setNotice(target,e.message,true);
    if(target==='authnotice') { $('authstatus').classList.add('error');$('authtitle').textContent='Не удалось завершить вход';$('authdetail').textContent=e.message; }
  } finally {button.disabled=false;if(button.textContent===busyText)button.textContent=old;}
}
function renderAuth(result) {
  authState=result.status;
  authDelivery=result.delivery||authDelivery;
  if(authState==='needs_api'||result.api_configured===false){$('apicredentials').open=true;$('authdetails').open=true;$('serverdetails').open=false;}
  $('authstatus').className='state'+(authState==='authorized'?' success':'');
  $('authbadge').className='tag'+(authState==='authorized'?' success':'');
  $('authbadge').textContent=authState==='authorized'?'Подключён':'Не подключён';
  $('loginform').hidden=authState==='authorized';$('codeform').hidden=!['waiting_code','waiting_password'].includes(authState);
  $('logout').hidden=authState!=='authorized';
  $('passwordfield').hidden=authState!=='waiting_password';$('codefield').hidden=authState==='waiting_password';
  $('qrform').hidden=authState!=='waiting_qr';
  if(result.qr_image)$('qrimage').src=result.qr_image;
  $('resend').hidden=authState!=='waiting_code';
  $('sendcode').hidden=['waiting_code','waiting_password','waiting_qr'].includes(authState);
  $('sendcode').textContent=authState==='waiting_code'?'Отправить код ещё раз':'Получить код';
  $('login').textContent=authState==='waiting_password'?'Подтвердить пароль':'Подтвердить код';
  const states={needs_api:['Настройте подключение к Telegram','Введите api_id и api_hash в раскрытых полях ниже, затем войдите по QR или номеру телефона.'],not_authorized:['Telegram пока не подключён','Введите номер и получите код подтверждения.'],waiting_code:['Telegram принял запрос кода','Введите полученный код ниже.'],waiting_qr:['Отсканируйте QR-код','В Telegram откройте Настройки → Устройства → Подключить устройство.'],waiting_password:['Код принят. Нужен пароль','Введите пароль двухэтапной проверки Telegram.'],authorized:['Вход выполнен','Профиль готов к выгрузке. Каналы загружаются автоматически.']};
  const [title,detail]=states[authState]||['Проверка входа','Проверяем состояние профиля…'];
  $('authtitle').textContent=title;$('authdetail').textContent=detail;
  if(authState==='waiting_code'&&authDelivery){const types={SentCodeTypeApp:'Telegram выбрал сообщение в приложении Telegram. Проверьте служебный чат Telegram на другом устройстве; это не SMS.',SentCodeTypeSms:'Telegram выбрал SMS на ваш номер.',SentCodeTypeCall:'Telegram выбрал телефонный звонок.',SentCodeTypeFlashCall:'Telegram выбрал проверочный звонок.',SentCodeTypeMissedCall:'Telegram выбрал пропущенный звонок.',SentCodeTypeEmailCode:'Telegram выбрал код по электронной почте.'};$('authdetail').textContent=types[authDelivery.type]||'Способ доставки, выбранный Telegram: '+authDelivery.type;$('codehelp').textContent=$('authdetail').textContent;updateResend();}
  $('authsteps').hidden=authState==='authorized';
  if(authState==='authorized'&&result.user){const u=result.user;$('authdetail').textContent=(u.name||'Telegram')+(u.username?' · @'+u.username:'')+' · ID '+u.id;$('code').value='';$('password').value='';$('phone').value='';$('apihash').value='';}
  $('stepcode').className=authState==='waiting_code'?'current':authState==='waiting_password'?'done':'';
  $('steppassword').className=authState==='waiting_password'?'current':'';
  $('stepaccount').className=['not_authorized','needs_api'].includes(authState)?'current':'done';
  $('accountdot').className='dot'+(authState==='authorized'?' ok':'');
  $('accountsummary').textContent=authState==='authorized'?'Telegram подключён':'Telegram не подключён';
  $('export').disabled=authState!=='authorized';
  if(authState==='waiting_code')$('code').focus();if(authState==='waiting_password')$('password').focus();
}
function updateResend(){if(authState!=='waiting_code'||!authDelivery)return;const seconds=Math.max(0,Math.ceil((authDelivery.retry_at||0)-Date.now()/1000));$('resend').disabled=!authDelivery.can_resend||seconds>0;$('resend').textContent=seconds>0?'Повторить через '+seconds+' с':authDelivery.can_resend?'Запросить следующий способ доставки':'Telegram не предложил повторную отправку';}
async function loadDialogs() {
  const ds=await api('dialogs?account='+encodeURIComponent($('account').value));
  $('dialogs').replaceChildren(...ds.map(d=>{const o=document.createElement('option');o.value=d.peer_id;o.label=d.title;return o;}));
  $('dialoghelp').textContent=ds.length+' диалогов загружено. Начните вводить ID или название из списка.';
}
async function checkAuth() {
  const profile=$('account').value;
  renderAuth({status:'checking'});$('export').disabled=true;
  for(const id of ['phone','code','password'])$(id).value='';
  $('peer').value='';$('dialogs').replaceChildren();
  $('dialoghelp').textContent='Загружаем диалоги выбранного аккаунта…';
  const profiles=await api('accounts');
  $('accountselect').replaceChildren(...[...new Set([...profiles,$('account').value])].map(name=>{const option=document.createElement('option');option.value=name;option.textContent=name;return option;}));
  $('accountselect').value=$('account').value;
  const r=await api('auth/status?account='+encodeURIComponent(profile));if($('account').value!==profile)return r;renderAuth(r);
  if(r.status==='authorized')await loadDialogs();else $('dialoghelp').textContent='Войдите в выбранный аккаунт, чтобы загрузить диалоги.';return r;
}
function makeButton(text, work) {const b=document.createElement('button');b.className='secondary small';b.textContent=text;b.onclick=()=>action(b,'jobnotice',work);return b;}
async function refreshJobs(force=false) {
  const jobs=await api('exports');
  const fingerprint=JSON.stringify(jobs);
  if(!force&&fingerprint===jobsFingerprint)return;
  jobsFingerprint=fingerprint;
  $('jobcount').textContent=jobs.length;
  $('joblist').replaceChildren();
  $('empty').hidden=jobs.length>0;
  for(const j of jobs) {
    const el=document.createElement('article');el.className='job';
    const top=document.createElement('div');top.className='job-title';
    const title=document.createElement('strong');title.textContent=j.title||'Определяем канал…';
    const state=document.createElement('span');state.className='tag'+(j.status==='completed'?' success':'');
    state.textContent=j.status==='deleting'?'Удаляется…':statusNames[j.status]||j.status;
    top.append(title,state);el.append(top);
    if(j.created_at){const date=document.createElement('p');date.className='job-date';date.textContent='Создано '+new Date(j.created_at).toLocaleString('ru-RU');el.append(date);}
    const meta=document.createElement('div');meta.className='job-meta';
    for(const text of [(j.messages_saved||0).toLocaleString('ru-RU')+' сообщений',(j.pages_read||0)+' страниц','Профиль '+(j.account_id||'main')]){const s=document.createElement('span');s.textContent=text;meta.append(s);}el.append(meta);
    if(j.status==='waiting_rate_limit'){
      const p=document.createElement('p');p.className='download-status';p.setAttribute('role','status');
      const seconds=Math.max(0,Math.ceil((Date.parse(j.retry_after||'')-Date.now())/1000));
      p.textContent=seconds>0?'Telegram ограничил частоту запросов. Продолжим автоматически через '+seconds+' сек.':'Время ожидания истекло. Автоматически продолжаем выгрузку…';el.append(p);
    }else if(j.last_error&&!['queued','running','downloading','writing','packaging'].includes(j.status)){const p=document.createElement('p');p.className='job-error';p.textContent=j.last_error;el.append(p);}
    const actions=document.createElement('div');actions.className='job-buttons';
    const active=['queued','running','downloading','writing','packaging'].includes(j.status);
    if(active)actions.append(makeButton('Пауза',async()=>{await api('exports/'+j.job_id+'/pause','POST');await refreshJobs(true);}));
    if(['paused','failed','waiting_auth','waiting_storage'].includes(j.status))actions.append(makeButton('Продолжить',async()=>{await api('exports/'+j.job_id+'/resume','POST');await refreshJobs(true);}));
    const remove=makeButton(active?'Отменить и удалить':'Удалить',async()=>{pendingDeletion=j.job_id;$('deletetext').textContent=j.title||'Задание выгрузки';$('deletedialog').showModal();});remove.classList.add('danger');remove.disabled=j.status==='deleting';actions.append(remove);
    if(['completed','completed_with_warnings','paused'].includes(j.status)){
      let format='jsonl';try{const options=JSON.parse(j.options||'{}');format=['full','selected'].includes(options.media)||options.format==='html'?'zip':options.format||'jsonl';}catch{}
      const download=makeButton('Скачать '+format.toUpperCase(),async()=>{
        const files=await api('exports/'+j.job_id+'/artifacts');
        const primary=files.find(f=>f.primary)||files.find(f=>!f.name.endsWith('.json')&&!f.name.includes('media-index'))||files[0];
        if(!primary){setNotice('jobnotice','Файл ещё не готов. Дождитесь завершения задания.');return;}
        const anchor=document.createElement('a');anchor.href=primary.download_url;anchor.download=primary.name;document.body.append(anchor);anchor.click();anchor.remove();
        download.textContent='Скачивание запрошено';
        status.textContent='Файл «'+primary.name+'» передан браузеру. Откройте «Загрузки» в панели браузера.'+(primary.name.endsWith('.zip')?' Распакуйте ZIP целиком и откройте index.html внутри папки.':' Обычно файл сохраняется в папку «Загрузки» на вашем компьютере.');
        let details=el.querySelector('.files');if(!details){details=document.createElement('div');details.className='files';el.append(details);}
        details.replaceChildren();const more=document.createElement('details');const summary=document.createElement('summary');summary.textContent='Другие файлы и контрольные суммы';more.append(summary);
        for(const f of files){if(f===primary||f.name.includes('media-index'))continue;const a=document.createElement('a');a.href=f.download_url;a.download=f.name.split('/').pop();a.textContent=f.name+' ('+(f.size/1024).toFixed(0)+' КБ)';more.append(a);}details.append(more);
      });
      download.classList.add('download-primary');actions.append(download);
      const status=document.createElement('p');status.className='download-status';status.setAttribute('role','status');
      el.append(actions,status);
    }else el.append(actions);
    $('joblist').append(el);
  }
}
$('connect').onclick=()=>action($('connect'),'authnotice',async()=>{sessionStorage.setItem('tgx_owner_token',$('token').value);const accounts=await api('accounts');$('profiles').replaceChildren(...accounts.map(a=>{const o=document.createElement('option');o.value=a;return o;}));connected=true;await checkAuth();await refreshJobs();},'Проверяем подключение…');
$('sendcode').onclick=()=>action($('sendcode'),'authnotice',async()=>{if(!$('phone').value.trim())throw Error('Укажите номер телефона с кодом страны.');const body={account:$('account').value,phone:$('phone').value};if($('apiid').value||$('apihash').value){body.api_id=Number($('apiid').value);body.api_hash=$('apihash').value;}const r=await api('auth/login','POST',body);$('apihash').value='';connected=true;renderAuth(r);if(r.status==='authorized')await checkAuth();},'Отправляем код…');
$('login').onclick=()=>action($('login'),'authnotice',async()=>{const r=await api('auth/login','POST',{account:$('account').value,code:authState==='waiting_code'?$('code').value:null,password:authState==='waiting_password'?$('password').value:null});renderAuth(r);if(r.status==='authorized'){await loadDialogs();await refreshJobs();$('authdetails').open=false;}},'Проверяем…');
$('resend').onclick=()=>action($('resend'),'authnotice',async()=>renderAuth(await api('auth/resend','POST',{account:$('account').value})),'Запрашиваем…');
$('qrlogin').onclick=()=>action($('qrlogin'),'authnotice',async()=>{const body={account:$('account').value};if($('apiid').value||$('apihash').value){body.api_id=Number($('apiid').value);body.api_hash=$('apihash').value;}const r=await api('auth/qr','POST',body);$('apihash').value='';renderAuth(r);connected=true;},'Готовим QR-код…');
$('inspect').onclick=()=>action($('inspect'),'exportnotice',async()=>{if(!$('peer').value)throw Error('Сначала выберите канал.');const r=await api('inspect?peer='+encodeURIComponent($('peer').value)+'&account='+encodeURIComponent($('account').value));$('probe').hidden=false;$('probe').textContent=r.history_readable?'История доступна. Найдено '+r.history_probe_count+' сообщений в проверочной странице. Защита контента: '+(r.content_protected?'включена':'выключена')+'.':'Проверка не завершена: '+(r.server_error?.message||'Telegram не вернул сообщения. Нужна дополнительная диагностика.');});
$('export').onclick=()=>action($('export'),'exportnotice',async()=>{if(!$('peer').value)throw Error('Выберите канал или введите его ID.');const options={mode:$('mode').value,media:$('media').value,format:$('format').value,min_id:Number($('minid').value)};if($('maxid').value)options.max_id=Number($('maxid').value);const r=await api('exports','POST',{account:$('account').value,peer:$('peer').value,options});setNotice('exportnotice','Выгрузка добавлена в очередь. Прогресс отображается в заданиях.');connected=true;await refreshJobs(true);},'Создаём задание…');
$('refresh').onclick=()=>action($('refresh'),'jobnotice',()=>refreshJobs(true));
$('deleteback').onclick=()=>$('deletedialog').close();
$('deleteconfirm').onclick=()=>action($('deleteconfirm'),'jobnotice',async()=>{await api('exports/'+pendingDeletion,'DELETE');$('deletedialog').close();pendingDeletion=null;await refreshJobs(true);},'Удаляем…');
$('logout').onclick=()=>$('logoutdialog').showModal();
$('logoutback').onclick=()=>$('logoutdialog').close();
$('logoutconfirm').onclick=()=>action($('logoutconfirm'),'authnotice',async()=>{const result=await api('auth/logout','POST',{account:$('account').value,forget_credentials:true});$('logoutdialog').close();for(const id of ['phone','code','password','apiid','apihash'])$(id).value='';$('profiles').replaceChildren();$('dialogs').replaceChildren();$('dialoghelp').textContent='Подключите аккаунт, чтобы загрузить диалоги.';$('authdetails').open=true;authDelivery=null;renderAuth({status:result.api_configured?'not_authorized':'needs_api',api_configured:result.api_configured});setNotice('authnotice','Вы вышли из Telegram. Данные входа удалены.');await refreshJobs(true);},'Завершаем сессию…');
$('account').onchange=()=>{if(connected)action($('connect'),'authnotice',checkAuth);};
$('accountselect').onchange=()=>{$('account').value=$('accountselect').value;$('account').onchange();};
$('addaccount').onclick=()=>{const names=[...$('accountselect').options].map(o=>o.value);let number=2;while(names.includes('account'+number))number++;$('account').value='account'+number;$('authdetails').open=true;$('account').onchange();$('account').focus();};
$('code').onkeydown=e=>{if(e.key==='Enter')$('login').click();};$('password').onkeydown=e=>{if(e.key==='Enter')$('login').click();};
setInterval(()=>{if(connected&&!document.hidden){refreshJobs().catch(e=>setNotice('jobnotice',e.message,true));if(authState==='waiting_qr')checkAuth().catch(e=>setNotice('authnotice',e.message,true));}},3000);
setInterval(updateResend,1000);
if($('token').value)$('connect').click();
