"""On-disk dataset shared by collect and analyze: manifest.json + measurements.jsonl + raw/*.csv.gz."""
from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

RESULTS_HOME = Path('~/printer_data/config/chopper-autotune').expanduser()


def load_json(path) -> dict:
    """Best-effort read of a small instrument-state JSON; {} when absent or unreadable."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_json(path, data: dict, merge: bool = False):
    """Best-effort atomic write of an instrument-state JSON. The KlipperScreen panel may
    read these files at any moment, so write tmp + os.replace — a torn write must never
    leave broken JSON. merge=True folds the new keys over the file's current content
    (per-motor instruments must not erase each other's entries). Failures are swallowed:
    remembering a result is not allowed to kill the run that produced it."""
    path = Path(path)
    try:
        if merge:
            data = {**load_json(path), **data}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.%s.tmp' % path.name)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    except OSError:
        pass


class Dataset:
    def __init__(self, root):
        self.root = Path(root)
        self.manifest_path = self.root / 'manifest.json'
        self.records_path = self.root / 'measurements.jsonl'
        self.raw_dir = self.root / 'raw'
        self._raw_writer = None
        self._raw_gate = None
        self._raw_errors = []

    @classmethod
    def create(cls, root, manifest: dict) -> 'Dataset':
        ds = cls(root)
        ds.raw_dir.mkdir(parents=True, exist_ok=True)
        if not ds.manifest_path.exists():
            ds.manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        return ds

    @classmethod
    def open(cls, root) -> 'Dataset':
        ds = cls(root)
        if not ds.manifest_path.exists():
            raise SystemExit('%s is not a dataset (no manifest.json)' % root)
        return ds

    def manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text())

    def update_manifest(self, **fields):
        manifest = self.manifest()
        manifest.update(fields)
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

    def append(self, record: dict):
        with self.records_path.open('a') as f:
            f.write(json.dumps(record) + '\n')

    def records(self) -> 'list[dict]':
        if not self.records_path.exists():
            return []
        result = []
        with self.records_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    result.append(json.loads(line))
                except ValueError:
                    # a crash mid-append leaves a truncated tail line; dropping that one
                    # measurement beats refusing to resume/analyze the whole dataset
                    print('%s: skipping a corrupt line' % self.records_path)
        return result

    def done_ids(self) -> 'set[str]':
        return {r['id'] for r in self.records() if r.get('status') == 'ok'}

    def store_raw_samples(self, measurement_id: str, samples) -> str:
        """Queue the raw capture for a background write and return its path at once.
        Formatting + gzipping ~5k lines between moves added 7-10 minutes to a full
        grid (measured) — the writer thread does it while the next move runs. The
        semaphore bounds how many captures wait in RAM; flush_raw() joins the queue."""
        if self._raw_writer is None:
            from concurrent.futures import ThreadPoolExecutor
            from threading import Semaphore
            self._raw_writer = ThreadPoolExecutor(max_workers=1)
            self._raw_gate = Semaphore(4)
        dst = self.raw_dir / (measurement_id + '.csv.gz')

        def write():
            try:
                lines = ['#time,accel_x,accel_y,accel_z']
                lines += ['%.6f,%.6f,%.6f,%.6f' % (s[0], s[1], s[2], s[3]) for s in samples]
                # level 1: ~3x faster than the default 9, ~8% larger files
                with gzip.open(dst, 'wt', compresslevel=1) as f:
                    f.write('\n'.join(lines) + '\n')
            except OSError as e:
                self._raw_errors.append('%s: %s' % (measurement_id, e))
            finally:
                self._raw_gate.release()

        self._raw_gate.acquire()
        self._raw_writer.submit(write)
        return str(dst.relative_to(self.root))

    def flush_raw(self):
        """Wait for every queued raw write; call at the end of a run (and before reading
        raw back) so a SystemExit path still lands the captures the records reference."""
        if self._raw_writer is not None:
            self._raw_writer.shutdown(wait=True)
            self._raw_writer = None
        for failure in self._raw_errors:
            print('Warning: raw capture not stored (%s)' % failure)
        self._raw_errors = []

    def open_raw(self, record: dict):
        self.flush_raw()
        return gzip.open(self.root / record['raw'], 'rt')
