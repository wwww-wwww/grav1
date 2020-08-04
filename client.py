#!/usr/bin/env python3

import os, subprocess, re, contextlib, requests, time, json, shutil
from tempfile import NamedTemporaryFile
from threading import Lock, RLock, Thread, Event
from collections import deque

bytes_map = ["B", "K", "M", "G"]

KEY_R = ord("R")
KEY_1 = ord("1")
KEY_2 = ord("2")
KEY_3 = ord("3")

def n_bytes(num_bytes):
  if num_bytes / 1024 < 1: return (num_bytes, 0)
  r = n_bytes(num_bytes / 1024)
  return (r[0], r[1] + 1)

def bytes_str(num_bytes):
  r = n_bytes(num_bytes)
  return f"{r[0]:.1f}{bytes_map[r[1]]}"

def print_progress_bytes(n, total):
  fill = "█" * int((n / total) * 10)
  return "{:3.0f}%|{:{}s}| {}/{}".format(100 * n / total, fill, 10, bytes_str(n), bytes_str(total))

def print_progress(n, total):
  fill = "█" * int((n / total) * 10)
  return "{:3.0f}%|{:{}s}| {}/{}".format(100 * n / total, fill, 10, n, total)

@contextlib.contextmanager
def tmp_file(stream, suffix, worker):
  try:
    file = NamedTemporaryFile(mode="wb", suffix=suffix, dir=".", delete=False)
    tmp_name = file.name
    downloaded = 0
    total_size = int(stream.headers["content-length"])
    for chunk in stream.iter_content(chunk_size=2**16):
      if worker.stopped: break
      if chunk:
        downloaded = downloaded + len(chunk)
        worker.update_status("downloading", print_progress_bytes(downloaded, total_size), progress=True)
        file.write(chunk)
    file.flush()
    file.close()
    yield tmp_name
  except: pass
  finally:
    while os.path.exists(tmp_name):
      try:
        os.remove(tmp_name)
      except: pass

def aom_vpx_encode(encoder, encoder_path, worker, job, video):
  encoder_params = job.encoder_params
  ffmpeg_params = job.ffmpeg_params

  if encoder == "aomenc" and "vmaf" in encoder_params and len(worker.client.args.vmaf_path) > 0:
    encoder_params += f" --vmaf-model-path={worker.client.args.vmaf_path}"

  vfs = [f"select=gte(n\\,{job.start})"]

  vf_match = re.search(r"(?:-vf\s\"([^\"]+?)\"|-vf\s([^\s]+?)\s)", ffmpeg_params)

  if vf_match:
    vfs.append(vf_match.group(1) or vf_match.group(2))
    ffmpeg_params = re.sub(r"(?:-vf\s\"([^\"]+?)\"|-vf\s([^\s]+?)\s)", "", ffmpeg_params).strip()

  vf = ",".join(vfs)

  output_filename = f"{video}.ivf"

  ffmpeg = [
    worker.client.args.ffmpeg, "-y", "-hide_banner",
    "-loglevel", "error",
    "-i", video,
    "-strict", "-1",
    "-pix_fmt", "yuv420p",
    "-vf", vf,
    "-vframes", job.frames
  ]

  if ffmpeg_params:
    ffmpeg.extend(ffmpeg_params.split(" "))

  ffmpeg.extend(["-f", "yuv4mpegpipe", "-"])

  aom = [encoder_path, "-", "--ivf", f"--fpf={video}.log", f"--threads={args.threads}", "--passes=2"]

  passes = [
    aom + re.sub(r"--denoise-noise-level=[0-9]+", "", encoder_params).split(" ") + ["--pass=1", "-o", os.devnull],
    aom + encoder_params.split(" ") + ["--pass=2", "-o", output_filename]
  ]

  if job.grain:
    if not job.grain_file:
      return False, None
    else:
      passes[1].append(f"--film-grain-table={job.grain_file}")

  total_frames = int(job.frames)

  success = True
  for pass_n, cmd in enumerate(passes, start=1):
    ffmpeg_pipe = subprocess.Popen(ffmpeg,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT)

    worker.pipe = subprocess.Popen(cmd,
      stdin=ffmpeg_pipe.stdout,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      universal_newlines=True)

    worker.progress = (pass_n, 0)
    worker.update_status(f"{encoder:.3s}", "pass:", pass_n, print_progress(0, total_frames), progress=True)

    while True:
      line = worker.pipe.stdout.readline().strip()

      if len(line) == 0 and worker.pipe.poll() is not None:
        break

      match = re.search(r"frame.*?\/([^ ]+?) ", line)
      if match:
        worker.progress = (pass_n, int(match.group(1)))
        worker.update_status(f"{encoder:.3s}", "pass:", pass_n, print_progress(int(match.group(1)), total_frames), progress=True)

    if ffmpeg_pipe.poll() is None:
      ffmpeg_pipe.kill()

    if worker.pipe.returncode != 0:
      success = False

  if os.path.isfile(f"{video}.log"):
    os.remove(f"{video}.log")

  return success, output_filename

