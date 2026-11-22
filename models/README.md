# Step 1 — Model share for WhisperX (air-gap preparation)

Part of the [WhisperX pipeline](../README.md).

The pipeline never downloads models at run time. All models live on an NFS
share that is mounted read-only into the transcription pods. This step
attaches the share to Kubernetes and fills it once, from a cluster (or any
machine) that has internet access.

| Item | Value |
|---|---|
| NFS server | set `server` in `10-storage.yaml` (the test lab used Nutanix Files) |
| Export | `/voice-models` (created by hand on the file server) |
| Namespace | `whisperx` |
| PV / PVC | `whisperx-voice-models` / `voice-models` |
| Mount point in pods | `/models` |

## Files

| File | Purpose |
|---|---|
| `10-storage.yaml` | Namespace, static NFS PersistentVolume, and the PVC bound to it |
| `20-download-job.yaml` | Download script (ConfigMap) and the one-off Job that runs it |

The PV is static (`storageClassName: ""`, plain `nfs:` source) because the
share already exists; the CSI provisioner is not involved. Reclaim policy is
`Retain`, so deleting the PV or PVC never touches the files.

## Prerequisites

1. An NFS export reachable from the worker nodes and writable by a regular
   user. The Job runs as UID/GID 1000 on purpose: the Nutanix Files export
   squashes root, and a squashed root can create files but not reopen them
   for writing, which makes the download fail half-way. Do not run it as root.
2. A Hugging Face account that has **accepted the terms** on each gated model
   page (log in, open the page, click "Agree"):
   - <https://huggingface.co/pyannote/speaker-diarization-3.1>
   - <https://huggingface.co/pyannote/segmentation-3.0>
   - <https://huggingface.co/pyannote/speaker-diarization-community-1> (optional)
3. A Hugging Face **read** token for that account.
4. Internet access from the pod to `huggingface.co` and `download.pytorch.org`.

## Run

Set `server` and `path` in `10-storage.yaml`, then:

```bash
kubectl apply -f 10-storage.yaml
```

Create the token Secret. The token is deliberately not stored in any file:

```bash
kubectl -n whisperx create secret generic hf-token --from-literal=token=<HF_TOKEN>
```

Start the download and follow it:

```bash
kubectl apply -f 20-download-job.yaml
```

```bash
kubectl -n whisperx logs -f job/model-download
```

The Job prints one `OK` / `SKIP` / `FAIL` line per model. `SKIP` is an
optional model that could not be fetched; `FAIL` is a required one and makes
the Job fail. The usual cause of `FAIL` on a pyannote model is that the terms
were not accepted with the account that owns the token.

To re-run (it resumes and skips finished files):

```bash
kubectl -n whisperx delete job model-download && kubectl apply -f 20-download-job.yaml
```

When the share is complete, remove the token from the cluster:

```bash
kubectl -n whisperx delete secret hf-token
```

## Models

| Stage | Model | Source | Required |
|---|---|---|---|
| Transcription | `Systran/faster-whisper-large-v3` | Hugging Face | yes |
| Transcription (faster option) | `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | Hugging Face | no |
| Alignment, English | `wav2vec2_fairseq_base_ls960_asr_ls960.pth` | download.pytorch.org (torchaudio) | yes |
| Alignment, Arabic | `jonatasgrosman/wav2vec2-large-xlsr-53-arabic` | Hugging Face | yes |
| Diarization pipeline | `pyannote/speaker-diarization-3.1` (gated) | Hugging Face | yes |
| Diarization segmentation | `pyannote/segmentation-3.0` (gated) | Hugging Face | yes |
| Diarization embedding | `pyannote/wespeaker-voxceleb-resnet34-LM` | Hugging Face | yes |
| Diarization, pyannote 4.x | `pyannote/speaker-diarization-community-1` (gated) | Hugging Face | no |

Voice activity detection needs no download: the default pyannote VAD model
ships inside the WhisperX pip package.

To add a language, add its alignment model to `HF_MODELS` in
`20-download-job.yaml` (WhisperX lists them in `whisperx/alignment.py`) and
re-run the Job.

## Layout on the share

```
/voice-models
├── hf/                     HF_HOME    (Hugging Face cache, hub/models--<org>--<name>/...)
├── torch/                  TORCH_HOME (hub/checkpoints/*.pth)
└── MANIFEST.txt            model list with the exact revision downloaded
```

Pipeline pods mount the PVC read-only and set:

```yaml
env:
  - { name: HF_HOME,        value: /models/hf }
  - { name: TORCH_HOME,     value: /models/torch }
  - { name: HF_HUB_OFFLINE, value: "1" }
volumeMounts:
  - { name: models, mountPath: /models, readOnly: true }
```

## Result in this lab (2026-11-22)

All required models downloaded; about 6 GB on the share in total.

| Model | Revision | Size |
|---|---|---|
| `Systran/faster-whisper-large-v3` | `edaa852e` | 2.9 GB |
| `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | `0a363e91` | 1.6 GB |
| `jonatasgrosman/wav2vec2-large-xlsr-53-arabic` | `af46c2d8` | 1.2 GB |
| `pyannote/speaker-diarization-3.1` | `84fd2591` | 11 MB |
| `pyannote/segmentation-3.0` | `e66f3d3b` | 6 MB |
| `pyannote/wespeaker-voxceleb-resnet34-LM` | `837717dd` | 26 MB |
| `wav2vec2_fairseq_base_ls960_asr_ls960.pth` | — | 360 MB |
| `pyannote/speaker-diarization-community-1` | not downloaded (403, terms not accepted); not needed by the pinned WhisperX 3.7.9 | — |

The first run was done as root and failed at the manifest write; the re-run
as UID 1000 completed. Because of that first run, some cache folders are
owned by the squashed root user and the re-run logs harmless
`Ignored error while writing tree cache file` warnings. A fresh share filled
only by the non-root Job does not show them.

## Moving to a fully air-gapped environment

The download Job cannot run there. Instead:

1. Run this step in a connected environment.
2. Copy the whole share content (`hf/`, `torch/`, `MANIFEST.txt`) to the
   air-gapped NFS export, preserving symlinks (the Hugging Face cache uses
   them), e.g. `rsync -a` or `tar`.
3. In the air-gapped cluster apply only `10-storage.yaml` (with the right
   `server` / `path`). No Secret and no Job are needed.
