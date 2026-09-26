"""Controlled Git operations, local patch application, Docker tests and PRs.

Agent responses never become shell commands. Tests only see a copy of tracked
project files inside a container. Git credentials remain in the coordinator.
"""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

import requests

from .providers import _clean_env
from .repository import readable_path


class ExecutionError(RuntimeError):
    """Safe public explanation; do not include credentials or raw exceptions."""


def kill_tree(process):
    if process.poll() is not None:
        return
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=False)
            if process.poll() is None:
                process.kill()
        process.wait(timeout=5)
    except (OSError,subprocess.TimeoutExpired):
        pass


def command(args, cwd, timeout, *, env=None, tick=None, check=True, max_bytes=1_000_000):
    process = None
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(args,cwd=cwd,env=env or _clean_env(),stdin=subprocess.DEVNULL,
                stdout=output,stderr=subprocess.STDOUT,shell=False,start_new_session=os.name=='posix')
            deadline = time.monotonic()+timeout
            while True:
                if tick:
                    tick()
                if output.tell()>max_bytes:
                    raise ExecutionError('Sortie de commande trop volumineuse.')
                if time.monotonic()>=deadline:
                    raise ExecutionError('Durée maximale de la commande atteinte.')
                try:
                    code = process.wait(timeout=min(.15,max(.01,deadline-time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    continue
            output.seek(0)
            raw = output.read(max_bytes+1)
            if len(raw)>max_bytes:
                raise ExecutionError('Sortie de commande trop volumineuse.')
            text = raw.decode('utf-8',errors='replace')
            if check and code:
                raise ExecutionError(f'La commande {Path(str(args[0])).name} a échoué (code {code}). Vérifier la configuration et le journal local.')
            return code,text
        except OSError:
            raise ExecutionError('Impossible de lancer Git ou Docker. Vérifier leur installation.') from None
        finally:
            if process:
                kill_tree(process)


def repo_url(repo):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) or any(p in ('.','..') for p in repo.split('/')):
        raise ExecutionError('Dépôt GitHub invalide.')
    return 'https://github.com/'+repo+'.git'


def valid_branch(branch):
    return (isinstance(branch,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/\-]{0,180}',branch)
            and '..' not in branch and '//' not in branch and not branch.endswith(('/','.','.lock'))
            and all(not p.startswith('.') for p in branch.split('/')))


def source_path(path):
    if not isinstance(path,str) or not path or len(path)>240 or any(c.isspace() for c in path):
        return False
    if any(part.lower().split('.')[0] in {'con','prn','aux','nul',*[f'com{i}' for i in range(1,10)],*[f'lpt{i}' for i in range(1,10)]} for part in path.split('/')):
        return False
    if path.startswith('.github/') or path.endswith(('.', ' ')):
        return False
    if Path(path).name.lower()=='auth.json' or Path(path).name.lower().startswith(('secrets.','credentials.')):
        return False
    return readable_path(path) or (Path(path).suffix in ('.sql','.json') and readable_path(str(Path(path).with_suffix('.py'))))


def validate_patch(patch):
    if not isinstance(patch,str) or not patch.strip() or len(patch)>80000 or '\x00' in patch:
        raise ExecutionError('Patch absent, binaire ou trop volumineux.')
    if any(line.startswith(('GIT binary patch','Binary files','rename from','rename to','copy from','copy to')) for line in patch.splitlines()):
        raise ExecutionError('Les patches binaires, copies et renommages ne sont pas pris en charge.')
    paths = set()
    headers = 0
    for line in patch.splitlines():
        if line.startswith(('new file mode ','deleted file mode ','old mode ','new mode ')):
            if line.rsplit(' ',1)[-1] not in ('100644','100755'):
                raise ExecutionError('Liens et sous-modules exclus des modifications.')
        if line.startswith('diff --git '):
            match = re.fullmatch(r'diff --git a/(\S+) b/(\S+)',line)
            if not match or match[1]!=match[2]:
                raise ExecutionError('En-tête de patch invalide.')
            paths.add(match[1])
            headers += 1
        elif line.startswith(('--- ','+++ ')):
            target = line[4:].split('\t',1)[0]
            if target!='/dev/null':
                if not target.startswith(('a/','b/')):
                    raise ExecutionError('Chemin de patch invalide.')
                paths.add(target[2:])
    if not 1<=headers<=20 or not 1<=len(paths)<=20 or any(not source_path(p) for p in paths):
        raise ExecutionError('Patch hors périmètre : seuls vingt fichiers source ou documentation au maximum sont modifiables.')
    return paths


class Workspace:
    def __init__(self, folder, job, config, budget):
        self.folder = Path(folder)
        self.job, self.config, self.budget = job,config,budget
        self.url = repo_url(job['repo'])
        if not valid_branch(job['base_branch']) or not re.fullmatch(r'duo/task-\d+-[a-f0-9]{12}',job['branch']):
            raise ExecutionError('Branche d’exécution invalide.')
        self.control = self.folder.parent/'control'
        self.control.mkdir(parents=True,exist_ok=True)
        (self.control/'hooks').mkdir(exist_ok=True)

    def git(self,args, *, auth=False, check=True, env_extra=None):
        env = _clean_env()
        env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT='0',
                   GIT_LFS_SKIP_SMUDGE='1',GIT_OPTIONAL_LOCKS='0')
        if auth:
            token = self.config.get('GITHUB_WRITE_TOKEN') or self.config.get('GITHUB_TOKEN')
            if not token:
                raise ExecutionError('Renseigner GITHUB_WRITE_TOKEN pour l’exécution réelle.')
            value = base64.b64encode(('x-access-token:'+token).encode()).decode()
            env.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                       GIT_CONFIG_VALUE_0='Authorization: Basic '+value)
        env.update(env_extra or {})
        options = ['-c','core.hooksPath='+str(self.control/'hooks'),'-c','credential.helper=',
            '-c','core.fsmonitor=false','-c','core.autocrlf=false','-c','core.quotePath=false',
            '-c','core.attributesFile='+os.devnull,'-c','protocol.file.allow=never',
            '-c','user.name=DUO PILOT','-c','user.email=duo-pilot@localhost']
        return command([self.config.get('GIT_BIN','git'),*options,*args],self.folder,
                       min(90,self.budget.remaining()),env=env,tick=self.budget.tick,check=check)

    def prepare(self):
        self.folder.mkdir(parents=True,exist_ok=True)
        if self.folder.is_symlink() or (self.folder/'.git').is_symlink():
            raise ExecutionError('Espace de travail inattendu.')
        if not (self.folder/'.git').exists():
            if any(self.folder.iterdir()):
                raise ExecutionError('Le dossier de travail existe déjà et n’est pas vide.')
            self.git(['init'])
        has_head = self.git(['rev-parse','--verify','HEAD'],check=False)[0]==0
        # Recover initialization/fetch interruptions before the first checkout.
        if not has_head:
            if any(p.name!='.git' for p in self.folder.iterdir()):
                raise ExecutionError('Préparation interrompue avec des fichiers inattendus. Dossier conservé.')
            code,origin = self.git(['remote','get-url','origin'],check=False)
            if code:
                self.git(['remote','add','origin',self.url])
            elif origin.strip()!=self.url:
                raise ExecutionError('Le dépôt local ne correspond plus à la mission.')
            self.git(['fetch','--no-tags','--depth=1','origin','refs/heads/'+self.job['base_branch']],auth=True)
            self.git(['checkout','-b',self.job['branch'],'FETCH_HEAD'])
        if self.git(['remote','get-url','origin'])[1].strip()!=self.url:
            raise ExecutionError('Le dépôt local ne correspond plus à la mission.')
        if self.git(['branch','--show-current'])[1].strip()!=self.job['branch']:
            raise ExecutionError('La branche locale a changé. Exécution arrêtée.')
        return self.head()

    def head(self):
        value = self.git(['rev-parse','HEAD'])[1].strip()
        if not re.fullmatch(r'[a-f0-9]{40}',value):
            raise ExecutionError('Référence Git illisible.')
        return value

    def clean(self):
        return not self.git(['status','--porcelain','--untracked-files=all'])[1].strip()

    def files(self):
        output = self.git(['ls-files','--stage','-z'])[1]
        entries = {}
        for row in output.split('\0'):
            if not row:
                continue
            meta,path = row.split('\t',1)
            mode,sha,stage = meta.split(' ')
            if stage=='0' and mode in ('100644','100755'):
                entries[path] = mode
        return entries

    def _safe_file(self,path):
        file = self.folder/path
        if not source_path(path) or file.is_symlink() or not file.resolve().is_relative_to(self.folder.resolve()):
            raise ExecutionError('Lecture ou écriture hors des fichiers autorisés.')
        for parent in file.parents:
            if parent==self.folder:
                break
            if parent.is_symlink():
                raise ExecutionError('Les liens symboliques ne sont pas suivis.')
        return file

    def read(self,path,start=1):
        try:
            file = self._safe_file(path)
            if path not in self.files() or not file.is_file() or file.stat().st_size>256000:
                return {'path':path,'status':'unavailable'}
            text = file.read_text(encoding='utf-8-sig')
            if '\x00' in text:
                return {'path':path,'status':'unavailable'}
            lines = text.splitlines()
            excerpt = '\n'.join(f'{i+1}: {line}' for i,line in enumerate(lines) if start-1<=i<start+179)[:10000]
            return {'path':path,'start_line':start,'total_lines':len(lines),'status':'read','content':excerpt,
                    'partial':start!=1 or len(lines)>=start+180 or len(excerpt)>=10000}
        except (ExecutionError,OSError,UnicodeError):
            return {'path':path,'status':'unavailable'}

    def context(self,requests=None):
        files = sorted(p for p in self.files() if source_path(p))
        if requests is None:
            preferred = [p for p in files if Path(p).name.lower() in ('readme.md','agents.md','architecture.md','pyproject.toml','requirements.txt')]
            requests = [{'path':p,'start_line':1} for p in preferred[:3]]
        return {'files':files[:350],'omitted_files':max(0,len(files)-350),
                'excerpts':[self.read(r['path'],r['start_line']) for r in requests]}

    def patch_intent(self,patch):
        paths = validate_patch(patch)
        if not self.clean():
            raise ExecutionError('Modifications locales imprévues : elles sont conservées, sans écrasement.')
        for path in paths:
            self._safe_file(path)
        before = self.head()
        patch_file = self.control/'change.patch'
        patch_file.write_text(patch,encoding='utf-8',newline='\n')
        index = self.control/('index-'+uuid.uuid4().hex)
        extra = {'GIT_INDEX_FILE':str(index)}
        try:
            self.git(['read-tree',before],env_extra=extra)
            self.git(['apply','--cached','--recount','--whitespace=nowarn',str(patch_file)],env_extra=extra)
            tree = self.git(['write-tree'],env_extra=extra)[1].strip()
        finally:
            index.unlink(missing_ok=True)
            Path(str(index)+'.lock').unlink(missing_ok=True)
        return {'before_sha':before,'tree':tree,'patch':patch}

    def finish_patch(self,intent):
        validate_patch(intent['patch'])
        head = self.head()
        tree = self.git(['rev-parse','HEAD^{tree}'])[1].strip()
        if tree==intent['tree'] and self.clean():
            return head
        if head!=intent['before_sha']:
            raise ExecutionError('La branche a changé pendant l’application du patch.')
        staged = self.git(['write-tree'])[1].strip()
        if self.clean():
            patch_file = self.control/'change.patch'
            patch_file.write_text(intent['patch'],encoding='utf-8',newline='\n')
            self.git(['apply','--index','--recount','--whitespace=nowarn',str(patch_file)])
        elif staged!=intent['tree'] or self.git(['diff','--name-only'])[1].strip() or self.git(['ls-files','--others','--exclude-standard'])[1].strip():
            raise ExecutionError('Patch interrompu avec des changements inattendus. Le dossier est conservé pour examen.')
        if self.git(['write-tree'])[1].strip()!=intent['tree']:
            raise ExecutionError('Le résultat du patch ne correspond pas au résultat prévu.')
        self.git(['commit','-m',f"DUO PILOT: carte {self.job['task_id']}, exécution {self.job['id']}"])
        return self.head()

    def diff(self,base_sha):
        diff = self.git(['diff','--no-ext-diff','--no-textconv','--no-renames',base_sha,'HEAD','--'])[1]
        if len(diff)>80000:
            raise ExecutionError('Changement trop large pour une revue complète : découper la mission.')
        return diff

    def push(self,sha):
        if sha!=self.head() or not self.clean():
            raise ExecutionError('Le code a changé depuis la revue.')
        # No force, no delete, and exactly one generated branch; never main/dev.
        self.git(['push','origin',sha+':refs/heads/'+self.job['branch']],auth=True)

    def test_copy(self,destination):
        destination = Path(destination)
        # The worker may have a private umask. Only this disposable snapshot
        # becomes readable to the container's unprivileged UID; never chmod its
        # parents (the service home, instance directory, or execution workspace).
        destination.chmod(0o755)
        total = 0
        for path,mode in self.files().items():
            parts = path.split('/')
            if any(p.lower() in ('..','.git','.env','instance','uploads','backups') or (p.startswith('.') and p not in ('.github',)) for p in parts):
                continue
            if parts[-1].lower() in ('auth.json','id_rsa','id_ed25519') or parts[-1].lower().startswith(('secrets.','credentials.')):
                continue
            source = self.folder/path
            if source.is_symlink() or not source.resolve().is_relative_to(self.folder.resolve()) or not source.is_file():
                continue
            total += source.stat().st_size
            if total>128_000_000:
                raise ExecutionError('Copie de test trop volumineuse.')
            folder = destination
            for part in parts[:-1]:
                folder = folder/part
                folder.mkdir(exist_ok=True)
                folder.chmod(0o755)
            target = destination/path
            shutil.copyfile(source,target)
            target.chmod(0o755 if mode=='100755' else 0o644)


