"""Human-invoked dependency image preparation, separate from autonomous tests."""
import json
from pathlib import Path
import re
import tempfile

from flask import current_app
from . import settings
from .execution_io import ExecutionError, command


def dependency_files(entry):
    root = Path(entry).resolve().parent
    files = {}

    def collect(file):
        file = Path(file).resolve()
        if not file.is_relative_to(root) or file.suffix!='.txt' or not file.is_file():
            raise ExecutionError('Les dépendances incluses doivent être des fichiers .txt dans le même dossier ou ses sous-dossiers.')
        name = file.relative_to(root).as_posix()
        if name in files:
            return
        if len(files)>=12 or file.stat().st_size>100000:
            raise ExecutionError('Fichiers de dépendances trop volumineux.')
        raw = file.read_text(encoding='utf-8-sig')
        files[name] = raw
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            include = re.fullmatch(r'(?:-r\s*|--requirement[ =]+|-c\s*|--constraint[ =]+)([A-Za-z0-9_./-]+\.txt)',line)
            if include:
                collect(file.parent/include[1])
                continue
            if line.startswith('-') or any(x in line for x in ('://','@','\\')) or not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[|\s|[<=>!~;]|$)',line):
                raise ExecutionError('Ce préparateur accepte les dépendances PyPI et les inclusions -r/-c. Pour des dépendances locales, privées ou Git, prépare une image adaptée manuellement (README).')
    try:
        collect(entry)
    except (OSError,UnicodeError):
        raise ExecutionError('Fichier de dépendances illisible.') from None
    return files


def prepare_image(topic,requirements=None,*,build=True):
    if topic not in ('cspilot','self'):
        raise ExecutionError('Projet inconnu.')
    if requirements is None:
        if topic=='cspilot':
            raise ExecutionError('Indiquer --requirements "C:/chemin/CS-PILOT/requirements-dev.txt" (ou requirements.txt). Consulte le fichier avant de construire l’image.')
        requirements = Path(current_app.root_path).parent/'requirements-dev.txt'
    requirements = Path(requirements).resolve()
    files = dependency_files(requirements)
    config = settings.effective_config()
    image = config[('CSPILOT' if topic=='cspilot' else 'SELF')+'_TEST_IMAGE']
    if '@' in image:
        raise ExecutionError('Pour construire une image, choisis un nom et un tag, sans digest @sha256.')
    parent = Path(current_app.instance_path)/'test-images'
    parent.mkdir(parents=True,exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=topic+'-',dir=parent))
    for name,raw in files.items():
        target = folder/'requirements'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(raw,encoding='utf-8')
    args = ['python','-m','pip','install','--no-cache-dir','pytest','-r','/opt/requirements/'+requirements.name]
    dockerfile = ('FROM python:3.12-slim\n'
        'RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*\n'
        'COPY requirements/ /opt/requirements/\nRUN '+json.dumps(args)+'\nWORKDIR /tmp\n')
    (folder/'Dockerfile').write_text(dockerfile,encoding='utf-8')
    if build:
        code,_ = command([config.get('DOCKER_BIN','docker'),'build','--tag',image,str(folder)],None,1800,check=False,max_bytes=4_000_000)
        if code:
            raise ExecutionError(f'Construction Docker échouée (code {code}). Relance docker build --tag {image} "{folder}" pour voir le détail ; le dossier est conservé.')
    return folder,image
