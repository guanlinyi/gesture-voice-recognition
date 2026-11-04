"""Recognition and on-site learning workspaces sharing one live camera."""
import json
from pathlib import Path
from steering_control.hardware.ui_fonts import FONT_DIRECTORY, configure_tk_fonts, prepare_tk_runtime

prepare_tk_runtime()
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

import cv2
from PIL import Image, ImageDraw, ImageFont, ImageTk

from .presentation import SentCommandNotice, arm_caption, conversation_rows, conversation_status, overlay_lines


def draw_inference_overlay(image, lines, font, *, notice=None, notice_font=None, notice_color=(255, 235, 0)):
    """Draw only on the resized live display image; inference frames stay untouched."""
    if image.width < 50 or image.height < 50:
        return image
    draw = ImageDraw.Draw(image)
    max_width = max(1, image.width - 44)
    wrapped = []
    rows = []
    if notice:
        rows.append((notice, notice_font or font, notice_color))
    rows.extend((line, font, (145, 234, 201) if index == 0 else (238, 244, 251))
                for index, line in enumerate(lines))
    for line, row_font, color in rows:
        part = ""
        for char in line:
            if part and draw.textlength(part + char, font=row_font) > max_width:
                wrapped.append((part, row_font, color))
                part = ""
            part += char
        if part:
            wrapped.append((part, row_font, color))
    panel_width = min(image.width - 16, int(max(draw.textlength(text, font=row_font)
                                              for text, row_font, _ in wrapped)) + 28)
    panel_height = min(image.height - 16, sum(row_font.size + 10 for _, row_font, _ in wrapped) + 20)
    panel = Image.new("RGBA", (panel_width, panel_height), (12, 22, 32, 215))
    image.paste(panel, (8, 8), panel)
    draw = ImageDraw.Draw(image)
    y = 16
    for text, row_font, color in wrapped:
        if y + row_font.size > panel_height + 8:
            break
        draw.text((20, y), text, font=row_font, fill=color, stroke_width=1 if color == (255, 235, 0) else 0,
                  stroke_fill=(0, 0, 0))
        y += row_font.size + 10
    return image

