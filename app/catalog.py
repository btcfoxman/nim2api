"""NIM model catalog and media-reference normalization."""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class ModelSpec:
    id: str
    upstream_model: str
    resolution: str
    family: str
    advanced_mode: str
    min_duration: int = 4
    max_duration: int = 15
    @property
    def limits(self) -> dict[str, int]:
        return {"images": 30, "videos": 10, "audios": 10} if self.family == "2.5" else ({"images": 10, "videos": 5, "audios": 5} if self.family == "wan" else {"images": 9, "videos": 3, "audios": 3})

    @property
    def max_visual_files(self) -> int:
        return {"2.5": 30, "wan": 10}.get(self.family, 9)

def spec(name, upstream, resolution, family, mode, minimum=4, maximum=15):
    return ModelSpec(name, upstream, resolution, family, mode, minimum, maximum)

MODELS = {m.id:m for m in (
    spec('sd-2-0','seedanceV2ImageToVideo','720p','2.0','seedance.v2fal'),
    spec('sd-2-0-1080p','seedanceV2ImageToVideo','1080p','2.0','seedance.v2fal'),
    spec('sd-2-0-4k','seedanceV2ImageToVideo','4k','2.0','seedance.v2fal'),
    spec('sd-2-0-fast','seedanceV2FastImageToVideo','720p','2.0','seedance.v2fal'),
    spec('sd-2-0-fast-480p','seedanceV2FastImageToVideo','480p','2.0','seedance.v2fal'),
    spec('sd-2-0-mini','falBytedanceSeedance2.0MiniReferenceToVideo','720p','2.0','seedance.v2fal'),
    spec('sd-2-0-mini-480p','falBytedanceSeedance2.0MiniReferenceToVideo','480p','2.0','seedance.v2fal'),
    spec('sd-2-5','seedanceV25ImageToVideo','720p','2.5','seedance.v25',4,30),
    spec('sd-2-5-480p','seedanceV25ImageToVideo','480p','2.5','seedance.v25',4,30),
    spec('sd-2-5-1080p','seedanceV25ImageToVideo','1080p','2.5','seedance.v25',4,30),
    spec('wan-3.0','wan30Reference2Video','720p','wan','wan.v30',2,30),
    spec('wan-3.0-480p','wan30Reference2Video','480p','wan','wan.v30',2,30),
    spec('wan-3.0-1080p','wan30Reference2Video','1080p','wan','wan.v30',2,30),
    spec('minimax-h3-768p','minimaxH3FastReferenceToVideo','768p','minimax','minimax.h3',5,15),
)}
RATIOS={'16:9','9:16','1:1','4:3','3:4','21:9'}
ALIASES=re.compile(r'(?<![\w<])(?P<at>@)?(?P<kind>视频|video|v|视|图片|image|img|图|音频|audio|音)\s*(?P<index>\d+)(?!\w)',re.I)

def _media_list(body:dict[str,Any], plural:str, alternate:str)->list[str]:
    value=body.get(plural,body.get(alternate,[]))
    if value is None:return []
    if isinstance(value,str):value=[value]
    if not isinstance(value,list):raise ValueError(f'{plural} must be an array')
    result=[]
    for item in value:
        if isinstance(item,dict):item=item.get('url') or item.get('asset_id')
        if not isinstance(item,str) or not item.strip():raise ValueError(f'{plural} contains an invalid item')
        result.append(item.strip())
    return result

def media_kind(label:str)->str:
    label=label.lower()
    if label in {'视频','video','v','视'}:return 'videos'
    if label in {'图片','image','img','图'}:return 'images'
    return 'audios'

def bind_prompt(prompt:str,media:dict[str,list[str]])->str:
    def replace(m:re.Match[str])->str:
        kind=media_kind(m.group('kind'));index=int(m.group('index'))
        if not media[kind] and not m.group('at'):return m.group(0)
        if not 1<=index<=len(media[kind]):
            if not m.group('at'):return m.group(0)  # A bare out-of-range label may be a section title.
            raise ValueError(f'prompt references {m.group(0)} but only {len(media[kind])} {kind} were provided')
        return '@'+{'images':'image','videos':'video','audios':'audio'}[kind]+str(index)
    return ALIASES.sub(replace,prompt)

def normalize_request(body:dict[str,Any])->dict[str,Any]:
    name=str(body.get('model') or 'sd-2-0-mini').strip();model=MODELS.get(name)
    if not model:raise ValueError(f'unsupported model: {name}')
    prompt=str(body.get('prompt') or '').strip()
    media={'images':_media_list(body,'images','image_urls'),'videos':_media_list(body,'videos','video_urls'),'audios':_media_list(body,'audios','audio_urls')}
    for item in body.get('content') or []:
        if not isinstance(item,dict):continue
        kind=item.get('type')
        if kind=='text' and not prompt:prompt=str(item.get('text') or '').strip()
        elif kind in {'image_url','video_url','audio_url'}:
            value=item.get(kind);value=value.get('url') if isinstance(value,dict) else value
            if value:media[{'image_url':'images','video_url':'videos','audio_url':'audios'}[kind]].append(str(value))
    if not prompt:raise ValueError('prompt is required')
    raw=body.get('duration',body.get('seconds',4))
    if isinstance(raw,bool) or not str(raw).isdigit():raise ValueError('duration must be an integer')
    duration=int(raw)
    if not model.min_duration<=duration<=model.max_duration:raise ValueError(f'duration must be {model.min_duration}-{model.max_duration} seconds for {name}')
    ratio=str(body.get('aspect_ratio') or body.get('ratio') or '16:9')
    if ratio not in RATIOS or (model.family=='wan' and ratio=='21:9'):raise ValueError(f'unsupported ratio for {name}: {ratio}')
    resolution=str(body.get('resolution') or model.resolution).lower()
    if resolution!=model.resolution:raise ValueError(f'{name} uses {model.resolution}; choose the corresponding alias')
    for kind,limit in model.limits.items():
        if len(media[kind])>limit:raise ValueError(f'{name} supports at most {limit} {kind}')
    if len(media['images'])+len(media['videos'])>model.max_visual_files:
        raise ValueError(f'{name} supports at most {model.max_visual_files} image and video references combined')
    return {'model':name,'upstream_model':model.upstream_model,'advanced_mode':model.advanced_mode,'duration':duration,'resolution':resolution,'aspect_ratio':ratio,'prompt':bind_prompt(prompt,media),**media}

def public_models()->list[dict[str,Any]]:
    return [{'id':m.id,'object':'model','owned_by':'nimapi','capabilities':{'durations':list(range(m.min_duration,m.max_duration+1)),'resolutions':[m.resolution],'aspect_ratios':sorted(RATIOS-({'21:9'} if m.family=='wan' else set())),'media_limits':m.limits,'max_visual_files':m.max_visual_files}} for m in MODELS.values()]