class DockerTests:
    def __init__(self,config,budget):
        self.config,self.budget = config,budget
        self.binary = config.get('DOCKER_BIN','docker')

    def resolve_image(self,image):
        _,raw = command([self.binary,'image','inspect','--format','{{.Id}}',image],None,
                        min(20,self.budget.remaining()),tick=self.budget.tick)
        image_id = raw.strip()
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',image_id):
            raise ExecutionError('Image Docker de test absente ou invalide. La construire avant de lancer une mission.')
        return image_id

    def run(self,workspace,image,argv):
        sha = workspace.head()
        if not workspace.clean():
            raise ExecutionError('Le dossier a changé avant les tests.')
        name = 'duopilot-'+uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix='duopilot-tests-') as folder:
            Path(folder).chmod(0o755)
            workspace.test_copy(folder)
            bootstrap = "import os,shutil,sys;shutil.copytree('/source','/tmp/project');os.chdir('/tmp/project');os.execvp(sys.argv[1],sys.argv[1:])"
            args = [self.binary,'run','--rm','--pull=never','--name',name,'--network=none','--read-only',
                '--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','--memory=1g','--cpus=2',
                '--user=65534:65534','--tmpfs','/tmp:rw,nosuid,size=536870912',
                '--mount','type=bind,source='+folder+',target=/source,readonly','--workdir','/tmp',
                '--env','HOME=/tmp','--env','PYTHONDONTWRITEBYTECODE=1','--env','CI=1',
                '--entrypoint','python',image,'-I','-c',bootstrap,*argv]
            try:
                code,output = command(args,None,min(self.config['EXECUTION_TEST_SECONDS'],self.budget.remaining()),
                                      tick=self.budget.tick,check=False)
                if sha!=workspace.head() or not workspace.clean():
                    raise ExecutionError('Le code a changé pendant les tests ; aucun résultat validé.')
                return {'passed':code==0,'exit_code':code,'output':output[-16000:],
                        'output_truncated':len(output)>16000,'image':image,'command':argv,'sha':sha}
            finally:
                # Killing the Docker client alone does not stop its container.
                command([self.binary,'rm','-f',name],None,10,check=False)


