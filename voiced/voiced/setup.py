"""Enable Codex text accessibility on its next launch without restarting it."""
from pathlib import Path
import os
import re


def enable_codex_accessibility(source=None, destination=None):
    directory = Path(os.environ.get("XDG_DATA_HOME", Path.home()/".local/share"))/"applications"
    destination = Path(destination) if destination else directory/"codex-desktop.desktop"
    source = Path(source) if source else Path('/usr/share/applications/codex-desktop.desktop')
    original = destination.read_text() if destination.exists() else source.read_text()
    changed=[]
    count=0
    for line in original.splitlines():
        if line.startswith('Exec=') and re.search(r'(?:^|\s)/[^\s]*codex-desktop(?:\s|$)',line[5:]):
            count+=1
            if '--force-renderer-accessibility' not in line:
                line=line.replace(' %u',' --force-renderer-accessibility %u').replace(' %U',' --force-renderer-accessibility %U') if re.search(r' %[uU](?:\s|$)',line) else line+' --force-renderer-accessibility'
        changed.append(line)
    if not count:
        raise ValueError('No Codex launch command found; the desktop entry was not changed.')
    updated='\n'.join(changed)+'\n'
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists() and updated!=original:
        backup=destination.with_suffix('.desktop.voiced-backup')
        if not backup.exists():backup.write_text(original)
    temporary=destination.with_suffix('.desktop.voiced-tmp')
    temporary.write_text(updated)
    temporary.replace(destination)
    return destination
