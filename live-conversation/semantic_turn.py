"""Local Smart Turn v3.2 inference for semantic end-of-turn decisions.

The model is maintained by Pipecat and consumes the final eight seconds of
16 kHz mono PCM.  This wrapper keeps model loading optional so Live
Conversation can still start with conservative endpointing if the asset is
temporarily unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class TurnDecision:
    complete: bool
    probability: float
    source: str


class SemanticTurnDetector:
    def __init__(self, model_path: str):
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"Smart Turn model not found: {path}")
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._extractor = WhisperFeatureExtractor(chunk_length=8)
        self._session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )

    def predict(self, pcm16: bytes, threshold: float = 0.5) -> TurnDecision:
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        target_samples = 8 * 16_000
        if len(audio) > target_samples:
            audio = audio[-target_samples:]
        elif len(audio) < target_samples:
            audio = np.pad(audio, (target_samples - len(audio), 0))
        inputs = self._extractor(
            audio,
            sampling_rate=16_000,
            return_tensors="np",
            padding="max_length",
            max_length=target_samples,
            truncation=True,
            do_normalize=True,
        )
        features = inputs.input_features.astype(np.float32)
        output = self._session.run(None, {"input_features": features})
        probability = float(np.asarray(output[0]).reshape(-1)[0])
        return TurnDecision(probability >= threshold, probability, "smart-turn-v3.2")