def cancel_job(job):
  try:
    client.session.post(
      f"{client.args.target}/cancel_job",
      data={
        "client": job.id,
        "scene": job.scene,
        "projectid": job.projectid
      }
    )
  except: pass

def upload(client, job, output):
  file = open(output, "rb")
  files = [("file", (os.path.splitext(job.filename)[0] + os.path.splitext(output)[1], file, "application/octet"))]
  try:
    if client.args.noui:
      print("uploading to", f"{client.args.target}/finish_job")
    r = client.session.post(
      f"{client.args.target}/finish_job",
      data={
        "client": job.id,
        "scene": job.scene,
        "projectid": job.projectid,
        "encoder": job.encoder,
        "version": encoder_versions[job.encoder],
        "encoder_params": job.encoder_params,
        "ffmpeg_params": job.ffmpeg_params,
        "grain": int(len(job.grain_file) > 0)
      },
      files=files)
    return r
  except:
    return False
  finally:
    file.close()

class Job:
  def __init__(self, r):
    self.id = r.headers["id"]
    self.filename = r.headers["filename"]
    self.scene = r.headers["scene"]
    self.encoder = r.headers["encoder"]
    self.encoder_params = r.headers["encoder_params"]
    self.ffmpeg_params = r.headers["ffmpeg_params"]
    self.projectid = r.headers["projectid"]
    self.frames = r.headers["frames"]
    self.start = r.headers["start"]
    self.request = r
    self.grain = int(r.headers["grain"]) if "grain" in r.headers else None
    self.grain_file = ""

