"""JavaScriptCore checks for playback boundary, cancellation and capture tags."""
from importlib import import_module
from pathlib import Path
import unittest

base = import_module("tests.browser_support")
HERE = Path(__file__).resolve().parents[1] / "steering_control"
PHONE = base.WEB / "phone.js"
SCRIPT = HERE / "web/input_js/confirmation.js"


@unittest.skipUnless(base.LIBRARY, "系统 JavaScriptCore 不可用")
class ConfirmationBrowserTests(unittest.TestCase):
    setUp = base.BrowserLogicTests.setUp
    tearDown = base.BrowserLogicTests.tearDown
    evaluate = base.BrowserLogicTests.evaluate
    stringify = base.BrowserLogicTests.stringify

    def load(self, *, delayed_decode=False):
        setup = base.BrowserLogicTests.browser_harness(self) + """
          const timers = [];
          setTimeout = (callback, delay) => { timers.push({callback, delay}); return timers.length; };
          clearTimeout = id => { if(timers[id-1]) timers[id-1].cancelled=true; };
          function fireTimer(delay) {
            const index = timers.findIndex(value=>value.delay===delay&&!value.cancelled&&!value.fired);
            if(index<0) throw Error('missing timer '+delay);
            timers[index].fired=true; timers[index].callback();
          }
          const clips = [], upload = [];
          let decodeResolve;
          Context.prototype.decodeAudioData = async () => ({duration:.1});
          Context.prototype.createBufferSource = () => {
            const clip={connect(){},start(){clips.push(this);},stop(){if(this.onended)this.onended();}};
            return clip;
          };
          let serverState={revision:0,model:'synthetic',message:'等待手势',voice:{ready:true,error:null},pending:null,last_action:null,
            input:{mode:'commands',token:'command-idle'}};
          function candidate(id='one',gesture='握拳',action='折叠') {
            serverState={...serverState,revision:serverState.revision+1,input:{mode:'muted',token:null},
              pending:{id,state:'prompting',gesture,action,prompt:`您是想要进行方向盘的${action}操作吗？`,playback_token:'token'}};
          }
          fetch = async (path,options={}) => {
            requests.push(path);
            let value;
            if(path==='/api/start') value={session_id:'test-phone'};
            else if(path==='/api/stop') value={events:[]};
            else if(path.startsWith('/api/audio')) {upload.push({path,value:new Float32Array(options.body)[0]});value={events:[]};}
            else if(path==='/api/confirmation/listen') {
              serverState={...serverState,revision:serverState.revision+1,pending:{...serverState.pending,state:'listening'},
                input:{mode:'confirmation',token:serverState.pending.id}};
              value=serverState;
            } else if(path==='/api/confirmation/cancel') {
              serverState={...serverState,revision:serverState.revision+1,pending:{...serverState.pending,state:'cancelled'},
                input:{mode:'commands',token:'command-resumed'}};
              value=serverState;
            } else if(path==='/api/confirmation/pause') {
              serverState={...serverState,revision:serverState.revision+1,
                pending:serverState.pending?{...serverState.pending,state:'cancelled'}:null,
                input:{mode:'muted',token:null}};
              value=serverState;
            } else value=JSON.parse(JSON.stringify(serverState));
            return {ok:true,json:async()=>value,arrayBuffer:async()=>new ArrayBuffer(4)};
          };
        """
        if delayed_decode:
            setup += "Context.prototype.decodeAudioData = () => new Promise(resolve=>decodeResolve=resolve);"
        self.evaluate(setup + PHONE.read_text() + SCRIPT.read_text())

    def settle(self):
        for _ in range(35):
            self.evaluate("void 0")

    def start_prompt(self):
        self.evaluate("candidate(); void elements.start.onclick();")
        self.settle()

    def test_audio_muted_until_question_finishes_and_listen_ack_arrives(self):
        self.load()
        self.start_prompt()
        self.assertEqual(self.evaluate("clips.length"), "1")
        self.evaluate("processor.port.onmessage({data:new Float32Array(4800).fill(.5).buffer});")
        self.settle()
        self.assertEqual(self.evaluate("upload[0].value"), "0")
        self.assertEqual(self.evaluate("requests.filter(path=>path==='/api/confirmation/listen').length"), "0")
        self.evaluate("clips[0].onended();")
        self.settle()
        self.evaluate("processor.port.onmessage({data:new Float32Array(4800).fill(.5).buffer});")
        self.settle()
        self.assertEqual(self.evaluate("JSON.stringify([upload[1].value,upload[1].path.includes('tag=one')])"), "[0.5,true]")

    def test_repeated_status_does_not_replay_same_prompt(self):
        self.load()
        self.start_prompt()
        for _ in range(3):
            self.evaluate("fireTimer(200);")
            self.settle()
        self.assertEqual(self.evaluate("clips.length"), "1")

    def test_stop_before_decode_finishes_discards_late_audio(self):
        self.load(delayed_decode=True)
        self.start_prompt()
        self.evaluate("void stopCapture();")
        self.settle()
        self.evaluate("decodeResolve({duration:.1});")
        self.settle()
        self.assertEqual(self.evaluate("JSON.stringify([clips.length,requests.includes('/api/confirmation/listen')])"), "[0,false]")

    def test_cancel_during_playback_never_opens_listening(self):
        self.load()
        self.start_prompt()
        self.evaluate("void elements.cancel.onclick();")
        self.settle()
        self.assertEqual(self.evaluate("requests.includes('/api/confirmation/listen')"), "false")
        self.assertEqual(self.evaluate("window.captureHooks.audioTag()"), "command-resumed")

    def test_queued_audio_keeps_tag_from_capture_time(self):
        self.evaluate(base.BrowserLogicTests.browser_harness(self) + PHONE.read_text())
        self.evaluate("""
          let tag='old', release;
          window.captureHooks={audioTag:()=>tag};
          void startButton.onclick();
        """)
        self.settle()
        self.evaluate("""
          const originalFetch=fetch;
          let waiting=true;
          fetch=async(path,options)=>{
            if(path.startsWith('/api/audio')&&waiting){
              waiting=false;
              await new Promise(resolve=>release=resolve);
            }
            return originalFetch(path,options);
          };
          processor.port.onmessage({data:new Float32Array(4800).buffer});
          processor.port.onmessage({data:new Float32Array(4800).buffer});
          tag='new';
          processor.port.onmessage({data:new Float32Array(4800).buffer});
          release();
        """)
        self.settle()
        self.assertEqual(self.evaluate("JSON.stringify(requests.filter(path=>path.startsWith('/api/audio')).map(path=>path.split('tag=')[1]))"), '["old","old","new"]')

    def test_end_capture_cancels_before_sending_final_flush(self):
        self.load()
        self.start_prompt()
        self.evaluate("clips[0].onended();")
        self.settle()
        self.evaluate("""
          const originalPost=processor.port.postMessage;
          processor.port.postMessage=value=>{
            if(value.type==='flush') processor.port.onmessage({data:new Float32Array(100).fill(.5).buffer});
            originalPost(value);
          };
          void stopCapture();
        """)
        self.settle()
        self.assertEqual(self.evaluate("upload[upload.length-1].value"), "0")
        self.assertEqual(self.evaluate("requests.indexOf('/api/confirmation/pause')<requests.findIndex(path=>path.startsWith('/api/audio'))"), "true")

    def test_three_successive_requests_show_matching_questions_and_clear_finished_one(self):
        self.load()
        self.start_prompt()
        self.assertIn("方向盘的折叠操作", self.evaluate("elements.question.textContent"))
        for request_id, gesture, action in (("two", "摆1字", "半折叠"), ("three", "手掌展开", "展开")):
            self.evaluate("void elements.cancel.onclick();")
            self.settle()
            self.assertEqual(self.evaluate("elements.question.textContent"), "等待手势或直接语音指令")
            self.evaluate(f"candidate('{request_id}','{gesture}','{action}'); fireTimer(200);")
            self.settle()
            self.assertEqual(self.evaluate("elements.question.textContent"), f"您是想要进行方向盘的{action}操作吗？")

    def test_live_inference_updates_without_changing_locked_question(self):
        self.load()
        self.start_prompt()
        self.evaluate("""
          serverState={...serverState,revision:serverState.revision+1,
            inference:{caption:'手掌张开 - 展开命令',prepared:false}};
          fireTimer(200);
        """)
        self.settle()
        self.assertIn("手掌张开 - 展开命令", self.evaluate("elements['live-inference'].textContent"))
        self.assertIn("方向盘的折叠操作", self.evaluate("elements.question.textContent"))
        self.assertIn("本次：握拳 → 折叠", self.evaluate("elements['request-label'].textContent"))
        self.evaluate("serverState={...serverState,revision:serverState.revision+1,pending:{...serverState.pending,state:'expired'}}; fireTimer(200);")
        self.settle()
        self.assertEqual(self.evaluate("elements.question.textContent"), "等待手势或直接语音指令")
        self.assertIn("上次：握拳 → 折叠", self.evaluate("elements['request-label'].textContent"))

    def test_idle_capture_sends_real_audio_for_direct_voice_commands(self):
        self.load()
        self.evaluate("void elements.start.onclick();")
        self.settle()
        self.evaluate("processor.port.onmessage({data:new Float32Array(4800).fill(.5).buffer});")
        self.settle()
        self.assertEqual(self.evaluate("JSON.stringify([upload[0].value,upload[0].path.includes('tag=command-idle'),clips.length])"), "[0.5,true,0]")

    def test_chat_is_read_aloud_and_default_microphone_filter_blocks_reply_echo(self):
        self.load()
        # HERE is .../reference_action_learning/experiments/<experiment>.
        reply = HERE / "web/input_js/reply.js"
        self.evaluate(reply.read_text() + (HERE / "web/input_js/chat.js").read_text())
        self.evaluate("""
          serverState.chat={phase:'idle',message:'可以继续说话',dialogue:{dialogue_id:'chat-one',revision:1,
            turns:[{id:'old-answer',user:'你好',assistant:'你好，可以继续聊。',state:'done',error:''}]}};
          void elements.start.onclick();
        """)
        self.settle()
        self.assertEqual(self.evaluate("clips.length"), "1")
        self.assertIn("助手：你好", self.evaluate("elements['chat-turns'].textContent"))
        self.evaluate("processor.port.onmessage({data:new Float32Array(4800).fill(.5).buffer});")
        self.settle()
        self.assertEqual(self.evaluate("upload[0].value"), "0")
        self.evaluate("elements['chat-interrupt'].checked=true; window.dialogueUI.onTranscript({type:'partial',text:'等一下'});")
        self.settle()
        self.assertEqual(self.evaluate("requests.includes('/api/dialogue/cancel')"), "true")
        self.evaluate("processor.port.onmessage({data:new Float32Array(4800).fill(.5).buffer});")
        self.settle()
        self.assertEqual(self.evaluate("upload[1].value"), "0.5")

    def test_immediate_answer_is_buffered_until_listen_ack_not_discarded(self):
        self.load()
        self.start_prompt()
        self.evaluate("""
          const normalFetch=fetch;
          let releaseListen;
          fetch=async(path,options)=>{
            if(path==='/api/confirmation/listen') await new Promise(resolve=>releaseListen=resolve);
            return normalFetch(path,options);
          };
          clips[0].onended();
        """)
        self.settle()
        self.evaluate("processor.port.onmessage({data:new Float32Array(4800).fill(.75).buffer});")
        self.settle()
        self.assertEqual(self.evaluate("upload[0].value"), "0")
        self.evaluate("releaseListen();")
        self.settle()
        self.evaluate("processor.port.onmessage({data:new Float32Array(4800).buffer});")
        self.settle()
        self.assertEqual(self.evaluate("JSON.stringify([upload[1].value,upload[1].path.includes('tag=one')])"), "[0.75,true]")


if __name__ == "__main__":
    unittest.main()
