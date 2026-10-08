"""Generate an American English WAV file using the local Kokoro model."""
import sys

import numpy as np
import soundfile as sf
from kokoro import KPipeline


output_file, voice = sys.argv[1], sys.argv[2]
text = sys.stdin.read().strip()
if not text:
    raise SystemExit("No narration script was provided.")

pipeline = KPipeline(lang_code="a", device="cpu")
segments = []
for _, _, audio in pipeline(text, voice=voice):
    if audio is not None:
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        segments.append(np.asarray(audio, dtype=np.float32).reshape(-1))
if not segments:
    raise SystemExit("Kokoro did not return any narration audio.")
sf.write(output_file, np.concatenate(segments), 24000, subtype="PCM_16")
