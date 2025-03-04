import sys
import os
import json
from datetime import datetime
import time
import tarfile
import gzip
import shutil
from pathlib import Path
import subprocess
import tempfile
import hashlib
from functools import reduce
import queue
import threading


def sha256sum(filename):
    # https://stackoverflow.com/a/44873382
    h = hashlib.sha256()
    b = bytearray(128*1024)
    mv = memoryview(b)
    with open(filename, 'rb', buffering=0) as f:
        for n in iter(lambda: f.readinto(mv), 0):
            h.update(mv[:n])
    return h.hexdigest()


def capture_files(d, excluded=None, as_set=False):
    captured = {}
    _set = set()
    for f in d.iterdir():
        if not f.is_file():
            continue
        digest = sha256sum(f)[:16]
        if as_set:
            _set.add(digest)
            continue
        elif excluded is not None and digest in excluded:
            continue

        ext = f.suffix or ".bin"
        target = Path("objects") / (digest + ext)
        if not target.exists():
            shutil.copy(f, target)
        captured[f.name] = digest + ext
    if as_set:
        return _set
    return captured


_libmagic_threadsafe = threading.Lock()


class TestEnv(object):
    def __init__(self, sample, is_tar):
        self.tmpdir = Path(tempfile.mkdtemp('ttrac'))
        submission_data_path = self.tmpdir / sample.stem
        with gzip.open(sample) as gz:
            with open(submission_data_path, "wb") as f:
                shutil.copyfileobj(gz, f)

        if is_tar:
            with tarfile.open(submission_data_path, 'r') as tar:
                def is_within_directory(directory, target):
                    
                    abs_directory = os.path.abspath(directory)
                    abs_target = os.path.abspath(target)
                
                    prefix = os.path.commonprefix([abs_directory, abs_target])
                    
                    return prefix == abs_directory
                
                def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
                
                    for member in tar.getmembers():
                        member_path = os.path.join(path, member.name)
                        if not is_within_directory(path, member_path):
                            raise Exception("Attempted Path Traversal in Tar File")
                
                    tar.extractall(path, members, numeric_owner=numeric_owner) 
                    
                
                safe_extract(tar, path=self.tmpdir)
            submission_data_path.unlink()

    def __enter__(self):
        return self.tmpdir

    def __exit__(self, exc, value, tb):
        shutil.rmtree(self.tmpdir)


BUNDLE_URL = "https://data1.fullyjustified.net/tlextras-2021.3r1.tar"
ARGUMENTS = [
    "-w", BUNDLE_URL,
    "--keep-logs", "--keep-intermediates",
]


def do_work(sample, maindoc, tectonic):
    print(sample)
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = "1456304492"
    with TestEnv(sample, is_tar=(maindoc != sample.stem)) as d:
        print(d)
        excluded = capture_files(d, as_set=True)
        start = time.time()
        try:
            test = subprocess.run([tectonic] + ARGUMENTS +
                                  [d / maindoc], timeout=600, cwd=d, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            statuscode = test.returncode
        except subprocess.TimeoutExpired:
            statuscode = -99999
        delta = time.time() - start
        results = capture_files(d, excluded=excluded)
        report = dict(sample=sample.stem, statuscode=statuscode,
                      seconds=delta, results=results)
    print(json.dumps(report))
    return report


def report(corpus, repo, name):
    with open(corpus + ".json") as f:
        sample_maindoc = json.load(f)

    branch = subprocess.check_output(
        "git rev-parse --abbrev-ref HEAD".split(), cwd=repo).decode().strip()
    commit = subprocess.check_output(
        "git rev-parse HEAD".split(), cwd=repo).decode().strip()
    timestamp = subprocess.check_output(
        "git show -s --format=%ci".split(), cwd=repo).decode().strip()

    meta = {
        "name": name,
        "branch": branch,
        "commit": commit,
        "link": None,
        "version": 0,
        "timestamp": timestamp,
        "dataset": Path(corpus).stem,
        "bundle_url": BUNDLE_URL,
        "meta": True
    }

    reportpath = Path("reports") / (name + ".jsonl")

    reportlog = open(reportpath, "w")

    reportlog.write(json.dumps(meta) + "\n")
    reportlog.flush()
    print(json.dumps(meta))

    subprocess.check_output("cargo build --release".split(), cwd=repo)
    tectonic = Path(repo) / "target" / "release" / "tectonic"
    # ensure that the tectonic binary is not replaced with another version
    tectonic_temp = tempfile.NamedTemporaryFile(
        suffix="tectonic", delete=False)
    shutil.copy2(tectonic, tectonic_temp.name)
    tectonic_temp.close()
    tectonic = tectonic_temp.name

    work = queue.Queue()
    outlock = threading.Lock()
    num_worker_threads = 5

    def worker():
        while True:
            item = work.get()
            if item is None:
                print("worker shutting down")
                break
            maindoc = sample_maindoc[item.stem]
            report = do_work(item, maindoc, tectonic)
            work.task_done()
            assert report
            with outlock:
                reportlog.write(json.dumps(report) + "\n")
                reportlog.flush()

    threads = []
    for _ in range(num_worker_threads):
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)

    for sample in Path(corpus).iterdir():
        if sample.stem not in sample_maindoc:
            continue
        work.put(sample)

    work.join()

    for i in range(num_worker_threads):
        work.put(None)
    for t in threads:
        t.join()
    reportlog.close()


assert len(sys.argv) == 4, "report_ci.py corpus repo name"
report(sys.argv[1], sys.argv[2], sys.argv[3])
