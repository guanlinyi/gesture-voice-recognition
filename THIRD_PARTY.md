# 第三方组件与模型来源

Python 包的固定版本见 `requirements.txt`。下列网址记录本程序使用的具体来源，不表示所有模型采用同一种许可证；模型使用和再分发条件以各上游的模型卡及许可证为准。大模型、引擎源码及二进制不随本仓库上传。

## 手势：Google MediaPipe

基础手势与 21 点估计使用 MediaPipe Gesture Recognizer；叠加本项目精简学习的 73 个数值参考样本。距离特征匹配是均方根距离的最近参考匹配，不训练或修改 MediaPipe 网络。

https://github.com/google-ai-edge/mediapipe

https://ai.google.dev/edge/mediapipe/solutions/vision/gesture_recognizer

固定模型下载：

https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task

SHA256：`97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482`

## 语音转写：X-ASR / sherpa-onnx

使用 480 ms 流式 Zipformer Transducer 中英标点 int8 模型，CPU 默认 2 线程。480 ms 为模型块长，不是端到端延迟承诺。

https://github.com/k2-fsa/sherpa-onnx

https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05.tar.bz2

压缩包 SHA256：`fa5f63d618e5a01526e275a358bb7772e403f84808a4769fba52cffd8160bf74`

## 语音合成：MyShell MeloTTS / sherpa-onnx

使用中英 VITS ONNX 声音，CPU 默认 6 线程。

https://github.com/myshell-ai/MeloTTS

https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2

压缩包 SHA256：`e58351ed7149f290a54534538badd4077cdbe6fddc964b24d0bee870415d1514`

ASR / TTS 摘要取自已实测部署使用的压缩包，用于固定版本，不是上游独立签名。

## 语言理解与聊天：Qwen / llama.cpp

默认 Qwen3.5-4B，使用 Unsloth 提供的 Q4_K_M GGUF。由 llama.cpp 同一服务分别完成指令分类和普通聊天。分类只能选择三个已定义命令；聊天文本本身不执行命令。

https://huggingface.co/Qwen/Qwen3.5-4B

https://huggingface.co/unsloth/Qwen3.5-4B-GGUF

https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf

权重 SHA256：`00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4`

引擎：

https://github.com/ggml-org/llama.cpp

固定 commit：`8642541b255bfb5dc39cd351a178be09013a55d6`（v0.8.0）。构建时核对 commit。

## 中文字体：Noto Sans CJK SC

包含 Regular / Bold OTF，来自本机 Noto CJK 2.004 字体集合的简体中文字体面，未修改字形。完整版权与许可保留在 `steering_control/hardware/fonts/noto_sans_sc/LICENSE`，包括 SIL Open Font License 1.1。

https://github.com/notofonts/noto-cjk

https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE

## 本项目代码

本次整理未代替作者选择原创代码的开源许可证。第三方许可证仅适用于各自组件。
