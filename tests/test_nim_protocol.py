from app.catalog import MODELS, normalize_request, bind_prompt
from app.client import NimClient, STATIC_BASE
import pytest


def test_required_aliases_and_defaults():
    expected = {'sd-2-0','sd-2-0-1080p','sd-2-0-4k','sd-2-0-fast','sd-2-0-mini',
                'sd-2-0-mini-480p','sd-2-0-fast-480p','sd-2-5','sd-2-5-480p',
                'sd-2-5-1080p','wan-3.0','wan-3.0-480p','wan-3.0-1080p','minimax-h3-768p'}
    assert expected <= set(MODELS)
    assert MODELS['sd-2-0-mini'].resolution == '720p'
    assert MODELS['sd-2-5'].limits == {'images':30,'videos':10,'audios':10}
    assert MODELS['wan-3.0'].limits == {'images':10,'videos':5,'audios':5}


def test_media_markers_and_upstream_visual_limit():
    body={'model':'sd-2-0-mini-480p','duration':4,'ratio':'16:9',
          'prompt':'参考 图片1 image9 音频2 Video3；视频6 是章节标题',
          'image_urls':['nim:wf-input/i%d.jpg'%i for i in range(6)],
          'audio_urls':['nim:wf-input/a%d.mp3'%i for i in range(3)],
          'video_urls':['nim:wf-input/v%d.mp4'%i for i in range(3)]}
    task=normalize_request(body)
    assert '@image1' in task['prompt'] and 'image9' in task['prompt']
    assert '@audio2' in task['prompt'] and '@video3' in task['prompt']
    assert len(task['images'])==6 and len(task['videos'])==3 and len(task['audios'])==3
    with pytest.raises(ValueError, match='image and video references combined'):
        normalize_request({**body,'image_urls':['nim:wf-input/i%d.jpg'%i for i in range(7)]})


def test_nim_submit_shape_and_completed_result():
    class Browser:
        def __init__(self):self.body=None
        def post(self,path,body):
            assert path=='/api/workflows/start';self.body=body
            return {'workflow':{'id':'wf-test'},'error':None,'creditsCharged':50}
        def get(self,path):
            assert path=='/api/workflows/recentWorkflows'
            return [{'id':'wf-test','finishedAt':'2026-10-14T00:00:00Z','output':'wf-output/video.mp4'}]
    client=NimClient.__new__(NimClient)
    client.account={'project_id':'project-test'}
    client.browser=Browser()
    inputs={'images':[{'filePath':'wf-input/image.jpg','duration':None}],
            'videos':[{'filePath':'wf-input/video.mp4','duration':2.0}],
            'audios':[{'filePath':'wf-input/audio.mp3','duration':4.0,'displayName':'audio.mp3'}]}
    normalized=normalize_request({'model':'sd-2-0-mini-480p','prompt':'使用图片1、视频1、音频1',
                                  'duration':4,'images':['image'],'videos':['video'],'audios':['audio']})
    result=client.submit(normalized,inputs)
    body=client.browser.body
    assert result['run_id']=='wf-test'
    assert body['mediaLength']==4000 and body['resolution']=='480p'
    assert body['externalModel']=='falBytedanceSeedance2.0MiniReferenceToVideo'
    assert body['meta']['structuredFileInputs'][1]['durations']==[2.0]
    assert client.poll('wf-test')['result_urls']==[STATIC_BASE+'wf-output/video.mp4']
