"""Learning workspace; saved-media previews run only after a user's selection."""
import json
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageTk

from .session import LABELS


class LearningPanel(ttk.Frame):
    def __init__(self, parent, session):
        super().__init__(parent)
        self.session = session
        self.on_change = lambda: None
        self.signature = None
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left, right = ttk.Frame(panes), ttk.Frame(panes, width=560)
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        self.video = ttk.Label(left, anchor="center")
        self.video.pack(fill="both", expand=True)
        tools = ttk.Frame(left)
        tools.pack(fill="x", padx=10, pady=8)
        ttk.Label(tools, text="采集标签").pack(side="left")
        self.label = tk.StringVar(value="握拳")
        self.label_box = ttk.Combobox(tools, textvariable=self.label, values=list(LABELS), state="readonly", width=12)
        self.label_box.pack(side="left", padx=8)
        self.photo_button = ttk.Button(tools, text="拍照 S", command=lambda: self.action(self.photo))
        self.photo_button.pack(side="left", padx=4)
        self.reference_button = ttk.Button(tools, text="参考录像 V", command=lambda: self.action(lambda: self.video_toggle("reference")))
        self.reference_button.pack(side="left", padx=4)
        self.test_button = ttk.Button(tools, text="验证录像 T", command=lambda: self.action(lambda: self.video_toggle("validation")))
        self.test_button.pack(side="left", padx=4)
        ttk.Label(left, text="一段录像保持同一个标签，可改变朝向与远近。验证录像只用于评估，不参与学习。", wraplength=660).pack(fill="x", padx=12, pady=(0, 8))
        self.base = tk.StringVar()
        ttk.Label(right, textvariable=self.base, wraplength=510).pack(fill="x", padx=12, pady=8)
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True, padx=8)
        self.trees = {}
        for kind, caption in (("image", "图片"), ("video", "视频")):
            page = ttk.Frame(self.tabs)
            self.tabs.add(page, text=caption)
            tree = ttk.Treeview(page, columns=("label", "split", "frames"), show="headings", selectmode="extended", height=7)
            for key, title, width in (("label", "标签", 140), ("split", "用途", 100), ("frames", "帧数", 65)):
                tree.heading(key, text=title)
                tree.column(key, width=width)
            scroll = ttk.Scrollbar(page, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            tree.pack(fill="both", expand=True)
            tree.bind("<<TreeviewSelect>>", self.preview_selected)
            self.trees[kind] = tree
        buttons = ttk.Frame(right)
        buttons.pack(fill="x", padx=8, pady=8)
        self.delete_button = ttk.Button(buttons, text="删除所选", command=lambda: self.action(self.exclude))
        self.delete_button.pack(side="left")
        ttk.Button(buttons, text="打开所选文件", command=lambda: self.action(self.open_selected)).pack(side="left", padx=8)
        ttk.Button(buttons, text="打开本批文件夹", command=lambda: self.action(lambda: self.open_path(session.directory))).pack(side="left")
        ttk.Button(right, text="评估本批验证录像", command=lambda: self.action(self.validate)).pack(anchor="w", padx=8, pady=4)
        self.preview = tk.Canvas(right, height=160, background="black", highlightthickness=0)
        self.preview.pack(fill="x", padx=8)
        self.preview_image = None
        line = ttk.Frame(right)
        line.pack(fill="x", padx=8, pady=8)
        self.name = tk.StringVar()
        self.name_entry = ttk.Entry(line, textvariable=self.name, width=21)
        self.name_entry.pack(side="left", fill="x", expand=True)
        self.save_button = ttk.Button(line, text="保存模型", command=lambda: self.action(self.save))
        self.save_button.pack(side="left", padx=8)
        self.state = tk.StringVar(value="未学习")
        ttk.Label(line, textvariable=self.state, width=10).pack(side="left")
        self.summary = tk.StringVar(value="采集素材后填写模型名称并保存；保存不会自动启用。")
        ttk.Label(right, textvariable=self.summary, wraplength=510, justify="left").pack(fill="x", padx=12, pady=8)
        self.last_result = None

    def action(self, callback):
        try:
            callback()
        except (OSError, ValueError, RuntimeError) as exc:
            messagebox.showerror("现场学习", str(exc), parent=self)

    def photo(self):
        item = self.session.photograph(self.label.get())
        self.tabs.select(0)
        self.refresh()
        self.trees["image"].selection_set(item["id"])
        self.trees["image"].see(item["id"])

    def video_toggle(self, split):
        if self.session.recording is not None:
            if self.session.recording["split"] != split:
                raise ValueError("请先结束当前录像，再更换录像用途")
            item = self.session.stop_video()
            self.label_box.configure(state="readonly")
            self.tabs.select(1)
            self.refresh()
            self.trees["video"].selection_set(item["id"])
            self.trees["video"].see(item["id"])
            return item
        self.session.start_video(self.label.get(), split)
        self.label_box.configure(state="disabled")
        self.refresh()

    def stop_recording(self):
        if self.session.recording is not None:
            self.session.stop_video()
            self.label_box.configure(state="readonly")
            self.tabs.select(1)
            self.refresh()

    def selected(self):
        kind = "image" if self.tabs.index(self.tabs.select()) == 0 else "video"
        ids = set(self.trees[kind].selection())
        return [item for item in self.session.items if item["id"] in ids]

    def exclude(self):
        items = self.selected()
        if not items:
            raise ValueError("请先选择图片或视频")
        if messagebox.askyesno("删除素材", "从本批学习中删除所选素材？原文件保留，可从文件夹找回。", parent=self):
            self.session.exclude([item["id"] for item in items])
            self.clear_preview()
            self.refresh()

    @staticmethod
    def open_path(path):
        subprocess.Popen(["xdg-open", str(Path(path).resolve())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_selected(self):
        items = self.selected()
        if not items:
            raise ValueError("请先选择图片或视频")
        self.open_path(self.session.directory / items[0]["file"])

    def clear_preview(self):
        self.preview.delete("all")
        self.preview_image = None

    def preview_selected(self, event=None):
        self.clear_preview()
        items = self.selected()
        if not items:
            return
        item = items[0]
        if item["kind"] == "image":
            try:
                with Image.open(self.session.directory / item["file"]) as original:
                    image = original.copy()
                image.thumbnail((450, 155))
                self.preview_image = ImageTk.PhotoImage(image)
                self.preview.create_image(6, 6, anchor="nw", image=self.preview_image)
            except (OSError, ValueError) as exc:
                self.summary.set(f"预览失败：{exc}")
        else:
            self.preview.create_text(12, 15, anchor="nw", fill="white", text="视频已保存；点击“打开所选文件”播放。\n" + item["file"], width=430)

    def save(self):
        self.session.save(self.name.get())
        self.refresh()

    def validate(self):
        name, report = self.session.validate()
        accuracy = "无有效关键点" if report["accuracy"] is None else f"{report['accuracy']:.1%}"
        self.summary.set(f"验证模型：{name}\n有效手部帧准确率：{accuracy}；错例 {report['errors']} 帧；"
                         f"没有关键点 {report['no_hand']} 帧（总帧数 {report['frames']}）。\n验证素材不参与学习。")

    def refresh(self):
        with self.session.lock:
            self._refresh()

    def _refresh(self):
        session = self.session
        self.base.set("学习基础模型：" + session.base.name)
        self.state.set(session.status)
        recording = session.recording
        self.reference_button.configure(text="结束录像 V" if recording and recording["split"] == "reference" else "参考录像 V")
        self.test_button.configure(text="结束录像 T" if recording and recording["split"] == "validation" else "验证录像 T")
        for button in (self.photo_button, self.save_button, self.delete_button, self.name_entry):
            button.configure(state="disabled" if session.busy or recording else "normal")
        for button in (self.reference_button, self.test_button):
            button.configure(state="disabled" if session.busy else "normal")
        if recording:
            other = self.test_button if recording["split"] == "reference" else self.reference_button
            other.configure(state="disabled")
        if session.error:
            self.summary.set("学习失败：" + session.error + "；素材已保留。")
        if session.result and session.result != self.last_result:
            self.last_result = session.result
            self.clear_preview()
            self.name.set("")
            report = session.report
            after = report["after"]
            validation = report["validation"]
            score = "尚无独立验证录像" if validation["accuracy"] is None else f"验证录像：{validation['accuracy']:.1%}，漏检关键点 {validation['no_hand']} 帧"
            self.summary.set(f"已保存：{session.result.parents[2].name}，新增 {report['added_references']} 个参考。\n"
                             f"训练素材回放：{after['usable'] - after['errors']}/{after['usable']} 帧正确（非独立验证）。\n"
                             f"{score}\n基础参考错例：{report['base_reference_replay']['errors']}。请在顶部选择模型进行验证，再启用。")
            self.on_change()
        signature = (session.revision, tuple(item["id"] for item in session.items))
        if signature != self.signature:
            self.signature = signature
            for kind, tree in self.trees.items():
                current = tree.get_children()
                if current:
                    tree.delete(*current)
                for item in session.items:
                    if item["kind"] == kind:
                        label = next((label for label, key in LABELS.items() if key == item["label"]), item["label"])
                        tree.insert("", "end", iid=item["id"], values=(label, "参考" if item["split"] == "reference" else "验证", len(item["rows"])))
