from .prelude import *
from scannerpy import FrameType, Kernel
from typing import Sequence

import numpy as np
import pickle
import pysrt
import codecs
import math
import sys
import os
import cv2
import logging
import time
import re
import tempfile
import scipy.io.wavfile as wavf
import multiprocessing

if not '/app/gentle' in sys.path:
    sys.path.append('/app/gentle')
import gentle

"""
Help functions for fid, time, second transfer
"""
def fid2second(fid, fps):
    second = 1. * fid / fps
    return second

def time2second(time):
    if len(time) == 3:
        return time[0]*3600 + time[1]*60 + time[2]
    elif len(time) == 4:
        return time[0]*3600 + time[1]*60 + time[2] + time[3] / 1000.0

def second2time(second, sep=','):
    h, m, s, ms = int(second) // 3600, int(second % 3600) // 60, int(second) % 60, int((second - int(second)) * 1000)
    return '{:02d}:{:02d}:{:02d}{:s}{:03d}'.format(h, m, s, sep, ms)


class TranscriptAligner():
    def __init__(self, seg_length=60, max_misalign=10, num_thread=8, transcript_path=None, media_path=None, align_dir=None):
        self.seg_length = seg_length
        self.text_shift = max_misalign
        self.num_thread = num_thread
        self.transcript_path = transcript_path
        self.media_path = media_path
        self.align_dir = align_dir
        self.sequential = True if self.media_path is None else False
        
        self.audio_shift = 1
        self.seg_idx = 0
        self.punctuation_all = ['>>', ',', ':', '[.]', '[?]']
        
        if not self.media_path is None:
            _, ext = os.path.splitext(self.media_path)
            self.video_name = os.path.basename(self.media_path)
        if ext == '.mp4':
            cap = cv2.VideoCapture(self.media_path)
            self.video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self.fps = cap.get(cv2.CAP_PROP_FPS)
            self.video_length = int(self.video_frames // self.fps)
            self.num_seg = int(self.video_length // self.seg_length)
        elif ext == '.wav':
            raise Exception("Not implemented error")
    
    def load_transcript(self, transcript_path):
        """"
        Load transcript from *.srt file
        """
        # Check file exist
        if not os.path.exists(transcript_path):
#             transcript_path = transcript_path.replace('cc5', 'cc1') 
#             if not os.path.exists(transcript_path):
            raise Exception("Transcript file does not exist")

        # Check encoded in uft-8
        try:
            file = codecs.open(transcript_path, encoding='utf-8', errors='strict')
            for line in file:
                pass
        except UnicodeDecodeError:
            raise Exception("Transcript not encoded in utf-8")

        transcript = []
        subs = pysrt.open(transcript_path)
        text_length = 0
        for sub in subs:
            transcript.append((sub.text, time2second(tuple(sub.start)[:3]), time2second(tuple(sub.end)[:3])))
            text_length += transcript[-1][2] - transcript[-1][1]

#         Check transcript completeness     
#             if 1. * text_length / video_desp['video_length'] < MIN_TRANSCRIPT:
#                 raise Exception("Transcript not complete")
        self.transcript = transcript

    def extract_transcript_segment(self, seg_idx):
        start = seg_idx * self.seg_length - self.text_shift
        end = (seg_idx + 1) * self.seg_length + self.text_shift
        punctuation_seg = []
        transcript_seg = ''
        for (text, ss, ee) in self.transcript:
            if ss > end:
                break
            if ss >= start:
                offset = len(transcript_seg)
                for p in self.punctuation_all:
                    pp = p.replace('[', '').replace(']', '')
                    for match in re.finditer(p, text):
                        punctuation_seg.append((match.start()+offset, pp))
                transcript_seg += text + ' ' 
        punctuation_seg.sort()
        return transcript_seg, punctuation_seg

    def dump_audio_segment(self, seg_idx, audio):
        ar = audio[0].shape[0] // self.seg_length
        audio_shift = ar * self.audio_shift
        if seg_idx == 0:
            audio_cat = np.concatenate((audio[1], audio[2][:audio_shift, ...]), 0)
        else:
            audio_cat = np.concatenate((audio[0][-audio_shift:, ...], audio[1], audio[2][:audio_shift, ...]), 0)
        audio_path = tempfile.NamedTemporaryFile(suffix='.wav').name
        wavf.write(audio_path, ar, audio_cat)
        return audio_path    
    
    def extract_transcript_all(self):
        self.text_seg_list = []
        self.punc_seg_list = []
        for seg_idx in range(self.num_seg):
            transcript_seg, punctuation_seg = self.extract_transcript_segment(seg_idx)
            self.text_seg_list.append(transcript_seg)
            self.punc_seg_list.append(punctuation_seg)
    
    def extract_audio_segment(self, seg_idx):
        start = seg_idx * self.seg_length
        start = start - self.audio_shift if seg_idx > 0 else start
        duration = self.seg_length 
        duration += self.audio_shift * 2 if seg_idx > 0 else self.audio_shift
        cmd = 'ffmpeg -i ' + self.media_path + ' -vn -acodec copy '
        cmd += '-ss {:d} -t {:d} '.format(start, duration)
        audio_path = tempfile.NamedTemporaryFile(suffix='.aac').name
        cmd += audio_path
        os.system(cmd)
        return audio_path
    
    def extract_audio_all(self):
        pool = multiprocessing.Pool(self.num_thread)
        self.audio_seg_list = pool.map(self.extract_audio_segment, [i for i in range(self.num_seg)])
    
    def align_segment_thread(self, seg_idx):
        return self.align_segment(seg_idx, self.audio_seg_list[seg_idx], self.text_seg_list[seg_idx], self.punc_seg_list[seg_idx])
    
    def align_segment(self, seg_idx, audio_path, transcript, punctuation):
        args = {'log': 'INFO',
            'nthreads': 1 if not self.sequential else self.num_thread,
            'conservative': True,
            'disfluency': True,
            }
        disfluencies = set(['uh', 'um'])
#         with open(self.text_seg_list[seg_idx]) as fh:
#             transcript = fh.read()

        resources = gentle.Resources()
        with gentle.resampled(audio_path) as wavfile:
            aligner = gentle.ForcedAligner(resources, transcript, nthreads=args['nthreads'], disfluency=args['disfluency'], conservative=args['conservative'], disfluencies=disfluencies)
            result = aligner.transcribe(wavfile)
            aligned_seg = [word.as_dict(without="phones") for word in result.words]

        # insert punctuation
        start_idx = 0
        for offset, p in punctuation:
            for word_idx, word in enumerate(aligned_seg[start_idx:]):
                if word['case'] != 'not-found-in-transcript': 
                    if p == '>>' and (offset == word['startOffset'] - 3 or offset == word['startOffset'] - 4): 
                        word['word'] = '>> ' + word['word']
                        start_idx += word_idx
                        break
                    if p != '>>' and offset == word['endOffset']:
                        word['word'] = word['word'] + p
                        start_idx += word_idx
                        break
        
        # post-process
        align_word_list = []
        seg_start = seg_idx * self.seg_length
        seg_start = seg_start - self.audio_shift if seg_idx > 0 else seg_start
        seg_shift = self.audio_shift if seg_idx > 0 else 0

        enter_alignment = False
        word_missing = []
        num_word_missing = 0
        for word_idx, word in enumerate(aligned_seg):
            if word['case'] == 'not-found-in-transcript':
                # align_word_list.append(('[Unknown]', (word['start'] + seg_start, word['end'] + seg_start)))
                pass
            elif word['case'] == 'not-found-in-audio':
                if enter_alignment:
                    word_missing.append(word['word'])
                    num_word_missing += 1
            else:
                assert(word['case'] == 'success')
                if word['start'] > self.seg_length + seg_shift:
                    break
                elif word['start'] >= seg_shift:
                    enter_alignment = True
                    if len(word_missing) <= 2:
                        num_word_missing -= len(word_missing)
                    if len(word_missing) > 0:
                        start = align_word_list[-1][1][1]
                        end = word['start'] + seg_start
                        step = (end - start) / len(word_missing)
                        for i, w in enumerate(word_missing):
                            align_word_list.append((w, (start+i*step, start+(i+1)*step)))
                        word_missing = []

                    align_word_list.append((word['word'], (word['start'] + seg_start, word['end'] + seg_start)))
        return {'align_word_list': align_word_list, 'num_word_missing': num_word_missing}    

    def dump_aligned_transcript(self, align_word_list, path):
        SRT_INTERVAL = 1
        outfile = open(path, 'w')
        start, end = None, None
        srt_idx = 1
        for idx, word in enumerate(align_word_list):
            if start is None:
                start, end = word[1]
                text = word[0] + ' '
                continue
            if word[1][0] > start + SRT_INTERVAL:
                line = str(srt_idx) + '\n'
                line += '{:s} --> {:s}\n'.format(second2time(start), second2time(end))
                line += text + '\n\n'
                outfile.write(line)
                start, end = word[1]
                text = word[0] + ' '
                srt_idx += 1
            else:
                text += word[0] + ' '
                end = word[1][1]
        line = str(srt_idx) + '\n'
        line += '{:s} --> {:s}\n'.format(second2time(start), second2time(end))
        line += text + '\n\n'
        outfile.write(line)
        outfile.close()

    def dump_aligned_transcript_byword(self, align_word_list, path):
        outfile = open(path, 'w')
        srt_idx = 1
        for idx, word in enumerate(align_word_list):
            start, end = word[1]
            line = str(srt_idx) + '\n'
            line += '{:s} --> {:s}\n'.format(second2time(start), second2time(end))
            line += word[0] + '\n\n'
            outfile.write(line)
            srt_idx += 1
        outfile.close()   
   
    def run_sequential(self, audio):
        transcript, punctuation = self.extract_transcript(self.seg_idx)
        audio_path = self.dump_audio(self.seg_idx, audio)
        result_seg = self.align_segment(self.seg_idx, audio_path, transcript, punctuation)
        self.seg_idx += 1
        return result_seg
    
    def run_all(self):
        self.load_transcript(self.transcript_path)
        self.extract_transcript_all()
        print("Extracting transcripts done")
        self.extract_audio_all()
        print("Extracting audio done")
        pool = multiprocessing.Pool(self.num_thread)
        self.result_all = pool.map(self.align_segment_thread, [i for i in range(self.num_seg)])
    
        align_word_list = []
        num_word_missing = 0
        for seg in self.result_all:
            align_word_list += [word for word in seg['align_word_list']]
            num_word_missing += seg['num_word_missing']
        print('word_missing', 1. * num_word_missing / len(align_word_list))
        if not self.align_dir is None:
            output_path = os.path.join(self.align_dir, self.video_name + '.word.srt')
            self.dump_aligned_transcript_byword(align_word_list, output_path)
            output_path = os.path.join(self.align_dir, self.video_name + '.align.srt')
            self.dump_aligned_transcript(align_word_list, output_path)
        

@scannerpy.register_python_op(unbounded_state=True, stencil=[-1,0,1])
class AlignTranscript(Kernel):
    def __init__(self, config):
        seg_length = config.args.get('seg_length', 60)
        text_shift = config.args.get('max_misalign', 10)
        num_thread = config.args.get('num_thread', 8)
        self.aligner = TranscriptAligner(seg_length, text_shift, num_thread)
    
    def new_stream(self, args):
        self.aligner.load_transcript(args['transcript_path'])
        
    def execute(self, audio: Sequence[FrameType]) -> bytes:
#         for idx, aud in enumerate(audio):
#             print(self.seg_idx, idx, aud.shape)
#             if self.seg_idx == 59:
#                 audio_path = '../app/data/seg_{:}.wav'.format(idx)
#                 wavf.write(audio_path, 44100, aud)
#         self.seg_idx += 1
#         return pickle.dumps([])
        
        result_seg = self.aligner.run_sequential(audio)
        return pickle.dumps(result_seg)


class AlignTranscriptPipeline(Pipeline):
    job_suffix = 'align_transcript'
    base_sources = ['audio']
    run_opts = {'pipeline_instances_per_node': 16}
    custom_opts = ['transcript']
    parser_fn = lambda _: lambda buf, _: pickle.loads(buf)

    def build_pipeline(self):
        return {
            'align_transcript':
            self._db.ops.AlignTranscript(
                audio=self._sources['audio'].op, 
                seg_length = 60,
                max_misalign = 10,
                )
        }

    def _build_jobs(self, cache):
        jobs = super(AlignTranscriptPipeline, self)._build_jobs(cache)
        for (job, transcript) in zip(jobs, self._custom_opts['transcript']):
            job._op_args[self._output_ops['align_transcript']] = {'transcript_path': transcript}
        return jobs

    def build_sink(self):
        return BoundOp(
            op=self._db.sinks.Column(columns=self._output_ops),
            args=[
                '{}_{}'.format(arg['path'], self.job_suffix)
                for arg in self._sources['audio'].args
            ])

align_transcript_pipeline = AlignTranscriptPipeline.make_runner()

def align_transcript(db, audio, transcript, cache=False, align_dir=None, res_path=None):
    result = align_transcript_pipeline(db=db, audio=audio, transcript=transcript, cache=cache)
    res_stats = {}
    for idx, res_video in enumerate(result):
        video_name = os.path.basename(transcript[idx])
        align_word_list = []
        num_word_missing = 0
        for seg in res_video.load():
            align_word_list += [word for word in seg['align_word_list']]
            num_word_missing += seg['num_word_missing']
        res_stats[video_name] = {'word_missing': 1. * num_word_missing / len(align_word_list)}
        if not align_dir is None:
            output_path = os.path.join(align_dir, video_name + '.word.srt')
            dump_aligned_transcript_byword(align_word_list, output_path)
            output_path = os.path.join(align_dir, video_name + '.align.srt')
            dump_aligned_transcript(align_word_list, output_path)
    if not res_path is None:
        pickle.dump(res_stats, open(res_path, 'wb'))
    return result