class GitHubPRs:
    def __init__(self,repo,config,budget):
        repo_url(repo)
        self.repo,self.config,self.budget = repo,config,budget

    def request(self,method,endpoint, *, params=None, body=None):
        allowed = (method=='GET' and (endpoint=='pulls' or re.fullmatch(r'pulls/\d+(/reviews|/comments)?|issues/\d+/comments',endpoint)))
        allowed = allowed or (method=='POST' and endpoint=='pulls') or (method=='PATCH' and re.fullmatch(r'pulls/\d+',endpoint) and set(body or {})=={'body'})
        if not allowed:
            raise ExecutionError('Opération GitHub exclue de l’exécuteur.')
        self.budget.tick()
        token = self.config.get('GITHUB_WRITE_TOKEN') or self.config.get('GITHUB_TOKEN')
        if not token:
            raise ExecutionError('Jeton GitHub d’écriture manquant.')
        headers = {'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json',
                   'X-GitHub-Api-Version':'2022-11-28','User-Agent':'Duo-Pilot/0.4'}
        try:
            with requests.request(method,'https://api.github.com/repos/'+self.repo+'/'+endpoint,
                    params=params,json=body,headers=headers,allow_redirects=False,
                    timeout=max(1,min(20,self.budget.remaining())),stream=True) as response:
                if response.status_code not in (200,201):
                    raise ExecutionError(f'GitHub a refusé l’opération (HTTP {response.status_code}). Aucun nouvel essai automatique.')
                data = bytearray()
                for part in response.iter_content(8192):
                    self.budget.tick()
                    data.extend(part)
                    if len(data)>1_000_000:
                        raise ExecutionError('Réponse GitHub trop volumineuse.')
                return json.loads(data)
        except (requests.RequestException,ValueError):
            raise ExecutionError('Réponse GitHub indisponible. Une reprise vérifiera les opérations déjà réalisées.') from None

    def open_heads(self):
        items = self.request('GET','pulls',params={'state':'open','per_page':100})
        if not isinstance(items,list):
            raise ExecutionError('Liste de PR invalide.')
        return [item.get('head',{}).get('ref','') for item in items]

    def find(self,branch):
        rows = self.request('GET','pulls',params={'state':'all','head':self.repo.split('/')[0]+':'+branch,'per_page':100})
        if not isinstance(rows,list):
            raise ExecutionError('Liste de PR invalide.')
        matches = [r for r in rows if r.get('head',{}).get('ref')==branch]
        return matches[0] if matches else None

    def feedback(self,number):
        collected = []
        for endpoint in (f'issues/{number}/comments',f'pulls/{number}/reviews',f'pulls/{number}/comments'):
            rows = self.request('GET',endpoint,params={'per_page':30})
            if not isinstance(rows,list):
                raise ExecutionError('Retours GitHub invalides.')
            collected.extend({'source':endpoint,'id':r.get('id'),'body':str(r.get('body',''))[:1500],
                              'state':r.get('state'),'path':r.get('path')} for r in rows[-6:])
        return collected

    def publish(self,job,sha,body):
        existing = self.find(job['branch'])
        if existing:
            if (existing.get('base',{}).get('ref')!=job['base_branch'] or existing.get('head',{}).get('sha')!=sha
                    or existing.get('state')!='open'):
                raise ExecutionError('La PR existante a été modifiée, fermée ou fusionnée. Actualiser son état avant de continuer.')
            self.request('PATCH',f"pulls/{existing['number']}",body={'body':body})
            return existing
        return self.request('POST','pulls',body={'title':json.loads(job['plan'])['title'][:200],
            'head':job['branch'],'base':job['base_branch'],'body':body,'draft':False,'maintainer_can_modify':True})
