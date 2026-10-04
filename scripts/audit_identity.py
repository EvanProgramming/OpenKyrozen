"""Non-secret provenance attached to production acceptance evidence."""
from __future__ import annotations
import hashlib
import importlib.metadata
import platform
from pathlib import Path
import subprocess
import sys


def audit_identity():
    root=Path(__file__).resolve().parents[1]
    def git(*args):
        result=subprocess.run(['git',*args],cwd=root,capture_output=True,text=True,check=False)
        return result.stdout.strip() if result.returncode==0 else 'unavailable'
    return {'commit':git('rev-parse','HEAD'),
            'patch_sha256':hashlib.sha256(git('diff','HEAD').encode()).hexdigest(),
            'platform':platform.platform(),'python':sys.version,'interpreter':sys.executable,
            'prefix':sys.prefix,'source_root':str(root),
            'dependencies':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()}}
