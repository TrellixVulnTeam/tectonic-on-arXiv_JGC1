import click
import magic

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

# https://stackoverflow.com/a/44873382
def sha256sum(filename):
    h  = hashlib.sha256()
    b  = bytearray(128*1024)
    mv = memoryview(b)
    with open(filename, 'rb', buffering=0) as f:
        for n in iter(lambda : f.readinto(mv), 0):
            h.update(mv[:n])
    return h.hexdigest()

def get_maindoc(p):
	texfiles = list(filter(lambda x: x.suffix == '.tex' or magic.detect_from_filename(x).mime_type == 'text/x-tex', p.iterdir()))
	if len(texfiles) < 2:
		return texfiles[0]
	for x in texfiles:
		with open(x, "rb") as f:
			if b"\\documentclass" in f.read():
				return x
	assert False, "multiple entry points?"

TAGS = {
	'no-font-for-pdf': "Cannot proceed without .vf or \"physical\" font for PDF output...",
	'latex-file-not-found': "LaTeX Error: File",
	'undefined-control-sequence': "! Undefined control sequence.",
	'not-latex': "LaTeX Error: Missing \\begin{document}",
	'uses-inputenc': "Package inputenc Error: inputenc is not designed for xetex or luatex.",
	'latex-error': "LaTeX Error",
	'bib-failed': "\\end{thebibliography}"
}

IMPLIED_TAGS = {k: set([k_ for k_, v_ in TAGS.items() if v_ in v and k_ != k]) for k, v in TAGS.items()}

def get_tags(p):
	if p.exists():
		with open(p) as f:
			data = f.read()
	else:
		return ["no-log-file"]
	# TODO: aho-croasick
	res = []
	for k, v in TAGS.items():
		if v in data:
			res.append(k)

	redundant = reduce(lambda a, b: a | b, [IMPLIED_TAGS[tag] for tag in res], set())
	return sorted(list(set(res) - redundant))

_CAPTURE_EXCLUDE = set()
def capture_files(d, exclude_all=False):
	global _CAPTURE_EXCLUDE
	_EXTS = {
		"application/pdf": ".pdf",
		"text/plain": ".txt",
	}
	captured = {}
	for f in d.iterdir():
		if not f.is_file():
			continue
		mt = magic.detect_from_filename(f).mime_type
		digest = sha256sum(f)[:16]
		if exclude_all:
			_CAPTURE_EXCLUDE.add(digest)
			continue
		elif digest in _CAPTURE_EXCLUDE:
			continue

		ext = _EXTS.get(mt, ".bin")
		target = Path("objects") / (digest + ext)
		if not target.exists():
			shutil.copy(f, target)
		captured[f.name] = digest + ext
	return captured



class TestEnv(object):
	def __init__(self, sample):
		self.tmpdir = Path(tempfile.mkdtemp('ttrac'))
		assert magic.detect_from_filename(sample).mime_type == 'application/x-gzip'

		submission_data_path = self.tmpdir / sample.stem


		with gzip.open(sample) as gz:
			with open(submission_data_path, "wb") as f:
				shutil.copyfileobj(gz, f)

		if magic.detect_from_filename(submission_data_path).mime_type == "application/x-tar":
			with tarfile.open(submission_data_path, 'r') as tar:
				tar.extractall(path=self.tmpdir)
			submission_data_path.unlink()
	def __enter__(self):
		return self.tmpdir
	def __exit__(self, exc, value, tb):
		shutil.rmtree(self.tmpdir)


@click.command()
@click.argument('corpus', type=click.Path(exists=True))
@click.argument('repo', type=click.Path(exists=True))
def report(corpus, repo):
	print(repo, corpus)

	name = subprocess.check_output("git describe --always --dirty --tags --exclude continuous".split(), cwd=repo).decode().strip()
	branch = subprocess.check_output("git rev-parse --abbrev-ref HEAD".split(), cwd=repo).decode().strip()
	timestamp = subprocess.check_output("git show -s --format=%ci".split(), cwd=repo).decode().strip()

	if branch != "HEAD":
		name = branch + "-" + name

	if "dirty" in name and click.confirm("repo is dirty => use current time?"):
		timestamp = datetime.now().isoformat()

	meta = {
		"name": name,
		"branch": branch,
		"timestamp": timestamp,
		"dataset": Path(corpus).stem,
		"meta": True
	}

	reportpath = Path("reports") / (name + ".jsonl")
	if reportpath.exists() and not click.confirm("overwrite existing report?"):
		return
	reportlog = open(reportpath, "w")

	reportlog.write(json.dumps(meta) + "\n")
	reportlog.flush()
	print(json.dumps(meta))

	subprocess.check_output("cargo build --release".split(), cwd=repo)

	for sample in Path(corpus).iterdir():
		print(sample)
		if sample.stat().st_size < 100:
			# submission was withdrawn
			continue

		report = {"engines": {}, "sample": sample.stem}

		env = os.environ.copy()
		env["SOURCE_DATE_EPOCH"] = "1456304492"
		env["LD_PRELOAD"] = "/usr/lib/faketime/libfaketime.so.1"
		env["FAKETIME"] = "2011-11-11 11:11:11"
		with TestEnv(sample) as d:
			capture_files(d, exclude_all=True)
			print(d)
			maindoc = get_maindoc(d).name # use relativ path for deterministic xelatex logs
			subprocess.run(["xelatex", '-interaction=batchmode', '-no-shell-escape', maindoc], capture_output=True, timeout=30, cwd=d, env=env)
			# TODO: the first run might influence the second one
			start = time.time()
			test = subprocess.run(["xelatex", '-interaction=batchmode', '-no-shell-escape', maindoc], capture_output=True, timeout=30, cwd=d, env=env)
			delta = time.time() - start
			print(test)
			# results = capture_files(d)
			results = None
			report["engines"]["xelatex"] = dict(statuscode=test.returncode, seconds=delta, results=results, tags=None)

		with TestEnv(sample) as d:
			capture_files(d, exclude_all=True)
			print(d)
			tectonic = Path(repo) / "target" / "release" / "tectonic"
			# fetch required files from network
			subprocess.run([tectonic, "--print", "-w=https://tectonic.newton.cx/bundles/tlextras-2018.1r0/bundle.tar", get_maindoc(d)], timeout=60*5, cwd=d) # don't inject libfaketime. fake time breaks https cert validation
			# the .xdv file might be interesting
			subprocess.run([tectonic, "--outfmt=xdv", get_maindoc(d)], timeout=60, cwd=d, env=env)
			start = time.time()
			test = subprocess.run([tectonic, "--keep-logs", get_maindoc(d)], timeout=30, cwd=d, env=env)
			delta = time.time() - start
			print(test)
			logfile = get_maindoc(d).with_suffix(".log")
			tags = get_tags(logfile)
			results = capture_files(d)
			report["engines"]["tectonic"] = dict(statuscode=test.returncode, seconds=delta, results=results, tags=tags)
		print(json.dumps(report))
		reportlog.write(json.dumps(report) + "\n")
		reportlog.flush()
	reportlog.close()




if __name__ == '__main__':
	report()
