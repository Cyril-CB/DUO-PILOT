import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from duopilot import execution_io as io, settings
from duopilot.executor import ReadBudget
from duopilot.test_images import dependency_files, prepare_image
from test_executor import patch, ORIGINAL, FIXED
from test_v4 import app


@pytest.mark.parametrize('path',['../app.py','.git/config','.github/workflows/run.yml','instance/secret.py',
    'auth.json','data/customer.json','dir/NUL.py','C:/app.py','app.py:secret','secrets.json','a/../app.py'])
def test_patch_cannot_target_control_or_data_files(path):
    value=patch().replace('a/app.py','a/'+path).replace('b/app.py','b/'+path)
    with pytest.raises(io.ExecutionError):
        io.validate_patch(value)


def test_patch_refuses_symlink_mode_and_binary():
    for value in (patch().replace('--- a/app.py','new file mode 120000\n--- a/app.py'),patch()+'GIT binary patch\n'):
        with pytest.raises(io.ExecutionError):
            io.validate_patch(value)


def workspace(tmp_path):
    job={'id':1,'task_id':1,'repo':'owner/repo','base_branch':'dev','branch':'duo/task-1-123456abcdef'}
    folder=tmp_path/'repo'
    folder.mkdir()
    value=io.Workspace(folder,job,{'GITHUB_WRITE_TOKEN':'private-test-token'},ReadBudget())
    value.git(['init'])
    value.git(['checkout','-b',job['branch']])
    (folder/'app.py').write_text(ORIGINAL)
    value.git(['add','app.py'])
    value.git(['commit','-m','Fixture'])
    return value


def test_git_patch_checkpoint_survives_staged_and_committed_states(tmp_path):
    value=workspace(tmp_path)
    intent=value.patch_intent(patch())
    value.git(['apply','--index',str(value.control/'change.patch')])
    sha=value.finish_patch(intent)
    assert (value.folder/'app.py').read_text()==FIXED
    assert value.finish_patch(intent)==sha
    assert value.clean()


@pytest.mark.skipif(os.name!='posix',reason='Permissions of the Linux VPS test container')
def test_snapshot_is_readable_under_private_umask_without_opening_parent_dirs(tmp_path):
    value=workspace(tmp_path)
    nested=value.folder/'module'/'tests'/'fixtures'
    nested.mkdir(parents=True)
    (nested/'example.txt').write_text('fixture for UID 65534\n')
    (value.folder/'module'/'tests'/'run.sh').write_text('#!/bin/sh\nexit 0\n')
    (value.folder/'module'/'tests'/'run.sh').chmod(0o755)
    value.git(['add','module'])
    value.git(['commit','-m','Nested test files'])
    private_home=tmp_path/'private-home'
    instance=private_home/'instance'
    destination=instance/'snapshot'
    instance.mkdir(parents=True)
    private_home.chmod(0o700)
    instance.chmod(0o700)
    (instance/'private.txt').write_text('Do not expose the parent directory')
    previous=os.umask(0o077)
    try:
        destination.mkdir()
        value.test_copy(destination)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(private_home.stat().st_mode)==0o700
    assert stat.S_IMODE(instance.stat().st_mode)==0o700
    assert stat.S_IMODE(destination.stat().st_mode)==0o755
    assert all(stat.S_IMODE(item.stat().st_mode)==0o755 for item in destination.rglob('*') if item.is_dir())
    assert stat.S_IMODE((destination/'module/tests/fixtures/example.txt').stat().st_mode)==0o644
    assert stat.S_IMODE((destination/'module/tests/run.sh').stat().st_mode)==0o755
    assert not (destination/'.git').exists()
    assert (destination/'module/tests/fixtures/example.txt').read_text()=='fixture for UID 65534\n'
    if os.geteuid()==0 and Path('/usr/bin/python3').exists():
        # As in a bind mount, the container starts inside /source without
        # needing traversal of its private host parents. Exercise the actual
        # container UID when this test runs as root, as in the build workspace.
        code=('from pathlib import Path\n'
              'assert Path("module/tests/fixtures/example.txt").read_text()=="fixture for UID 65534\\n"\n'
              'try:\n Path("../private.txt").read_text()\n'
              'except PermissionError:\n pass\n'
              'else:\n raise AssertionError("private parent was exposed")\n')
        try:
            subprocess.run(['/usr/bin/python3','-I','-c',code],cwd=destination,user=65534,group=65534,
                           extra_groups=[],check=True,capture_output=True,text=True)
        except PermissionError as exc:
            # Some managed build containers report UID 0 without CAP_SETUID /
            # CAP_SETGID. The exact other-user permission checks above still
            # run there; this optional real UID probe needs those capabilities.
            if exc.errno!=1:
                raise


def test_unexpected_staged_change_is_preserved_and_blocks_checkpoint(tmp_path):
    value=workspace(tmp_path)
    intent=value.patch_intent(patch())
    (value.folder/'app.py').write_text('Unexpected human code\n')
    value.git(['add','app.py'])
    with pytest.raises(io.ExecutionError,match='inattendus'):
        value.finish_patch(intent)
    assert (value.folder/'app.py').read_text()=='Unexpected human code\n'


