'use strict';
const $=id=>document.getElementById(id);
let snapshot=null,dirty=false,busy=false,nativeSnapshot=null;
const controls=new Map(),nativeControls=new Map();
// Presentation only: never reset inactive values or change runtime defaults.
const choices={
  speech_model:['openclaw-live-conversation:4b','qwen3.5:4b','qwen3.5:0.8b'],
  degraded_speech_model:['qwen3.5:0.8b','openclaw-live-conversation:4b','qwen3.5:4b'],
  qwen_tts_model:['Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice'],
  // https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice#supported-speakers
  qwen_tts_voice:['ryan','aiden','vivian','serena','uncle_fu','dylan','eric','ono_anna','sohee'],
};
function applicable(key){
  const value=k=>{const input=controls.get(k);return input?.type==='checkbox'?input.checked:input?.value;};
  if(['tts_speaker_id','tts_threads','tts_speed'].includes(key))return value('tts_backend')==='kokoro';
  if(key.startsWith('qwen_tts_')||key.startsWith('default_qwen_'))return value('tts_backend')==='qwen';
  if((key.startsWith('asr_vad_')&&key!=='asr_vad_filter')||['asr_min_speech_duration_ms','asr_min_silence_duration_ms','asr_speech_pad_final_ms','asr_speech_pad_partial_ms'].includes(key))return value('asr_vad_filter')===true;
  if(['recording_retention_days','recording_max_bytes'].includes(key))return value('audio_capture')===true;
  if(key==='backchannel_display_text')return value('spoken_backchannels_enabled')===true;
  return true;
}
function setValue(input,value){
  if(input.type==='checkbox')input.checked=value;
  else {if(input.tagName==='SELECT'&&![...input.options].some(o=>o.value===String(value)))input.add(new Option(String(value),String(value)));input.value=value;}
  input.syncChoice?.();
}

