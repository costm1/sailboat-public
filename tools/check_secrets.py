"""General scanner over tracked files and all Git blobs, without token verification."""
import json,socket,subprocess,tempfile
from pathlib import Path
from detect_secrets.settings import default_settings
from detect_secrets.core.scan import scan_file

def git(*args):return subprocess.check_output(['git',*args],stderr=subprocess.DEVNULL)

def main():
    def denied(*args,**kwargs):raise RuntimeError('Network disabled during secret scanning')
    socket.socket.connect=denied;socket.create_connection=denied;socket.getaddrinfo=denied
    findings=[];scanned=0
    with default_settings() as settings:
        settings.disable_filters('detect_secrets.filters.common.is_ignored_due_to_verification_policies')
        # Disable verification at plugin level too. Never validate candidate secrets online.
        from detect_secrets.settings import get_plugins
        for plugin in get_plugins():plugin.verify=lambda *args,**kwargs:None
        for name in filter(None,git('ls-files','-z').decode().split('\0')):
            scanned+=1
            for secret in scan_file(name):
                findings.append({'location':name,'line':secret.line_number,'type':secret.type})
        with tempfile.TemporaryDirectory() as scratch:
            for row in git('cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype)').decode().splitlines():
                oid,kind=row.split()
                if kind not in ('blob','commit','tag'):continue
                data=git('cat-file',kind,oid)
                try:data.decode('utf-8')
                except UnicodeDecodeError:continue
                path=Path(scratch)/'object.txt';path.write_bytes(data);scanned+=1
                for secret in scan_file(str(path)):
                    findings.append({'location':'git:'+oid,'line':secret.line_number,'type':secret.type})
    print(json.dumps({'scanner':'detect-secrets','items_scanned':scanned,'findings':findings},indent=2))
    raise SystemExit(bool(findings))

if __name__=='__main__':main()