class Client:
  def __init__(self, config, args):
    self.config = config
    self.args = args
    self.workers = []
    self.numworkers = int(args.workers)
    self.completed = 0
    self.failed = 0
    self.lock = Lock()
    self.session = requests.Session()
    self.scr = None
    self.render_lock = RLock()
    
    self.menu = type("", (), {})
    self.menu.selected_item = 0
    self.menu.items = ["add", "remove", "kill", "quit"]
    self.menu.scroll = 0

    self.refreshing = False
    self.screen_thread = Thread(target=self.screen, daemon=True)
    self.refresh = Event()
    self.screen_thread.start()

    self.worker_timer = Event()

    self.stopping = False
    self.exit_event = Event()
    self.exit_message = None

    self.encode = {
      "aom": lambda worker, job, video: aom_vpx_encode("aom", args.aomenc, worker, job, video),
      "vpx": lambda worker, job, video: aom_vpx_encode("vpx", args.vpxenc, worker, job, video)
    }

    self.download_lock = Lock()

    self.upload_queue = deque()
    self.upload_queue_event = Event()
    self.upload_loop = Thread(target=self._upload_loop, daemon=True)
    self.upload_loop.start()
    self.uploading = None

  def _upload_loop(self):
    while True:
      if len(self.upload_queue) == 0:
        self.upload_queue_event.wait()

      self.upload_queue_event.clear()
      
      job, output = self.upload_queue.popleft()
      self.uploading = job

      uploads = 3
      while True:
        r = upload(self, job, output)
        
        if r:
          if r.text == "saved":
            self.completed += 1
            break
          elif r.text == "bad upload" and uploads > 0:
            if self.args.noui:
              print("bad upload", "retrying", job.projectid, job.scene)
            uploads -= 1
            time.sleep(1)
          else:
            if self.args.noui:
              print("failed", r.status_code, r.text, job.projectid, job.scene)
            self.failed += 1
            if self.args.noui:
              print("error", r.status_code)
            break
        else:
          if self.args.noui:
            print("unable to connect, trying again")
          time.sleep(1)
      
      while os.path.isfile(output):
        try:
          os.remove(output)
        except:
          time.sleep(1)

      self.uploading = None
      self.refresh_screen()

  def upload(self, job, output):
    self.upload_queue.append((job, output))
    self.upload_queue_event.set()
    self.refresh_screen()

  def fetch_grain_table(self, projectid, scene):
    try:
      r = self.session.get(f"{self.args.target}/api/get_grain/{projectid}/{scene}", timeout=3, stream=True)
      if r.status_code != 200:
        return None

      return r
    except:
      return None

  def fetch_new_job(self):
    jobs = [{"projectid": worker.job.projectid, "scene": worker.job.scene} for worker in self.workers if worker.job is not None]
    jobs.extend([{"projectid": up[0].projectid, "scene": up[0].scene} for up in self.upload_queue])
    if self.uploading:
      jobs.append({"projectid": self.uploading.projectid, "scene": self.uploading.scene})

    jobs_str = json.dumps(jobs)
    try:
      r = self.session.get(f"{self.args.target}/api/get_job/{jobs_str}", timeout=3, stream=True)
      if r.status_code != 200:
        return None

      job = Job(r)

      if r.headers["version"] != encoder_versions[job.encoder]:
        cancel_job(job)

        if job.encoder == "aom":
          if os.path.isfile("aomenc.exe"):
            self.config["r"] = len(self.workers)
            save_config(self.config)
            os.remove("aomenc.exe")
            self.stop(f"bad aom version. have: {encoder_versions[job.encoder]} required: {r.headers['version']}\n\nRestart to re-download.")
          else:
            client.stop(f"bad aom version. have: {encoder_versions[job.encoder]} required: {r.headers['version']}")

        if job.encoder == "vpx":
          if os.path.isfile("vpxenc.exe"):
            self.config["r"] = len(self.workers)
            save_config(self.config)
            os.remove("vpxenc.exe")
            self.stop(f"bad vpx version. have: {encoder_versions[job.encoder]} required: {r.headers['version']}\n\nRestart to re-download.")
          else:
            self.stop(f"bad vpx version. have: {encoder_versions[job.encoder]} required: {r.headers['version']}")

        return None

      return job
    except:
      return None

  def stop(self, message=""):
    self.stopping = True
    for worker in self.workers:
      worker.kill()
    
    self.exit_event.set()
    self.exit_message = message

  def add_worker(self, worker):
    if self.stopping: return
    self.workers.append(worker)
    worker.start()

  def remove_worker(self, worker):
    if worker in self.workers:
      self.workers.remove(worker)

  def _refresh_screen(self):
    self.header.set_text(f"workers: {self.numworkers} hit: {self.completed} miss: {self.failed}")

  def screen(self):
    while self.refresh.wait():
      if not self.scr: continue
      self.render_lock.acquire()
      msg = []
      for i, worker in enumerate(self.workers, start=1):
        msg.append(f"{i:2} {worker.status}")

      n_active = len([worker for worker in self.workers if worker.pipe])
      n_uploading = len(self.upload_queue) + 1 if self.uploading else 0
      footer = " ".join([f"[{item}]" if i == self.menu.selected_item else f" {item} " for i, item in enumerate(self.menu.items)])

      self.scr.erase()

      (mlines, mcols) = self.scr.getmaxyx()

      header = []
      for line in textwrap.wrap(f"workers: {self.numworkers} active: {n_active} uploading: {n_uploading} "
        f"hit: {self.completed} miss: {self.failed}", width=mcols):
        header.append(line)

      body_y = len(header)
      window_size = mlines - body_y - 1
      self.menu.scroll = max(min(self.menu.scroll, len(self.workers) - window_size), 0)

      for i, line in enumerate(header):
        self.scr.insstr(i, 0, line.ljust(mcols), curses.color_pair(1))

      for i, line in enumerate(msg[self.menu.scroll:window_size + self.menu.scroll], start=body_y):
        self.scr.insstr(i, 0, line)

      pad = " " * (mcols - len(footer) - len(self.args.target) - 1)
      self.scr.insstr(mlines - 1, 0, f"{footer}{pad}{self.args.target}"[:mcols].ljust(mcols) , curses.color_pair(1))
      
      self.scr.refresh()
      self.refresh.clear()
      self.render_lock.release()

  def refresh_screen(self):
    self.refresh.set()
  
  def key_loop(self, scr):
    while True:
      c = scr.getch()

      if c == curses.KEY_UP:
        self.menu.scroll -= 1
      elif c == curses.KEY_DOWN:
        self.menu.scroll += 1
      elif c == curses.KEY_LEFT:
        self.menu.selected_item = max(self.menu.selected_item - 1, 0)
      elif c == curses.KEY_RIGHT:
        self.menu.selected_item = min(self.menu.selected_item + 1, len(self.menu.items) - 1)
      elif c == 10 or c == curses.KEY_ENTER:
        menu_action = self.menu.items[self.menu.selected_item]

        if menu_action == "add":
          self.numworkers += 1
          while len(self.workers) < self.numworkers:
            new_worker = Worker(self)
            self.add_worker(new_worker)

        elif menu_action == "remove":
          self.numworkers = max(self.numworkers - 1, 0)
          if self.lock.locked() and any(worker for worker in self.workers if worker.job is None and not worker.lock_acquired):
            self.lock.release()
          elif any(worker for worker in self.workers if worker.lock_acquired):
            self.worker_timer.set()

        elif menu_action == "kill":
          if len(self.workers) == self.numworkers or any(worker for worker in self.workers if worker.job is None):
            self.numworkers = max(self.numworkers - 1, 0)

          if self.lock.locked() and any(worker for worker in self.workers if worker.job is None and not worker.lock_acquired):
            self.lock.release()
          elif any(worker for worker in self.workers if worker.lock_acquired):
            self.worker_timer.set()
          else:
            sorted_workers = sorted([worker for worker in self.workers if not worker.stopped], key= lambda x: (1 if x.pipe else 0, x.progress, x.lock_acquired, 1 if x.job else None))
            if len(sorted_workers) > 0:
              sorted_workers[0].status = "killing"
              sorted_workers[0].kill()
                
              self.remove_worker(sorted_workers[0])
          
        elif menu_action == "quit":
          self.stop()
      elif c == KEY_R:
        with self.render_lock:
          self.scr.clear()
          self.scr.refresh()

      self.refresh_screen()
  
  def window(self, scr):
    self.scr = scr

    curses.curs_set(0)
    scr.nodelay(0)

    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)

    self.refresh_screen()
    
    k_t = Thread(target=self.key_loop, args=(scr,), daemon=True)
    k_t.start()

    self.exit_event.wait()
    for worker in self.workers:
      while worker.thread.is_alive():
        worker.kill()
        time.sleep(1)

    curses.curs_set(1)

