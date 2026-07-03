"""On-disk dataset shared by collect and analyze: manifest.json + measurements.jsonl + raw/*.csv.gz."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

RESULTS_HOME = Path('~/printer_data/config/chopper-autotune').expanduser()


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

    def append(self, record: dict):
        with self.records_path.open('a') as f:
            f.write(json.dumps(record) + '\n')

    def records(self) -> 'list[dict]':
        if not self.records_path.exists():
            return []
        with self.records_path.open() as f:
            return [json.loads(line) for line in f if line.strip()]

    def done_ids(self) -> 'set[str]':
        return {r['id'] for r in self.records() if r.get('status') == 'ok'}

    def store_raw_samples(self, measurement_id: str, samples) -> str:
        dst = self.raw_dir / (measurement_id + '.csv.gz')
        with gzip.open(dst, 'wt') as f:
            f.write('#time,accel_x,accel_y,accel_z\n')
            for s in samples:
                f.write('%.6f,%.6f,%.6f,%.6f\n' % (s[0], s[1], s[2], s[3]))
        return str(dst.relative_to(self.root))

    def open_raw(self, record: dict):
        return gzip.open(self.root / record['raw'], 'rt')
