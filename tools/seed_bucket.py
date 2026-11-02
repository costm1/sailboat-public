"""Explicit one-time empty seed into the owner's new private Bucket key."""
import argparse,json,sqlite3,tempfile
from contextlib import closing
from pathlib import Path
from sailboat.instance_config import validate_environment
from sailboat.bucket_store import BucketSnapshots
from sailboat.cloud_store import checked_snapshot

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args();env=json.loads(args.config.read_text())
    if validate_environment(env)['storage']!='bucket':raise ValueError('Explicit Bucket configuration required')
    if env.get('SAILBOAT_BROKER_WRITES_DISABLED','1')!='1':raise ValueError('Seed only during DISARMED setup')
    # The seed must come from the adjacent fresh setup, never arbitrary imported history.
    body=(args.config.parent/'state/state.snapshot').read_bytes()
    data,manifest=BucketSnapshots.decode(body)
    if manifest['generation']!=0:raise ValueError('Only an unused setup seed may be uploaded')
    with tempfile.TemporaryDirectory() as scratch:
        path=Path(scratch)/'seed.sqlite';path.write_bytes(data);checked_snapshot(path)
        with closing(sqlite3.connect(path)) as db:
            if db.execute("SELECT v FROM meta WHERE k='mode'").fetchone()[0]!='DISARMED':raise ValueError('Seed is not DISARMED')
            allowed={'meta','events','sqlite_sequence'}
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                # Generated schema includes empty account, request, private and risk tables.
                if table not in allowed and db.execute('SELECT count(*) FROM "'+table.replace('"','""')+'"').fetchone()[0]:
                    raise ValueError('Seed includes nonempty state; initialize a fresh directory')
    store=BucketSnapshots(env['SAILBOAT_STATE_BUCKET'],env['SAILBOAT_STATE_KEY'],namespace=env['SAILBOAT_STATE_NAMESPACE'],
        access_key=env['SAILBOAT_STATE_S3_ACCESS_KEY'],secret_key=env['SAILBOAT_STATE_S3_SECRET_KEY'])
    store.client.put_object(Bucket=store.bucket,Key=store.key,Body=body,ContentType='application/octet-stream',IfNoneMatch='*')
    print('New checkpoint seeded; existing keys are never overwritten. No brokerage actions.')

if __name__=='__main__':main()
