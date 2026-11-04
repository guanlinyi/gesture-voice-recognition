# 方向盘手势与语音控制

在 Jetson AGX Orin 上运行的手势识别、中文语音控制与本地聊天程序。安卓手机通过同一 Wi-Fi 下的网页提供麦克风和扬声器，电脑显示实时手势、用户／系统对话及命令发送结果。

支持两种控制方式：

- **手势**：OK 准备 → 工作手势 → 系统语音询问 → 用户明确确认 → 发送指令。
- **语音**：直接说“帮我把方向盘折叠”等指令；明确指令直接发送，其他表达交给本地语言模型理解或聊天。

| 工作手势 | 操作 | 数字输出 | 命令 |
| --- | --- | --- | --- |
| 握拳 | 折叠 | `1` | `FOLD` |
| 食指竖起 | 半折叠／半展开 | `2` | `HALF` |
| 手掌展开 | 展开 | `3` | `UNFOLD` |

半折叠与半展开指同一个中间状态。确认支持“是的是的”“对对对”等重复肯定；“算了”“先保持原样”等否定也会取消。确认只使用完整识别结果，每次操作都需要重新做 OK。

## 环境与安装

验证平台：Jetson AGX Orin、JetPack 6.2 / Ubuntu 22.04、Python 3.10。默认读取 CAM4。其他 Linux 相机可通过 `--camera` 指定；其他平台未做硬件验证。

Python 依赖：

```bash
python -m pip install -r requirements.txt
```

另需系统提供 Tk、Fontconfig、OpenSSL、`ip`、Git、CMake、GCC/G++。Tk 不能通过 pip 安装。Ubuntu 可安装：

```bash
sudo apt install python3-tk libfontconfig1 openssl iproute2 git cmake build-essential
```

Jetson 默认使用 `/usr/local/cuda/bin/nvcc` 编译 CUDA SM87 的 llama.cpp。也可选择 CPU 构建。

## 准备模型

```bash
python prepare.py                 # 全部模型及本地聊天引擎
python run.py --check             # 只检查模型文件与依赖
python run.py
```

也可分步准备：

```bash
python prepare.py gesture         # MediaPipe 手势网络
python prepare.py speech          # 流式中文 ASR 和中文 TTS
python prepare.py chat            # Qwen GGUF + llama.cpp CUDA SM87
python prepare.py chat --cpu      # CPU 备选
```

聊天权重约 2.74 GB，首次编译需要等待，终端会打印日志路径。下载支持断点续传，网络中断后重新运行相同命令。已有文件会保留，摘要或续传内容不匹配时停止。

仓库包含精简学习后的 **73 个数值参考样本**：握拳 20、OK 36、手掌展开 17；食指竖起使用基础网络识别。仅保存标签和 210 维距离特征，没有采集图片、视频、原文件路径。模型权重按需下载，详见 `THIRD_PARTY.md`。

当前整理机器已有 `config.local.json`，直接复用原项目中的模型和引擎；`data/python/` 缓存现有 sherpa-onnx 运行包。二者均被 Git 忽略。新机器按上述步骤安装、准备即可，不需要原实验目录。

## 手机连接

1. 手机与电脑连接同一个 Wi-Fi，运行 `python run.py`。
2. 安卓 Chrome 打开终端打印的 `https://电脑地址:8765/`。程序从 `ip route` 获取电脑 IP，网关地址不是电脑地址。
3. 首次使用在页面下载本机 CA 证书，按安卓“设置 → 安全 → 安装证书 → CA 证书”安装，再重新打开网页并允许麦克风。
4. 点一次“开始手势语音确认”，即可持续收音、聊天和控制；结束时点“结束收音”。

保持网页在前台。声音跟随手机系统输出，耳机输出由手机系统选择。“允许语音打断”建议配合耳机；这是播放与收音协调机制，不是训练好的全双工语音模型。默认朗读结束后继续收音。

换 IP 会重新生成服务器证书，并尽量复用本目录已有的 CA。此独立项目第一次运行会生成自己的 CA，和原实验目录的证书分别管理。