class Preview:
    def __init__(self, size, *, model, control, on_pause=None):
        from .learning.session import LearningSession
        from .learning.panel import LearningPanel
        self.active_model = model
        self.control = control
        self.on_pause = on_pause or (lambda: None)
        self.learning = LearningSession(model)
        self.mode = "recognition"
        self.model_paths = {}
        prepare_tk_runtime()
        self.root = tk.Tk()
        family = configure_tk_fonts(self.root)
        self.root.title("方向盘手势与语音控制")
        self.root.geometry(size)
        self.running = True
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda event: self.close())
        self.root.bind("q", self.quit_shortcut)
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=10, pady=8)
        ttk.Label(toolbar, text="选择模型").pack(side="left")
        self.model_choice = tk.StringVar()
        self.models = ttk.Combobox(toolbar, textvariable=self.model_choice, state="readonly", width=36)
        self.models.pack(side="left", padx=8)
        self.models.bind("<<ComboboxSelected>>", lambda event: self.action(self.select_model))
        ttk.Button(toolbar, text="删除模型", command=lambda: self.action(self.delete_model)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="验证所选模型", command=lambda: self.action(self.validate_selected)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="启用所选模型", command=lambda: self.action(self.activate_selected)).pack(side="left", padx=4)
        ttk.Button(toolbar, text="打开模型学习图片", command=lambda: self.action(self.open_model_folder)).pack(side="left", padx=4)
        self.active_name = tk.StringVar(value="当前使用：" + model.name)
        ttk.Label(toolbar, textvariable=self.active_name).pack(side="left", padx=10)
        self.pages = ttk.Notebook(self.root)
        self.pages.pack(fill="both", expand=True)
        recognition = ttk.Frame(self.pages)
        self.pages.add(recognition, text="识别与交互")
        panes = ttk.Panedwindow(recognition, orient="horizontal")
        panes.pack(fill="both", expand=True)
        sidebar = ttk.Frame(panes, width=340)
        sidebar.pack_propagate(False)
        right = ttk.Frame(panes)
        panes.add(sidebar, weight=0)
        panes.add(right, weight=1)
        ttk.Label(sidebar, text="问答记录", font=(family, 15, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        self.conversation_state = tk.StringVar(value="等待连接")
        ttk.Label(sidebar, textvariable=self.conversation_state, wraplength=310, justify="left").pack(fill="x", padx=12, pady=(0, 8))
        self.messages = ScrolledText(sidebar, width=27, wrap="word", state="disabled", font=(family, 13),
                                     background="#152131", foreground="#e8f0fa", relief="flat", padx=12, pady=12)
        self.messages.pack(fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        self.messages.tag_configure("user", foreground="#8fe0cb")
        self.messages.tag_configure("system", foreground="#dce7fa")
        self.messages.tag_configure("partial", foreground="#a9bacb")
        self.messages.tag_configure("sent", foreground="#ffeb00", font=(family, 14, "bold"))
        self.messages.tag_configure("error", foreground="#ffaaa5")
        self.record_signature = None
        self.video = ttk.Label(right, anchor="center")
        self.video.pack(fill="both", expand=True)
        self.info = tk.StringVar()
        ttk.Label(right, textvariable=self.info, wraplength=1100, justify="left").pack(fill="x", padx=18, pady=12)
        self.image = None
        self.overlay_fonts = {}
        self.notice_fonts = {}
        self.sent_notice = SentCommandNotice()
        self.learning_panel = LearningPanel(self.pages, self.learning)
        self.learning_panel.on_change = self.refresh_models
        self.pages.add(self.learning_panel, text="现场学习")
        self.pages.bind("<<NotebookTabChanged>>", self.mode_changed)
        self.root.bind("s", lambda event: self.shortcut("photo"))
        self.root.bind("v", lambda event: self.shortcut("reference"))
        self.root.bind("t", lambda event: self.shortcut("validation"))
        self.refresh_models()

    @property
    def is_learning(self):
        return self.pages.index(self.pages.select()) == 1

    def action(self, callback):
        try:
            callback()
        except (ValueError, RuntimeError, OSError) as exc:
            messagebox.showerror("模型与学习", str(exc), parent=self.root)

    def refresh_models(self):
        old = self.model_choice.get()
        self.model_paths = {}
        for path in self.learning.choices():
            name = json.loads(path.read_text(encoding="utf-8"))["name"]
            caption = name
            while caption in self.model_paths:
                caption += " ·"
            self.model_paths[caption] = path
        self.models.configure(values=list(self.model_paths))
        current = next((caption for caption, path in self.model_paths.items()
                        if path == self.learning.base.manifest_path.resolve()), next(iter(self.model_paths), ""))
        self.model_choice.set(old if old in self.model_paths else current)

    def selected_model(self):
        from .model import SavedGestureModel
        if self.model_choice.get() not in self.model_paths:
            raise ValueError("请选择模型")
        model = SavedGestureModel(self.model_paths[self.model_choice.get()])
        if model.model_path != self.active_model.model_path or model.classifier_options != self.active_model.classifier_options:
            raise ValueError("所选模型的基础网络或分类设置与当前相机会话不一致，请重新启动并指定 --model")
        return model

    def select_model(self):
        try:
            self.learning.set_base(self.selected_model())
        except (ValueError, OSError, RuntimeError):
            current = self.learning.base.manifest_path.resolve()
            for caption, path in self.model_paths.items():
                if path == current:
                    self.model_choice.set(caption)
                    break
            raise
        self.learning_panel.clear_preview()
        self.learning.validation_model = self.learning.base

    def validate_selected(self):
        self.learning._idle()
        model = self.selected_model()
        self.pages.select(self.learning_panel)
        self.mode_changed()
        self.learning.validation_model = model
        self.learning_panel.summary.set("正在验证：" + model.name + "。实时识别显示在左侧；可用 T 录制独立验证录像。")

    def activate_selected(self):
        self.learning._idle()
        model = self.selected_model()
        self.control.set_learning(True)
        self.on_pause()
        self.active_model.policy.gate.cancel()
        self.active_model = model
        self.learning.set_base(model)
        with self.control.lock:
            self.control.model_name = model.name
            self.control._event("model_activated", manifest=str(model.manifest_path), model_name=model.name)
        self.active_name.set("当前使用：" + model.name)
        self.control.set_learning(self.is_learning)

    def open_model_folder(self):
        path = self.model_paths[self.model_choice.get()]
        record = json.loads(path.read_text(encoding="utf-8"))
        directory = (path.parent / record.get("training", {}).get("directory", ".")).resolve()
        self.learning_panel.open_path(directory)

    def delete_model(self):
        self.learning._idle()
        path = self.model_paths[self.model_choice.get()]
        if path == self.active_model.manifest_path:
            raise ValueError("请先启用另一个模型，再删除当前使用的模型")
        if messagebox.askyesno("删除模型", "从模型列表删除所选模型？模型文件和学习素材保留在原目录。", parent=self.root):
            self.learning.exclude_model(path)
            self.learning.set_base(self.active_model)
            self.refresh_models()

    def mode_changed(self, event=None):
        target = "learning" if self.is_learning else "recognition"
        if target == self.mode:
            return
        if target == "recognition" and self.learning.busy:
            self.pages.select(self.learning_panel)
            return
        if target == "recognition":
            self.learning_panel.action(self.learning_panel.stop_recording)
        self.control.set_learning(target == "learning")
        self.active_model.policy.gate.cancel()
        self.on_pause()
        self.mode = target

    def shortcut(self, operation):
        focused = self.root.focus_get()
        if not self.is_learning or focused and focused.winfo_class() in ("Entry", "TEntry", "TCombobox", "Text"):
            return
        callback = self.learning_panel.photo if operation == "photo" else lambda: self.learning_panel.video_toggle(operation)
        self.learning_panel.action(callback)

    def quit_shortcut(self, event=None):
        focused = self.root.focus_get()
        if not focused or focused.winfo_class() not in ("Entry", "TEntry", "TCombobox", "Text"):
            self.close()

    def close(self):
        self.control.set_learning(True)
        self.on_pause()
        self.running = False

    def update_conversation(self, snapshot):
        self.conversation_state.set(conversation_status(snapshot))
        rows = conversation_rows(snapshot)
        signature = tuple((row["id"], row["role"], row["text"], row["state"], row["kind"]) for row in rows)
        if signature == self.record_signature:
            return
        self.record_signature = signature
        first, last = self.messages.yview()
        follow = last >= .97
        self.messages.configure(state="normal")
        self.messages.delete("1.0", "end")
        for row in rows:
            role = "用户" if row["role"] == "user" else "系统"
            suffix = "（识别中）" if row["state"] == "partial" else ""
            tag = row["kind"] if row["kind"] in ("sent", "error") else "partial" if suffix else row["role"]
            self.messages.insert("end", f"{role}：{row['text']}{suffix}\n\n", tag)
        self.messages.configure(state="disabled")
        if follow:
            self.messages.see("end")
        else:
            self.messages.yview_moveto(first)

    def update(self, frame, prediction, workflow, snapshot):
        self.learning_panel.refresh()
        self.update_conversation(snapshot)
        self.root.update_idletasks()
        video = self.learning_panel.video if self.is_learning else self.video
        width = max(1, video.winfo_width())
        height = max(1, video.winfo_height())
        ratio = min(width / frame.shape[1], height / frame.shape[0])
        image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(image).resize((max(1, int(frame.shape[1] * ratio)), max(1, int(frame.shape[0] * ratio))))
        size = max(16, min(26, image.width // 45))
        if size not in self.overlay_fonts:
            self.overlay_fonts[size] = ImageFont.truetype(str(FONT_DIRECTORY / "NotoSansCJKsc-Regular.otf"), size)
            self.notice_fonts[size] = ImageFont.truetype(str(FONT_DIRECTORY / "NotoSansCJKsc-Bold.otf"), size + 10)
        notice = self.sent_notice.update(snapshot["last_action"], snapshot.get("last_rejection"))
        if self.is_learning:
            notice = None
        lines = (["现场学习 · 控制命令已暂停", "识别：" + ("食指竖起" if prediction.label == "摆1字" else prediction.label or "未知")]
                 if self.is_learning else [arm_caption(snapshot)] + overlay_lines(snapshot["inference"], snapshot["pending"]))
        image = draw_inference_overlay(image, lines, self.overlay_fonts[size],
                                       notice=notice, notice_font=self.notice_fonts[size], notice_color=self.sent_notice.color)
        self.image = ImageTk.PhotoImage(image)
        video.configure(image=self.image)
        voice = snapshot["voice"]
        voice_text = "提示音已就绪" if voice["ready"] else "正在准备提示音"
        if voice["error"]:
            voice_text = "提示音失败：" + voice["error"]
        label = "食指竖起" if prediction.label == "摆1字" else prediction.label or "未知"
        last = snapshot["last_action"]
        self.info.set(f"模型：{snapshot['model']} · {voice_text}\n当前识别：{label} · {workflow}\n"
                      f"{snapshot['message']}\n最近提交：{last['action'] + '（数字 ' + str(last['id']) + '）' if last else '无'}。关闭窗口或 Q / Esc 退出。")
        self.root.update()
        return self.running

    def destroy(self):
        self.learning.close()
        self.root.destroy()
