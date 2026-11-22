# WhisperX batch worker. CUDA and cuDNN come from the pip wheels (torch cu128),
# so a plain Python base is enough; the host only needs the NVIDIA driver and
# container runtime.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && python -m nltk.downloader -d /usr/share/nltk_data punkt_tab \
 && pip freeze > /app/requirements.lock

COPY transcribe.py /app/transcribe.py

# Models are never downloaded at run time: mount the model share at /models.
ENV HF_HOME=/models/hf \
    TORCH_HOME=/models/torch \
    PYANNOTE_CACHE=/models/hf/hub \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    NLTK_DATA=/usr/share/nltk_data \
    # pyannote 3.x checkpoints are full pickles; torch >= 2.6 refuses them by
    # default. The checkpoints come from the curated model share.
    TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/mpl \
    NUMBA_CACHE_DIR=/tmp/numba \
    PYTHONUNBUFFERED=1

USER 1000:1000
WORKDIR /tmp
ENTRYPOINT ["python", "/app/transcribe.py"]
