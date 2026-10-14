"""NIM same-origin workflow client, using the account's logged-in browser and proxy."""
from __future__ import annotations
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from .browser import CDPBrowser, BrowserError, BrowserRejected, account_proxy_url
from .media import _download, MediaError
from .store import Store

class NimError(Exception):
    pass

STATIC_BASE='https://static.nim.video/output/'

def _duration(path:Path)->float|None:
    try:
        result=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,timeout=25)
        value=float(result.stdout.strip())
        return round(value,4) if result.returncode==0 and value>0 else None
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return None

class NimClient:
    def __init__(self, account:dict[str,Any], *, store:Store|None=None):
        self.account=account
        self.browser=CDPBrowser(int(account['cdp_port']))
        self.store=store

    def account_state(self)->dict[str,Any]:
        data=self.browser.get('/api/credits/getUserCreditsData')
        if not isinstance(data,dict) or 'uid' not in data:
            raise BrowserError('Nim credit response did not confirm an authenticated session')
        buckets={key:float((data.get(key) or {}).get('count') or 0) for key in ('subCredits','cpCredits','packCredits','grantCredits')}
        return {'available_balance':sum(buckets.values()),'buckets':buckets}

    def daily_checkin(self)->dict[str,Any]:
        # The captured NIM session did not issue a claim endpoint. Never emulate
        # a daily claim by guessing an undocumented API call.
        return {'already_checked_in':True,'received':0,'unsupported':True}

    def upload_inputs(self, normalized:dict[str,Any])->dict[str,list[dict[str,Any]]]:
        assets={kind:[] for kind in ('images','videos','audios')}
        proxy=account_proxy_url(self.account)
        for kind in assets:
            for source in normalized[kind]:
                if source.startswith('nim:'):
                    value=source[4:].strip('/')
                    if not value.startswith('wf-input/'):
                        raise MediaError('Nim asset path must start with wf-input/')
                    assets[kind].append({'filePath':value,'duration':None,'displayName':Path(value).name})
                    continue
                data,mime,filename=_download(source,kind,proxy)
                digest=hashlib.sha256(data).hexdigest()
                cached=self.store.media_cache_get(int(self.account['id']),kind,digest) if self.store else None
                if kind == 'images' and cached and cached.get('nim_asset_id','').startswith('wf-input/'):
                    assets[kind].append({'filePath':cached['nim_asset_id'],'duration':cached.get('duration'),
                                         'displayName':filename})
                    continue
                suffix=Path(filename).suffix or {'images':'.png','videos':'.mp4','audios':'.mp3'}[kind]
                with tempfile.NamedTemporaryFile(prefix='nim-media-',suffix=suffix,delete=False) as file:
                    file.write(data);path=Path(file.name)
                try:
                    duration=_duration(path) if kind!='images' else None
                    uploaded=self.browser.upload_file(path)
                finally:
                    path.unlink(missing_ok=True)
                record={'filePath':uploaded['filePath'],'fileId':uploaded.get('fileId'),
                        'duration':duration,'displayName':filename}
                assets[kind].append(record)
                if self.store:
                    self.store.media_cache_put(int(self.account['id']),kind,digest,
                        {'nim_asset_id':record['filePath'],'asset_id':uploaded.get('fileId') or '',
                         'mime':mime,'bytes':len(data)})
        return assets

    def submit(self, normalized:dict[str,Any], assets:dict[str,list[dict[str,Any]]])->dict[str,Any]:
        project_id=str(self.account.get('project_id') or self.browser.project_id())
        if not project_id:
            raise BrowserRejected('Nim account has no video project; open the project in its browser first')
        structured=[]
        for kind,name in (('images','image_urls'),('videos','video_urls'),('audios','audio_urls')):
            records=assets[kind]
            if not records:continue
            item={'name':name,'paths':[record['filePath'] for record in records],
                  'durations':[record.get('duration') for record in records]}
            if kind=='audios':item['displayNames']=[record.get('displayName') or 'audio' for record in records]
            structured.append(item)
        body={'type':'image2video_dc','positive':normalized['prompt'],'negative':'',
              'enhancePrompt':False,'seed':'-1','mediaLength':normalized['duration']*1000,
              'requestedAspectRatio':normalized['aspect_ratio'],'generateAudio':True,
              'resolution':normalized['resolution'],'projectId':project_id,'basedOn':None,
              'source':'omnibox_advanced_mode_button','meta':{'advancedMode':normalized['advanced_mode'],
              'structuredFileInputs':structured,'batchSize':1},
              'externalModel':normalized['upstream_model'],'isPrivate':False}
        response=self.browser.post('/api/workflows/start',body)
        error=response.get('error')
        if error or response.get('parallelLimitReached'):
            raise BrowserRejected(f'Nim rejected generation: {str(error or "parallel limit reached")[:300]}')
        workflow=response.get('workflow') or {}
        workflow_id=workflow.get('id')
        if not workflow_id:
            raise BrowserError('Nim submit response did not contain workflow id; outcome unknown')
        return {'run_id':workflow_id,'thread_id':workflow_id,'credits_charged':response.get('creditsCharged')}

    def poll(self, run_id:str, thread_id:str='')->dict[str,Any]:
        workflows=self.browser.get('/api/workflows/recentWorkflows')
        if not isinstance(workflows,list):
            raise BrowserError('Nim recentWorkflows returned an unexpected response')
        item=next((value for value in workflows if value.get('id')==run_id or value.get('workflowId')==run_id),None)
        if item is None:
            return {'status':'running','result_urls':[],'error':'','error_code':''}
        error=str(item.get('errorCause') or '')
        if item.get('erroredAt') or item.get('cancelledAt'):
            return {'status':'failed','result_urls':[],'error':error or 'Nim generation failed',
                    'error_code':str(item.get('errorId') or '')}
        output=item.get('output') or ''
        if item.get('finishedAt') and output:
            paths=output if isinstance(output,list) else [output]
            urls=[]
            for path in paths:
                if isinstance(path,dict):path=path.get('path') or path.get('url') or ''
                path=str(path)
                if not path:continue
                urls.append(path if path.startswith('https://') else STATIC_BASE+path.lstrip('/'))
            return {'status':'succeeded','result_urls':urls,'error':'','error_code':''}
        return {'status':'running','result_urls':[],'error':'','error_code':''}
