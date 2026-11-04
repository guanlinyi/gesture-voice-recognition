"""Live-only preview; never opens the saved reference photos or recordings."""
from steering_control.hardware.ui_fonts import FONT_DIRECTORY, configure_tk_fonts, prepare_tk_runtime

prepare_tk_runtime()
import tkinter as tk
from tkinter import ttk
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
    def __init__(self, size):
        prepare_tk_runtime()
        self.root = tk.Tk()
        family = configure_tk_fonts(self.root)
        self.root.title("方向盘手势与语音控制")
        self.root.geometry(size)
        self.running = True
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda event: self.close())
        self.root.bind("q", lambda event: self.close())
        panes = ttk.Panedwindow(self.root, orient="horizontal")
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

    def close(self):
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
        self.update_conversation(snapshot)
        self.root.update_idletasks()
        width = max(1, self.video.winfo_width())
        height = max(1, self.video.winfo_height())
        ratio = min(width / frame.shape[1], height / frame.shape[0])
        image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(image).resize((max(1, int(frame.shape[1] * ratio)), max(1, int(frame.shape[0] * ratio))))
        size = max(16, min(26, image.width // 45))
        if size not in self.overlay_fonts:
            self.overlay_fonts[size] = ImageFont.truetype(str(FONT_DIRECTORY / "NotoSansCJKsc-Regular.otf"), size)
            self.notice_fonts[size] = ImageFont.truetype(str(FONT_DIRECTORY / "NotoSansCJKsc-Bold.otf"), size + 10)
        notice = self.sent_notice.update(snapshot["last_action"], snapshot.get("last_rejection"))
        image = draw_inference_overlay(image, [arm_caption(snapshot)] + overlay_lines(snapshot["inference"], snapshot["pending"]), self.overlay_fonts[size],
                                       notice=notice, notice_font=self.notice_fonts[size], notice_color=self.sent_notice.color)
        self.image = ImageTk.PhotoImage(image)
        self.video.configure(image=self.image)
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
        self.root.destroy()
