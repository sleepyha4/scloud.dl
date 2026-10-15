'use strict';
const ALARM='scweb-poll';let polling=false;
function cleanServer(v){const u=new URL(v);if(!((u.protocol==='https:')||(u.protocol==='http:'&&['localhost','127.0.0.1'].includes(u.hostname))))throw Error('Use HTTPS, or localhost for local testing.');if(u.username||u.password||u.search||u.hash)throw Error('Use just the website address.');return u.origin}
async function settings(){const s=await chrome.storage.local.get(['server','key','jobs']);return {server:s.server||'',key:s.key||'',jobs:s.jobs||[]}}
async function api(route,data){const s=await settings();if(!s.server)throw Error('Set your website address in the extension popup first.');const options={headers:{'X-App-Key':s.key}};if(data!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(data)}const r=await fetch(s.server+route,options);const v=await r.json().catch(()=>({}));if(!r.ok)throw Error(v.detail||`Server error ${r.status}`);return v}
async function rememberJob(j){const s=await settings();const jobs=s.jobs.filter(x=>x.job_id!==j.job_id);jobs.unshift({job_id:j.job_id,progress:0,complete:false,processed:[],tracks:j.tracks.map(t=>({...t})),message:'Queued',started:Date.now()});await chrome.storage.local.set({jobs:jobs.slice(0,12)});await chrome.alarms.create(ALARM,{periodInMinutes:0.5})}
async function start(inspection,selected){const data=await api('/api/start',{inspection_id:inspection,selected});await rememberJob(data);return data}
async function refresh(){if(polling)return;polling=true;try{const s=await settings();const next=[];for(const entry of s.jobs){if(entry.complete){next.push(entry);continue}let state;try{state=await api('/api/jobs/'+entry.job_id)}catch(err){next.push({...entry,message:err.message});continue}
const processed=new Set(entry.processed||[]);if(!state.running){for(const item of state.tracks){if(item.status!=='done'||processed.has(item.index))continue;try{const filename=(item.filename||'SoundCloud Audio').replace(/[\\/:*?"<>|]/g,'_');const downloadUrl=s.server+item.download_url;await chrome.downloads.download({url:downloadUrl,filename,conflictAction:'uniquify',saveAs:false});processed.add(item.index)}catch(err){/* Allow retry on the next alarm. */}}}
next.push({...entry,tracks:state.tracks,progress:state.progress,complete:!state.running && state.tracks.filter(x=>x.status==='done').every(x=>processed.has(x.index)),processed:[...processed],message:state.message})}
await chrome.storage.local.set({jobs:next});if(next.every(j=>j.complete))await chrome.alarms.clear(ALARM)}finally{polling=false}}
chrome.alarms.onAlarm.addListener(alarm=>{if(alarm.name===ALARM)refresh().catch(()=>{})});
chrome.runtime.onInstalled.addListener(()=>{chrome.alarms.create(ALARM,{periodInMinutes:0.5})});
chrome.runtime.onMessage.addListener((msg,sender,send)=>{(async()=>{switch(msg.type){
case 'inspect':return await api('/api/inspect',{url:msg.url});
case 'start':return await start(msg.inspection_id,msg.selected);
case 'refresh':await refresh();return {ok:true};
case 'config':{const server=cleanServer(msg.server);await chrome.storage.local.set({server,key:msg.key||''});return {ok:true}}
case 'enqueue':{const data=await api('/api/inspect',{url:msg.url});await start(data.inspection_id,[0]);return {ok:true}}
case 'open_playlist':{const s=await settings();if(!s.server){await chrome.tabs.create({url:chrome.runtime.getURL('popup.html')});return {ok:false}}await chrome.tabs.create({url:s.server+'/?url='+encodeURIComponent(msg.url)});return {ok:true}}
case 'open_settings':await chrome.tabs.create({url:chrome.runtime.getURL('popup.html')});return {ok:true};
default:throw Error('Unknown request')}})().then(v=>send({ok:true,data:v}),e=>{if(msg.type==='enqueue')chrome.tabs.create({url:chrome.runtime.getURL('popup.html')}).catch(()=>{});send({ok:false,error:e.message})});return true});