class Worker:
  def __init__(self, client):
    self.status = ""
    self.client = client
    self.lock_acquired = False
    self.thread = None
    self.job = None
    self.pipe = None
    self.stopped = False
    self.progress = (0, 0)
    self.id = 0

  def kill(self):
    self.stopped = True
    if self.pipe and self.pipe.poll() is None:
      self.pipe.kill()
    
    if self.job:
      cancel_job(self.job)

  def start(self):
    self.thread = Thread(target=lambda: self.work(), daemon=True)
    self.thread.start()

  def update_status(self, *argv, progress=False):
    message = " ".join([str(arg) for arg in argv])
    if self.stopped: return
    if self.client.args.noui and not progress:
      print(self.id, message)
    else:
      self.status = message
      self.client.refresh_screen()

  def work(self):
    while True:
      self.update_status("waiting", progress=True)

      self.client.lock.acquire()
      self.lock_acquired = True

      if len(self.client.workers) > self.client.numworkers or self.stopped:
        if self.client.lock.locked():
          self.client.lock.release()
        self.client.remove_worker(self)
        return

      if self.client.download_lock.locked():
        self.client.lock.release()
        self.lock_acquired = False
        continue

      self.client.download_lock.acquire()
      self.update_status("downloading")

      while True:
        self.job = self.client.fetch_new_job()
        if self.job: break
        for i in range(0, 15):
          self.client.worker_timer.clear()
          if len(self.client.workers) > self.client.numworkers or self.stopped:
            if self.client.lock.locked():
              self.client.lock.release()
            self.client.download_lock.release()
            self.client.remove_worker(self)
            return
          self.update_status(f"waiting...{15-i:2d}")
          self.client.worker_timer.wait(1)
          self.client.worker_timer.clear()

      self.update_status("received", self.job.projectid, self.job.scene)
      
      try:
        with tmp_file(self.job.request, self.job.filename, self) as file:
          if self.client.lock.locked():
            self.client.lock.release()
          self.client.download_lock.release()
          self.lock_acquired = False

          if self.stopped:
            return

          if self.job.encoder in self.client.encode:
            if self.job.grain:
              attempts = 15
              while attempts > 0:
                grain_table = self.client.fetch_grain_table(self.job.projectid, self.job.scene)
                if grain_table: break
                self.client.worker_timer.clear()
                self.client.worker_timer.wait(1)
                self.client.worker_timer.clear()
                attempt -= 1

              if self.stopped:
                return
                
              if grain_table:
                try:
                  with tmp_file(grain_table, f"{self.job.scene}.table", self) as grain_file:
                    self.job.grain_file = grain_file
                    success, output = self.client.encode[self.job.encoder](self, self.job, file)
                except:
                  success, output = False, None
              else:
                success, output = False, None

            else:
              success, output = self.client.encode[self.job.encoder](self, self.job, file)
          else:
            success, output = False, None

          if self.pipe and self.pipe.poll() is None:
            self.pipe.kill()

          self.pipe = None

          if success:
            self.client.upload(self.job, output)
            self.job = None
          elif output:
            while os.path.exists(output):
              try:
                os.remove(output)
              except: pass

      except: pass  

      if self.lock_acquired:
        self.client.lock.release()
        self.client.download_lock.release()

    self.client.remove_worker(self)