## 常用参数

```bash
python run.py --camera 4 --window-size 1600x900
python run.py --camera 0 --no-auto-camera
python run.py --stream http://手机地址/video
python run.py --no-draw-landmarks
python run.py --udp 127.0.0.1:9000
python run.py --format jsonl
python run.py --chat-api http://127.0.0.1:8080/v1 --chat-model local-voice-chat
python run.py --help
```

默认窗口 1600×900，绘制关键点，推理帧最大宽度 640。自动相机初始化默认开启；只有配置了厂商初始化脚本且使用 CAM4 时才调用。脚本需支持 `--check --no-preview` 和 `--no-preview`，程序通过 Bash 调用，不保存密码。未配置则直接打开摄像头。

可将 `config.example.json` 复制为 `config.local.json`，填写 `camera_init_script` 或已下载的模型路径。相对路径以仓库根目录为准，空值使用默认值。也可设置对应环境变量，例如 `STEERING_ASR_MODEL`、`STEERING_TTS_MODEL`、`STEERING_LLAMA_SERVER`、`STEERING_CHAT_MODEL`。`chat_model` 配置指 GGUF 路径，CLI `--chat-model` 指 API 模型名。

## 接入机械臂

默认每次成功发送在 **stdout 单独输出一行 `1`、`2` 或 `3`**。日志和提示使用 stderr；`data/runs/本次编号/output_jsonl/events.jsonl` 保存完整事件。仅启动本程序不会自动接上机械臂。

其他 Python 程序可以直接调入口，传入发送函数和状态读取函数：

```python
from steering_control.run import main


def send_command(event):
    # 用实际串口、ROS 或其他传输接口替换这里。
    print(event["id"], event["command"])


def read_arm_state():
    # 从机械臂最新反馈读取，未收到新反馈时也可返回 None。
    return {"state": "折叠", "moving": False}


if __name__ == "__main__":
    main([], dispatch=send_command, arm_state_provider=read_arm_state)
```

`arm_state_provider` 在摄像头主循环调用，应快速返回。`state` 接受 `FOLD` / `HALF` / `UNFOLD` 或中文状态，“半展开”归一为“半折叠”。`moving` 表示仍在执行。发出命令后程序先记录执行方向；实际完成必须由机械臂反馈，不能靠计时猜测。未接反馈时，演示中发送一次折叠后会持续拒绝展开，反之亦然。

折叠执行中拒绝展开，展开执行中拒绝折叠；语音、手势确认、语言模型理解共用这一检查。执行期间发出 HALF 不会清除原执行方向。画面常驻当前状态，成功发送用黄色显示，拒绝用红色显示。`--udp HOST:PORT` 发送完整 JSON 事件，不是单个数字；它只提供发送通道，反馈仍通过 Python 接口接入。

## 代码与验证

```text
run.py / prepare.py           简短入口
steering_control/
  run.py                     组装服务和实时推理
  confirmation.py            确认、取消、超时、互斥与统一发送
  commands.py / interaction.py 语音命令解析及模型理解
  gesture/                   数值参考匹配与 OK 门控
  asr/ / tts/ / chat/         转写、播报、聊天及模型准备
  hardware/                  相机、MediaPipe、字体与绘制
  phone.py / web/            手机网页及 HTTP 接口
  preview.py / presentation.py 电脑画面与对话记录
assets/output_json/          清理来源路径后的数值模型
tests/                       状态机、网页、接口与数值回归
data/                        本机下载、构建、证书和日志（忽略上传）
```

```bash
python -m unittest discover -s tests -t . -v
```

测试不读取保存的图片或视频，不打开摄像头、麦克风或机械臂。浏览器逻辑测试使用可选的系统 JavaScriptCore；没有该库时跳过。实验原版本已由用户实机验收；整理版本的自动检查不替代再次实测相机、手机和机械臂。

`.gitignore` 排除本机配置、模型权重、编译产物、运行记录、原始媒体与证书私钥。使用 Git 上传源码即可，避免把整个工作目录作为包含 `data/` 的压缩包上传。
