// Real Chromium coverage. Requires playwright-core, Chrome, and Python aiohttp.
// Overrides: PLAYWRIGHT_MODULE, CHROME_BIN, LIVE_SETTINGS_PYTHON.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve,join} from 'node:path';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'/home/john/nodejs/lib/node_modules/openclaw/node_modules/playwright-core');
const repo=resolve(import.meta.dirname,'..'),temp=await mkdtemp(join(tmpdir(),'live-settings-ui-'));
const harness=String.raw`
import asyncio,json,sys
from pathlib import Path
from aiohttp import web
sys.path.insert(0,sys.argv[1]+'/live-conversation')
from settings_config import SETTINGS_SCHEMA,defaults,validate_settings
root=Path(sys.argv[1])/'live-conversation'
values=defaults();revision=1;mode='ok';puts=0
async def asset(request):
    return web.FileResponse(root/('settings.js' if request.path.endswith('.js') else 'settings.html'))
async def api(request):
    global values,revision,puts
    if request.method=='PUT':
        puts+=1
        body=await request.json()
        if mode=='failure':return web.json_response({'error':'Simulated persistence failure'},status=500)
        if mode=='conflict' or body['revision']!=revision:return web.json_response({'error':'Settings changed elsewhere. Reload saved settings before saving.'},status=409)
        try: values=validate_settings(body['values'],values)
        except ValueError as error:return web.json_response({'error':str(error)},status=400)
        revision+=1
    return web.json_response({'schema':SETTINGS_SCHEMA,'values':values,'revision':revision,'restart_required':[s['key'] for s in SETTINGS_SCHEMA if s['apply']=='restart' and values[s['key']]!=s['default']],'read_only':{'input_sample_rate_hz':16000}})
async def test(request):
    global mode
    if request.method=='POST':mode=(await request.json())['mode']
    return web.json_response({'puts':puts})
async def restart(request):return web.json_response({'ok':True})
async def main():
    app=web.Application();app.router.add_get('/settings',asset);app.router.add_get('/settings.js',asset);app.router.add_route('*','/api/settings',api);app.router.add_route('*','/test',test);app.router.add_post('/api/settings/restart',restart)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start();print(site._server.sockets[0].getsockname()[1],flush=True);await asyncio.Event().wait()
asyncio.run(main())
`;
await writeFile(join(temp,'server.py'),harness);
const server=spawn(process.env.LIVE_SETTINGS_PYTHON||'/home/john/.openclaw/tools/pipecat-live-conversation/venv/bin/python',[join(temp,'server.py'),repo],{stdio:['ignore','pipe','pipe']});
let browser,stderr='';server.stderr.on('data',x=>stderr+=x);
try{
  const port=await Promise.race([once(server.stdout,'data').then(([x])=>Number(String(x).trim())),once(server,'exit').then(()=>{throw new Error(stderr);})]);
  assert.ok(port);const base=`http://127.0.0.1:${port}`;
  browser=await chromium.launch({executablePath:process.env.CHROME_BIN||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:412,height:915}});
  await context.addInitScript(()=>{const initial={aec:true,noise_suppression:true,automatic_gain:true,output_gain:1,prefer_bluetooth:true};window.OpenClawNativeAudio={getLiveConversationAudioSettings:()=>JSON.stringify({...JSON.parse(localStorage.getItem('phone')||JSON.stringify(initial)),read_only:{input_sample_rate_hz:16000}}),configureLiveConversationAudioSettings:json=>{if(window.phoneFailure)return JSON.stringify({error:'Simulated native failure'});const values=JSON.parse(json);localStorage.setItem('phone',json);window.phoneSaved=values;return json;}};});
  const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));page.on('dialog',dialog=>dialog.accept());
  await page.goto(base+'/settings');await page.waitForFunction(()=>document.querySelector('#status').textContent==='Settings loaded.');
  const initial=await (await page.request.get(base+'/api/settings')).json();
  for(const spec of initial.schema){const input=page.locator('#setting-'+spec.key);assert.equal(await input.count(),1,spec.key+' renders once');assert.ok(await input.getAttribute('id'));if(spec.type==='boolean')assert.equal(await input.isChecked(),spec.default);else assert.equal(await input.inputValue(),String(spec.default));}
  assert.equal(await page.locator('#groups .field').count(),initial.schema.length);
  assert.equal(await page.locator('#nativeFields .field').count(),5);
  assert.equal(await page.locator('#count').textContent(),`${initial.schema.length+5-10} settings`);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'mobile has no horizontal overflow');
  // Every dependent control follows the draft parent, including search and counts.
  const row=key=>page.locator('#setting-'+key).locator('xpath=ancestor::div[contains(@class,"field")]');
  const visibility=async(keys,visible)=>{for(const key of keys)assert.equal(await row(key).isVisible(),visible,key);assert.equal(await page.locator('#count').textContent(),`${await page.locator('.field:visible').count()} settings`);};
  const qwen=initial.schema.filter(s=>s.key.startsWith('qwen_tts_')||s.key.startsWith('default_qwen_')).map(s=>s.key);
  const kokoro=['tts_speaker_id','tts_threads','tts_speed'];
  await visibility(qwen,false);await visibility(kokoro,true);
  await page.locator('#setting-tts_backend').selectOption('qwen');await visibility(qwen,true);await visibility(kokoro,false);
  await page.locator('#setting-qwen_tts_voice-choice').selectOption('aiden');
  await page.locator('#setting-tts_backend').selectOption('kokoro');
  await page.locator('#search').fill('qwen_tts_voice');assert.equal(await page.locator('.field:visible').count(),0);await page.locator('#search').fill('');
  await page.locator('#setting-tts_backend').selectOption('qwen');assert.equal(await page.locator('#setting-qwen_tts_voice-choice').inputValue(),'aiden');
  for(const [parent,keys] of [
    ['asr_vad_filter',initial.schema.filter(s=>(s.key.startsWith('asr_vad_')&&s.key!=='asr_vad_filter')||['asr_min_speech_duration_ms','asr_min_silence_duration_ms','asr_speech_pad_final_ms','asr_speech_pad_partial_ms'].includes(s.key)).map(s=>s.key)],
    ['audio_capture',['recording_retention_days','recording_max_bytes']],
    ['spoken_backchannels_enabled',['backchannel_display_text']],
  ]){await page.locator('#setting-'+parent).uncheck();await visibility(keys,false);await page.locator('#setting-'+parent).check();await visibility(keys,true);}
  for(const key of ['speech_model','degraded_speech_model','qwen_tts_model','qwen_tts_voice']){
    const select=page.locator('#setting-'+key+'-choice');assert.equal(await select.isVisible(),true);
    await select.selectOption('__custom__');await page.locator('#setting-'+key).fill(key==='qwen_tts_voice'?'custom_voice':'custom/model:tag');
  }
  await page.locator('#setting-tts_backend').selectOption('kokoro');
  await page.locator('#save').click();await page.waitForFunction(()=>!busy&&!dirty&&document.querySelector('#status').textContent.startsWith('Saved.'));
  const customSaved=await (await page.request.get(base+'/api/settings')).json();assert.equal(customSaved.values.qwen_tts_voice,'custom_voice');
  await page.reload();await page.waitForFunction(()=>document.querySelector('#setting-speech_model-choice')?.value==='custom/model:tag');
  await page.locator('#setting-tts_backend').selectOption('qwen');assert.equal(await page.locator('#setting-qwen_tts_voice-choice').inputValue(),'custom_voice');
  await page.locator('#reset').click();await visibility(qwen,false);await visibility(kokoro,true);assert.equal(await page.locator('#setting-speech_model-choice').inputValue(),initial.values.speech_model);
  await page.locator('#save').click();await page.waitForFunction(()=>!busy&&!dirty&&document.querySelector('#status').textContent.startsWith('Saved.'));

  await page.locator('#search').fill('zzzz-no-matching-option');assert.equal(await page.locator('.field:visible').count(),0);assert.equal(await page.locator('#count').textContent(),'0 settings');
  await page.locator('#search').fill('speech_num_predict');assert.equal(await page.locator('.field:visible').count(),1);await page.locator('#search').fill('');
  await page.locator('#setting-action_confirmation').selectOption('confirm');await page.locator('#setting-audio_capture').check();await page.locator('#setting-speech_num_predict').fill('400');
  for(const key of ['aec','noise_suppression','automatic_gain','prefer_bluetooth'])await page.locator('#setting-'+key).uncheck();await page.locator('#setting-output_gain').fill('0.5');
  await page.locator('#save').click();await page.waitForFunction(()=>!busy&&!dirty&&document.querySelector('#status').textContent.startsWith('Saved.'));
  assert.deepEqual(await page.evaluate(()=>window.phoneSaved),{aec:false,noise_suppression:false,automatic_gain:false,output_gain:.5,prefer_bluetooth:false});
  assert.equal(await page.locator('#restart').isVisible(),true);
  await page.locator('summary').click();assert.ok((await page.locator('#readOnly').textContent()).includes('16000'));
  const restarted=page.waitForRequest(request=>request.url().endsWith('/api/settings/restart')&&request.method()==='POST');await page.locator('#restart').click();await restarted;await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('restart requested'));
  await page.reload();await page.waitForFunction(()=>document.querySelector('#setting-speech_num_predict')?.value==='400');assert.equal(await page.locator('#setting-action_confirmation').inputValue(),'confirm');assert.equal(await page.locator('#setting-output_gain').inputValue(),'0.5');assert.equal(await page.locator('#setting-aec').isChecked(),false);
  const before=(await (await page.request.get(base+'/test')).json()).puts;
  await page.locator('#setting-output_gain').fill('3');await page.locator('#save').click();await page.waitForFunction(()=>document.querySelector('#status').className==='error');assert.equal((await (await page.request.get(base+'/test')).json()).puts,before,'invalid native gain prevented before API');await page.locator('#setting-output_gain').fill('0.5');
  await page.locator('#setting-speech_num_predict').fill('-1');await page.locator('#save').click();await page.waitForFunction(()=>document.querySelector('#status').className==='error');assert.equal((await (await page.request.get(base+'/test')).json()).puts,before,'invalid numeric prevented before API');
  await page.locator('#reload').click();await page.waitForFunction(()=>document.querySelector('#setting-speech_num_predict').value==='400');
  for(const [mode,text] of [['conflict','Settings changed elsewhere'],['failure','Simulated persistence failure']]){await page.request.post(base+'/test',{data:{mode}});await page.locator('#setting-speech_num_predict').fill('410');await page.locator('#save').click();await page.waitForFunction(text=>document.querySelector('#status').textContent.includes(text),text);assert.equal(await page.locator('#save').isEnabled(),true);assert.equal(await page.locator('#setting-speech_num_predict').inputValue(),'410');}
  await page.request.post(base+'/test',{data:{mode:'ok'}});await page.locator('#reload').click();await page.waitForFunction(()=>document.querySelector('#setting-speech_num_predict').value==='400');
  await page.evaluate(()=>window.phoneFailure=true);await page.locator('#setting-speech_num_predict').fill('420');await page.locator('#save').click();await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('phone audio was not saved'));assert.equal(await page.locator('#save').isEnabled(),true);await page.evaluate(()=>window.phoneFailure=false);
  await page.locator('#reset').click();assert.equal(await page.locator('#setting-speech_num_predict').inputValue(),String(initial.values.speech_num_predict));assert.equal((await (await page.request.get(base+'/api/settings')).json()).values.speech_num_predict,420,'reset does not persist without save');await page.locator('#save').click();await page.waitForFunction(()=>!busy&&!dirty&&document.querySelector('#status').textContent.startsWith('Saved.'));
  await page.reload();await page.waitForFunction(()=>document.querySelector('#status').textContent==='Settings loaded.');assert.equal(await page.locator('#setting-speech_num_predict').inputValue(),String(initial.values.speech_num_predict));
  const plain=await browser.newContext({viewport:{width:1280,height:800}}),unsupported=await plain.newPage();await unsupported.goto(base+'/settings');await unsupported.waitForFunction(()=>document.querySelector('#status').textContent==='Settings loaded.');assert.equal(await unsupported.locator('#nativeGroup').isVisible(),false);assert.equal(await unsupported.locator('#count').textContent(),`${initial.schema.length-10} settings`);await unsupported.locator('#setting-action_confirmation').selectOption('confirm');await unsupported.locator('#save').click();await unsupported.waitForFunction(()=>!busy&&!dirty&&document.querySelector('#status').textContent.startsWith('Saved.'));assert.deepEqual(errors,[]);
  console.log(`Real Chromium settings UI passed: ${initial.schema.length} schema controls, mobile layout, search, save/reload/defaults, validation, conflict/failure recovery, all five native settings, native failure and unsupported bridge.`);
}finally{if(browser)await browser.close();server.kill();await rm(temp,{recursive:true,force:true});}
