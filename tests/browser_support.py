"""Exercise JS with the system JavaScriptCore engine; no browser/device capture."""
import ctypes
import ctypes.util
from pathlib import Path
import unittest


LIBRARY = ctypes.util.find_library("javascriptcoregtk-4.0")
WEB = Path(__file__).resolve().parents[1] / "steering_control/web/input_js"


@unittest.skipUnless(LIBRARY, "可选的系统 JavaScriptCore 不可用")
class BrowserLogicTests(unittest.TestCase):
    def setUp(self):
        self.lib = ctypes.CDLL(LIBRARY)
        pointer = ctypes.c_void_p
        signatures = {
            "JSGlobalContextCreate": ([pointer], pointer),
            "JSGlobalContextRelease": ([pointer], None),
            "JSStringCreateWithUTF8CString": ([ctypes.c_char_p], pointer),
            "JSStringRelease": ([pointer], None),
            "JSEvaluateScript": ([pointer, pointer, pointer, pointer, ctypes.c_int, ctypes.POINTER(pointer)], pointer),
            "JSValueToStringCopy": ([pointer, pointer, ctypes.POINTER(pointer)], pointer),
            "JSStringGetMaximumUTF8CStringSize": ([pointer], ctypes.c_size_t),
            "JSStringGetUTF8CString": ([pointer, pointer, ctypes.c_size_t], ctypes.c_size_t),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result
        self.context = self.lib.JSGlobalContextCreate(None)

    def tearDown(self):
        self.lib.JSGlobalContextRelease(self.context)

    def stringify(self, value):
        string = self.lib.JSValueToStringCopy(self.context, value, None)
        size = self.lib.JSStringGetMaximumUTF8CStringSize(string)
        buffer = ctypes.create_string_buffer(size)
        self.lib.JSStringGetUTF8CString(string, buffer, size)
        self.lib.JSStringRelease(string)
        return buffer.value.decode("utf-8")

    def evaluate(self, source):
        string = self.lib.JSStringCreateWithUTF8CString(source.encode("utf-8"))
        exception = ctypes.c_void_p()
        value = self.lib.JSEvaluateScript(self.context, string, None, None, 1, ctypes.byref(exception))
        self.lib.JSStringRelease(string)
        if exception.value:
            self.fail(self.stringify(exception.value))
        return self.stringify(value)

    def test_worklet_chunks_downmix_and_tail_flush(self):
        harness = """
        const sampleRate = 16000;
        const sent = [];
        class AudioWorkletProcessor {
          constructor() { this.port = {postMessage: value => sent.push(value)}; }
        }
        function registerProcessor(name, value) { globalThis.Capture = value; }
        """
        self.evaluate(harness + WEB.joinpath("capture.js").read_text())
        result = self.evaluate("""
          const capture = new Capture();
          for (let i=0; i<2; i++) capture.process([[new Float32Array(800).fill(1), new Float32Array(800).fill(-1)]]);
          capture.process([[new Float32Array(100).fill(0.5)]]);
          capture.port.onmessage({data:{type:'flush'}});
          capture.process([[new Float32Array(100).fill(1)]]);
          JSON.stringify([sent.length, new Float32Array(sent[0]).length,
                          new Float32Array(sent[0])[0], new Float32Array(sent[1]).length,
                          new Float32Array(sent[1])[0], sent[2].type]);
        """)
        self.assertEqual(result, '[3,1600,0,100,0.5,"flushed"]')

    def browser_harness(self):
        return """
          const elements = {};
          const document = {getElementById: id => elements[id] || (elements[id] = {textContent:'', disabled:false})};
          const track = {stop() { globalThis.trackStopped = true; }};
          const requests = [];
          const navigator = {mediaDevices:{getUserMedia: async options => {
            if(options.video !== false) throw Error('摄像头不得开启');
            return {getTracks:()=>[track], getAudioTracks:()=>[track]};
          }}, sendBeacon:()=>true};
          function setTimeout() { return 1; }
          function clearTimeout() {}
          class AbortController { constructor(){this.signal={};} abort(){} }
          class Context {
            constructor(){this.sampleRate=48000;this.state='running';this.audioWorklet={addModule:async()=>{}};}
            async resume() {}
            async close() {this.state='closed';}
            createMediaStreamSource(){return {connect(){},disconnect(){}};}
            createGain(){return {gain:{value:1},connect(){},disconnect(){}};}
          }
          class AudioWorkletNode {
            constructor(){this.port={postMessage:()=>this.port.onmessage({data:{type:'flushed'}})};}
            connect(){} disconnect(){}
          }
          const window = {isSecureContext:true,AudioContext:Context,addEventListener(){}};
          async function fetch(path, options) {
            requests.push(path);
            const payload = path === '/api/start' ? {session_id:'test-phone'} :
                            path === '/api/stop' ? {events:[{type:'final',text:'安卓中文'}]} :
                            {events:[{type:'partial',text:'安卓'}]};
            return {ok:true,json:async()=>payload};
          }
        """

    def test_browser_start_audio_stop_and_release_microphone(self):
        self.evaluate(self.browser_harness() + WEB.joinpath("phone.js").read_text())
        self.evaluate("""
          globalThis.outcome = 'pending';
          (async()=>{
            await startButton.onclick();
            processor.port.onmessage({data:new Float32Array(4800).buffer});
            await pumping;
            await stopCapture();
            globalThis.outcome = JSON.stringify([requests, trackStopped,
              elements.final.textContent, elements.partial.textContent,
              elements.start.disabled, elements.stop.disabled]);
          })().catch(error=>globalThis.outcome='ERROR:'+error);
        """)
        for _ in range(20):
            result = self.evaluate("globalThis.outcome")
            if result != "pending":
                break
        self.assertEqual(result, '[["/api/start","/api/audio?session=test-phone&sequence=0","/api/stop"],true,"安卓中文","",false,true]')

    def test_insecure_page_never_requests_microphone(self):
        self.evaluate(self.browser_harness() + WEB.joinpath("phone.js").read_text())
        self.evaluate("""
          window.isSecureContext=false;
          globalThis.outcome='pending';
          startButton.onclick().then(()=>globalThis.outcome=JSON.stringify([requests.length,
            elements.status.textContent.includes('HTTPS'), elements.start.disabled]));
        """)
        for _ in range(20):
            result = self.evaluate("globalThis.outcome")
            if result != "pending":
                break
        self.assertEqual(result, '[0,true,false]')

    def test_microphone_remains_active_across_multiple_utterances(self):
        self.evaluate(self.browser_harness() + WEB.joinpath("phone.js").read_text())
        self.evaluate("""
          globalThis.outcome='pending';
          (async()=>{
            await startButton.onclick();
            for(let i=0;i<2;i++){
              processor.port.onmessage({data:new Float32Array(4800).buffer});
              await pumping;
              render({events:[{type:'final',text:'问题'+i}]});
            }
            globalThis.outcome=JSON.stringify([requests.filter(path=>path==='/api/start').length,
              requests.filter(path=>path.startsWith('/api/audio')).length,accepting,Boolean(globalThis.trackStopped),stopButton.disabled]);
          })().catch(error=>globalThis.outcome='ERROR:'+error);
        """)
        for _ in range(20):
            result = self.evaluate("globalThis.outcome")
            if result != 'pending':
                break
        self.assertEqual(result, '[1,2,true,false,false]')


if __name__ == "__main__":
    unittest.main()
