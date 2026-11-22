#!/usr/bin/env python3
"""WhisperX batch worker.

Reads audio files from an S3 bucket/prefix, runs transcription, word-level
alignment and speaker diarization on a GPU, and writes the results to another
S3 bucket/prefix. Models are loaded once and reused for every file.

All configuration is by environment variable (see README.md). Nothing is
downloaded at run time: models come from HF_HOME / TORCH_HOME on a mounted
share and HF_HUB_OFFLINE=1 should be set.

For a test without S3, set LOCAL_INPUT_DIR and LOCAL_OUTPUT_DIR instead.
"""
import gc
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

import torch
import whisperx
from whisperx.diarize import DiarizationPipeline

log = logging.getLogger("worker")

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".aac", ".wma", ".webm", ".amr"}
SAMPLE_RATE = 16000


def env(name, default=None):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def env_bool(name, default):
    return str(env(name, default)).lower() in ("1", "true", "yes", "on")


def env_int(name, default=None):
    v = env(name)
    return default if v is None else int(v)


CFG = {
    "endpoint": env("S3_ENDPOINT_URL"),
    "verify_ssl": env_bool("S3_VERIFY_SSL", "true"),
    "in_bucket": env("INPUT_BUCKET"),
    "in_prefix": env("INPUT_PREFIX", ""),
    "out_bucket": env("OUTPUT_BUCKET"),
    "out_prefix": env("OUTPUT_PREFIX", ""),
    "local_in": env("LOCAL_INPUT_DIR"),
    "local_out": env("LOCAL_OUTPUT_DIR"),
    "model": env("WHISPER_MODEL", "large-v3"),
    "compute_type": env("COMPUTE_TYPE", "float16"),
    "batch_size": env_int("BATCH_SIZE", 16),
    "language": env("LANGUAGE"),  # e.g. "ar" or "en"; empty = detect per file
    "diarize": env_bool("DIARIZE", "true"),
    "diarize_model": env("DIARIZE_MODEL", "pyannote/speaker-diarization-3.1"),
    "min_speakers": env_int("MIN_SPEAKERS"),
    "max_speakers": env_int("MAX_SPEAKERS"),
    "overwrite": env_bool("OVERWRITE", "false"),
    "device": env("DEVICE", "cuda"),
}


# ---------------------------------------------------------------- storage --
class S3Store:
    def __init__(self):
        import boto3
        from botocore.config import Config

        self.s3 = boto3.client(
            "s3",
            endpoint_url=CFG["endpoint"],
            verify=CFG["verify_ssl"],
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5}),
        )
        if not CFG["in_bucket"] or not CFG["out_bucket"]:
            sys.exit("INPUT_BUCKET and OUTPUT_BUCKET are required")

    def list_inputs(self):
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=CFG["in_bucket"], Prefix=CFG["in_prefix"]
        ):
            for o in page.get("Contents", []):
                if Path(o["Key"]).suffix.lower() in AUDIO_EXT:
                    keys.append(o["Key"])
        return sorted(keys)

    def rel(self, key):
        return key[len(CFG["in_prefix"]):].lstrip("/")

    def out_key(self, rel_name):
        p = CFG["out_prefix"].strip("/")
        return f"{p}/{rel_name}" if p else rel_name

    def exists(self, rel_name):
        from botocore.exceptions import ClientError

        try:
            self.s3.head_object(Bucket=CFG["out_bucket"], Key=self.out_key(rel_name))
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def fetch(self, key, dest):
        self.s3.download_file(CFG["in_bucket"], key, dest)

    def put(self, rel_name, text, content_type):
        self.s3.put_object(
            Bucket=CFG["out_bucket"],
            Key=self.out_key(rel_name),
            Body=text.encode("utf-8"),
            ContentType=f"{content_type}; charset=utf-8",
        )


