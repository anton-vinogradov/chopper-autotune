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
        dst = self.raw_dir / (measurement_id + '.csv.gz')
        lines = ['#time,accel_x,accel_y,accel_z']
        lines += ['%.6f,%.6f,%.6f,%.6f' % (s[0], s[1], s[2], s[3]) for s in samples]
        # level 1: ~3x faster than the default 9 on the hot path, ~8% larger files
        with gzip.open(dst, 'wt', compresslevel=1) as f:
            f.write('\n'.join(lines) + '\n')
        return str(dst.relative_to(self.root))

    def open_raw(self, record: dict):
        return gzip.open(self.root / record['raw'], 'rt')
