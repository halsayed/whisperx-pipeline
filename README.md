# WhisperX voice-to-text pipeline on Kubernetes

A batch pipeline that takes voice recordings from an S3 bucket, transcribes
them on a GPU with [WhisperX](https://github.com/m-bain/whisperX), and writes
the results to another bucket (or prefix). For each recording it produces the
text, word-level timestamps, and a speaker-labelled dialog.

It is built to run **air-gapped**: models come from a pre-filled NFS share and
the worker never contacts the internet.

```
 S3 bucket (in/)                Kubernetes Job (1 GPU)                 S3 bucket (out/)
 call-01.wav   ─────────►  transcribe ─► align ─► diarize  ─────────►  call-01.json
 call-02.mp3                    ▲                                      call-01.srt / .vtt
                                │ read-only                            call-01.dialog.txt
                     NFS share /models (Whisper, wav2vec2, pyannote)
```

| Stage | What it does | Model |
|---|---|---|
| Transcribe | Speech to text, language detection | faster-whisper `large-v3` |
| Align | Word-level timestamps | wav2vec2 (one per language: English, Arabic) |
| Diarize | Who spoke when | `pyannote/speaker-diarization-3.1` |

## Repository layout

| Path | Purpose |
|---|---|
| `models/` | Step 1: attach the NFS model share and download the models ([guide](models/README.md)) |
| `worker/` | Worker image: `Dockerfile`, `transcribe.py`, pinned `requirements.txt`, full `requirements.lock` |
| `build/` | Step 2: optional lab registry and in-cluster image build |
| `deploy/` | Step 3: pipeline config, the Job, optional egress policy |
| `samples/` | Script that generates the two synthetic test recordings |

## Prerequisites

- Kubernetes with NVIDIA GPUs exposed as `nvidia.com/gpu` (GPU operator or DRA
  driver). Tested on NKP, Kubernetes 1.36, NVIDIA driver 580.
- An NFS export for models (about 6 GB used).
- An S3-compatible endpoint, an access key with list/read on the input
  location and write on the output location.
- A container registry the cluster can pull from, or the lab registry in
  `build/`.
- A Hugging Face account for the one-time model download (not needed in the
  air-gapped site).

## Step 1 — Models

Follow [models/README.md](models/README.md). At the end the namespace
`whisperx` exists and the PVC `voice-models` holds all models.

## Step 2 — Worker image

The image is `python:3.12-slim` + ffmpeg + the pinned Python packages. CUDA and
cuDNN come from the pip wheels, so no NVIDIA base image is needed.

**Build with Docker** (about 10 minutes; the result is 4.6 GB compressed,
linux/amd64):

```bash
docker build --platform linux/amd64 -t <registry>/whisperx-worker:3.7.9-1 worker/
```

```bash
docker login -u <user>
```

```bash
docker push <registry>/whisperx-worker:3.7.9-1
```

Then set that image name in `deploy/20-job.yaml`. The default there,
`docker.io/aksiio/whisperx-worker:3.7.9-1`, is the author's build in a
private Docker Hub repository, so replace it unless you have access.

If your registry is private, create a pull Secret; the Job already refers to
it by the name `dockerhub-pull` (for Docker Hub the server is
`https://index.docker.io/v1/`):

```bash
kubectl -n whisperx create secret docker-registry dockerhub-pull --docker-server=<registry> --docker-username=<user> --docker-password=<read-only token>
```

The build resolves the same
package versions as `worker/requirements.lock`; compare with
`docker run --rm --entrypoint cat <image> /app/requirements.lock`.

**Without Docker or a registry (used for the first test run):** build inside the
cluster with kaniko and push to a small in-cluster registry. Nodes pull from
it as `localhost:30500/...`, which needs no node configuration. The registry
has no authentication or TLS; use it only in a lab.

```bash
kubectl apply -f build/10-lab-registry.yaml
```

```bash
kubectl -n whisperx create configmap worker-src --from-file=worker/ --dry-run=client -o yaml | kubectl apply -f -
```

```bash
kubectl apply -f build/20-build-job.yaml
```

```bash
kubectl -n whisperx logs -f job/worker-build
```

The build takes about 10 minutes; the image is 4.6 GB compressed. To rebuild
after a change, delete the Job, re-create the ConfigMap, and apply again.

## Step 3 — Configure and run

1. Edit `deploy/10-config.yaml`: S3 endpoint, buckets, prefixes, model options.
2. Create the S3 credentials Secret (keys are never stored in the repo):

   ```bash
   kubectl -n whisperx create secret generic s3-credentials --from-literal=AWS_ACCESS_KEY_ID=<key> --from-literal=AWS_SECRET_ACCESS_KEY=<secret>
   ```

3. Edit `deploy/20-job.yaml`: the image name, the GPU model in `nodeSelector`,
   and `hostAliases` (only if cluster DNS cannot resolve your S3 host).
4. Apply the config and, optionally, the egress policy (set its CIDR to your
   S3 endpoint IP):

   ```bash
   kubectl apply -f deploy/10-config.yaml -f deploy/30-egress-policy.yaml
   ```

5. Upload recordings to the input prefix, then start a run:

   ```bash
   kubectl create -f deploy/20-job.yaml
   ```

   ```bash
   kubectl -n whisperx logs -f -l app=whisperx-transcribe
   ```

Each run processes every audio file under the input prefix that has no
`.json` output yet, then exits. Running it again only picks up new files.
Supported inputs: wav, mp3, m4a, mp4, flac, ogg, opus, aac, wma, webm, amr.

### Settings (`deploy/10-config.yaml`)

| Variable | Default | Meaning |
|---|---|---|
| `S3_ENDPOINT_URL` | — | S3 endpoint |
| `S3_VERIFY_SSL` | `true` | `false` for self-signed certificates |
| `INPUT_BUCKET` / `INPUT_PREFIX` | — | Where recordings are read from |
| `OUTPUT_BUCKET` / `OUTPUT_PREFIX` | — | Where results are written (may be the same bucket) |
| `WHISPER_MODEL` | `large-v3` | `large-v3-turbo` is also on the share |
| `COMPUTE_TYPE` | `float16` | `int8_float16` for GPUs with 8 GB or less |
| `BATCH_SIZE` | `16` | Use `4` on small GPUs |
| `LANGUAGE` | empty | `ar` / `en` to force; empty detects per file |
| `DIARIZE` | `true` | Speaker labels on or off |
| `MIN_SPEAKERS` / `MAX_SPEAKERS` | empty | Optional hints for diarization |
| `OVERWRITE` | `false` | `true` re-processes finished files |

## Outputs

For `in/call-01.wav` the Job writes, under the output prefix:

| File | Content |
|---|---|
| `call-01.json` | Language, duration, speakers, speaker turns, segments, every word with start/end time, confidence and speaker |
| `call-01.srt`, `call-01.vtt` | Subtitles with speaker tags |
| `call-01.dialog.txt` | Readable dialog, one line per speaker turn |

Dialog example (from the test run):

```
[00:00:00 - 00:00:05] SPEAKER_01: Good morning, and thank you for calling the support desk. My name is Sarah. How can I help you today?
[00:00:06 - 00:00:13] SPEAKER_00: Hello Sarah, I am having trouble with my storage cluster. One of the nodes went offline last night at around 11.30.
```

The log ends with one `OK` line per file (timings per stage) and a `SUMMARY`
line (files, failures, audio seconds, wall seconds, peak GPU memory). The Job
fails if any file fails; the other files are still processed.

## Test result (2026-11-22)

Run as the Job in `deploy/`, with the egress policy applied, on an
**RTX A2000 (6 GB)** with `COMPUTE_TYPE=int8_float16`, `BATCH_SIZE=4`.

| Recording | Language | Length | Processing | Speed | Speakers found |
|---|---|---|---|---|---|
| `support-call-ar.wav` | Arabic (detected) | 58.8 s | 12.3 s | 4.8× real time | 2 of 2 |
| `support-call-en.mp3` | English (detected) | 39.8 s | 5.5 s | 7.3× real time | 2 of 2 |

- Model load 9 s; whole run 27 s; peak GPU memory 3.1 GiB.
- All words got timestamps and a speaker (78 Arabic, 118 English).
- All six speaker turns were correct in both dialogs.
- A pod with the worker's label could not reach huggingface.co or pypi.org,
  only the S3 endpoint, so the run used no internet.
- A second run found "0 to process, 2 already done".

Limits of this test: the recordings are short and synthetic (macOS
text-to-speech; the second Arabic speaker is the same voice pitch-shifted), so
they say little about accuracy on real calls. The first file of each language
includes loading its alignment model, which lowers the speed figure. No run
has been done yet on a larger GPU with `float16` and batch size 16.

## Moving to an air-gapped site

1. **Models:** copy the share content as described in
   [models/README.md](models/README.md); apply only `models/10-storage.yaml`.
2. **Image:** export it from the connected side and load it into the site's
   registry, for example `docker save` / `docker load` / `docker push`, or
   `skopeo copy`. (Not exercised in this lab.)
3. **Deploy:** Step 3 as above. No Hugging Face token is needed.

## Things that were not obvious

- **WhisperX version.** 3.8+ uses pyannote.audio 4, which always loads a file
  from the gated `pyannote/speaker-diarization-community-1`, even when asked
  for the 3.1 pipeline. This build pins WhisperX 3.7.9 with pyannote.audio
  3.4.0. To move to 3.8+, accept that model's terms, add it to the share, and
  set `DIARIZE_MODEL=pyannote/speaker-diarization-community-1`.
- **pyannote cache.** pyannote.audio 3.x ignores `HF_HOME`; the image sets
  `PYANNOTE_CACHE=/models/hf/hub` so it finds the models on the share.
- **torch.load.** torch 2.6+ refuses pyannote 3.x checkpoints by default; the
  image sets `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1`. Only put trusted models on
  the share.
- **NLTK data.** Alignment needs the `punkt_tab` tokenizer; it is baked into
  the image because it would otherwise be downloaded at run time.
- **NFS root squash.** See the note in `models/README.md`; the worker runs as
  UID 1000 and only reads the share.
- **Kaniko and ConfigMaps.** ConfigMap files are symlinks, so the build Job
  copies them into an emptyDir before building.

## Clean up

```bash
kubectl delete namespace whisperx
```

```bash
kubectl delete pv whisperx-voice-models
```

The PV uses `Retain`, so the models stay on the NFS share.
