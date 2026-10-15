import io
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import zipfile
import types
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import app as server

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setattr(server,'TEMP_ROOT',tmp_path)
    monkeypatch.setattr(server,'APP_KEY','testing-key')
    with server.lock:
        server.jobs.clear();server.inspections.clear()
    yield TestClient(server.app)

@pytest.mark.parametrize('url,ok',[
  ('https://soundcloud.com/umay_works/1-who-that-is?utm_campaign=test',True),
  ('https://soundcloud.com/name/sets/list-one',True),
  ('https://evil.com/name/track',False),
  ('http://soundcloud.com/name/track',False),
  ('https://soundcloud.com/name',False),
  ('https://soundcloud.com@evil.com/name/track',False),
])
def test_urls(url,ok):
    try: server.valid_url(url);result=True
    except ValueError: result=False
    assert result is ok

def test_auth(client):
    assert client.post('/api/inspect',json={'url':'https://soundcloud.com/a/b'}).status_code==401
    assert client.get('/api/health').json()['protected'] is True

def test_inspection_playlist(client):
    def fake(url):return {'kind':'playlist','title':'Test Playlist','tracks':[
      {'title':'One','artist':'A','url':'https://soundcloud.com/a/one'},
      {'title':'Two','artist':'B','url':'https://soundcloud.com/b/two'}]}
    with patch.object(server,'inspect_sync',side_effect=fake):
        response=client.post('/api/inspect',json={'url':'https://soundcloud.com/a/sets/playlist'},headers={'X-App-Key':'testing-key'})
    assert response.status_code==200,response.text
    body=response.json();assert body['tracks'][1]['title']=='Two'
    with patch.object(server.workers,'submit') as f:
        r=client.post('/api/start',json={'inspection_id':body['inspection_id'],'selected':[1]},headers={'X-App-Key':'testing-key'})
    assert r.status_code==200,r.text
    assert r.json()['tracks'][0]['title']=='Two'
    assert f.called
    assert client.get('/api/jobs/'+r.json()['job_id'],headers={'X-App-Key':'testing-key'}).status_code==200
    assert client.get('/api/jobs/'+r.json()['job_id']).status_code==401
    assert client.get('/api/jobs/'+r.json()['job_id']+'/files/0?ticket=wrong').status_code==403


def test_url_normalisation():
    url=server.valid_url('https://soundcloud.com/artist/song?utm_source=a&si=not-secret')
    assert url=='https://soundcloud.com/artist/song'
    url=server.valid_url('https://soundcloud.com/a/song?secret_token=s-abc&utm_source=a')
    assert url.endswith('?secret_token=s-abc')


def test_fake_download_and_zip(client,tmp_path,monkeypatch):
    # Emulates extractor output; ffmpeg generates real AAC to verify the tagging/ZIP pipeline.
    fake_data={'id':'1695021411','title':'Mohajer & Lawrence Lee - Who That Is',
       'uploader':'UMAY','webpage_url':'https://soundcloud.com/umay_works/1-who-that-is','thumbnail':None}
    class FakeYDL:
        def __init__(self,opts):self.opts=opts
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def extract_info(self,url,download):
            p=pathlib.Path(self.opts['outtmpl'].replace('%(ext)s','m4a'))
            subprocess.run(['ffmpeg','-f','lavfi','-i','sine=frequency=600:duration=0.15','-ac','2',
                            '-c:a','aac','-b:a','128k','-y',str(p)],check=True,capture_output=True)
            for hook in self.opts.get('progress_hooks',[]):hook({'status':'downloading','total_bytes':100,'downloaded_bytes':80})
            return dict(fake_data)
    monkeypatch.setitem(sys.modules,'yt_dlp',types.SimpleNamespace(YoutubeDL=FakeYDL))
    monkeypatch.setattr(server,'artwork_bytes',lambda i:(None,None,None))
    tracks=[{'title':'Who That Is','artist':'Mohajer & Lawrence Lee','url':'https://soundcloud.com/umay_works/1-who-that-is'}]*2
    job=server.prepare_job({'tracks':tracks},[0,1])
    server.run_job(job)
    data=job.public()
    assert data['progress']==100
    assert all(t['status']=='done' for t in data['tracks'])
    assert '[1695021411]' not in data['tracks'][0]['filename']
    assert data['tracks'][0]['filename'].startswith('Mohajer & Lawrence Lee - Who That Is')
    assert job.bundle_path.exists()
    with zipfile.ZipFile(job.bundle_path) as z:
        assert len(z.namelist())==2
        assert z.namelist()[0]!=z.namelist()[1]
    t=client.get(data['tracks'][0]['download_url']);assert t.status_code==200 and len(t.content)>100
    zipped=client.get(data['zip_url']);assert zipped.status_code==200 and zipped.content[:2]==b'PK'


def test_site_and_extension_page(client):
    assert 'SoundCloud Downloader' in client.get('/').text
    assert 'Download extension ZIP' in client.get('/extension.html').text
    assert 'umay_works' not in client.get('/').text