windows_binaries = [
  ("vmaf_v0.6.1.pkl", "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.pkl", "binary"),
  ("vmaf_v0.6.1.pkl.model", "https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.pkl.model", "binary"),
  ("ffmpeg.exe", "https://www.sfu.ca/~ssleong/ffmpeg.zip", "zip"),
  ("vpxenc.exe", "https://www.sfu.ca/~ssleong/vpxenc.exe", "binary")
]

def get_aomenc_version():
  if not shutil.which(args.aomenc):
    print("aomenc not found, exiting in 3s")
    time.sleep(3)
    exit()
  p = subprocess.run([args.aomenc, "--help"], stdout=subprocess.PIPE)
  r = re.search(r"av1\s+-\s+(.+)\n", p.stdout.decode("utf-8"))
  return r.group(1).replace("(default)", "").strip()

def get_vpxenc_version():
  if not shutil.which("vpxenc"):
    print("vpxenc not found, exiting in 3s")
    time.sleep(3)
    exit()
  p = subprocess.run(["vpxenc", "--help"], stdout=subprocess.PIPE)
  r = re.search(r"vp9\s+-\s+(.+)\n", p.stdout.decode("utf-8"))
  return r.group(1).replace("(default)", "").strip()

def save_config(config):
  json.dump(config, open("config", "w+"))

if __name__ == "__main__":
  import argparse

  parser = argparse.ArgumentParser()
  parser.add_argument("target", type=str, nargs="?", default="http://localhost:7899")
  parser.add_argument("--vmaf-model-path", dest="vmaf_path", default="vmaf_v0.6.1.pkl" if os.name == "nt" else "")
  parser.add_argument("--workers", dest="workers", default=1)
  parser.add_argument("--threads", dest="threads", default=8)
  parser.add_argument("--noui", action="store_const", const=True)
  parser.add_argument("--aomenc", default="aomenc", help="path to aomenc")
  parser.add_argument("--vpxenc", default="vpxenc", help="path to vpxenc")
  parser.add_argument("--ffmpeg", default="ffmpeg", help="path to ffmpeg")

  args = parser.parse_args()

  if os.name == "nt":
    if not os.path.isfile("aomenc.exe"):
      with requests.get("https://ci.appveyor.com/api/projects/marcomsousa/build-aom") as r:
        latest_job = r.json()["build"]["jobs"][0]["jobId"]
        windows_binaries.append(("aomenc.exe", f"https://ci.appveyor.com/api/buildjobs/{latest_job}/artifacts/aomenc.exe", "binary"))
    for file in windows_binaries:
      if not os.path.isfile(file[0]):
        print(file[0], "is missing, downloading...")

        r = requests.get(file[1])

        if file[2] == "binary":
          with open(file[0], "wb+") as f:
            f.write(r.content)

        if file[2] == "zip":
          print("unpacking")
          from zipfile import ZipFile
          from io import BytesIO
          zipdata = BytesIO()
          zipdata.write(r.content)
          zipfile = ZipFile(zipdata)
          with zipfile.open(file[0]) as file_in_zip:
            with open(file[0], "wb+") as f:
              f.write(file_in_zip.read())

  encoder_versions = {"aom": get_aomenc_version(), "vpx": get_vpxenc_version()}

  if os.path.exists("config"):
    try:
      config = json.load(open("config", "r"))
    except:
      config = {}
  else:
    config = {}

  client = Client(config, args)

  if args.workers == 1 and "r" in config:
    n_workers = config["r"]
    del config["r"]
    save_config(config)
  else:
    n_workers = args.workers

  for i in range(0, int(n_workers)):
    client.add_worker(Worker(client))

  if args.noui:
    for worker in client.workers:
      worker.thread.join()
  else:
    import curses, textwrap
    curses.wrapper(lambda scr: client.window(scr))
    if client.exit_message:
      print(client.exit_message)
      time.sleep(3)
