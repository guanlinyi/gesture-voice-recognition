# 森云 SG8A / ISX031 摄像头支持

为新机器提供当前摄像头所需的驱动、设备树和启动脚本。适用 **Jetson AGX Orin / aarch64、JetPack 6.2、L4T R36.4.3、内核 5.15.148-tegra、SG8A 扩展板、SG3S-ISX031C-GMSL2F**。已有正常出图环境的机器不必重新安装。

默认链路 `0010`、相机型号编号 `5`、设备 `/dev/video4`。链路参数 `0` 表示 6 Gb/s、`1` 表示 3 Gb/s，不是帧率。系统、扩展板或相机型号不同时，不能直接套用本包。

## 文件与来源

```text
scripts/install.sh                 首次安装；支持只检查、备份与恢复
scripts/quick_bring_up_auto.sh     日常初始化；可 source 后调用 run_camera
input_bin/Image.bin               厂商内核，安装目标为 /boot/Image
input_ko/                         五个厂商内核模块
input_dtbo/                       GMSL2x8 设备树叠加文件
source/                           厂商提供的源码片段与版权声明
input_md/                         原厂说明与版本信息
output_json/package_manifest.json 来源、文件长度、SHA256 与适配平台
```

来源包为 `SG8A_ORIN_GMSL2-F_V2_AGX_Orin_YUV_JP6.2_L4TR36.4.3`。清单中的 19 个厂商文件保持原始字节；两个脚本从本机已使用版本整理，调整目录和认证方式。未带入本机启动配置、历史备份、密码或采集媒体。`input_md/Readme.md` 保留上游原文，里面的固定分区 UUID 和启动配置不能直接复制到另一台机器。

第三方文件保留自身版权与许可标记；`source/COPYING.GPL-2` 附上源码中声明的 GPL-2.0 文本，不为所有二进制统一指定许可。随包源码是厂商提供的片段，**不是完整、可独立重编译的内核和全部驱动源码**；完整对应源码及厂商组件的使用、再分发条件需向原提供方确认。

## 首次安装

以下命令均在**仓库根目录**执行，使用安装应用时的 Python 环境。先核对设备版本并保存工作。

```bash
sudo apt install v4l-utils kmod util-linux

# 只检查平台、源文件和安装目标，不修改系统。
PYTHON="$(command -v python)" bash camera_support/sensing_sg8a/scripts/install.sh --dry-run

# 确认需要部署这套驱动后，明确执行安装。
sudo env "PYTHON=$(command -v python)" bash camera_support/sensing_sg8a/scripts/install.sh
```

安装程序先备份并校验，再替换 `/boot/Image`、模块目录中的 `tegra-camera.ko`、`nvhost-nvcsi-t194.ko`，安装 `/boot/tegra234-camera-yuv-gmsl2x8-overlay.dtbo`，移除模块目录中两个旧的 max9295/max9296 文件，最后执行 `depmod`。日常启动从本包加载 max9295/max9296/sgx-yuv-gmsl2 模块。

备份位于 `/var/backups/sg8a-camera/` 下独立目录；安装失败会尝试恢复。脚本不修改 `/boot/extlinux/extlinux.conf`，不自动重启。备份只覆盖受影响文件，不是完整系统快照。

然后手动启用设备树：

```bash
sudo /opt/nvidia/jetson-io/jetson-io.py
```

选择 `Configure Jetson AGX CSI Connector` → `Configure for compatible hardware` → `Jetson Sensing YUV GMSL2x8` → 保存配置。检查本机生成的启动项，保存工作后按工具提示重启。不要用别人的 extlinux 配置替换本机文件；选项不存在时先排查 Jetson-IO、板卡和叠加文件。

## 日常启动

先接通相机和扩展板电源，再运行：

```bash
# 只检查工具、平台、模块文件；不加载驱动、不读取摄像头画面。
bash camera_support/sensing_sg8a/scripts/quick_bring_up_auto.sh --check --no-preview

# 在终端完成初始化。需要权限时由 sudo 自己询问密码。
bash camera_support/sensing_sg8a/scripts/quick_bring_up_auto.sh --no-preview

# 普通用户启动应用，默认自动调用仓库内的 CAM4 初始化脚本。
python run.py
```

首次加载驱动需要权限。应用调用脚本时不提供密码、不等待输入；没有有效 sudo 凭据时，会提示先在终端运行初始化命令。驱动已加载后，不再为加载动作要求认证，仍检查设备读写权限并配置相机。不要用 sudo 运行整个识别程序。

`--no-preview` 不打开预览，应用随后读取相机。手动预览可单独运行不带此参数的脚本；结束预览后再启动应用。`--desktop` 结束后保留终端供查看日志。

自定义脚本优先级为 CLI `--camera-init-script` → 环境变量 `STEERING_CAMERA_INIT_SCRIPT` → `config.local.json` 的 `camera_init_script` → 仓库内脚本。旧机器已有本机配置会继续使用原脚本；将该字段设为 `null` 或显式指定即可改用本包：

```bash
python run.py --camera-init-script camera_support/sensing_sg8a/scripts/quick_bring_up_auto.sh
```

## 换相机与排错

普通摄像头或 DroidCam 不需要森云驱动：

```bash
python run.py --camera 0 --no-auto-camera
python run.py --camera 4 --no-auto-camera
python run.py --stream http://手机地址/video
```

- **初始化检查失败**：按日志排查版本、缺失工具或驱动文件。程序在启动语音和聊天服务前停止，不自动安装内核。
- **需要 sudo 认证**：在终端运行日常初始化命令；密码只交给 sudo，不写进脚本、配置或命令行。
- **设备不存在**：核对首次安装、设备树、重启、接线和供电。仅复制应用不会自动配置设备树。
- **没有读写权限**：检查设备属组和用户权限。若属于 `video` 组，可由管理员把用户加入该组，重新登录后再运行。
- **设备存在却绿屏／超时**：模块加载不代表收到有效图像，注意先供电再初始化。脚本不自动卸载模块；需要重新初始化硬件时，保存工作后正常重启。
- **相机被占用或采集失败**：关闭其他预览。运行中读取失败会退出并取消未确认操作，目前不自动重连或切换相机。

## 恢复安装前文件

把下面的路径替换为安装日志中打印的**本次备份目录**：

```bash
sudo env "PYTHON=$(command -v python)" bash camera_support/sensing_sg8a/scripts/install.sh --restore /var/backups/sg8a-camera/本次备份目录
```

恢复会校验备份、恢复受安装程序修改的文件并重建模块索引，不自动重启。Jetson-IO 管理的设备树启动项不会随此命令回退；恢复出错时不要直接重启。

## 验证边界

原目录对应环境已有实际使用记录。本次移入仓库、调整相对路径和认证逻辑后的版本，先做文件摘要、脚本语法和模拟流程检查；不能代替新机器首次安装及实际出图验证。本次结果见 `相机支持整理_20261104.md`。
