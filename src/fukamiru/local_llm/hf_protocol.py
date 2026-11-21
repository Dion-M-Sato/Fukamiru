"""Pinned model and runtime artifacts for the 33B BF16 proofreading backend."""

MODEL = {
    "model_repo": "llm-jp/llm-jp-4.1-33b-thinking-gguf",
    "model_revision": "f91445c4ac24e9b75357681017f955e7e14bbca2",
    "model_file": "llm-jp-4.1-33b-thinking-BF16.gguf",
    "model_bytes": 66445273632,
    "model_sha256": "af885a7fdd474b7f009a2fb8ab694daa2f9c9ce91660ad4526d4265f98673325",
    "llama_version": "b12375",
    "llama_assets": [
        ("cudart-llama-b12375-bin-ubuntu-cuda-12.8-x64.tar.gz",
         "85e106462d96a5506383a3833c607607196669e65c736964dcb4c76e84e6db67"),
        ("llama-b12375-bin-ubuntu-cuda-12.8-x64.tar.gz",
         "36ad425cb846ec831ec48b7789b032e43e6df931f95eed7fcbd7fabdbd521225"),
    ],
}