class LocalStore:
    def __init__(self):
        self.src, self.dst = Path(CFG["local_in"]), Path(CFG["local_out"])
        self.dst.mkdir(parents=True, exist_ok=True)

    def list_inputs(self):
        return sorted(str(p.relative_to(self.src)) for p in self.src.rglob("*")
                      if p.suffix.lower() in AUDIO_EXT)

    def rel(self, key):
        return key

    def exists(self, rel_name):
        return (self.dst / rel_name).exists()

    def fetch(self, key, dest):
        Path(dest).write_bytes((self.src / key).read_bytes())

    def put(self, rel_name, text, content_type):
        p = self.dst / rel_name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- formats --
def ts(seconds, sep=","):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(segments):
    out = []
    for i, s in enumerate(segments, 1):
        spk = f"[{s['speaker']}] " if s.get("speaker") else ""
        out.append(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{spk}{s['text'].strip()}\n")
    return "\n".join(out)


def to_vtt(segments):
    out = ["WEBVTT\n"]
    for s in segments:
        spk = f"<v {s['speaker']}>" if s.get("speaker") else ""
        out.append(f"{ts(s['start'], '.')} --> {ts(s['end'], '.')}\n{spk}{s['text'].strip()}\n")
    return "\n".join(out)


def to_dialog(segments):
    """Group the transcript into speaker turns.

    Uses word-level speakers when alignment produced them, so a segment that
    spans two speakers is split at the change; otherwise falls back to the
    segment's speaker.
    """
    pieces = []  # (speaker, start, end, text)
    for s in segments:
        seg_spk = s.get("speaker") or "UNKNOWN"
        words = s.get("words") or []
        if words and any("speaker" in w for w in words):
            last_end = s["start"]
            for w in words:
                start = w.get("start", last_end)   # digits etc. may have no timing
                end = w.get("end", start)
                last_end = end
                pieces.append((w.get("speaker") or seg_spk, start, end, w["word"]))
        else:
            pieces.append((seg_spk, s["start"], s["end"], s["text"].strip()))

    turns = []
    for spk, start, end, text in pieces:
        if turns and turns[-1]["speaker"] == spk:
            turns[-1]["end"] = max(turns[-1]["end"], end)
            turns[-1]["text"] += " " + text
        else:
            turns.append({"speaker": spk, "start": start, "end": end, "text": text})
    for t in turns:
        t["start"], t["end"] = round(t["start"], 3), round(t["end"], 3)
    lines = [f"[{ts(t['start'], '.')[:8]} - {ts(t['end'], '.')[:8]}] {t['speaker']}: {t['text']}" for t in turns]
    return "\n".join(lines) + "\n", turns


# ----------------------------------------------------------------- models --
class Models:
    def __init__(self):
        dev = CFG["device"]
        t = time.time()
        self.asr = whisperx.load_model(
            CFG["model"], dev, compute_type=CFG["compute_type"], language=CFG["language"]
        )
        log.info("loaded whisper %s (%s) in %.1fs", CFG["model"], CFG["compute_type"], time.time() - t)
        self.align = {}  # language -> (model, metadata) or None when unavailable
        self.diar = None
        if CFG["diarize"]:
            t = time.time()
            self.diar = DiarizationPipeline(model_name=CFG["diarize_model"], device=dev)
            log.info("loaded diarization %s in %.1fs", CFG["diarize_model"], time.time() - t)

    def aligner(self, lang):
        if lang not in self.align:
            try:
                t = time.time()
                self.align[lang] = whisperx.load_align_model(language_code=lang, device=CFG["device"])
                log.info("loaded alignment model for '%s' in %.1fs", lang, time.time() - t)
            except Exception as e:  # model for this language is not on the share
                log.warning("no alignment model for '%s' (%s); word timestamps skipped", lang, e)
                self.align[lang] = None
        return self.align[lang]


def process(models, audio_path):
    """Returns (result dict, timings dict)."""
    tm = {}
    audio = whisperx.load_audio(audio_path)
    duration = len(audio) / SAMPLE_RATE

    t = time.time()
    result = models.asr.transcribe(audio, batch_size=CFG["batch_size"], language=CFG["language"])
    lang = result["language"]
    tm["transcribe_s"] = round(time.time() - t, 2)

    aligned = False
    al = models.aligner(lang)
    if al:
        t = time.time()
        result = whisperx.align(result["segments"], al[0], al[1], audio, CFG["device"],
                                return_char_alignments=False)
        tm["align_s"] = round(time.time() - t, 2)
        aligned = True

    speakers = []
    if models.diar:
        t = time.time()
        diar = models.diar(audio, min_speakers=CFG["min_speakers"], max_speakers=CFG["max_speakers"])
        result = whisperx.assign_word_speakers(diar, result)
        tm["diarize_s"] = round(time.time() - t, 2)
        speakers = sorted({s["speaker"] for s in result["segments"] if s.get("speaker")})

    result.update({
        "language": lang,
        "duration_s": round(duration, 2),
        "word_timestamps": aligned,
        "speakers": speakers,
    })
    return result, tm


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    for noisy in ("whisperx", "pyannote", "faster_whisper", "botocore", "lightning", "pytorch_lightning"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    store = LocalStore() if CFG["local_in"] else S3Store()
    keys = store.list_inputs()
    todo = [k for k in keys
            if CFG["overwrite"] or not store.exists(str(Path(store.rel(k)).with_suffix(".json")))]
    log.info("found %d audio file(s), %d to process, %d already done", len(keys), len(todo), len(keys) - len(todo))
    if not todo:
        return 0

    if CFG["device"] == "cuda":
        if not torch.cuda.is_available():
            sys.exit("DEVICE=cuda but no GPU is visible in the container")
        g = torch.cuda.get_device_properties(0)
        log.info("GPU: %s, %.0f GiB", g.name, g.total_memory / 2**30)

    t0 = time.time()
    models = Models()
    load_s = time.time() - t0

    summary, failed = [], 0
    for key in todo:
        rel = Path(store.rel(key))
        t = time.time()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = os.path.join(tmp, "audio" + rel.suffix.lower())
                store.fetch(key, local)
                result, tm = process(models, local)
            segs = result["segments"]
            dialog, turns = to_dialog(segs)
            doc = {
                "source": key,
                "language": result["language"],
                "duration_s": result["duration_s"],
                "model": CFG["model"],
                "word_timestamps": result["word_timestamps"],
                "speakers": result["speakers"],
                "turns": turns,
                "segments": segs,
            }
            # .json is written last: its presence marks the file as done.
            store.put(str(rel.with_suffix(".srt")), to_srt(segs), "application/x-subrip")
            store.put(str(rel.with_suffix(".vtt")), to_vtt(segs), "text/vtt")
            store.put(str(rel.with_suffix(".dialog.txt")), dialog, "text/plain")
            store.put(str(rel.with_suffix(".json")), json.dumps(doc, ensure_ascii=False, indent=1), "application/json")
            wall = time.time() - t
            row = {"file": key, "status": "ok", "language": result["language"],
                   "duration_s": result["duration_s"], "wall_s": round(wall, 2),
                   "speed_x": round(result["duration_s"] / wall, 1) if wall else None,
                   "speakers": len(result["speakers"]), "segments": len(segs), **tm}
            log.info("OK %s", json.dumps(row, ensure_ascii=False))
        except Exception as e:
            failed += 1
            row = {"file": key, "status": "failed", "error": f"{type(e).__name__}: {e}"}
            log.exception("FAILED %s", key)
        summary.append(row)
        gc.collect()
        if CFG["device"] == "cuda":
            torch.cuda.empty_cache()

    total = {"files": len(summary), "failed": failed, "model_load_s": round(load_s, 1),
             "audio_s": round(sum(r.get("duration_s", 0) for r in summary), 1),
             "wall_s": round(time.time() - t0, 1)}
    if CFG["device"] == "cuda":
        total["gpu_peak_gib"] = round(torch.cuda.max_memory_allocated() / 2**30, 2)
    log.info("SUMMARY %s", json.dumps(total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
