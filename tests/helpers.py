"""In-memory ASR and HTTP stand-ins; never use devices or media."""
import io

class FakeStream:
    def __init__(self):
        self.received = []
        self.finished = False

    def accept_waveform(self, rate, samples):
        self.received.append((rate, samples.copy()))

    def input_finished(self):
        self.finished = True

class FakeRecognizer:
    def __init__(self):
        self.text = ""
        self.endpoint = False
        self.resets = 0
        self.stream = FakeStream()

    def create_stream(self):
        return self.stream

    def is_ready(self, stream):
        return False

    def get_result(self, stream):
        return self.text

    def is_endpoint(self, stream):
        return self.endpoint

    def reset(self, stream):
        self.text = ""
        self.endpoint = False
        self.resets += 1

class MemoryConnection:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, *args):
        pass