def test_git_auth_is_in_environment_only_and_push_has_one_generated_destination(tmp_path,monkeypatch):
    value=workspace(tmp_path)
    sha=value.head()
    captured=[]
    def command(args,cwd,timeout,**kwargs):
        captured.append((args,kwargs))
        return 0,''
    monkeypatch.setattr(io,'command',command)
    monkeypatch.setattr(value,'head',lambda:sha)
    monkeypatch.setattr(value,'clean',lambda:True)
    value.push(sha)
    args,options=captured[0]
    assert 'private-test-token' not in str(args)
    assert args[-3:]==['push','origin',sha+':refs/heads/duo/task-1-123456abcdef']
    assert '--force' not in args and '--delete' not in args
    assert options['env']['GIT_CONFIG_VALUE_0'].startswith('Authorization: Basic ')
    assert options['env']['GIT_CONFIG_GLOBAL']==io.os.devnull
    assert any('core.hooksPath=' in a for a in args)


def test_docker_has_no_network_no_host_credentials_and_is_cleaned_up(tmp_path,monkeypatch):
    value=workspace(tmp_path)
    calls=[]
    def command(args,*_,**kwargs):
        calls.append(args)
        return 0,'2 passed'
    monkeypatch.setattr(io,'command',command)
    monkeypatch.setattr(value,'head',lambda:'a'*40)
    monkeypatch.setattr(value,'clean',lambda:True)
    monkeypatch.setattr(value,'test_copy',lambda destination:None)
    report=io.DockerTests({'EXECUTION_TEST_SECONDS':30},ReadBudget()).run(value,'sha256:'+'b'*64,['python','-m','pytest'])
    args=calls[0]
    assert report['passed']
    assert '--network=none' in args and '--read-only' in args and '--pull=never' in args
    assert '--cap-drop=ALL' in args and '--security-opt=no-new-privileges' in args
    assert '--user=65534:65534' in args
    assert args[args.index('--mount')+1].endswith('target=/source,readonly')
    assert '.git' not in str(args) and 'private-test-token' not in str(args)
    assert calls[-1][1:3]==['rm','-f']


def test_docker_rejects_code_change_during_tests_and_still_stops_container(tmp_path,monkeypatch):
    value=workspace(tmp_path)
    state={'sha':'a'*40}
    calls=[]
    def command(args,*_,**kwargs):
        calls.append(args)
        state['sha']='b'*40
        return 0,'passed'
    monkeypatch.setattr(io,'command',command)
    monkeypatch.setattr(value,'head',lambda:state['sha'])
    monkeypatch.setattr(value,'clean',lambda:True)
    monkeypatch.setattr(value,'test_copy',lambda destination:None)
    with pytest.raises(io.ExecutionError,match='pendant les tests'):
        io.DockerTests({'EXECUTION_TEST_SECONDS':30},ReadBudget()).run(value,'sha256:'+'b'*64,['python','-m','pytest'])
    assert calls[-1][1:3]==['rm','-f']


@pytest.mark.parametrize('method,endpoint,body',[
    ('PUT','pulls/17/merge',{}),('PATCH','pulls/17',{'state':'closed'}),
    ('POST','issues',{}),('DELETE','git/refs/heads/dev',{}),('PUT','branches/dev/protection',{}),
])
def test_github_excludes_merge_branch_protection_and_other_mutations(method,endpoint,body,monkeypatch):
    monkeypatch.setattr(io.requests,'request',lambda *a,**k:pytest.fail('HTTP request forbidden'))
    remote=io.GitHubPRs('owner/repo',{'GITHUB_WRITE_TOKEN':'never-send'},ReadBudget())
    with pytest.raises(io.ExecutionError,match='exclue'):
        remote.request(method,endpoint,body=body)


def test_command_timeout_stops_and_does_not_expose_stderr(tmp_path):
    with pytest.raises(io.ExecutionError,match='Durée'):
        io.command([sys.executable,'-c','import time; time.sleep(5)'],tmp_path,0.05)
    with pytest.raises(io.ExecutionError) as exc:
        io.command([sys.executable,'-c',"import sys; print('secret');sys.exit(1)"],tmp_path,5)
    assert 'secret' not in str(exc.value)


def test_dependency_image_collects_only_reviewed_requirements(app,tmp_path):
    req=tmp_path/'requirements-dev.txt'
    req.write_text('-r requirements.txt\npytest==9.1.1\n')
    (tmp_path/'requirements.txt').write_text('Flask==3.1.3\n')
    (tmp_path/'.env').write_text('SECRET=never-copy')
    with app.app_context():
        app.instance_path=str(tmp_path/'instance')
        folder,image=prepare_image('cspilot',req,build=False)
        assert image=='cspilot-tests:local'
        assert {p.name for p in (folder/'requirements').iterdir()}=={'requirements.txt','requirements-dev.txt'}
        dockerfile=(folder/'Dockerfile').read_text()
        assert 'FROM python:3.12-slim' in dockerfile and 'COPY requirements/' in dockerfile
        assert not list(folder.rglob('.env'))
        with pytest.raises(io.ExecutionError,match='Indiquer'):
            prepare_image('cspilot',None,build=False)
    req.write_text('-r ../external.txt\n')
    with pytest.raises(io.ExecutionError):
        dependency_files(req)
    req.write_text('project @ https://secret.example/package\n')
    with pytest.raises(io.ExecutionError):
        dependency_files(req)
