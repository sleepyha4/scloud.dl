"""Single-service web app for exact-link SoundCloud track and playlist inspection.

Uses yt-dlp for formats and creator-authorized audio acquisition. DRM is not bypassed.
Public hosting should be protected with APP_KEY. Files are staged temporarily on server.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from metadata import parse_track, filename_for, artwork_bytes, normalize_artwork, embed_tags

ROOT = pathlib.Path(__file__).resolve().parent
TEMP_ROOT = pathlib.Path(tempfile.gettempdir()) / 'sc-web-jobs'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
APP_KEY = os.environ.get('APP_KEY', '').strip()
MAX_SELECTED = 25
MAX_PLAYLIST = 100
MAX_TRACK_BYTES = 160 * 1024 * 1024
JOB_TTL_SECONDS = 45 * 60
SC_HOSTS = {'soundcloud.com', 'www.soundcloud.com', 'm.soundcloud.com', 'on.soundcloud.com', 'api-v2.soundcloud.com', 'api.soundcloud.com'}

app = FastAPI(title='SoundCloud Web', docs_url=None, redoc_url=None)
lock = threading.RLock()
inspections: dict[str, dict] = {}
jobs: dict[str, 'DownloadJob'] = {}
workers = ThreadPoolExecutor(max_workers=2)


def check_key(raw: str | None):
    if APP_KEY and not secrets.compare_digest((raw or ''), APP_KEY):
        raise HTTPException(401, 'Enter the access key configured for this server.')


def valid_url(raw: str) -> str:
    if not isinstance(raw, str) or len(raw) > 2000:
        raise ValueError('Enter a SoundCloud track or playlist URL.')
    parsed = urllib.parse.urlsplit(raw.strip())
    if parsed.scheme != 'https' or (parsed.hostname or '').lower() not in SC_HOSTS or parsed.username or parsed.password or parsed.port:
        raise ValueError('Only HTTPS SoundCloud links are supported.')
    if (parsed.hostname or '').lower().startswith('api'):
        if not re.fullmatch(r'/tracks/(?:soundcloud%3Atracks%3A)?\d+', parsed.path, re.I):
            raise ValueError('Unsupported SoundCloud track URL.')
    elif (parsed.hostname or '').lower() != 'on.soundcloud.com':
        p = [x for x in parsed.path.strip('/').split('/') if x]
        if not (len(p) == 2 or (len(p) == 3 and (p[1] == 'sets' or p[2].startswith('s-'))) or (len(p) == 4 and p[1] == 'sets' and p[3].startswith('s-'))):
            raise ValueError('Use a track or a /sets/ playlist link.')
        if p[0].lower() in {'discover','you','search','charts','stream','upload','settings','pages'}:
            raise ValueError('Use a specific track or playlist.')
    # Preserve a private track's secret token, discard marketing/tracking query strings.
    qs = urllib.parse.parse_qs(parsed.query)
    secret = qs.get('secret_token', [''])[0]
    return urllib.parse.urlunsplit(('https', parsed.netloc.lower(), parsed.path.rstrip('/'),
                                   urllib.parse.urlencode({'secret_token':secret}) if secret else '', ''))


def item_url(info: dict) -> str:
    for v in (info.get('webpage_url'), info.get('url'), info.get('original_url')):
        if isinstance(v, str):
            try:
                return valid_url(v)
            except ValueError:
                pass
    ident = str(info.get('id') or '')
    if ident.isdigit():
        return 'https://api-v2.soundcloud.com/tracks/' + ident
    raise ValueError('Could not identify a SoundCloud track in playlist.')


def summarise(info: dict, source: str) -> dict:
    data = dict(info)
    data['_source_url'] = source
    meta = parse_track(data)
    return dict(id=str(info.get('id') or ''), title=meta['title'], artist=meta['artist'],
                uploader=meta['uploader'], duration=info.get('duration'),
                artwork=info.get('thumbnail'), url=source, genre=meta.get('genre',''))


def get_info(url: str, flat: bool = False) -> dict:
    from yt_dlp import YoutubeDL
    opts = {'quiet':True, 'no_warnings':True, 'skip_download':True,
            'extract_flat':'in_playlist' if flat else False,
            'playlistend':MAX_PLAYLIST if flat else None,
            'socket_timeout':20, 'retries':2, 'noplaylist':False,
            'extractor_args':{'soundcloud':{'formats':['*']}}}
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def inspect_sync(url: str) -> dict:
    data = get_info(url, flat='/sets/' in urllib.parse.urlsplit(url).path)
    if not data:
        raise ValueError('SoundCloud returned no track information.')
    is_playlist = data.get('_type') == 'playlist' or bool(data.get('entries'))
    if is_playlist:
        tracks=[]
        for entry in data.get('entries') or []:
            if not isinstance(entry, dict):
                continue
            try:
                src=item_url(entry)
            except ValueError:
                continue
            tracks.append(summarise(entry, src))
        if not tracks:
            raise ValueError('No accessible tracks found in this playlist.')
        return {'kind':'playlist','title':data.get('title') or 'Playlist', 'uploader':data.get('uploader') or '',
                'artwork':data.get('thumbnail') or '', 'tracks':tracks}
    return {'kind':'track','title':data.get('title') or 'Track', 'tracks':[summarise(data, url)]}


def cleanup():
    now=time.time()
    with lock:
        for k,v in list(inspections.items()):
            if now-v['created']>JOB_TTL_SECONDS:
                inspections.pop(k,None)
        for k,job in list(jobs.items()):
            if not job.running and now-job.created>JOB_TTL_SECONDS:
                jobs.pop(k,None)
                shutil.rmtree(job.directory, ignore_errors=True)


class InspectRequest(BaseModel):
    url: str = Field(max_length=2000)

class StartRequest(BaseModel):
    inspection_id: str
    selected: list[int] = Field(default_factory=list)


@dataclass
class DownloadJob:
    id: str
    ticket: str
    tracks: list[dict]
    directory: pathlib.Path
    created: float=field(default_factory=time.time)
    entries: list[dict]=field(default_factory=list)
    running: bool=True
    progress: float=0.0
    current_fraction: float=0.0
    message: str='Queued'
    bundle_path: pathlib.Path | None=None

    def public(self) -> dict:
        with lock:
            count=len(self.entries)
            finished=sum(1 for x in self.entries if x['status'] in ('done','failed'))
            pct=(finished+self.current_fraction)/max(1,count)*100 if self.running else 100
            return {'job_id':self.id,'running':self.running, 'progress':round(min(100,pct),1),
                    'total':count,'completed':finished,'message':self.message,
                    'tracks':[{k:v for k,v in row.items() if k not in ('path',)} for row in self.entries],
                    'zip_url':f'/api/jobs/{self.id}/bundle?ticket={self.ticket}' if self.bundle_path else None}


def prepare_job(inspection: dict, selected: list[int]):
    tracks=inspection['tracks']
    if not selected:
        selected=list(range(len(tracks))) if len(tracks)==1 else []
    if not selected or len(selected)>MAX_SELECTED or len(set(selected)) != len(selected) or any(type(i)!=int or i<0 or i>=len(tracks) for i in selected):
        raise ValueError(f'Select between 1 and {MAX_SELECTED} valid tracks.')
    jobid=secrets.token_urlsafe(17)
    directory=TEMP_ROOT/jobid
    directory.mkdir(mode=0o700)
    j=DownloadJob(id=jobid,ticket=secrets.token_urlsafe(24),tracks=[tracks[i] for i in selected],directory=directory)
    j.entries=[{'index':k,'title':t['title'],'artist':t['artist'],'status':'queued',
                'progress':0,'error':None,'filename':None,'download_url':None} for k,t in enumerate(j.tracks)]
    with lock: jobs[jobid]=j
    return j


def run_job(job: DownloadJob):
    from yt_dlp import YoutubeDL
    n=len(job.tracks)
    used_names=set()
    for idx, track in enumerate(job.tracks):
        with lock:
            row=job.entries[idx]; row['status']='downloading'; job.message=f'Downloading {idx+1} of {n}'; job.current_fraction=0
        work=job.directory / f'item-{idx}'
        work.mkdir(exist_ok=True)
        def hook(event):
            if event.get('status') != 'downloading': return
            total=event.get('total_bytes') or event.get('total_bytes_estimate') or 0
            if total:
                with lock:
                    job.current_fraction=min(.96,max(0,event.get('downloaded_bytes',0)/total))
                    job.entries[idx]['progress']=round(job.current_fraction*100)
        try:
            opts={'format':'download/bestaudio/best', 'outtmpl':str(work/'audio.%(ext)s'),
                  'quiet':True,'no_warnings':True,'noplaylist':True,
                  'progress_hooks':[hook], 'socket_timeout':25, 'retries':2,
                  'extractor_args':{'soundcloud':{'formats':['*']}},
                  'max_filesize':MAX_TRACK_BYTES,
                  'overwrites':False}
            with YoutubeDL(opts) as ydl:
                info=ydl.extract_info(track['url'], download=True)
            audio_files=[p for p in work.iterdir() if p.is_file() and p.suffix.lower() in ('.m4a','.mp3','.flac','.wav','.aiff','.aif','.opus','.ogg','.aac','.mp4','.webm')]
            if not audio_files: raise RuntimeError('No usable audio file returned.')
            audio=max(audio_files,key=lambda p:p.stat().st_size)
            if audio.stat().st_size>MAX_TRACK_BYTES: raise RuntimeError('File exceeds server size limit.')
            meta_info=dict(info); meta_info['_source_url']=track['url']
            meta=parse_track(meta_info)
            img,mime,_=artwork_bytes(info)
            img,mime=normalize_artwork(img,mime,work,shutil.which('ffmpeg'))
            try:
                embed_tags(audio,meta,img,mime)
            except Exception:
                # Downloaded audio is still useful when a container has no tag writer.
                pass
            name=filename_for(meta,audio.suffix[1:])
            stem=pathlib.Path(name).stem; ext=audio.suffix
            serial=2
            while name.casefold() in used_names or (job.directory/name).exists():
                name=f'{stem} ({serial}){ext}';serial+=1
            used_names.add(name.casefold())
            target=job.directory/name
            shutil.move(str(audio),str(target))
            # Verify that the result has an actual decodable audio stream.
            ff=subprocess.run(['ffprobe','-v','error','-select_streams','a:0','-show_entries','stream=codec_name,bit_rate','-of','json',str(target)],capture_output=True,text=True,timeout=30)
            if ff.returncode or not json.loads(ff.stdout).get('streams'):
                raise RuntimeError('Resulting file does not contain decodable audio.')
            with lock:
                row.update(status='done',progress=100,filename=name,path=str(target),
                           codec=json.loads(ff.stdout)['streams'][0].get('codec_name'),
                           bitrate_kbps=round(int(json.loads(ff.stdout)['streams'][0].get('bit_rate') or 0)/1000),
                           download_url=f'/api/jobs/{job.id}/files/{idx}?ticket={job.ticket}')
        except Exception as exc:
            with lock:
                row.update(status='failed',progress=100,error=re.sub(r'https?://\S+','[URL]',str(exc))[:220])
        finally:
            shutil.rmtree(work,ignore_errors=True)
            with lock: job.current_fraction=0
    with lock:
        job.message='Finished'
    done=[row for row in job.entries if row['status']=='done']
    if len(done)>1:
        zip_path=job.directory/'selected-tracks.zip'
        try:
            with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_STORED,allowZip64=True) as z:
                for row in done:
                    z.write(row['path'],arcname=row['filename'])
            with lock:job.bundle_path=zip_path
        except Exception:
            zip_path.unlink(missing_ok=True)
    with lock: job.running=False


@app.middleware('http')
async def security_headers(request:Request, call_next):
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    if request.url.path.startswith('/api'):
        response.headers['Cache-Control']='no-store'
    # Extension's origin is opaque/random; no cookies involved and APP_KEY is required remotely.
    if request.headers.get('origin','').startswith('chrome-extension://'):
        response.headers['Access-Control-Allow-Origin']=request.headers['origin']
        response.headers['Vary']='Origin'
        response.headers['Access-Control-Allow-Headers']='Content-Type,X-App-Key'
        response.headers['Access-Control-Allow-Methods']='GET,POST,OPTIONS'
    return response

@app.options('/api/{rest:path}')
async def preflight(request:Request, rest:str):
    return JSONResponse({})

@app.get('/api/health')
def health():
    store_url = os.environ.get('CHROME_WEB_STORE_URL', '').strip()
    if not (store_url.startswith('https://chromewebstore.google.com/detail/') or store_url.startswith('https://chrome.google.com/webstore/detail/')):
        store_url = ''
    return {'ok':True,'version':'2.0','protected':bool(APP_KEY),'store_url':store_url}

@app.post('/api/inspect')
async def inspect(request:InspectRequest,x_app_key:str|None=Header(None)):
    check_key(x_app_key)
    cleanup()
    try: url=valid_url(request.url)
    except ValueError as exc: raise HTTPException(400,str(exc))
    try: data=await run_in_threadpool(inspect_sync,url)
    except Exception as exc: raise HTTPException(422, f'Could not inspect this SoundCloud link: {str(exc)[:190]}')
    ident=secrets.token_urlsafe(16)
    inspections[ident]={**data,'created':time.time()}
    return {'inspection_id':ident,**data}

@app.post('/api/start')
def start(req:StartRequest,x_app_key:str|None=Header(None)):
    check_key(x_app_key)
    cleanup()
    data=inspections.get(req.inspection_id)
    if not data: raise HTTPException(404,'Inspection expired. Inspect the link again.')
    with lock:
        if sum(1 for j in jobs.values() if j.running)>=2:
            raise HTTPException(429,'Server busy. Try again when another queue completes.')
    try:j=prepare_job(data,req.selected)
    except ValueError as exc:raise HTTPException(400,str(exc))
    workers.submit(run_job,j)
    return j.public()

@app.get('/api/jobs/{job_id}')
def status(job_id:str,x_app_key:str|None=Header(None)):
    check_key(x_app_key)
    j=jobs.get(job_id)
    if not j:raise HTTPException(404,'Queue not found or expired.')
    return j.public()


def authorized_file(job_id:str,ticket:str|None):
    j=jobs.get(job_id)
    if not j or not ticket or not secrets.compare_digest(ticket,j.ticket):
        raise HTTPException(403,'Download link expired or invalid.')
    if time.time()-j.created>JOB_TTL_SECONDS:
        raise HTTPException(410,'Download expired. Start a new queue.')
    return j

@app.get('/api/jobs/{job_id}/files/{idx}')
def file(job_id:str,idx:int,ticket:str|None=None):
    j=authorized_file(job_id,ticket)
    if idx<0 or idx>=len(j.entries):raise HTTPException(404)
    row=j.entries[idx]
    if row['status']!='done' or not row.get('path'):raise HTTPException(404,'Audio not ready.')
    return FileResponse(row['path'],filename=row['filename'],media_type='application/octet-stream')

@app.get('/api/jobs/{job_id}/bundle')
def bundle(job_id:str,ticket:str|None=None):
    j=authorized_file(job_id,ticket)
    if not j.bundle_path or not j.bundle_path.exists():raise HTTPException(404,'ZIP not ready.')
    return FileResponse(j.bundle_path,filename='SoundCloud tracks.zip',media_type='application/zip')

@app.get('/')
def home():return FileResponse(ROOT/'public'/'index.html')

app.mount('/', StaticFiles(directory=str(ROOT/'public'),html=True),name='site')
