import glob
import json
import os
import subprocess
import threading
import time

try:                                   # Python 2 (classic EventGhost)
    from urllib2 import Request, urlopen
except ImportError:                    # Python 3
    from urllib.request import Request, urlopen

# ----------------------------------------------------------------------------
#  SETTINGS
# ----------------------------------------------------------------------------
SERVER_URL = "http://192.168.0.36:1880/ai/task"    # Node-RED flow endpoint
OLLAMA_API = "http://127.0.0.1:11434/api/ps"       # lists the models Ollama currently has loaded
POLL_S = 2.0                                       # how often to check (seconds); 1 is fine too
ACTIVE_GPU_PCT = 10                                 # GPU load at/above this counts as "working"
IDLE_HOLD_S = 6                                    # GPU must stay below the threshold this long before we send "stop"
REQUIRE_LOADED_MODEL = True                        # True: need a loaded Ollama model + busy GPU. False: Ollama just has to be running
GPU_INDEX = 0                                      # which GPU (nvidia-smi index) your 2080 is
HTTP_TIMEOUT = 3                                   # seconds, for Ollama and Node-RED requests
INFO_EVERY_S = 30                                  # while idle, resend model/GPU details this often (and at once when the model changes)
NVIDIA_SMI = ["nvidia-smi", r"C:\Windows\Sysnative\nvidia-smi.exe", r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe"]
# Sysnative matters: EventGhost is a 32-bit program, so Windows silently redirects
# C:\Windows\System32 to SysWOW64 for it, and the 64-bit nvidia-smi is not there.
# The script also searches the NVIDIA driver store automatically.
# ----------------------------------------------------------------------------

# nvidia-smi queries, richest first. If a driver rejects a field, the next set is tried.
SMI_QUERIES = [
    ["index", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw"],
    ["index", "utilization.gpu"],
]
GIB = 1073741824.0

try:
    eg                                 # defined by EventGhost
    IN_EVENTGHOST = True
except NameError:
    IN_EVENTGHOST = False


def log(text):
    print("[ollama-gpu] " + str(text))


def _f(value):
    """float() that turns nvidia-smi's '[N/A]' / '[Not Supported]' into None."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class OllamaGpuMonitor(object):

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self._smi = None               # the nvidia-smi path that worked
        self._smi_query = None         # the field list that worked
        self._warned = {}              # throttle repeated warnings
        self._info_sig = None          # last model details sent while idle
        self._info_ts = 0.0
        self.active = False
        self.last_busy = 0.0

    # -- control --------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run)
        self._thread.daemon = True
        self._thread.start()
        log("started: polling every %ss, reporting to %s" % (POLL_S, SERVER_URL))

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(POLL_S + 8)
        if self.active:                # tell Node-RED the task is over
            self._post({"event": "stop"})
            self.active = False
        log("stopped")

    # -- helpers --------------------------------------------------------------
    def _warn(self, key, text):
        now = time.time()
        if now - self._warned.get(key, 0) >= 60:
            self._warned[key] = now
            log(text)

    def _smi_candidates(self):
        if self._smi:
            return [self._smi]
        candidates = list(NVIDIA_SMI)
        root = os.environ.get("SystemRoot", r"C:\Windows")
        for base in ("Sysnative", "System32"):           # newer drivers keep it in the driver store
            candidates += sorted(glob.glob(os.path.join(root, base, "DriverStore", "FileRepository", "nv*", "nvidia-smi.exe")))
        return candidates

    def _smi_run(self, exe, fields):
        """Returns (returncode, stdout, stderr) or None if the exe cannot be started."""
        args = ["--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"]
        try:
            kw = {}
            if os.name == "nt":
                kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW: no console flash
            p = subprocess.Popen([exe] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        except OSError:
            return None
        killer = threading.Timer(6, p.kill)              # never hang on a stuck nvidia-smi
        killer.start()
        try:
            out, err = p.communicate()
        finally:
            killer.cancel()
        return p.returncode, out.decode("utf-8", "ignore"), err.decode("utf-8", "ignore")

    def read_gpu(self):
        """Dict for GPU_INDEX: util (%), mem_used_mb, mem_total_mb, temp_c, power_w.
        Fields the driver cannot report are None. Returns None if nvidia-smi fails."""
        queries = [self._smi_query] if self._smi_query else SMI_QUERIES
        problems = []
        for exe in self._smi_candidates():
            for fields in queries:
                result = self._smi_run(exe, fields)
                if result is None:
                    problems.append("%s: cannot start" % exe)
                    break                                # next exe
                code, text, err = result
                if code != 0:
                    problems.append("%s: exit code %s %s" % (exe, code, err.strip()[:80]))
                    continue                             # try the simpler field list
                for line in text.splitlines():
                    parts = [x.strip() for x in line.split(",")]
                    if len(parts) < 2 or parts[0] != str(GPU_INDEX):
                        continue
                    util = _f(parts[1])
                    if util is None:
                        problems.append("%s: GPU %s load not a number (%r)" % (exe, GPU_INDEX, parts[1]))
                        continue
                    self._smi = exe
                    self._smi_query = fields
                    row = dict(zip(fields, parts))
                    return {
                        "util": util,
                        "mem_used_mb": _f(row.get("memory.used")),
                        "mem_total_mb": _f(row.get("memory.total")),
                        "temp_c": _f(row.get("temperature.gpu")),
                        "power_w": _f(row.get("power.draw")),
                    }
                problems.append("%s: ran, but no row for GPU index %s (output: %r)" % (exe, GPU_INDEX, text.strip()[:60]))
        self._warn("smi", "cannot read the GPU with nvidia-smi:\n    " + "\n    ".join(problems))
        return None

    def read_ollama(self):
        """None = Ollama not reachable, [] = running but no model loaded, else the loaded models from /api/ps."""
        try:
            r = urlopen(OLLAMA_API, timeout=HTTP_TIMEOUT)
            try:
                data = json.loads(r.read().decode("utf-8"))
            finally:
                r.close()
        except Exception:
            return None
        return data.get("models") or []

    @staticmethod
    def model_name(m):
        return m.get("name") or m.get("model") or "model"

    def llm_info(self, models):
        """Model details for Node-RED / the M5Stack, from Ollama /api/ps. None if nothing is loaded."""
        if not models:
            return None
        m = models[0]
        det = m.get("details") or {}
        info = {
            "model": self.model_name(m),
            "family": det.get("family") or "",
            "params": det.get("parameter_size") or "",
            "quant": det.get("quantization_level") or "",
            "loaded": len(models),
        }
        size = m.get("size") or 0
        vram = m.get("size_vram")
        if size:
            info["size_gb"] = round(size / GIB, 2)
        if vram is not None:
            info["vram_gb"] = round(vram / GIB, 2)
            if size:                                     # < 100 = part of the model spilled to CPU/RAM
                info["gpu_offload_pct"] = int(round(100.0 * vram / size))
        ctx = m.get("context_length")                    # newer Ollama builds report this
        if ctx:
            try:
                info["ctx"] = int(ctx)
            except (ValueError, TypeError):
                pass
        return info

    @staticmethod
    def gpu_info(stats):
        if not stats:
            return None
        return {
            "temp_c": stats.get("temp_c"),
            "power_w": stats.get("power_w"),
            "mem_used_mb": stats.get("mem_used_mb"),
            "mem_total_mb": stats.get("mem_total_mb"),
        }

    def _post(self, payload):
        try:
            req = Request(SERVER_URL, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
            r = urlopen(req, timeout=HTTP_TIMEOUT)
            r.read()
            r.close()
            return True
        except Exception as e:
            self._warn("post", "cannot reach Node-RED at %s (%s) - will keep retrying" % (SERVER_URL, e))
            return False

    # -- main loop ------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:     # never let the thread die
                self._warn("tick", "unexpected error: %r" % (e,))
            self._stop.wait(POLL_S)

    def _tick(self):
        now = time.time()
        stats = self.read_gpu()
        gpu = stats["util"] if stats else None
        models = self.read_ollama()
        if models is None:
            model = None                                 # Ollama not reachable
        elif models:
            model = self.model_name(models[0])
        else:
            model = ""                                   # running, nothing loaded
        llm = self.llm_info(models)
        details = {"llm": llm, "gpu_info": self.gpu_info(stats)}

        busy = gpu is not None and gpu >= ACTIVE_GPU_PCT
        if REQUIRE_LOADED_MODEL:
            working = busy and bool(model)
        else:
            working = busy and model is not None

        def body(base):
            base.update(details)
            return base

        if working:
            self.last_busy = now
            if not self.active:
                if self._post(body({"event": "start", "gpu_pct": gpu, "task": ("ollama " + model).strip()})):
                    self.active = True        # if this fails we simply retry on the next poll
                    log("AI work started (%s, GPU %d%%)" % (model or "ollama", gpu))
            else:
                self._post(body({"event": "update", "gpu_pct": gpu}))
        elif self.active:
            if now - self.last_busy >= IDLE_HOLD_S:
                b = body({"event": "stop"})
                if gpu is not None:
                    b["gpu_pct"] = gpu
                if self._post(b):
                    self.active = False
                    log("AI work finished")
            else:                      # short lull between prompts: keep the gauge honest
                b = body({"event": "update"})
                if gpu is not None:
                    b["gpu_pct"] = gpu
                self._post(b)
        else:
            # Idle: keep the M5Stack's model line current. "info" never starts a task, and
            # deliberately carries no gpu_pct so gaming load can't be mistaken for AI work.
            sig = json.dumps(llm, sort_keys=True)
            if sig != self._info_sig or now - self._info_ts >= INFO_EVERY_S:
                if self._post(body({"event": "info"})):
                    if sig != self._info_sig:
                        log("model info: %s" % ((llm or {}).get("model") or "nothing loaded"))
                    self._info_sig = sig
                    self._info_ts = now


# ----------------------------------------------------------------------------
#  entry point
# ----------------------------------------------------------------------------
if IN_EVENTGHOST:
    try:                               # re-running the action (or reloading the config) replaces the old monitor
        eg.globals.ollamaGpuMon.stop()
    except AttributeError:
        pass
    eg.globals.ollamaGpuMon = OllamaGpuMonitor()
    eg.globals.ollamaGpuMon.start()
elif __name__ == "__main__":
    _mon = OllamaGpuMonitor()
    _mon.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _mon.stop()