function status(message,error=false){$('status').textContent=message;$('status').className=error?'error':'success';}
function markDirty(){dirty=true;$('save').disabled=busy;filter();}
function inputFor(spec,value){let input;if(spec.type==='boolean'){input=document.createElement('input');input.type='checkbox';input.checked=value;}else if(spec.options){input=document.createElement('select');for(const option of spec.options){const el=document.createElement('option');el.value=String(option);el.textContent=String(option);input.append(el);}input.value=String(value);}else{input=document.createElement(spec.type==='string'&&(String(value).length>90||spec.key.includes('instructions'))?'textarea':'input');if(input.tagName==='INPUT')input.type=['integer','number','float'].includes(spec.type)?'number':'text';input.value=value??'';if(spec.type==='string'){if(spec.min!=null)input.minLength=spec.min;if(spec.max!=null)input.maxLength=spec.max;}if(spec.min!=null)input.min=spec.min;if(spec.max!=null)input.max=spec.max;if(input.type==='number')input.step=spec.type==='integer'?'1':'any';}input.id='setting-'+spec.key;input.dataset.kind=spec.type;input.addEventListener('input',markDirty);return input;}
function rowFor(spec,value,map){const row=document.createElement('div');row.className='field';row.dataset.search=[spec.group,spec.label,spec.key,spec.description].join(' ').toLowerCase();const label=document.createElement('label');label.htmlFor='setting-'+spec.key;label.textContent=spec.label;const description=document.createElement('small');description.textContent=spec.description||'';label.append(description);const tag=document.createElement('span');tag.className='tag';tag.textContent=spec.apply==='restart'?'Apply with service restart':spec.apply==='reconnect'?'Applies next conversation':'Applies when saved';label.append(tag);const key=document.createElement('div');key.className='key';key.textContent=spec.key;label.append(key);const input=inputFor(spec,value);map.set(spec.key,input);row.dataset.key=spec.key;
if(choices[spec.key]){
  const wrapper=document.createElement('div'),select=document.createElement('select');
  select.id=input.id+'-choice';label.htmlFor=select.id;
  for(const option of new Set([...choices[spec.key],spec.default,value]))select.add(new Option(option,option));
  select.add(new Option('Custom…','__custom__'));
  input.setAttribute('aria-label',spec.label+' custom value');
  input.syncChoice=()=>{if(![...select.options].some(o=>o.value===input.value))select.add(new Option(input.value,input.value));select.value=input.value;input.hidden=true;};
  input.syncChoice();
  select.addEventListener('change',()=>{input.hidden=select.value!=='__custom__';if(!input.hidden)input.focus();else input.value=select.value;markDirty();});
  wrapper.append(select,input);row.append(label,wrapper);
}else row.append(label,input);
return row;}
function render(data){snapshot=data;controls.clear();$('groups').replaceChildren();const groups=new Map();for(const spec of data.schema){if(!groups.has(spec.group)){const fieldset=document.createElement('fieldset'),legend=document.createElement('legend');legend.textContent=spec.group;fieldset.append(legend);groups.set(spec.group,fieldset);$('groups').append(fieldset);}groups.get(spec.group).append(rowFor(spec,data.values[spec.key],controls));}$('readOnly').textContent=JSON.stringify(data.read_only||{},null,2);$('restart').hidden=!(data.restart_required||[]).length;dirty=false;$('save').disabled=true;filter();}
function nativeLoad(){nativeControls.clear();$('nativeFields').replaceChildren();if(!window.OpenClawNativeAudio?.getLiveConversationAudioSettings){$('nativeGroup').hidden=true;$('phoneNotice').hidden=false;return;}try{nativeSnapshot=JSON.parse(OpenClawNativeAudio.getLiveConversationAudioSettings());if(nativeSnapshot.error)throw new Error(nativeSnapshot.error);$('phoneNotice').hidden=true;const values=nativeSnapshot.values||nativeSnapshot;for(const [key,label,type,min,max] of [['aec','Acoustic echo cancellation','boolean'],['noise_suppression','Noise suppression','boolean'],['automatic_gain','Automatic microphone gain','boolean'],['output_gain','Speaker output gain','number',0,2],['prefer_bluetooth','Prefer Bluetooth audio','boolean']]){$('nativeFields').append(rowFor({key,label,type,min,max,group:'Phone audio',apply:'reconnect',description:'Stored on this phone. Stop and start the conversation to apply.'},values[key],nativeControls));}$('nativeInfo').textContent=JSON.stringify(nativeSnapshot.read_only||{});$('nativeGroup').hidden=false;}catch(error){return 'Phone audio settings could not be loaded: '+error.message;}}
function filter(){const q=$('search').value.toLowerCase().trim();let count=0;for(const row of document.querySelectorAll('.field')){row.hidden=!applicable(row.dataset.key)||!row.dataset.search.includes(q);if(!row.hidden)count++;}for(const group of $('groups').children)group.hidden=![...group.querySelectorAll('.field')].some(row=>!row.hidden);$('nativeGroup').hidden=!nativeControls.size||![...$('nativeFields').querySelectorAll('.field')].some(row=>!row.hidden);$('count').textContent=count+' settings';}
async function request(method='GET',body){const response=await fetch('/api/settings',{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,cache:'no-store'});let data;try{data=await response.json();}catch{throw new Error('Settings service returned an invalid response.');}if(!response.ok)throw new Error(data.error||'Settings request failed ('+response.status+').');return data;}
async function load(){busy=true;$('save').disabled=true;try{render(await request());const nativeError=nativeLoad();filter();status(nativeError||((snapshot.restart_required||[]).length?'Saved changes are waiting for a service restart.':'Settings loaded.'),!!nativeError);}catch(e){status(e.message,true);}finally{busy=false;}}
function valuesFrom(map){const values={};for(const [key,input] of map){if(!input.reportValidity())throw new Error('Check '+key+'.');values[key]=input.type==='checkbox'?input.checked:['integer','number','float'].includes(input.dataset.kind)?Number(input.value):input.value;if(input.type==='number'&&input.value==='')throw new Error(key+' needs a number.');}return values;}
$('save').onclick=async()=>{if(!snapshot||busy)return;busy=true;$('save').disabled=true;let serviceSaved=false;try{const values=valuesFrom(controls);const phone=valuesFrom(nativeControls);const data=await request('PUT',{values,revision:snapshot.revision});serviceSaved=true;render(data);if(nativeControls.size){const result=JSON.parse(OpenClawNativeAudio.configureLiveConversationAudioSettings(JSON.stringify(phone)));if(result.error)throw new Error(result.error);nativeLoad();filter();}status(data.restart_required?.length?'Saved. Apply pending service settings when you are ready to restart the voice service.':'Saved. Microphone and phone audio changes apply next conversation.');}catch(e){status((serviceSaved?'Service settings saved, but phone audio was not saved: ':'')+e.message,true);dirty=true;}finally{busy=false;$('save').disabled=!dirty;}};
$('reload').onclick=()=>{if(!dirty||confirm('Discard unsaved changes and reload saved settings?'))load();};
$('reset').onclick=()=>{if(!snapshot||!confirm('Replace service settings with deployment defaults? Phone audio stays unchanged. Nothing changes until you save.'))return;for(const spec of snapshot.schema){const input=controls.get(spec.key);setValue(input,spec.default);}markDirty();status('Defaults are in the form. Save to apply them.');};
$('restart').onclick=async()=>{if(dirty){status('Save or reload unsaved changes before applying.',true);return;}if(!confirm('Restart the Live Conversation voice service to apply saved settings? Active conversations will disconnect.'))return;try{const response=await fetch('/api/settings/restart',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:snapshot.revision})});const data=await response.json();if(!response.ok)throw new Error(data.error||'Restart failed.');status('Voice service restart requested. Use Reload saved after it is ready.');}catch(e){status(e.message,true);}};
$('search').addEventListener('input',filter);$('settingsForm').onsubmit=e=>e.preventDefault();window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});load();
