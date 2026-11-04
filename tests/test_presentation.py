"""Preparation/command display is distinct from the locked confirmation request."""
from importlib import import_module
import unittest

PACKAGE = "steering_control"
view = import_module(PACKAGE + ".presentation")


class InferencePresentationTests(unittest.TestCase):
    def test_idle_and_unprepared_work_gesture_do_not_claim_preparation(self):
        for label in (None, "握拳", "食指竖起", "手掌展开"):
            with self.subTest(label=label):
                lines = view.overlay_lines(view.inference_view(label))
                self.assertEqual(lines[0], "未进入准备状态")
                self.assertNotIn("已进入准备状态", lines)

    def test_ok_is_not_prepared_until_hold_passes(self):
        self.assertEqual(view.overlay_lines(view.inference_view("OK手势"))[0], "OK - 准备确认中")
        self.assertEqual(view.overlay_lines(view.inference_view("OK手势", True))[0], "OK - 准备状态")

    def test_three_prepared_gestures_show_their_own_commands(self):
        for label, caption in (("握拳", "握拳 - 折叠命令"), ("摆1字", "食指竖起 - 半折叠命令"),
                               ("手掌展开", "手掌张开 - 展开命令")):
            with self.subTest(label=label):
                self.assertEqual(view.overlay_lines(view.inference_view(label, True))[0], caption)

    def test_live_palm_does_not_relabel_locked_fist_request(self):
        lines = view.overlay_lines(view.inference_view("手掌展开"), {"state": "listening", "gesture": "握拳"})
        self.assertIn("手掌张开 - 展开命令", lines[0])
        self.assertIn("握拳 - 折叠命令", lines[1])
        self.assertIn("等待语音确认", lines[2])

    def test_expired_pending_is_not_shown_as_current_operation(self):
        lines = view.overlay_lines(view.inference_view(None), {"state": "expired", "gesture": "握拳"})
        self.assertEqual(lines[0], "未进入准备状态")
        self.assertFalse(any("本次锁定" in line for line in lines))

    def test_pc_history_combines_question_user_answer_send_result_and_chat(self):
        snapshot = {"history": [
            {"id": "q", "role": "system", "text": "您是想要折叠方向盘吗？", "state": "done", "kind": "question", "at": 1.},
            {"id": "u", "role": "user", "text": "是的", "state": "done", "kind": "message", "at": 2.},
            {"id": "s", "role": "system", "text": "发送方向盘的折叠命令（数字 1）", "state": "done", "kind": "sent", "at": 3.}],
            "chat": {"dialogue": {"turns": [{"id": "chat", "user": "你好", "assistant": "你好，继续聊。", "state": "done", "created_at": 4.}]}}}
        rows = view.conversation_rows(snapshot)
        self.assertEqual([row["text"] for row in rows], ["您是想要折叠方向盘吗？", "是的", "发送方向盘的折叠命令（数字 1）", "你好", "你好，继续聊。"])
        snapshot["history"].append({"id": "u2", "role": "user", "text": "你好", "state": "done", "kind": "message", "at": 3.9})
        self.assertEqual(sum(row["role"] == "user" and row["text"] == "你好" for row in view.conversation_rows(snapshot)), 1)


class SentCommandNoticeTests(unittest.TestCase):
    def test_no_notice_before_submission_and_exact_text_for_all_three_actions(self):
        for action in ("折叠", "半折叠", "展开"):
            with self.subTest(action=action):
                notice = view.SentCommandNotice(clock=lambda: 100.)
                self.assertIsNone(notice.update(None))
                self.assertEqual(notice.update({"request_id": "one", "action": action}),
                                 f"发送方向盘的{action}命令")

    def test_repeated_snapshots_expire_but_a_new_confirmation_restarts_notice(self):
        now = [100.]
        notice = view.SentCommandNotice(clock=lambda: now[0])
        first = {"request_id": "one", "action": "折叠"}
        notice.update(first)
        now[0] += 3.9
        self.assertEqual(notice.update(first), "发送方向盘的折叠命令")
        now[0] += .2
        self.assertIsNone(notice.update(first))
        self.assertEqual(notice.update({"request_id": "two", "action": "折叠"}), "发送方向盘的折叠命令")

    def test_real_font_draws_yellow_notice_on_generated_blank_canvas(self):
        import numpy as np
        from PIL import Image, ImageFont
        from steering_control.hardware.ui_fonts import FONT_DIRECTORY
        preview = import_module(PACKAGE + ".preview")
        font = ImageFont.truetype(str(FONT_DIRECTORY / "NotoSansCJKsc-Regular.otf"), 24)
        bold = ImageFont.truetype(str(FONT_DIRECTORY / "NotoSansCJKsc-Bold.otf"), 34)
        for action in ("折叠", "半折叠", "展开"):
            with self.subTest(action=action):
                image = Image.new("RGB", (960, 540), (65, 65, 65))
                preview.draw_inference_overlay(image, ["未进入准备状态"], font,
                                               notice=f"发送方向盘的{action}命令", notice_font=bold)
                pixels = np.asarray(image)[8:90, 8:900]
                yellow = np.count_nonzero((pixels[:, :, 0] > 220) & (pixels[:, :, 1] > 200) & (pixels[:, :, 2] < 50))
                self.assertGreater(yellow, 100)
                self.assertEqual(image.getpixel((950, 530)), (65, 65, 65))
